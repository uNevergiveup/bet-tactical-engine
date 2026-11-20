"""Gestión obligatoria de XI confirmados y control de elegibilidad PREMATCH (P0).

Estados canónicos obligatorios:
- LINEUPS_PENDING
- LINEUPS_API_CONFIRMED
- LINEUPS_EXTERNAL_CONFIRMED
- LINEUPS_MANUAL_VERIFIED
- LINEUPS_PROBABLE_ONLY
- LINEUPS_UNAVAILABLE_FINAL
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from bet_engine.context.research_parser import parse_research_identity_and_metadata

MANUAL_LINEUP_SCHEMA = "1.0-manual-lineup"
DEFAULT_LINEUP_CUTOFF_MINUTES = 15

_INVALID_SOURCE_VALUES = {"", "UNKNOWN", "DESCONOCIDO", "N/A", "NA", "NONE", "NULL", "ANONIMO", "ANONYMOUS", "SIN_FUENTE"}
_PRIMARY_OFFICIAL_KINDS = {"OFFICIAL_CLUB", "FEDERATION", "ORGANIZER_PRIMARY"}
_CONFIRMED_MANUAL_KINDS = {"MANUAL_VERIFIED", "QUALITATIVE_VERIFIED", *_PRIMARY_OFFICIAL_KINDS}
_EXTERNAL_CONFIRMED_KINDS = {"EXTERNAL_PROVIDER"}
_PROBABLE_KINDS = {"PROBABLE", "PRESS_PROBABLE", "ESTIMATED", "PROJECTED"}


class LineupStatus(str, Enum):
    LINEUPS_PENDING = "LINEUPS_PENDING"
    LINEUPS_API_CONFIRMED = "LINEUPS_API_CONFIRMED"
    LINEUPS_EXTERNAL_CONFIRMED = "LINEUPS_EXTERNAL_CONFIRMED"
    LINEUPS_MANUAL_VERIFIED = "LINEUPS_MANUAL_VERIFIED"
    LINEUPS_PROBABLE_ONLY = "LINEUPS_PROBABLE_ONLY"
    LINEUPS_UNAVAILABLE_FINAL = "LINEUPS_UNAVAILABLE_FINAL"


CONFIRMED_LINEUP_STATUSES: frozenset[LineupStatus] = frozenset(
    {
        LineupStatus.LINEUPS_API_CONFIRMED,
        LineupStatus.LINEUPS_EXTERNAL_CONFIRMED,
        LineupStatus.LINEUPS_MANUAL_VERIFIED,
    }
)


def is_confirmed_complete(status: str | LineupStatus | None) -> bool:
    if status is None:
        return False
    val = status.value if isinstance(status, LineupStatus) else str(status)
    return val in {s.value for s in CONFIRMED_LINEUP_STATUSES}


def _norm_text(value: Any) -> str:
    txt = unicodedata.normalize("NFKD", str(value or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9\s]", " ", txt)).strip().lower()


def _parse_tz_dt(raw: Any, label: str) -> datetime:
    if isinstance(raw, datetime):
        dt = raw
    else:
        txt = str(raw or "").strip()
        if not txt:
            raise ValueError(f"LINEUP_TIMESTAMP_MISSING:{label}")
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"LINEUP_TIMEZONE_REQUIRED:{label}")
    return dt.astimezone(timezone.utc)


def _normalize_player_entry(raw: Any, idx: int) -> dict[str, Any]:
    if isinstance(raw, str):
        name = raw.strip()
        return {"id": None, "name": name, "number": None, "pos": None, "grid": None}
    if not isinstance(raw, dict):
        raise ValueError(f"LINEUP_INVALID_PLAYER_ENTRY:idx={idx}")
    p = raw.get("player") if isinstance(raw.get("player"), dict) else raw
    name = str(p.get("name") or p.get("player_name") or "").strip()
    pid = p.get("id") if p.get("id") is not None else p.get("player_id")
    if pid is not None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"LINEUP_INVALID_PLAYER_ID:{pid}")
    return {
        "id": pid,
        "name": name,
        "number": p.get("number") if p.get("number") is not None else raw.get("number"),
        "pos": p.get("pos") or p.get("position") or raw.get("pos"),
        "grid": p.get("grid") or raw.get("grid"),
    }


def _validate_team_starters(
    starters_raw: Any,
    *,
    side: str,
    require_player_ids: bool = False,
    seen_global_ids: set[int] | None = None,
    seen_global_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(starters_raw, list):
        raise ValueError(f"LINEUP_STARTERS_MUST_BE_LIST:{side}")
    if len(starters_raw) > 11:
        raise ValueError(f"LINEUP_TOO_MANY_STARTERS:{side}:count={len(starters_raw)}")
    if len(starters_raw) != 11:
        raise ValueError(f"LINEUP_INCOMPLETE_TEAM:{side}:count={len(starters_raw)}")

    normalized: list[dict[str, Any]] = []
    local_ids: set[int] = set()
    local_names: set[str] = set()
    for idx, raw_item in enumerate(starters_raw):
        item = _normalize_player_entry(raw_item, idx)
        if not item["name"]:
            raise ValueError(f"LINEUP_INVALID_PLAYER_NAME:{side}:idx={idx}")
        if require_player_ids and item["id"] is None:
            raise ValueError(f"LINEUP_PLAYER_ID_REQUIRED:{side}:{item['name']}")
        norm_name = _norm_text(item["name"])
        if not norm_name:
            raise ValueError(f"LINEUP_INVALID_PLAYER_NAME:{side}:idx={idx}")
        if item["id"] is not None:
            if item["id"] in local_ids:
                raise ValueError(f"LINEUP_DUPLICATE_PLAYER_ID:{side}:{item['id']}")
            if seen_global_ids is not None and item["id"] in seen_global_ids:
                raise ValueError(f"LINEUP_CROSS_TEAM_DUPLICATE_ID:{item['id']}")
            local_ids.add(item["id"])
        if norm_name in local_names:
            raise ValueError(f"LINEUP_DUPLICATE_PLAYER_NAME:{side}:{item['name']}")
        if seen_global_names is not None and norm_name in seen_global_names:
            raise ValueError(f"LINEUP_CROSS_TEAM_DUPLICATE_NAME:{item['name']}")
        local_names.add(norm_name)
        normalized.append(item)

    if seen_global_ids is not None:
        seen_global_ids.update(local_ids)
    if seen_global_names is not None:
        seen_global_names.update(local_names)
    return normalized


def _validate_team_identity(
    team_block: dict[str, Any],
    *,
    side: str,
    expected_team_id: int | None,
    expected_team_name: str | None,
) -> tuple[int | None, str]:
    tid = team_block.get("team_id") if team_block.get("team_id") is not None else team_block.get("id")
    tname = str(team_block.get("team_name") or team_block.get("name") or "").strip()
    if expected_team_id is not None and tid is not None:
        if isinstance(tid, bool) or int(tid) != int(expected_team_id):
            raise ValueError(f"LINEUP_TEAM_ID_MISMATCH:{side}:got={tid}:expected={expected_team_id}")
    if expected_team_name and tname:
        en = _norm_text(expected_team_name)
        tn = _norm_text(tname)
        if en and tn and en != tn and en not in tn and tn not in en:
            # Permitir coincidencia por tokens clave (al menos un token >= 4 chars)
            e_tokens = {tok for tok in en.split() if len(tok) >= 4}
            t_tokens = {tok for tok in tn.split() if len(tok) >= 4}
            if not (e_tokens & t_tokens):
                raise ValueError(f"LINEUP_TEAM_NAME_MISMATCH:{side}:got={tname}:expected={expected_team_name}")
    if tid is None and not tname:
        raise ValueError(f"LINEUP_TEAM_IDENTITY_MISSING:{side}")
    return (int(tid) if tid is not None else expected_team_id), (tname or str(expected_team_name or ""))


def validate_manual_lineup_payload(
    payload: dict[str, Any],
    *,
    expected_fixture_id: int,
    expected_event_id: str,
    expected_home_id: int | None = None,
    expected_away_id: int | None = None,
    expected_home_name: str | None = None,
    expected_away_name: str | None = None,
    kickoff_utc: str | datetime,
    now_utc: str | datetime | None = None,
    allow_probable_only: bool = False,
) -> dict[str, Any]:
    """Valida estrictamente un archivo estructurado de XI manual o externo confirmado."""
    if not isinstance(payload, dict):
        raise ValueError("LINEUP_PAYLOAD_MUST_BE_OBJECT")

    fid = payload.get("fixture_id")
    eid = str(payload.get("event_id") or payload.get("bwin_event_id") or "").strip()
    if fid is None or isinstance(fid, bool) or int(fid) != int(expected_fixture_id):
        raise ValueError(f"LINEUP_FIXTURE_MISMATCH:got={fid}:expected={expected_fixture_id}")
    if not eid or eid != str(expected_event_id):
        raise ValueError(f"LINEUP_EVENT_MISMATCH:got={eid}:expected={expected_event_id}")

    source_name = str(payload.get("source") or payload.get("source_name") or "").strip()
    if _norm_text(source_name).upper() in _INVALID_SOURCE_VALUES or len(source_name) < 2:
        raise ValueError("LINEUP_SOURCE_IDENTITY_MISSING")

    source_url = payload.get("source_url") or payload.get("url")
    if source_url is not None:
        url_txt = str(source_url).strip()
        if url_txt and not re.match(r"^https?://\S+$", url_txt, re.IGNORECASE):
            raise ValueError(f"LINEUP_INVALID_SOURCE_URL:{url_txt}")
        source_url = url_txt or None

    captured_dt = _parse_tz_dt(
        payload.get("captured_at_utc") or payload.get("captured_at") or payload.get("capture_utc"),
        "captured_at_utc",
    )
    ko_dt = _parse_tz_dt(kickoff_utc, "kickoff_utc")
    if captured_dt >= ko_dt:
        raise ValueError(f"LINEUP_CAPTURED_AFTER_KICKOFF:{captured_dt.isoformat()}>={ko_dt.isoformat()}")
    if now_utc is not None:
        now_dt = _parse_tz_dt(now_utc, "now_utc")
        if captured_dt > now_dt:
            raise ValueError(f"LINEUP_CAPTURED_IN_FUTURE:{captured_dt.isoformat()}>{now_dt.isoformat()}")

    raw_kind = str(
        payload.get("source_kind") or payload.get("confirmation_level") or "MANUAL_VERIFIED"
    ).strip().upper()
    if raw_kind in _PROBABLE_KINDS or payload.get("confirmed") is False:
        if not allow_probable_only:
            raise ValueError("LINEUP_PROBABLE_CANNOT_BE_PROMOTED_TO_CONFIRMED")
        target_status = LineupStatus.LINEUPS_PROBABLE_ONLY
    elif raw_kind in _EXTERNAL_CONFIRMED_KINDS:
        target_status = LineupStatus.LINEUPS_EXTERNAL_CONFIRMED
    elif raw_kind in _CONFIRMED_MANUAL_KINDS:
        target_status = LineupStatus.LINEUPS_MANUAL_VERIFIED
    else:
        raise ValueError(f"LINEUP_UNSUPPORTED_SOURCE_KIND:{raw_kind}")

    teams_doc = payload.get("teams")
    if not isinstance(teams_doc, dict) or "home" not in teams_doc or "away" not in teams_doc:
        raise ValueError("LINEUP_TEAMS_HOME_AND_AWAY_REQUIRED")

    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    api_response: list[dict[str, Any]] = []
    sides_meta: dict[str, Any] = {}
    for side, exp_id, exp_name in (
        ("home", expected_home_id, expected_home_name),
        ("away", expected_away_id, expected_away_name),
    ):
        blk = teams_doc.get(side)
        if not isinstance(blk, dict):
            raise ValueError(f"LINEUP_TEAM_BLOCK_INVALID:{side}")
        tid, tname = _validate_team_identity(
            blk, side=side, expected_team_id=exp_id, expected_team_name=exp_name
        )
        starters_raw = blk.get("starters") if "starters" in blk else blk.get("startXI")
        starters = _validate_team_starters(
            starters_raw,
            side=side,
            require_player_ids=False,
            seen_global_ids=seen_ids,
            seen_global_names=seen_names,
        )
        # Nunca confundir titulares con suplentes: verificar solapamiento explícito
        subs_raw = blk.get("substitutes") or []
        if isinstance(subs_raw, list):
            starter_names_local = {_norm_text(s["name"]) for s in starters}
            starter_ids_local = {s["id"] for s in starters if s["id"] is not None}
            for s_idx, sub_item in enumerate(subs_raw):
                sub_norm = _normalize_player_entry(sub_item, s_idx) if isinstance(sub_item, (dict, str)) else None
                if not sub_norm:
                    continue
                if sub_norm["id"] is not None and sub_norm["id"] in starter_ids_local:
                    raise ValueError(f"LINEUP_STARTER_ALSO_IN_SUBSTITUTES:{side}:{sub_norm['id']}")
                if sub_norm["name"] and _norm_text(sub_norm["name"]) in starter_names_local:
                    raise ValueError(f"LINEUP_STARTER_ALSO_IN_SUBSTITUTES:{side}:{sub_norm['name']}")
        coach_blk = blk.get("coach") if isinstance(blk.get("coach"), dict) else {}
        api_response.append(
            {
                "team": {"id": tid, "name": tname},
                "formation": blk.get("formation"),
                "coach": {
                    "id": coach_blk.get("id") or blk.get("coach_id"),
                    "name": coach_blk.get("name") or blk.get("coach_name"),
                },
                "startXI": [
                    {
                        "player": {
                            "id": s["id"],
                            "name": s["name"],
                            "number": s["number"],
                            "pos": s["pos"],
                            "grid": s["grid"],
                        }
                    }
                    for s in starters
                ],
                "substitutes": list(blk.get("substitutes") or []),
            }
        )
        sides_meta[side] = {
            "team_id": tid,
            "team_name": tname,
            "starters_count": len(starters),
            "starters": starters,
        }

    uncertainties = [str(u).strip() for u in (payload.get("uncertainties") or []) if str(u).strip()]
    screenshot_refs_raw = payload.get("screenshots") or []
    screenshot_refs: list[dict[str, Any]] = []
    if isinstance(screenshot_refs_raw, list):
        for item in screenshot_refs_raw:
            if isinstance(item, str) and item.strip():
                screenshot_refs.append({"path": item.strip()})
            elif isinstance(item, dict) and item.get("path"):
                screenshot_refs.append(dict(item))

    return {
        "schema_version": str(payload.get("schema_version") or MANUAL_LINEUP_SCHEMA),
        "fixture_id": int(expected_fixture_id),
        "event_id": str(expected_event_id),
        "lineup_status": target_status.value,
        "confirmed_complete_22": is_confirmed_complete(target_status),
        "source": source_name,
        "source_url": source_url,
        "source_kind": raw_kind,
        "captured_at_utc": captured_dt.isoformat(),
        "kickoff_utc": ko_dt.isoformat(),
        "uncertainties": uncertainties,
        "screenshots": screenshot_refs,
        "interpretation_mode": str(payload.get("interpretation_mode") or "CODEX_ASSISTED_OR_STRUCTURED_MANUAL"),
        "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
        "validation_scope": "JSON_SCHEMA_IDENTITY_AND_TEMPORAL_CHECKS_ONLY",
        "real_source_verification": "DECLARED_PROVENANCE_NOT_EXTERNALLY_ATTESTED",
        "source_verification_limits": [
            "Validado offline: esquema, fixture/evento, equipos, 11+11 sin duplicados ni solapamiento con suplentes, URL HTTP(S) sintáctica y captura < kickoff.",
            "NO verificado externamente: autenticidad real de la publicación del club/federación ni consulta online a source_url.",
        ],
        "sides": sides_meta,
        "api_like_payload": {
            "get": "fixtures/lineups",
            "parameters": {"fixture": str(expected_fixture_id)},
            "errors": [],
            "results": 2,
            "response": api_response,
            "provenance": {
                "lineup_status": target_status.value,
                "source": source_name,
                "source_url": source_url,
                "source_kind": raw_kind,
                "captured_at_utc": captured_dt.isoformat(),
            },
        },
    }


def load_and_validate_manual_lineup_file(
    source_path: Path,
    *,
    expected_fixture_id: int,
    expected_event_id: str,
    expected_home_id: int | None = None,
    expected_away_id: int | None = None,
    expected_home_name: str | None = None,
    expected_away_name: str | None = None,
    kickoff_utc: str | datetime,
    now_utc: str | datetime | None = None,
    allow_probable_only: bool = False,
) -> dict[str, Any]:
    path = Path(source_path)
    if not path.is_file():
        raise ValueError(f"LINEUP_MANUAL_FILE_NOT_FOUND:{path}")
    raw_bytes = path.read_bytes()
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    validated = validate_manual_lineup_payload(
        doc,
        expected_fixture_id=expected_fixture_id,
        expected_event_id=expected_event_id,
        expected_home_id=expected_home_id,
        expected_away_id=expected_away_id,
        expected_home_name=expected_home_name,
        expected_away_name=expected_away_name,
        kickoff_utc=kickoff_utc,
        now_utc=now_utc,
        allow_probable_only=allow_probable_only,
    )
    validated["source_file_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    validated["source_file_path"] = str(path.resolve())
    return validated


def extract_fixture_team_identity(
    context_doc: dict[str, Any] | None = None,
    *,
    case_doc: dict[str, Any] | None = None,
    identity_doc: dict[str, Any] | None = None,
    selection_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extrae identidad canónica (IDs prioritarios, nombres y alias) desde todos los esquemas del pipeline."""
    ctx_doc = context_doc if isinstance(context_doc, dict) else {}
    fx = ctx_doc.get("fixture") if isinstance(ctx_doc.get("fixture"), dict) else {}
    mm = ctx_doc.get("match") if isinstance(ctx_doc.get("match"), dict) else {}
    ctx_teams = ((ctx_doc.get("context") or {}).get("teams") or {}) if isinstance(ctx_doc.get("context"), dict) else {}
    case_d = case_doc if isinstance(case_doc, dict) else {}
    ident_d = identity_doc if isinstance(identity_doc, dict) else {}
    sel_d = selection_doc if isinstance(selection_doc, dict) else {}

    def _int_or_none(val: Any) -> int | None:
        if isinstance(val, bool) or val is None:
            return None
        try:
            ival = int(val)
            return ival if ival > 0 else None
        except (TypeError, ValueError):
            return None

    home_fx_team = fx.get("home_team") if isinstance(fx.get("home_team"), dict) else {}
    away_fx_team = fx.get("away_team") if isinstance(fx.get("away_team"), dict) else {}
    home_ctx_team = ctx_teams.get("home") if isinstance(ctx_teams.get("home"), dict) else {}
    away_ctx_team = ctx_teams.get("away") if isinstance(ctx_teams.get("away"), dict) else {}
    home_ident = ident_d.get("home") if isinstance(ident_d.get("home"), dict) else {}
    away_ident = ident_d.get("away") if isinstance(ident_d.get("away"), dict) else {}
    home_case = case_d.get("home") if isinstance(case_d.get("home"), dict) else {}
    away_case = case_d.get("away") if isinstance(case_d.get("away"), dict) else {}

    home_id = next(
        (
            cand
            for cand in (
                _int_or_none(fx.get("home_id")),
                _int_or_none(home_fx_team.get("team_id")),
                _int_or_none(home_fx_team.get("id")),
                _int_or_none(home_ctx_team.get("id")),
                _int_or_none(home_ident.get("api_team_id")),
                _int_or_none(home_case.get("api_team_id")),
                _int_or_none(sel_d.get("home_api_id")),
            )
            if cand is not None
        ),
        None,
    )
    away_id = next(
        (
            cand
            for cand in (
                _int_or_none(fx.get("away_id")),
                _int_or_none(away_fx_team.get("team_id")),
                _int_or_none(away_fx_team.get("id")),
                _int_or_none(away_ctx_team.get("id")),
                _int_or_none(away_ident.get("api_team_id")),
                _int_or_none(away_case.get("api_team_id")),
                _int_or_none(sel_d.get("away_api_id")),
            )
            if cand is not None
        ),
        None,
    )

    home_candidates = [
        str(v).strip()
        for v in (
            fx.get("home_name"),
            home_fx_team.get("name"),
            home_ctx_team.get("name"),
            home_ident.get("api_name"),
            home_case.get("name"),
            sel_d.get("home"),
            home_ident.get("bwin_name"),
            mm.get("home_name"),
            mm.get("home_name_raw"),
        )
        if v and str(v).strip()
    ]
    away_candidates = [
        str(v).strip()
        for v in (
            fx.get("away_name"),
            away_fx_team.get("name"),
            away_ctx_team.get("name"),
            away_ident.get("api_name"),
            away_case.get("name"),
            sel_d.get("away"),
            away_ident.get("bwin_name"),
            mm.get("away_name"),
            mm.get("away_name_raw"),
        )
        if v and str(v).strip()
    ]

    fixture_id = next(
        (
            cand
            for cand in (
                _int_or_none(fx.get("fixture_id")),
                _int_or_none(mm.get("verified_fixture_id")),
                _int_or_none(ident_d.get("api_fixture_id")),
                _int_or_none(case_d.get("fixture_id")),
                _int_or_none(sel_d.get("fixture_id")),
            )
            if cand is not None
        ),
        0,
    )
    event_id = next(
        (
            str(v).strip()
            for v in (
                case_d.get("event_id"),
                mm.get("event_id"),
                ident_d.get("bwin_event_id"),
                sel_d.get("bwin_event_id"),
            )
            if v is not None and str(v).strip()
        ),
        "",
    )
    kickoff_utc = next(
        (
            v
            for v in (
                fx.get("date"),
                ident_d.get("kickoff_utc"),
                case_d.get("kickoff_madrid"),
                sel_d.get("kickoff_madrid"),
            )
            if v
        ),
        None,
    )
    is_national_team = bool(home_ctx_team.get("national") or away_ctx_team.get("national"))
    return {
        "fixture_id": int(fixture_id),
        "event_id": event_id,
        "home_id": home_id,
        "away_id": away_id,
        "home_name": home_candidates[0] if home_candidates else "",
        "away_name": away_candidates[0] if away_candidates else "",
        "home_aliases": list(dict.fromkeys(home_candidates)),
        "away_aliases": list(dict.fromkeys(away_candidates)),
        "kickoff_utc": kickoff_utc,
        "is_national_team": is_national_team,
    }


