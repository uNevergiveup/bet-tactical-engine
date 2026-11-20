"""Catálogo canónico comercial Bwin independiente del motor probabilístico (Fase 2 - Tarea B).

Extrae todas las líneas y selecciones reales de una captura TXT Bwin preservando
cadenas literales, cuotas Decimal, alcance temporal, entidad (partido/equipo/jugador),
procedencia SHA-256 y estado de liquidación, separando cobertura comercial de cobertura
estadística/modelada.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bet_engine.orchestration.shadow_pipeline import _expand_team_keys, _match_team_key

COMMERCIAL_STATES = frozenset({
    "OFFERED_AND_MODELED",
    "OFFERED_NOT_MODELED",
    "MAPPING_UNRESOLVED",
    "SETTLEMENT_UNVERIFIED",
    "NOT_OFFERED",
    "PRICE_UNAVAILABLE",
})


def _norm(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _to_decimal(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        d = Decimal(str(raw).strip().replace(",", "."))
        return d if d > Decimal("1.0") else None
    except (InvalidOperation, ValueError):
        return None


def _parse_ou_selection(sel_raw: str) -> tuple[str, Decimal] | None:
    s = _norm(sel_raw)
    m = re.match(r"^(mas de|más de|over)\s+(\d+(?:[.,]\d+)?)$", s)
    if m:
        return "over", Decimal(m.group(2).replace(",", "."))
    m = re.match(r"^(menos de|under)\s+(\d+(?:[.,]\d+)?)$", s)
    if m:
        return "under", Decimal(m.group(2).replace(",", "."))
    return None


def _stable_selection_id(event_id: str | None, header: str, selection: str) -> str:
    raw = f"{event_id or ''}|{header.strip()}|{selection.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class BwinCatalogEntry:
    """Entrada canónica de una selección real en el catálogo Bwin."""

    selection_id: str
    event_id: str | None
    fixture_id: int | None
    market_header_literal: str
    selection_literal: str
    market_family: str
    canonical_key: str | None
    direction: str | None
    line: str | None
    odd: str | None
    temporal_scope: str  # FT | H1 | H2 | UNKNOWN
    target_scope: str  # MATCH | HOME | AWAY | PLAYER | UNKNOWN
    target_entity: str | None
    captured_at_utc: str | None
    source_txt_path: str
    source_txt_sha256: str
    settlement_rules_available: bool
    settlement_status: str  # VERIFIED_STANDARD | SETTLEMENT_UNVERIFIED
    commercial_coverage: str
    interpretation_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_equivalence_forbidden(family_a: str, family_b: str) -> str | None:
    """Devuelve código de bloqueo si dos conceptos no son equivalentes aunque parezcan próximos."""
    a, b = family_a.upper(), family_b.upper()
    pair = frozenset({a, b})
    forbidden_pairs = {
        frozenset({"SHOTS", "ON_TARGET"}): "FORBIDDEN_EQUIVALENCE:SHOTS_VS_ON_TARGET",
        frozenset({"PLAYER_SHOTS", "PLAYER_ON_TARGET"}): "FORBIDDEN_EQUIVALENCE:PLAYER_SHOTS_VS_ON_TARGET",
        frozenset({"FOULS_COMMITTED", "FOULS_DRAWN"}): "FORBIDDEN_EQUIVALENCE:FOULS_COMMITTED_VS_DRAWN",
        frozenset({"CARDS_TOTAL", "CARD_POINTS"}): "FORBIDDEN_EQUIVALENCE:CARDS_VS_CARD_POINTS",
        frozenset({"YELLOW_CARDS", "CARDS_WITH_RED"}): "FORBIDDEN_EQUIVALENCE:YELLOW_VS_YELLOW_PLUS_RED",
        frozenset({"CORNERS_TEAM", "CORNERS_TOTAL"}): "FORBIDDEN_EQUIVALENCE:TEAM_CORNERS_VS_TOTAL_CORNERS",
        frozenset({"CORNERS_H1", "CORNERS_FT"}): "FORBIDDEN_EQUIVALENCE:H1_CORNERS_VS_FT_CORNERS",
        frozenset({"PLAYER_SHOTS_2_PLUS", "OVER_2_5_SHOTS"}): "FORBIDDEN_EQUIVALENCE:2_PLUS_IS_NOT_OVER_2_5",
        frozenset({"PLAYER_SUB", "PLAYER_STARTER"}): "FORBIDDEN_EQUIVALENCE:SUB_VS_STARTER",
        frozenset({"GOALS_OVER_2_5", "BTTS_YES"}): "FORBIDDEN_EQUIVALENCE:OVER_2_5_VS_BTTS",
    }
    return forbidden_pairs.get(pair)


def is_default_modeled_key(canonical_key: str | None, market_family: str) -> bool:
    """Indica si una clave comercial corresponde a una familia modelada por bwin_evaluator.py."""
    if not canonical_key:
        return False
    if market_family in {"1X2", "DOUBLE_CHANCE", "DNB", "BTTS", "GOALS_FT", "GOALS_H1", "GOALS_TEAM", "CORNERS_TOTAL", "CORNERS_TEAM", "EXACT_SCORE"}:
        return True
    return False


def extract_bwin_catalog(
    bwin_txt_path: Path,
    *,
    event_id: str | None = None,
    fixture_id: int | None = None,
    home_name: str | None = None,
    away_name: str | None = None,
    home_aliases: tuple[str, ...] = (),
    away_aliases: tuple[str, ...] = (),
    captured_at_utc: str | None = None,
    modeled_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Extrae el catálogo comercial completo desde un snapshot TXT Bwin sin inventar cuotas."""
    path = Path(bwin_txt_path)
    if not path.is_file():
        raise FileNotFoundError(f"BWIN_TXT_NOT_FOUND:{path}")

    from motor_probabilistico import parse_odds

    parsed = parse_odds(path)
    txt_sha256 = _sha256_file(path)
    raw_text = path.read_text(encoding="utf-8", errors="replace")

    # Try to extract capture timestamp from header if not provided
    if captured_at_utc is None:
        m_ts = re.search(r"(?:captured_at_utc|timestamp_utc|fecha_captura)\s*[:=]\s*(\S+)", raw_text[:1200], re.I)
        if m_ts:
            captured_at_utc = m_ts.group(1).strip()

    home_keys = _expand_team_keys(home_name, home_aliases)
    away_keys = _expand_team_keys(away_name, away_aliases)

    entries: list[BwinCatalogEntry] = []
    seen_ids: set[str] = set()
    duplicate_count = 0
    unexpanded_blocks = 0

    for block in parsed.get("markets", []):
        header_lit = str(block.get("market") or "").strip()
        m_low = _norm(header_lit)
        sels = block.get("selections") or []

        if not sels:
            unexpanded_blocks += 1
            entries.append(
                BwinCatalogEntry(
                    selection_id=_stable_selection_id(event_id, header_lit, "__UNEXPANDED__"),
                    event_id=event_id,
                    fixture_id=fixture_id,
                    market_header_literal=header_lit,
                    selection_literal="",
                    market_family="UNEXPANDED_OR_EMPTY",
                    canonical_key=None,
                    direction=None,
                    line=None,
                    odd=None,
                    temporal_scope="UNKNOWN",
                    target_scope="UNKNOWN",
                    target_entity=None,
                    captured_at_utc=captured_at_utc,
                    source_txt_path=str(path),
                    source_txt_sha256=txt_sha256,
                    settlement_rules_available=False,
                    settlement_status="SETTLEMENT_UNVERIFIED",
                    commercial_coverage="PRICE_UNAVAILABLE",
                    interpretation_status="UNEXPANDED_OR_EMPTY_BLOCK",
                )
            )
            continue

        # Temporal scope
        if "1a parte" in m_low or "1ª parte" in m_low or "primera parte" in m_low:
            t_scope = "H1"
        elif "2a parte" in m_low or "2ª parte" in m_low or "segunda parte" in m_low:
            t_scope = "H2"
        elif "tiempo reglamentario" in m_low or "|" not in m_low:
            t_scope = "FT"
        else:
            t_scope = "UNKNOWN"

        for s in sels:
            sel_lit = str(s.get("selection") or "").strip()
            odd_dec = _to_decimal(s.get("odd"))
            sid = _stable_selection_id(event_id, header_lit, sel_lit)
            if sid in seen_ids:
                duplicate_count += 1
                continue
            seen_ids.add(sid)

            fam, ckey, direction, line_str, tgt_scope, tgt_ent, sett_avail, sett_status, interp = _classify_selection(
                m_low=m_low,
                header_lit=header_lit,
                sel_lit=sel_lit,
                t_scope=t_scope,
                home_keys=home_keys,
                away_keys=away_keys,
                home_name=home_name,
                away_name=away_name,
            )

            if odd_dec is None:
                comm_cov = "PRICE_UNAVAILABLE"
            elif fam == "UNMAPPED_COMMERCIAL_MARKET":
                comm_cov = "MAPPING_UNRESOLVED"
            elif sett_status == "SETTLEMENT_UNVERIFIED":
                comm_cov = "SETTLEMENT_UNVERIFIED"
            else:
                is_modeled = (ckey in modeled_keys) if modeled_keys is not None else is_default_modeled_key(ckey, fam)
                comm_cov = "OFFERED_AND_MODELED" if is_modeled else "OFFERED_NOT_MODELED"

            entries.append(
                BwinCatalogEntry(
                    selection_id=sid,
                    event_id=event_id,
                    fixture_id=fixture_id,
                    market_header_literal=header_lit,
                    selection_literal=sel_lit,
                    market_family=fam,
                    canonical_key=ckey,
                    direction=direction,
                    line=line_str,
                    odd=str(odd_dec) if odd_dec is not None else None,
                    temporal_scope=t_scope,
                    target_scope=tgt_scope,
                    target_entity=tgt_ent,
                    captured_at_utc=captured_at_utc,
                    source_txt_path=str(path),
                    source_txt_sha256=txt_sha256,
                    settlement_rules_available=sett_avail,
                    settlement_status=sett_status,
                    commercial_coverage=comm_cov,
                    interpretation_status=interp,
                )
            )

    by_cov: dict[str, int] = {}
    by_fam: dict[str, int] = {}
    settlement_audit: dict[str, dict[str, int]] = {
        "modeled_with_verified_settlement": {},
        "commercial_verified_not_modeled": {},
        "needs_api_settlement_reconciliation": {},
        "mapping_unresolved": {},
        "price_unavailable": {},
    }
    for e in entries:
        by_cov[e.commercial_coverage] = by_cov.get(e.commercial_coverage, 0) + 1
        by_fam[e.market_family] = by_fam.get(e.market_family, 0) + 1
        if e.commercial_coverage == "PRICE_UNAVAILABLE":
            bkt = "price_unavailable"
        elif e.commercial_coverage == "MAPPING_UNRESOLVED":
            bkt = "mapping_unresolved"
        elif e.commercial_coverage == "SETTLEMENT_UNVERIFIED":
            bkt = "needs_api_settlement_reconciliation"
        elif e.commercial_coverage == "OFFERED_AND_MODELED":
            bkt = "modeled_with_verified_settlement"
        else:
            bkt = "commercial_verified_not_modeled"
        settlement_audit[bkt][e.market_family] = settlement_audit[bkt].get(e.market_family, 0) + 1

    return {
        "schema_version": "1.0-bwin-commercial-catalog",
        "event_id": event_id,
        "fixture_id": fixture_id,
        "captured_at_utc": captured_at_utc,
        "source_txt_path": str(path),
        "source_txt_sha256": txt_sha256,
        "total_selections": len(entries),
        "duplicate_selections_skipped": duplicate_count,
        "unexpanded_blocks": unexpanded_blocks,
        "counts_by_commercial_coverage": by_cov,
        "counts_by_family": by_fam,
        "settlement_audit_summary": settlement_audit,
        "entries": [e.to_dict() for e in entries],
    }


