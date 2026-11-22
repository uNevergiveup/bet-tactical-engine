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
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bet_engine.orchestration.shadow_pipeline import _expand_team_keys, _match_team_key
from .market_equivalence import canonicalize_selection_key

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


COLLECTIVE_COUNT_METRICS = {
    "SHOTS_COLLECTIVE": "shots",
    "ON_TARGET_COLLECTIVE": "on_target",
    "FOULS_COLLECTIVE": "fouls",
    "OFFSIDES_COLLECTIVE": "offsides",
}
_COLLECTIVE_HEADERS = {
    "tiros": "SHOTS_COLLECTIVE", "tiros a porteria": "ON_TARGET_COLLECTIVE",
    "tiros a puerta": "ON_TARGET_COLLECTIVE", "remates a puerta": "ON_TARGET_COLLECTIVE",
    "faltas": "FOULS_COLLECTIVE", "fueras de juego": "OFFSIDES_COLLECTIVE",
}

ON_TARGET_SETTLEMENT_POLICY = {
    "policy_version": "bwin-opta-on-target-v1",
    "settlement_provider": "OPTA_STATS_PERFORM",
    "settlement_basis": "USER_CONFIRMED_2026-11-22",
    "inclusive_threshold_rule": "N_PLUS_EQUALS_OVER_N_MINUS_0_5",
    "historical_evidence_provider": "API_FOOTBALL",
    "provider_reconciliation_status": "DEFINITION_ALIGNED_EMPIRICAL_RECONCILIATION_PENDING",
    "post_event_correction_policy": "NOT_DOCUMENTED_LOCALLY_PREMATCH_SNAPSHOT_IMMUTABLE",
}


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
    settlement_provider: str | None = None
    settlement_basis: str | None = None
    historical_evidence_provider: str | None = None
    provider_reconciliation_status: str | None = None
    post_event_correction_policy: str | None = None

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
    if market_family == "ON_TARGET_COLLECTIVE":
        return bool(_DYNAMIC_COUNT_KEY_RE.fullmatch(canonical_key)) and not canonical_key.startswith(("h1_", "h2_"))
    return False


_DYNAMIC_COUNT_KEY_RE = re.compile(
    r"(?:(?:h1|h2)_)?(?:(?:home|away)_)?(?:over|under)_\d+_\d+_(?:shots|on_target|fouls|offsides)"
)


def _count_contract_key(metric: str, direction: str, line: Decimal, side: str, period: str) -> str | None:
    if side not in {"MATCH", "HOME", "AWAY"} or period not in {"FT", "H1", "H2"}:
        return None
    period_prefix = "" if period == "FT" else f"{period.lower()}_"
    side_prefix = "" if side == "MATCH" else f"{side.lower()}_"
    line_slug = str(line).replace(".", "_")
    return f"{period_prefix}{side_prefix}{direction}_{line_slug}_{metric}"


def _settlement_metadata(family: str, status: str) -> dict[str, str | None]:
    policy = ON_TARGET_SETTLEMENT_POLICY if family == "ON_TARGET_COLLECTIVE" and status == "VERIFIED_STANDARD" else {}
    return {
        "settlement_provider": policy.get("settlement_provider"),
        "settlement_basis": policy.get("settlement_basis"),
        "historical_evidence_provider": policy.get("historical_evidence_provider"),
        "provider_reconciliation_status": policy.get("provider_reconciliation_status"),
        "post_event_correction_policy": policy.get("post_event_correction_policy"),
    }


def _coverage_family(header: str, family: str) -> str:
    """Agrupa por tema comercial, incluso cuando falta un mapeo contractual."""
    label = _norm(header)
    if family.startswith("CORNERS") or "corner" in label:
        return "CORNERS"
    if family.startswith("CARDS") or "tarjeta" in label:
        return "CARDS"
    for token, group in (
        ("fuera de juego", "OFFSIDES"), ("paradas", "SAVES"),
        ("tiros a porteria", "ON_TARGET"), ("tiros", "SHOTS"),
        ("falta", "FOULS"), ("asistencia", "ASSISTS"),
        ("crack bwin", "BWIN_SPECIALS"),
    ):
        if token in label:
            return group
    if family == "COMBO_OR_HTFT" or " y " in label:
        return "COMBINED"
    if family.startswith(("GOALS", "BTTS", "PLAYER_SCORER", "FIRST_TO_SCORE")) or "gol" in label:
        return "GOALS"
    if family.startswith(("1X2", "DNB", "DOUBLE_CHANCE", "EXACT_SCORE", "EUROPEAN_HANDICAP")):
        return "RESULT"
    return "OTHER"