def _validate_api_response_blocks(
    response: Any,
    *,
    home_id: int | None,
    away_id: int | None,
    home_name: str | None = None,
    away_name: str | None = None,
    home_aliases: list[str] | None = None,
    away_aliases: list[str] | None = None,
    require_player_ids: bool = True,
) -> dict[str, Any]:
    if not isinstance(response, list) or not response:
        return {
            "home_valid_11": False,
            "away_valid_11": False,
            "both_valid_22": False,
            "errors": [],
            "counts": {"home": 0, "away": 0},
        }
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    counts = {"home": 0, "away": 0}
    valid = {"home": False, "away": False}
    errors: list[str] = []
    seen_sides: set[str] = set()

    home_norm_set = {_norm_text(n) for n in ([home_name, *(home_aliases or [])]) if n and _norm_text(n)}
    away_norm_set = {_norm_text(n) for n in ([away_name, *(away_aliases or [])]) if n and _norm_text(n)}

    for blk in response:
        if not isinstance(blk, dict):
            errors.append("API_LINEUP_BLOCK_NOT_OBJECT")
            continue
        t_obj = blk.get("team") or {}
        tid = t_obj.get("id")
        tname = str(t_obj.get("name") or "").strip()
        tnorm = _norm_text(tname)
        side = None
        # Prioridad 1: coincidencia exacta por Team ID numérico
        if home_id is not None and not isinstance(tid, bool) and tid is not None and int(tid) == int(home_id):
            side = "home"
        elif away_id is not None and not isinstance(tid, bool) and tid is not None and int(tid) == int(away_id):
            side = "away"
        # Prioridad 2: nombre/alias solo si no hay IDs o no contradice los IDs esperados
        elif home_norm_set and tnorm in home_norm_set and (home_id is None or tid is None):
            side = "home"
        elif away_norm_set and tnorm in away_norm_set and (away_id is None or tid is None):
            side = "away"
        elif home_id is None and away_id is None and tname:
            # Fallback controlado vía match_identity si existe equivalente lingüístico seguro
            try:
                from match_identity import compare_report_name

                if home_name and compare_report_name(tname, {"api_name": home_name, "bwin_name": (home_aliases or [None])[-1]}, {"api_name": away_name}) in {"EXACT", "ALIAS"}:
                    side = "home"
                elif away_name and compare_report_name(tname, {"api_name": away_name, "bwin_name": (away_aliases or [None])[-1]}, {"api_name": home_name}) in {"EXACT", "ALIAS"}:
                    side = "away"
            except Exception:
                pass
        if side is None:
            errors.append(f"API_LINEUP_UNEXPECTED_TEAM:id={tid}:name={tname}")
            continue
        if side in seen_sides:
            errors.append(f"API_LINEUP_DUPLICATE_TEAM_BLOCK:{side}")
            valid[side] = False
            continue
        seen_sides.add(side)
        starters_raw = blk.get("startXI") or []
        counts[side] = len(starters_raw) if isinstance(starters_raw, list) else 0
        try:
            _validate_team_starters(
                starters_raw,
                side=side,
                require_player_ids=require_player_ids,
                seen_global_ids=seen_ids,
                seen_global_names=seen_names,
            )
            valid[side] = True
        except ValueError as exc:
            errors.append(str(exc))
            valid[side] = False

    return {
        "home_valid_11": valid["home"],
        "away_valid_11": valid["away"],
        "both_valid_22": bool(valid["home"] and valid["away"] and not errors),
        "errors": errors,
        "counts": counts,
    }