def _classify_selection(
    *,
    m_low: str,
    header_lit: str,
    sel_lit: str,
    t_scope: str,
    home_keys: set[str],
    away_keys: set[str],
    home_name: str | None,
    away_name: str | None,
) -> tuple[str, str | None, str | None, str | None, str, str | None, bool, str, str]:
    sel_low = _norm(sel_lit)
    ou = _parse_ou_selection(sel_lit)

    # 1. Player props: Jugador - Total de tiros (2+), tiros a porteria (2+), faltas (2+), asistencias (1+)
    m_player = re.match(
        r"^jugador\s*-\s*total de\s+(tiros a porter[ií]a|tiros|faltas|asistencias)\s*\((\d+)\+\)",
        m_low,
    )
    if m_player:
        kind_raw = m_player.group(1)
        thresh = int(m_player.group(2))
        fam_map = {
            "tiros": "PLAYER_SHOTS",
            "tiros a porteria": "PLAYER_ON_TARGET",
            "tiros a portería": "PLAYER_ON_TARGET",
            "faltas": "PLAYER_FOULS_COMMITTED",
            "asistencias": "PLAYER_ASSISTS",
        }
        fam = fam_map.get(kind_raw, "PLAYER_PROP")
        slug = f"{thresh}_plus"
        ckey = f"player_{fam.lower()}_{slug}_{_norm(sel_lit).replace(' ', '_')}"
        return fam, ckey, f"{thresh}+", str(thresh), "PLAYER", sel_lit, False, "SETTLEMENT_UNVERIFIED", "PLAYER_PROP_COMMERCIAL"

    # 1b. Goleadores (player goalscorers)
    if m_low.startswith("goleadores"):
        return "PLAYER_SCORER", None, None, None, "PLAYER", sel_lit, False, "SETTLEMENT_UNVERIFIED", "PLAYER_SCORER_SETTLEMENT_UNVERIFIED"

    # 2. Cards (fixes 'Número de tarjetas en total' + 'Total de tarjetas' + team cards + Mas tarjetas)
    if re.search(r"(?:n[uú]mero de tarjetas en total|total de tarjetas|total tarjetas|tarjetas en total)", m_low):
        is_team = " - " in m_low and not m_low.startswith(("numero de tarjetas", "número de tarjetas", "total de tarjetas"))
        side = None
        ent = None
        if is_team:
            pref = m_low.split(" - ", 1)[0].strip()
            if _match_team_key(pref, home_keys):
                side, ent = "HOME", home_name
            elif _match_team_key(pref, away_keys):
                side, ent = "AWAY", away_name
        tgt_scope = side or "MATCH"
        fam = f"CARDS_{t_scope}_{tgt_scope}" if is_team or t_scope != "FT" else "CARDS_TOTAL"
        if ou:
            d_str, line_dec = ou
            l_slug = str(line_dec).replace(".", "_")
            pref_ck = f"{side.lower()}_" if side else ""
            h1_ck = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
            ckey = f"{h1_ck}{pref_ck}{d_str}_{l_slug}_cards"
            return fam, ckey, d_str, str(line_dec), tgt_scope, ent, False, "SETTLEMENT_UNVERIFIED", "COMMERCIAL_CARDS_SETTLEMENT_UNVERIFIED"
        return fam, None, None, None, tgt_scope, ent, False, "SETTLEMENT_UNVERIFIED", "CARDS_NON_OU"

    if m_low.startswith("mas tarjetas") or m_low.startswith("más tarjetas"):
        return "CARDS_MOST", None, None, None, "MATCH", None, False, "SETTLEMENT_UNVERIFIED", "CARDS_MOST_SETTLEMENT_UNVERIFIED"

    # 3. Corners (team vs match, H1/H2 vs FT, most corners)
    if m_low.startswith("equipo con mas corners") or m_low.startswith("equipo con más córners"):
        fam = f"CORNERS_MOST_{t_scope}" if t_scope != "FT" else "CORNERS_MOST"
        return fam, None, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    if "total de corners" in m_low or "total de córners" in m_low:
        is_team = " - total de c" in m_low
        side = None
        ent = None
        if is_team:
            pref = re.split(r"\s*-\s*total de c[oó]rners", m_low, maxsplit=1)[0].strip()
            if _match_team_key(pref, home_keys):
                side, ent = "HOME", home_name
            elif _match_team_key(pref, away_keys):
                side, ent = "AWAY", away_name
        tgt_scope = side or "MATCH"
        fam = "CORNERS_TEAM" if side else "CORNERS_TOTAL"
        if t_scope == "H1":
            fam = f"{fam}_H1"
        elif t_scope == "H2":
            fam = f"{fam}_H2"
        if ou:
            d_str, line_dec = ou
            l_slug = str(line_dec).replace(".", "_")
            pref_ck = f"{side.lower()}_" if side else ""
            h1_ck = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
            ckey = f"{h1_ck}{pref_ck}{d_str}_{l_slug}_corners"
            return fam, ckey, d_str, str(line_dec), tgt_scope, ent, True, "VERIFIED_STANDARD", "OK"

    # 4. Goals (match vs team, H1/H2 vs FT)
    if "total de goles" in m_low and "ambos equipos" not in m_low and "resultado del partido" not in m_low:
        is_team = " - total de goles" in m_low
        side = None
        ent = None
        if is_team:
            pref = m_low.split(" - total de goles", 1)[0].strip()
            if _match_team_key(pref, home_keys):
                side, ent = "HOME", home_name
            elif _match_team_key(pref, away_keys):
                side, ent = "AWAY", away_name
        tgt_scope = side or "MATCH"
        if side:
            fam = f"GOALS_TEAM_{t_scope}" if t_scope != "FT" else "GOALS_TEAM"
        else:
            fam = f"GOALS_{t_scope}" if t_scope != "FT" else "GOALS_FT"
        if ou:
            d_str, line_dec = ou
            l_slug = str(line_dec).replace(".", "_")
            pref_ck = f"{side.lower()}_" if side else ""
            h1_ck = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
            ckey = f"{h1_ck}{pref_ck}{d_str}_{l_slug}_goals"
            return fam, ckey, d_str, str(line_dec), tgt_scope, ent, True, "VERIFIED_STANDARD", "OK"

    # 5. BTTS
    if m_low.startswith("ambos equipos marcan") and "y " not in m_low:
        fam = f"BTTS_{t_scope}" if t_scope != "FT" else "BTTS"
        pref_btts = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
        if sel_low in {"si", "sí", "yes"}:
            ckey = f"{pref_btts}btts_yes"
            return fam, ckey, "yes", None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"
        if sel_low == "no":
            ckey = f"{pref_btts}btts_no"
            return fam, ckey, "no", None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    # 6. 1X2, Double Chance, DNB, Handicap 1X2, Exact Score, First to score, Combos
    if m_low.startswith("resultado del partido") and "y " not in m_low:
        fam = f"1X2_{t_scope}" if t_scope != "FT" else "1X2"
        pref = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
        if sel_low == "x":
            return fam, f"{pref}draw", "draw", None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"
        if _match_team_key(sel_low, home_keys):
            return fam, f"{pref}home_win", "home", None, "HOME", home_name, True, "VERIFIED_STANDARD", "OK"
        if _match_team_key(sel_low, away_keys):
            return fam, f"{pref}away_win", "away", None, "AWAY", away_name, True, "VERIFIED_STANDARD", "OK"

    if m_low.startswith("doble oportunidad"):
        fam = f"DOUBLE_CHANCE_{t_scope}" if t_scope != "FT" else "DOUBLE_CHANCE"
        pref = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
        parts = [p.strip() for p in sel_low.split(" o ")]
        if len(parts) == 2:
            has_x = "x" in parts
            has_h = any(_match_team_key(p, home_keys) for p in parts if p != "x")
            has_a = any(_match_team_key(p, away_keys) for p in parts if p != "x")
            ck = f"{pref}double_chance_1x" if (has_h and has_x) else (f"{pref}double_chance_x2" if (has_a and has_x) else (f"{pref}double_chance_12" if (has_h and has_a) else None))
            return fam, ck, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"
        return fam, None, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    if m_low.startswith("ganador sin empate"):
        fam = f"DNB_{t_scope}" if t_scope != "FT" else "DNB"
        pref = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
        if _match_team_key(sel_low, home_keys):
            return fam, f"{pref}dnb_home", "home", None, "HOME", home_name, True, "VERIFIED_STANDARD", "OK"
        if _match_team_key(sel_low, away_keys):
            return fam, f"{pref}dnb_away", "away", None, "AWAY", away_name, True, "VERIFIED_STANDARD", "OK"
        return fam, None, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    if m_low.startswith("marcador exacto"):
        fam = f"EXACT_SCORE_{t_scope}" if t_scope != "FT" else "EXACT_SCORE"
        pref = "h1_" if t_scope == "H1" else ("h2_" if t_scope == "H2" else "")
        m_sc = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", sel_lit.strip())
        ck = f"{pref}exact_score_{int(m_sc.group(1))}_{int(m_sc.group(2))}" if m_sc else None
        return fam, ck, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    if m_low.startswith("handicap 1x2") or m_low.startswith("hándicap 1x2"):
        return "EUROPEAN_HANDICAP_1X2", None, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    if m_low.startswith("1er equipo en marcar"):
        fam = f"FIRST_TO_SCORE_{t_scope}" if t_scope != "FT" else "FIRST_TO_SCORE"
        return fam, None, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    if any(m_low.startswith(p) for p in ("resultado del partido y ", "resultado (1x2) y ", "ambos equipos marcan y ", "descanso/final", "descanso o final", "gana ambas partes")):
        return "COMBO_OR_HTFT", None, None, None, "MATCH", None, True, "VERIFIED_STANDARD", "OK"

    return "UNMAPPED_COMMERCIAL_MARKET", None, None, None, "MATCH", None, False, "SETTLEMENT_UNVERIFIED", "UNMAPPED_HEADER_PRESERVED"
