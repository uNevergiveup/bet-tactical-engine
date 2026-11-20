"""Parser estructurado de informes cualitativos PREMATCH (Etapa 2 / Auditoría V1.0).

Unifica la lectura tolerante y verificable de identidad, marcas temporales ISO 8601,
alineaciones probables/oficiales (incluyendo tablas paralelas por equipo) y
procedencia (Grok, Perplexity, consolidación Codex y hashes) para `prematch_case.py`
y `validate_prematch.py`, sin relajar bloqueos ante conflictos reales.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

_ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})"
)

_EVENT_ID_ALIASES = {
    "event_id",
    "event_id_bwin",
    "evento_bwin",
    "bwin_event_id",
    "id_evento_bwin",
}

_FIXTURE_ID_ALIASES = {
    "fixture_id",
    "fixture_id_api_football",
    "api_fixture_id",
    "fixture_api",
    "fixture_api_football",
    "id_fixture_api",
}

_KICKOFF_UTC_ALIASES = {"kickoff_utc", "fecha_utc", "kickoff_utc_iso"}
_KICKOFF_MADRID_ALIASES = {
    "kickoff_madrid",
    "kickoff_europe_madrid",
    "kickoff_cet",
    "kickoff_cest",
    "kickoff_espana",
}
_CUTOFF_ALIASES = {
    "analysis_cutoff_utc",
    "cutoff_utc",
    "cutoff_madrid",
    "corte_documental_utc",
    "corte_analitico_utc",
}
_GENERATED_ALIASES = {
    "report_generated_utc",
    "report_completed_madrid",
    "report_completed_utc",
}


def _norm_ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9]+", " ", stripped).strip().lower())


def _canonical_key(raw_key: str) -> str:
    cleaned = re.sub(r"[`*]", "", raw_key).lstrip("-* ").strip()
    return re.sub(r"[^a-z0-9]+", "_", _norm_ascii(cleaned)).strip("_")


def _extract_scalar_pairs(raw: str, *, max_chars: int = 8000) -> list[tuple[str, str]]:
    """Extrae pares clave->valor de cabecera Markdown y declaraciones explícitas de ID en todo el informe."""
    pairs: list[tuple[str, str]] = []
    pos = 0
    id_aliases = _EVENT_ID_ALIASES | _FIXTURE_ID_ALIASES
    for line in raw.splitlines(keepends=True):
        text = line.strip()
        in_header = pos < max_chars
        pos += len(line)
        if not text:
            continue
        if text.startswith("|"):
            if not in_header:
                continue
            cells = [c.strip() for c in text.strip("|").split("|")]
            if len(cells) >= 2 and not re.fullmatch(r"[-:\s]+", cells[0]):
                ckey = _canonical_key(cells[0])
                cval = cells[1].strip(" *`")
                if ckey:
                    pairs.append((ckey, cval))
            continue
        if ":" not in text:
            continue
        lhs, rhs = text.split(":", 1)
        ckey = _canonical_key(lhs)
        if not ckey:
            continue
        if in_header or ckey in id_aliases:
            pairs.append((ckey, rhs.strip(" `*")))
    return pairs


def _extract_id_digits(raw_val: str) -> str | None:
    text = str(raw_val or "").strip()
    if re.fullmatch(r"\d{4,}", text):
        return text
    hit = re.search(r"(?<!\d)(\d{5,})(?!\d)", text)
    return hit.group(1) if hit else None


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    norm_v = str(value).strip()
    if norm_v.lower().startswith("null") or _norm_ascii(norm_v) in {"null", "none", "n d", "desconocida"}:
        return None
    m = _ISO_RE.search(norm_v)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group().replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def _extract_parallel_xi_table(
    raw: str,
    home: str | None,
    away: str | None,
    home_bwin: str | None = None,
    away_bwin: str | None = None,
    *,
    club_matcher: Any = None,
) -> dict[str, dict[str, Any]]:
    """Extrae tablas paralelas '| N.º | Equipo A — PROBABLE | Equipo B — PROBABLE |'."""
    if not home or not away:
        return {}

    def _matches_team(col_header: str, api_name: str, bwin_name: str | None) -> bool:
        core = re.split(r"\s+[—–-]\s+", re.sub(r"\([^)]*\)|\[[^]]*\]", "", col_header).strip(), maxsplit=1)[0].strip()
        if not core:
            return False
        if club_matcher is not None:
            return bool(club_matcher(core, api_name, bwin_name))
        c_tok = set(_norm_ascii(core).split())
        t_tok = set(_norm_ascii(api_name).split()) | set(_norm_ascii(bwin_name or "").split())
        return bool(c_tok and t_tok and (c_tok <= t_tok or t_tok <= c_tok))

    sections = re.split(r"(?=^#{2,5}\s+)", raw, flags=re.M)
    for sec in sections:
        h_m = re.match(r"^#{2,5}\s+([^\n]+)", sec)
        if not h_m:
            continue
        sec_heading = _norm_ascii(h_m.group(1))
        if not any(tok in sec_heading for tok in ("xi", "alineacion", "once")):
            continue
        # Buscar tabla paralela con columnas para ambos clubes
        home_col = away_col = None
        home_players: list[str] = []
        away_players: list[str] = []
        col_headers: list[str] = []
        started = False
        for line in sec[h_m.end():].splitlines():
            if not line.lstrip().startswith("|"):
                if started and (home_players or away_players):
                    break
                continue
            cells = [re.sub(r"[`*]", "", c).strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r"[-:\s]+", c or "-") for c in cells):
                continue
            if not started:
                for idx, ch in enumerate(cells):
                    if home_col is None and _matches_team(ch, home, home_bwin):
                        home_col = idx
                    elif away_col is None and _matches_team(ch, away, away_bwin):
                        away_col = idx
                if home_col is not None and away_col is not None:
                    col_headers = cells
                    started = True
                continue
            if max(home_col, away_col) >= len(cells):
                continue
            if not re.fullmatch(r"\d{1,2}", cells[0]):
                continue
            p_h = re.sub(r"\s*\[[^\]]+\]\([^)]*\)", "", cells[home_col]).strip(" ;.,")
            p_a = re.sub(r"\s*\[[^\]]+\]\([^)]*\)", "", cells[away_col]).strip(" ;.,")
            if p_h and "?" not in p_h and _norm_ascii(p_h) not in {"n d", "no disponible"}:
                home_players.append(p_h)
            if p_a and "?" not in p_a and _norm_ascii(p_a) not in {"n d", "no disponible"}:
                away_players.append(p_a)

        if home_players or away_players:
            def _classify(col_text: str) -> str:
                low = _norm_ascii(col_text) + " " + sec_heading
                if "probable" in low or "proyectad" in low:
                    return "PROBABLE"
                if "confirmado oficial" in low or "oficial confirmad" in low:
                    return "CONFIRMADO_OFICIAL_DECLARADO_NO_VERIFICADO"
                return "PUBLICADO_PREPARTIDO_DECLARADO_NO_VERIFICADO"

            out: dict[str, dict[str, Any]] = {}
            if home_players:
                out["local"] = {
                    "players": home_players,
                    "classification": _classify(col_headers[home_col]),
                    "heading": h_m.group(1).strip(),
                    "rows": len(home_players),
                }
            if away_players:
                out["visitante"] = {
                    "players": away_players,
                    "classification": _classify(col_headers[away_col]),
                    "heading": h_m.group(1).strip(),
                    "rows": len(away_players),
                }
            return out
    return {}


def parse_research_identity_and_metadata(
    raw: str,
    *,
    home: str | None = None,
    away: str | None = None,
    home_bwin: str | None = None,
    away_bwin: str | None = None,
    club_matcher: Any = None,
) -> dict[str, Any]:
    """Extrae identidad verificable, fechas ISO 8601, autoría/proveniencia y XI paralelos."""
    pairs = _extract_scalar_pairs(raw)
    event_ids: list[str] = []
    fixture_ids: list[str] = []
    fields: dict[str, str] = {}

    alias_map = {
        **{k: "event_id_bwin" for k in _EVENT_ID_ALIASES},
        **{k: "fixture_id" for k in _FIXTURE_ID_ALIASES},
        **{k: "kickoff_utc" for k in _KICKOFF_UTC_ALIASES},
        **{k: "kickoff_madrid" for k in _KICKOFF_MADRID_ALIASES},
        **{k: "analysis_cutoff_utc" for k in _CUTOFF_ALIASES},
        **{k: "report_generated_utc" for k in _GENERATED_ALIASES},
    }

    for k, v in pairs:
        if k in _EVENT_ID_ALIASES:
            dig = _extract_id_digits(v)
            if dig and dig not in event_ids:
                event_ids.append(dig)
        if k in _FIXTURE_ID_ALIASES:
            dig = _extract_id_digits(v)
            if dig and dig not in fixture_ids:
                fixture_ids.append(dig)
        target = alias_map.get(k)
        if target and target not in fields:
            fields[target] = v

    # Autoría y hashes fuente preservados en el cuerpo
    sources_attributed = {
        "grok": bool(re.search(r"\bgrok\b", raw, re.I)),
        "perplexity": bool(re.search(r"\bperplexity\b", raw, re.I)),
        "codex_consolidation": bool(re.search(r"\bconsolidado\b|\bcodex\b", raw, re.I)),
    }
    sha_refs = sorted(set(re.findall(r"\b[0-9a-f]{64}\b", raw.lower())))

    parallel_xi = _extract_parallel_xi_table(
        raw,
        home,
        away,
        home_bwin,
        away_bwin,
        club_matcher=club_matcher,
    )

    kickoff_utc = _parse_iso_dt(fields.get("kickoff_utc"))
    kickoff_madrid = _parse_iso_dt(fields.get("kickoff_madrid"))
    cutoff_dt = _parse_iso_dt(fields.get("analysis_cutoff_utc"))
    generated_dt = _parse_iso_dt(fields.get("report_generated_utc"))

    return {
        "event_ids_found": event_ids,
        "fixture_ids_found": fixture_ids,
        "event_id_conflict": len(event_ids) > 1,
        "fixture_id_conflict": len(fixture_ids) > 1,
        "event_id": event_ids[0] if len(event_ids) == 1 else None,
        "fixture_id": fixture_ids[0] if len(fixture_ids) == 1 else None,
        "fields": fields,
        "kickoff_utc": kickoff_utc,
        "kickoff_madrid": kickoff_madrid,
        "cutoff_utc": cutoff_dt,
        "generated_utc": generated_dt,
        "sources_attributed": sources_attributed,
        "sha256_references": sha_refs,
        "parallel_xi": parallel_xi,
        "has_source_registry": bool(
            re.search(
                r"registro de fuentes|registro documental de fuentes|fuentes y enlaces|fuentes consultadas|fuentes relevantes|referencias [úu]tiles y procedencia",
                raw,
                re.I,
            )
            or re.search(r"(?m)^\[[SA]\d{1,3}\]:\s*https?://", raw)
        ),
    }


def validate_research_identity(raw: str, *, expected_event_id: str, expected_fixture_id: int) -> dict[str, Any]:
    """Valida identidad de informe cualitativo aceptando variantes documentadas y bloqueando conflictos."""
    parsed = parse_research_identity_and_metadata(raw)
    if parsed["event_id_conflict"]:
        raise ValueError(f"QUALITATIVE_RESEARCH_DUPLICATE_CONFLICTING_EVENT_IDS: {parsed['event_ids_found']}")
    if parsed["fixture_id_conflict"]:
        raise ValueError(f"QUALITATIVE_RESEARCH_DUPLICATE_CONFLICTING_FIXTURE_IDS: {parsed['fixture_ids_found']}")
    ev_found = parsed["event_ids_found"]
    fix_found = parsed["fixture_ids_found"]
    if str(expected_event_id) not in ev_found or str(expected_fixture_id) not in fix_found:
        raise ValueError("QUALITATIVE_RESEARCH_IDENTITY_MISMATCH")
    return parsed