def extract_confirmed_lineup_from_research(
    markdown_text: str,
    *,
    expected_fixture_id: int,
    expected_event_id: str,
    home_name: str,
    away_name: str,
    home_id: int | None,
    away_id: int | None,
    kickoff_utc: str | datetime,
    now_utc: str | datetime | None = None,
) -> dict[str, Any] | None:
    """Extrae XI CONFIRMADO desde investigación cualitativa SOLO si tiene procedencia verificable.

    Nunca promueve secciones PROBABLE a confirmadas.
    """
    if not markdown_text:
        return None
    # Busca sección explícita de XI CONFIRMADO (OFICIAL) con metadata de procedencia
    sec_match = re.search(
        r"(?ims)^#{1,4}\s*.*?\bXI\s+CONFIRMADO(?:\s+OFICIAL)?\b.*?(?=^#{1,4}\s|\Z)",
        markdown_text,
    )
    if not sec_match:
        return None
    section = sec_match.group(0)
    if re.search(r"\bPROBABLE\b", section.splitlines()[0], re.IGNORECASE):
        return None

    src_m = re.search(r"(?im)^\s*[-*]?\s*\*{0,2}(?:Fuente|Source)\*{0,2}\s*:\s*(.+)$", section)
    cap_m = re.search(
        r"(?im)^\s*[-*]?\s*\*{0,2}(?:Captura|Captured(?:\s+UTC)?|Hora\s+de\s+captura)\*{0,2}\s*:\s*(.+)$",
        section,
    )
    url_m = re.search(r"(?im)^\s*[-*]?\s*\*{0,2}URL\*{0,2}\s*:\s*(\S+)", section)
    if not src_m or not cap_m:
        return None

    # Extrae tabla Markdown paralela de titulares dentro de la sección confirmada
    meta = parse_research_identity_and_metadata(section, home=home_name, away=away_name)
    par = meta.get("parallel_xi") or {}
    home_blk = par.get("local") or {}
    away_blk = par.get("visitante") or {}
    home_names = list(home_blk.get("players") or [])
    away_names = list(away_blk.get("players") or [])
    if len(home_names) != 11 or len(away_names) != 11:
        return None
    if home_blk.get("classification") == "PROBABLE" or away_blk.get("classification") == "PROBABLE":
        return None

    cap_raw = re.sub(r"\s*\([^)]*\)\s*$", "", cap_m.group(1).strip())
    payload = {
        "schema_version": MANUAL_LINEUP_SCHEMA,
        "fixture_id": int(expected_fixture_id),
        "event_id": str(expected_event_id),
        "source": src_m.group(1).strip(),
        "source_url": url_m.group(1).strip() if url_m else None,
        "source_kind": "QUALITATIVE_VERIFIED",
        "captured_at_utc": cap_raw,
        "teams": {
            "home": {
                "team_id": home_id,
                "team_name": home_name,
                "starters": [{"name": n} for n in home_names],
            },
            "away": {
                "team_id": away_id,
                "team_name": away_name,
                "starters": [{"name": n} for n in away_names],
            },
        },
    }
    try:
        return validate_manual_lineup_payload(
            payload,
            expected_fixture_id=expected_fixture_id,
            expected_event_id=expected_event_id,
            expected_home_id=home_id,
            expected_away_id=away_id,
            expected_home_name=home_name,
            expected_away_name=away_name,
            kickoff_utc=kickoff_utc,
            now_utc=now_utc,
            allow_probable_only=False,
        )
    except ValueError:
        return None