def _capture_coverage(raw_text, parsed, sha256, diagnostics_path):
    header = raw_text.split("MERCADO:", 1)[0]
    declared = re.search(r"\|\s*Selecciones:\s*(\d+)", header)
    widgets = re.search(r"Aviso de cobertura:\s*(\d+)\s+widget", header)
    rows = sum(len(block["selections"]) for block in parsed["markets"])
    expected = int(declared[1]) if declared else None
    gaps = {}
    if expected is not None and expected != rows:
        gaps["selection_count_mismatch"] = {"declared": expected, "parsed": rows}
    if widgets and int(widgets[1]):
        gaps["recommendations_not_expanded"] = int(widgets[1])
    empty = [block["market"] for block in parsed["markets"] if not block["selections"]]
    if empty:
        gaps["empty_txt_markets"] = empty
    evidence = None
    if diagnostics_path is not None:
        path = Path(diagnostics_path)
        doc = json.loads(path.read_bytes())
        if (str(doc.get("event_id")) != parsed["event_id"] or doc.get("estado") != "correcto"
                or doc.get("selecciones") != (expected if expected is not None else rows)
                or (doc.get("snapshot_sha256") and doc["snapshot_sha256"] != sha256)):
            raise ValueError("BWIN_CAPTURE_DIAGNOSTICS_MISMATCH")
        evidence = {"path": str(path), "sha256": _sha256_file(path),
                    "binding": "SNAPSHOT_SHA256" if doc.get("snapshot_sha256") else "LEGACY_EVENT_AND_COUNT_ONLY"}
        if doc.get("ambos_equipos_tarjetas"):
            evidence["both_teams_cards"] = doc["ambos_equipos_tarjetas"]
        if doc.get("vista_todo"):
            evidence["all_markets_view"] = doc["vista_todo"]
        final = doc.get("final") or {}
        for key, value in {
            "extraction_errors": doc.get("errores"), "empty_scraper_markets": doc.get("vacios"),
            "unavailable_options": doc.get("no_disponibles"), "closed_accordions": final.get("cerrados"),
            "show_more_remaining": final.get("mostrar_mas"),
            "recommendations_not_expanded": doc.get("recomendados_no_expandidos"),
            "scroll_bottom_not_reached": final.get("fondo") is False,
        }.items():
            if value:
                gaps[key] = value
    return {"status": "GAPS_RECORDED" if gaps else "COMPLETENESS_NOT_CERTIFIED",
            "declared_selection_rows": expected, "parsed_selection_rows": rows,
            "market_contexts": len({block["market"] for block in parsed["markets"]}),
            "diagnostics": evidence, "gaps": gaps}


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
    diagnostics_path: Path | None = None,
) -> dict[str, Any]:
    """Extrae el catálogo comercial completo desde un snapshot TXT Bwin sin inventar cuotas."""
    path = Path(bwin_txt_path)
    if not path.is_file():
        raise FileNotFoundError(f"BWIN_TXT_NOT_FOUND:{path}")

    from motor_probabilistico import parse_odds

    parsed = parse_odds(path)
    if event_id is not None and str(event_id) != parsed["event_id"]:
        raise ValueError("BWIN_CATALOG_EVENT_ID_MISMATCH")
    event_id = parsed["event_id"]
    txt_sha256 = _sha256_file(path)
    raw_text = path.read_text(encoding="utf-8", errors="replace")

    # Try to extract capture timestamp from header if not provided
    if captured_at_utc is None:
        from datetime import datetime, timezone

        capture = parsed.get("capture")
        if capture:
            try:
                observed = datetime.fromisoformat(capture)
                if observed.tzinfo is not None:
                    captured_at_utc = observed.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    if captured_at_utc is None:
        m_ts = re.search(r"(?:captured_at_utc|timestamp_utc|fecha_captura)\s*[:=]\s*(\S+)", raw_text[:1200], re.I)
        if m_ts:
            captured_at_utc = m_ts.group(1).strip()

    home_keys = _expand_team_keys(home_name, home_aliases)
    away_keys = _expand_team_keys(away_name, away_aliases)

    entries: list[BwinCatalogEntry] = []
    seen_quotes = set()
    prices_by_selection = defaultdict(set)
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
        if re.search(r"\b(?:entre el|minuto|\d{2}:\d{2})", m_low):
            t_scope = "UNKNOWN"
        elif "1a parte" in m_low or "1ª parte" in m_low or "primera parte" in m_low:
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
            if (sid, odd_dec) in seen_quotes:
                duplicate_count += 1
                continue
            seen_quotes.add((sid, odd_dec))
            prices_by_selection[sid].add(odd_dec)

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
            elif t_scope == "UNKNOWN":
                comm_cov = "MAPPING_UNRESOLVED"
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
                    **_settlement_metadata(fam, sett_status),
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

    coverage = _capture_coverage(raw_text, parsed, txt_sha256, diagnostics_path)
    conflicts = sum(len(prices) > 1 for prices in prices_by_selection.values())
    if conflicts:
        coverage["gaps"]["selections_with_conflicting_prices"] = conflicts
        coverage["status"] = "GAPS_RECORDED"
    families = defaultdict(lambda: {"markets": set(), "priced": 0, "contracts": set(),
                                    "unresolved": 0, "verdicts": Counter()})
    catalog_rows = []
    all_contracts = set()
    for e in entries:
        family = _coverage_family(e.market_header_literal, e.market_family)
        summary = families[family]
        summary["markets"].add(e.market_header_literal)
        summary["priced"] += e.odd is not None
        verdict = e.commercial_coverage
        contract_id = None
        if re.search(r"\b(live|resto del partido|marcador actual|en directo)\b", _norm(e.market_header_literal)):
            verdict = "INAPPLICABLE_PREMATCH"
        elif len(prices_by_selection[e.selection_id]) > 1:
            verdict = "CONFLICTING_CAPTURE_PRICES"
        elif e.odd is not None and e.settlement_rules_available and e.canonical_key and e.temporal_scope in {"FT", "H1", "H2"}:
            contract = canonicalize_selection_key(e.canonical_key)
            contract_id = contract.contract_id if contract and contract.period == e.temporal_scope else e.canonical_key
            summary["contracts"].add(contract_id)
            all_contracts.add(contract_id)
        if e.odd is not None and contract_id is None:
            summary["unresolved"] += 1
        summary["verdicts"][verdict] += 1
        row = e.to_dict()
        if len(prices_by_selection[e.selection_id]) > 1:
            row.update(selection_id=_stable_selection_id(e.event_id, e.market_header_literal, f"{e.selection_literal}|{e.odd}"),
                       canonical_key=None)
        if verdict in {"CONFLICTING_CAPTURE_PRICES", "INAPPLICABLE_PREMATCH", "MAPPING_UNRESOLVED"}:
            row.update(canonical_key=None, commercial_coverage="MAPPING_UNRESOLVED")
        catalog_rows.append({**row, "coverage_family": family, "contract_id": contract_id,
                             "verdict": verdict, "commercial_reason": verdict if verdict != e.commercial_coverage else e.interpretation_status})

    return {
        "schema_version": "1.0-bwin-commercial-catalog",
        "event_id": event_id,
        "fixture_id": fixture_id,
        "captured_at_utc": captured_at_utc,
        "source_txt_path": str(path),
        "source_txt_sha256": txt_sha256,
        "settlement_policies": {ON_TARGET_SETTLEMENT_POLICY["policy_version"]: ON_TARGET_SETTLEMENT_POLICY},
        "total_selections": len(entries),
        "duplicate_selections_skipped": duplicate_count,
        "unexpanded_blocks": unexpanded_blocks,
        "counts_by_commercial_coverage": by_cov,
        "counts_by_family": by_fam,
        "settlement_audit_summary": settlement_audit,
        "capture_coverage": coverage,
        "unique_resolved_contracts": len(all_contracts),
        "coverage_by_family": {
            family: {"markets": len(s["markets"]), "priced_selections": s["priced"],
                     "unique_resolved_contracts": len(s["contracts"]),
                     "priced_without_resolved_contract": s["unresolved"],
                     "by_verdict": dict(s["verdicts"])}
            for family, s in sorted(families.items())
        },
        "entries": catalog_rows,
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
    joint = None
    if re.match(r"^ambos equipos (?:recibiran|reciben|recibir) x o mas tarjetas\b", m_low):
        joint = "CARDS_BOTH_TEAMS"
    elif re.match(r"^ambos equipos x o mas corners\b", m_low):
        joint = "CORNERS_BOTH_TEAMS"
    if joint:
        outcome = re.fullmatch(r"([1-9]\d*)\s*\+\s*·\s*(si|no)", sel_low)
        if outcome:
            threshold, direction = outcome.groups()
            return (joint, None, "yes" if direction == "si" else "no", threshold,
                    "MATCH", None, False, "SETTLEMENT_UNVERIFIED", "EACH_TEAM_AT_LEAST_THRESHOLD")
        return joint, None, None, None, "MATCH", None, False, "SETTLEMENT_UNVERIFIED", "THRESHOLD_OR_OUTCOME_UNRESOLVED"

    if re.match(r"^recibe una tarjeta(?:\s*\||\s*\(|$)", m_low):
        return "PLAYER_CARDS", None, None, None, "PLAYER", sel_lit, False, "SETTLEMENT_UNVERIFIED", "PLAYER_CARD_SETTLEMENT_UNVERIFIED"
    if m_low == "jugador marca o asiste":
        return "PLAYER_GOAL_OR_ASSIST", None, None, None, "PLAYER", sel_lit, False, "SETTLEMENT_UNVERIFIED", "PLAYER_PROP_COMMERCIAL"
    crack = re.fullmatch(r"crack bwin(?: nuevo)?\s*\(([1-9]\d*)\+\)", m_low)
    if crack:
        return "CRACK_BWIN", None, "at_least", crack[1], "PLAYER", sel_lit, False, "SETTLEMENT_UNVERIFIED", "PLAYER_POINTS_COMPOSITE"
    for prefix, family in (("tarjeta roja - si/no", "RED_CARD_YES_NO"), ("penalti - si/no", "PENALTY_YES_NO")):
        if m_low.split(" | ", 1)[0] == prefix:
            direction = {"si": "yes", "no": "no"}.get(sel_low)
            return family, None, direction, None, "MATCH", None, False, "SETTLEMENT_UNVERIFIED", "YES_NO_COMMERCIAL"

    # Exact labels observed in the collective-count markets. Preserve X+ as
    # an inclusive threshold without assuming a model or settlement equivalence.
    base = m_low.split(" | ", 1)[0]
    prefix, separator, label = base.partition(": ")
    if not separator:
        prefix, separator, label = base.partition(" - ")
    metric_label = (label if separator else base).removeprefix("total de ").removesuffix(" del partido")
    family = _COLLECTIVE_HEADERS.get(metric_label)
    if family:
        side, entity = ("MATCH", None)
        if separator:
            home_hit, away_hit = _match_team_key(prefix, home_keys), _match_team_key(prefix, away_keys)
            if home_hit != away_hit:
                side, entity = ("HOME", home_name) if home_hit else ("AWAY", away_name)
            else:
                side = "UNKNOWN"
        plus = re.fullmatch(r"([1-9]\d*)\+\s*(.+)", sel_low)
        if plus and _COLLECTIVE_HEADERS.get(plus[2]) == family:
            if family == "ON_TARGET_COLLECTIVE":
                commercial_threshold = int(plus[1])
                model_line = Decimal(commercial_threshold) - Decimal("0.5")
                key = _count_contract_key("on_target", "over", model_line, side, t_scope)
                if key:
                    return (family, key, "at_least", plus[1], side, entity, True, "VERIFIED_STANDARD",
                            "OPTA_SETTLEMENT_USER_CONFIRMED_INCLUSIVE_THRESHOLD")
            return family, None, "at_least", plus[1], side, entity, False, "SETTLEMENT_UNVERIFIED", "COUNT_THRESHOLD_COMMERCIAL"
        if ou:
            if family == "ON_TARGET_COLLECTIVE":
                direction, line = ou
                key = _count_contract_key("on_target", direction, line, side, t_scope)
                if key:
                    return (family, key, direction, str(line), side, entity, True, "VERIFIED_STANDARD",
                            "OPTA_SETTLEMENT_USER_CONFIRMED_COUNT_OU")
            return family, None, ou[0], str(ou[1]), side, entity, False, "SETTLEMENT_UNVERIFIED", "COUNT_OU_COMMERCIAL"

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
        tgt_scope = side or ("UNKNOWN" if is_team else "MATCH")
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

    if re.fullmatch(r"(?:.+ - )?total de corners(?:\s*\|.*)?", m_low):
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
        if is_team and side is None:
            return "CORNERS_TEAM", None, None, None, "UNKNOWN", None, False, "SETTLEMENT_UNVERIFIED", "TEAM_UNRESOLVED"
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
    if re.fullmatch(r"(?:.+ - )?total de goles(?:\s*\|.*)?", m_low):
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
        if is_team and side is None:
            return "GOALS_TEAM", None, None, None, "UNKNOWN", None, False, "SETTLEMENT_UNVERIFIED", "TEAM_UNRESOLVED"
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