def evaluate_fixture_lineup_state(
    *,
    api_lineups_response: Any = None,
    manual_lineup: dict[str, Any] | None = None,
    qualitative_markdown: str | None = None,
    qualitative_probable_counts: dict[str, int] | None = None,
    fixture_id: int,
    event_id: str,
    home_id: int | None,
    away_id: int | None,
    home_name: str,
    away_name: str,
    kickoff_utc: str | datetime,
    now_utc: str | datetime | None = None,
    lineup_cutoff_minutes: int = DEFAULT_LINEUP_CUTOFF_MINUTES,
    user_manual_absent: bool = False,
    home_aliases: list[str] | None = None,
    away_aliases: list[str] | None = None,
    is_national_team: bool | None = None,
    require_primary_official: bool = False,
    api_acquired_at_utc: str | datetime | None = None,
    last_verified_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Determina el estado canónico (uno de los 6 estados P0) y la elegibilidad PREMATCH."""
    ko_dt = _parse_tz_dt(kickoff_utc, "kickoff_utc")
    now_dt = _parse_tz_dt(now_utc, "now_utc") if now_utc is not None else datetime.now(timezone.utc)
    minutes_to_ko = round((ko_dt - now_dt).total_seconds() / 60.0, 2)
    cutoff_reached = minutes_to_ko <= float(lineup_cutoff_minutes)

    api_check = _validate_api_response_blocks(
        api_lineups_response,
        home_id=home_id,
        away_id=away_id,
        home_name=home_name,
        away_name=away_name,
        home_aliases=home_aliases,
        away_aliases=away_aliases,
        require_player_ids=True,
    )

    acq_age_min = None
    acq_iso = None
    if api_acquired_at_utc is not None:
        try:
            acq_dt = _parse_tz_dt(api_acquired_at_utc, "api_acquired_at_utc")
            acq_iso = acq_dt.isoformat()
            acq_age_min = round((now_dt - acq_dt).total_seconds() / 60.0, 2)
        except ValueError:
            acq_iso = None

    if api_check["both_valid_22"]:
        verif_label = (
            "API_FOOTBALL_PROVIDER_RECORD_NOT_PRIMARY_CERTIFIED"
            if is_national_team
            else "API_FOOTBALL_PROVIDER_RECORD_NOT_CLUB_CERTIFIED"
        )
        eligible_pick = not bool(require_primary_official)
        reason = (
            "PRIMARY_OFFICIAL_SOURCE_REQUIRED_NOT_ARCHIVED"
            if require_primary_official
            else "API_FOOTBALL_22_STARTERS_VERIFIED"
        )
        return {
            "status": LineupStatus.LINEUPS_API_CONFIRMED.value,
            "lineup_structural_status": "COMPLETE_22_STRUCTURED",
            "confirmed_complete_22": True,
            "provider_confirmed": True,
            "primary_official_verified": False,
            "official_source_tier": "STRUCTURED_PROVIDER",
            "require_primary_official": bool(require_primary_official),
            "eligible_for_actionable_pick": eligible_pick,
            "eligible_for_final_analysis": True,
            "confirmed_sides": {"home": True, "away": True},
            "starter_counts": api_check["counts"],
            "source_origin": "API_FOOTBALL",
            "acquired_at_utc": acq_iso,
            "age_minutes": acq_age_min,
            "validation_scope": "API_RESPONSE_STRUCTURE_AND_IDENTITY_CHECKS",
            "real_source_verification": verif_label,
            "lineup_cutoff_minutes": int(lineup_cutoff_minutes),
            "minutes_to_kickoff": minutes_to_ko,
            "cutoff_reached": cutoff_reached,
            "validation_errors": [],
            "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
            "reason": reason,
        }

    if manual_lineup and is_confirmed_complete(manual_lineup.get("lineup_status")):
        status_val = str(manual_lineup["lineup_status"])
        src_kind = str(manual_lineup.get("source_kind") or "").strip().upper()
        primary_archived = bool(
            src_kind in _PRIMARY_OFFICIAL_KINDS
            and manual_lineup.get("primary_evidence_verified") is True
        )
        tier = (
            "PRIMARY_OFFICIAL"
            if primary_archived
            else ("STRUCTURED_PROVIDER" if status_val == LineupStatus.LINEUPS_EXTERNAL_CONFIRMED.value else "MANUAL_VERIFIED")
        )
        eligible_pick = (not bool(require_primary_official)) or primary_archived
        reason = (
            "PRIMARY_OFFICIAL_SOURCE_REQUIRED_NOT_ARCHIVED"
            if (require_primary_official and not primary_archived)
            else f"{status_val}_22_STARTERS_VERIFIED"
        )
        return {
            "status": status_val,
            "lineup_structural_status": "COMPLETE_22_STRUCTURED",
            "confirmed_complete_22": True,
            "provider_confirmed": status_val == LineupStatus.LINEUPS_EXTERNAL_CONFIRMED.value,
            "primary_official_verified": primary_archived,
            "official_source_tier": tier,
            "require_primary_official": bool(require_primary_official),
            "eligible_for_actionable_pick": eligible_pick,
            "eligible_for_final_analysis": True,
            "confirmed_sides": {"home": True, "away": True},
            "starter_counts": {
                "home": (manual_lineup.get("sides") or {}).get("home", {}).get("starters_count", 11),
                "away": (manual_lineup.get("sides") or {}).get("away", {}).get("starters_count", 11),
            },
            "source_origin": manual_lineup.get("source"),
            "source_url": manual_lineup.get("source_url"),
            "captured_at_utc": manual_lineup.get("captured_at_utc"),
            "validation_scope": manual_lineup.get("validation_scope") or "JSON_SCHEMA_IDENTITY_AND_TEMPORAL_CHECKS_ONLY",
            "real_source_verification": manual_lineup.get("real_source_verification") or "DECLARED_PROVENANCE_NOT_EXTERNALLY_ATTESTED",
            "lineup_cutoff_minutes": int(lineup_cutoff_minutes),
            "minutes_to_kickoff": minutes_to_ko,
            "cutoff_reached": cutoff_reached,
            "validation_errors": [],
            "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
            "reason": reason,
        }

    # Procedencia cualitativa confirmada verificable (Req 6)
    if qualitative_markdown:
        qual_confirmed = extract_confirmed_lineup_from_research(
            qualitative_markdown,
            expected_fixture_id=fixture_id,
            expected_event_id=event_id,
            home_name=home_name,
            away_name=away_name,
            home_id=home_id,
            away_id=away_id,
            kickoff_utc=ko_dt,
            now_utc=now_dt,
        )
        if qual_confirmed is not None:
            status_val = qual_confirmed["lineup_status"]
            eligible_pick = not bool(require_primary_official)
            return {
                "status": status_val,
                "lineup_structural_status": "COMPLETE_22_STRUCTURED",
                "confirmed_complete_22": True,
                "provider_confirmed": status_val == LineupStatus.LINEUPS_EXTERNAL_CONFIRMED.value,
                "primary_official_verified": False,
                "official_source_tier": "MANUAL_VERIFIED",
                "require_primary_official": bool(require_primary_official),
                "eligible_for_actionable_pick": eligible_pick,
                "eligible_for_final_analysis": True,
                "confirmed_sides": {"home": True, "away": True},
                "starter_counts": {"home": 11, "away": 11},
                "source_origin": qual_confirmed["source"],
                "source_url": qual_confirmed.get("source_url"),
                "captured_at_utc": qual_confirmed.get("captured_at_utc"),
                "lineup_cutoff_minutes": int(lineup_cutoff_minutes),
                "minutes_to_kickoff": minutes_to_ko,
                "cutoff_reached": cutoff_reached,
                "validation_errors": [],
                "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
                "reason": "QUALITATIVE_CONFIRMED_WITH_VERIFIABLE_PROVENANCE" if eligible_pick else "PRIMARY_OFFICIAL_SOURCE_REQUIRED_NOT_ARCHIVED",
                "extracted_manual_payload": qual_confirmed,
            }

    # No existen 22 titulares confirmados: detectar si solo hay probables, parcial o pendiente/final no disponible
    prob_counts = dict(qualitative_probable_counts or {})
    if not prob_counts and qualitative_markdown:
        rmeta = parse_research_identity_and_metadata(qualitative_markdown, home=home_name, away=away_name)
        par = rmeta.get("parallel_xi") or {}
        loc_blk = par.get("local") or {}
        vis_blk = par.get("visitante") or {}
        if loc_blk.get("classification") == "PROBABLE" or vis_blk.get("classification") == "PROBABLE":
            prob_counts = {
                "home": len(loc_blk.get("players") or []),
                "away": len(vis_blk.get("players") or []),
            }

    partial_api = bool(api_check["home_valid_11"] != api_check["away_valid_11"])
    has_probables = (prob_counts.get("home", 0) > 0 or prob_counts.get("away", 0) > 0) or (
        manual_lineup is not None and manual_lineup.get("lineup_status") == LineupStatus.LINEUPS_PROBABLE_ONLY.value
    )

    if cutoff_reached or user_manual_absent:
        status_enum = LineupStatus.LINEUPS_UNAVAILABLE_FINAL
        struct_status = "UNAVAILABLE_AT_CUTOFF"
        reason = "MISSING_CONFIRMED_22_STARTERS_AT_FINAL_CUTOFF"
    elif partial_api:
        status_enum = LineupStatus.LINEUPS_PENDING
        struct_status = "PARTIAL_STRUCTURED"
        missing_side = "away" if api_check["home_valid_11"] else "home"
        reason = f"PARTIAL_LINEUP_ONLY_ONE_TEAM_CONFIRMED:missing={missing_side}"
    elif has_probables:
        status_enum = LineupStatus.LINEUPS_PROBABLE_ONLY
        struct_status = "PROBABLE_ONLY"
        reason = "ONLY_PROBABLE_LINEUPS_AVAILABLE_NOT_ACTIONABLE"
    else:
        status_enum = LineupStatus.LINEUPS_PENDING
        struct_status = "PENDING"
        reason = "AWAITING_CONFIRMED_22_STARTERS"

    preserved_prior = None
    if isinstance(last_verified_state, dict) and last_verified_state.get("confirmed_complete_22"):
        prior_ts = last_verified_state.get("acquired_at_utc") or last_verified_state.get("captured_at_utc")
        prior_age = None
        if prior_ts:
            try:
                prior_age = round((now_dt - _parse_tz_dt(prior_ts, "last_verified_at")).total_seconds() / 60.0, 2)
            except ValueError:
                prior_age = None
        preserved_prior = {
            "status": last_verified_state.get("status"),
            "source_origin": last_verified_state.get("source_origin"),
            "acquired_at_utc": prior_ts,
            "age_minutes": prior_age,
            "stale": bool(prior_age is not None and prior_age > 60.0),
        }

    out_doc = {
        "status": status_enum.value,
        "lineup_structural_status": struct_status,
        "confirmed_complete_22": False,
        "provider_confirmed": False,
        "primary_official_verified": False,
        "official_source_tier": "SECONDARY_OR_PROBABLE" if has_probables else "NONE",
        "require_primary_official": bool(require_primary_official),
        "eligible_for_actionable_pick": False,
        "eligible_for_final_analysis": False,
        "confirmed_sides": {"home": api_check["home_valid_11"], "away": api_check["away_valid_11"]},
        "partial_team_confirmed": partial_api,
        "starter_counts": api_check["counts"],
        "probable_counts": prob_counts,
        "source_origin": None,
        "validation_scope": "NO_COMPLETE_CONFIRMED_XI",
        "real_source_verification": "UNVERIFIED",
        "lineup_cutoff_minutes": int(lineup_cutoff_minutes),
        "minutes_to_kickoff": minutes_to_ko,
        "cutoff_reached": cutoff_reached,
        "validation_errors": list(api_check["errors"]),
        "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
        "reason": reason,
    }
    if preserved_prior is not None:
        out_doc["last_verified_lineup"] = preserved_prior
    return out_doc


def compare_probable_vs_confirmed_absences(
    probable_names: list[str],
    confirmed_names: list[str],
) -> list[dict[str, Any]]:
    """Identifica jugadores ausentes del XI sin asumir lesión ni sanción."""
    conf_norm = {_norm_text(n) for n in confirmed_names if _norm_text(n)}
    omitted: list[dict[str, Any]] = []
    for name in probable_names:
        if _norm_text(name) and _norm_text(name) not in conf_norm:
            omitted.append(
                {
                    "player_name": name,
                    "status": "ABSENT_FROM_STARTING_XI_REASON_UNVERIFIED",
                    "inferred_injury": False,
                    "inferred_suspension": False,
                    "interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
                }
            )
    return omitted


def select_next_eligible_ranked_candidate(
    ranked_rows: list[dict[str, Any]],
    *,
    excluded_event_ids: set[str] | list[str] = (),
    excluded_fixture_ids: set[int] | list[int] = (),
    now_utc: str | datetime,
    min_lead_minutes: int = 45,
) -> dict[str, Any]:
    """Reconsidera automáticamente el siguiente candidato elegible tras descarte por XI."""
    now_dt = _parse_tz_dt(now_utc, "now_utc")
    excl_events = {str(e) for e in excluded_event_ids if str(e)}
    excl_fixtures = {int(f) for f in excluded_fixture_ids if f is not None}
    skipped_audit: list[dict[str, Any]] = []

    for row in ranked_rows:
        fid = row.get("fixture_id")
        eid = str(row.get("bwin_event_id") or row.get("event_id") or "").strip()
        if (fid is not None and int(fid) in excl_fixtures) or (eid and eid in excl_events):
            skipped_audit.append({"fixture_id": fid, "event_id": eid or None, "reason": "ALREADY_DISCARDED_OR_EXCLUDED"})
            continue
        if row.get("hard_block_reasons") or row.get("eligible") is False or row.get("research_eligible") is False:
            skipped_audit.append({"fixture_id": fid, "event_id": eid or None, "reason": "NOT_ELIGIBLE_IN_RANKING"})
            continue
        ko_raw = row.get("kickoff_madrid") or row.get("kickoff_utc")
        if not ko_raw:
            skipped_audit.append({"fixture_id": fid, "event_id": eid or None, "reason": "MISSING_KICKOFF"})
            continue
        ko_dt = _parse_tz_dt(ko_raw, "candidate_kickoff")
        lead_min = round((ko_dt - now_dt).total_seconds() / 60.0, 2)
        if lead_min < float(min_lead_minutes):
            skipped_audit.append(
                {
                    "fixture_id": fid,
                    "event_id": eid or None,
                    "lead_minutes": lead_min,
                    "reason": f"INSUFFICIENT_REMAINING_TIME({lead_min}m<{min_lead_minutes}m)",
                }
            )
            continue
        return {
            "status": "NEXT_CANDIDATE_SELECTED",
            "selected_candidate": dict(row),
            "selected_fixture_id": fid,
            "selected_event_id": eid or None,
            "lead_minutes": lead_min,
            "skipped": skipped_audit,
        }

    return {
        "status": "NO_ELIGIBLE_NEXT_CANDIDATE",
        "selected_candidate": None,
        "selected_fixture_id": None,
        "selected_event_id": None,
        "skipped": skipped_audit,
    }


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def prepare_manual_lineup_inbox(
    event_dir: Path,
    *,
    event_id: str,
    fixture_id: int,
    home_name: str,
    away_name: str,
    home_id: int | None,
    away_id: int | None,
    kickoff_iso: str,
) -> dict[str, str]:
    """Crea la bandeja `lineups/manual/` con plantilla JSON y contrato visual para Codex (sin OCR obligatorio)."""
    manual_dir = Path(event_dir) / "lineups" / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    template_path = manual_dir / f"manual_lineup_template_{event_id}.json"
    prompt_path = manual_dir / f"codex_manual_xi_prompt_{event_id}.md"

    if not template_path.is_file():
        template_doc = {
            "schema_version": MANUAL_LINEUP_SCHEMA,
            "fixture_id": int(fixture_id),
            "event_id": str(event_id),
            "source": "COMPLETAR_FUENTE_REAL_EJ_Sofascore_Club_Oficial",
            "source_url": None,
            "source_kind": "OFFICIAL_CLUB",
            "confirmed": False,
            "captured_at_utc": "YYYY-MM-DDTHH:MM:SS+00:00",
            "interpretation_mode": "CODEX_VISUAL_ASSISTED",
            "screenshots": [],
            "uncertainties": [],
            "teams": {
                "home": {
                    "team_id": home_id,
                    "team_name": home_name,
                    "formation": None,
                    "starters": [],
                    "substitutes": [],
                },
                "away": {
                    "team_id": away_id,
                    "team_name": away_name,
                    "formation": None,
                    "starters": [],
                    "substitutes": [],
                },
            },
        }
        template_path.write_text(json.dumps(template_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not prompt_path.is_file():
        prompt_text = "\n".join(
            [
                f"# Instrucción Codex — XI Manuales Visuales · Evento {event_id} (Fixture {fixture_id})",
                "",
                f"- Partido: **{home_name}** vs **{away_name}**",
                f"- Kickoff: `{kickoff_iso}`",
                f"- Carpeta de capturas originales: `{manual_dir.resolve()}`",
                f"- JSON de salida esperado: `{(manual_dir / f'manual_lineup_{event_id}.json').resolve()}`",
                "",
                "## Reglas obligatorias (NO usar OCR ciego ni inventar datos)",
                "1. Interpreta visualmente las capturas adjuntas o depositadas en la carpeta `lineups/manual/`.",
                "2. Separa estrictamente los **11 titulares (`starters`)** de los **suplentes (`substitutes`)**.",
                "3. Comprueba si la captura indica XI **CONFIRMADO / OFICIAL** o **PROBABLE / PROYECTADO**:",
                "   - Si es CONFIRMADO y anterior al kickoff: `\"confirmed\": true`, `\"source_kind\": \"OFFICIAL_CLUB\"` (o `\"EXTERNAL_PROVIDER\"` / `\"FEDERATION\"` / `\"MANUAL_VERIFIED\"`).",
                "   - Si es PROBABLE, incompleto o dudoso: `\"confirmed\": false`, `\"source_kind\": \"PROBABLE\"` y anota la causa en `uncertainties`.",
                "4. Registra `source`, `source_url` (si consta), `captured_at_utc` con zona horaria y los archivos originales en `screenshots`.",
                "5. Nunca inventes jugadores, dorsales ni IDs que no aparezcan en las imágenes.",
                "",
                "## Siguiente comando tras generar el JSON",
                "```bash",
                f".venv/bin/python prematch_case.py sync-manual-lineups --event-id {event_id}",
                "```",
                "",
            ]
        )
        prompt_path.write_text(prompt_text, encoding="utf-8")

    return {
        "manual_dir": str(manual_dir.resolve()),
        "template_path": str(template_path.resolve()),
        "prompt_path": str(prompt_path.resolve()),
        "expected_json_path": str((manual_dir / f"manual_lineup_{event_id}.json").resolve()),
    }


def scan_manual_lineup_inbox(event_dir: Path, *, event_id: str) -> dict[str, Any]:
    """Inspecciona `outputs/matches/<EVENT_ID>/lineups/manual/` preservando capturas originales."""
    manual_dir = Path(event_dir) / "lineups" / "manual"
    if not manual_dir.is_dir():
        return {"present": False, "manual_dir": str(manual_dir), "screenshots": [], "json_candidates": []}

    screenshots: list[dict[str, Any]] = []
    json_candidates: list[str] = []
    for p in sorted(manual_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() in _IMAGE_EXTS:
            raw = p.read_bytes()
            screenshots.append(
                {
                    "path": str(p.resolve()),
                    "name": p.name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                }
            )
        elif p.suffix.lower() == ".json" and "template" not in p.name.lower() and not p.name.startswith("."):
            if p.name == f"manual_lineup_{event_id}.json" or p.name.startswith("manual_lineup"):
                json_candidates.append(str(p.resolve()))
    # Priorizar manual_lineup_<event_id>.json sobre cualquier otro nombre
    preferred = str((manual_dir / f"manual_lineup_{event_id}.json").resolve())
    if preferred in json_candidates:
        json_candidates.remove(preferred)
        json_candidates.insert(0, preferred)
    return {
        "present": True,
        "manual_dir": str(manual_dir.resolve()),
        "screenshots": screenshots,
        "json_candidates": json_candidates,
    }
