#!/usr/bin/env python3
"""Gestión auditable de URLs Bwin manuales y cola de reconciliación (sin saltar controles PREMATCH)."""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import bwin_discovery as bd
import prematch_prospective_trial as trial

MANUAL_LINKS_SCHEMA = "1.0-manual-bwin-links"
QUEUE_SCHEMA = "1.0-bwin-reconciliation-queue"
MANUAL_BWIN_MIN_LEAD_MINUTES = 5
PREPARE_MIN_LEAD_MINUTES = 45
MARKDOWN_URL_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")

CANONICAL_BWIN_STATES = frozenset({
    "BWIN_VERIFIED",
    "BWIN_NOT_SCANNED_BUDGET",
    "BWIN_SEARCH_INCOMPLETE",
    "BWIN_NOT_FOUND_IN_SCANNED_PAGES",
    "BWIN_IDENTITY_CONFLICT",
    "BWIN_MANUAL_LINK_PENDING_VALIDATION",
    "BWIN_MANUAL_LINK_VERIFIED",
})


class ManualBwinLinkError(ValueError):
    """Error de validación o identidad en vinculación manual de URL Bwin."""


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_manual_bwin_spec(spec: str) -> tuple[int, str]:
    """Parsea '<FIXTURE_ID>=<BWIN_URL>' de forma estricta."""
    if "=" not in str(spec or ""):
        raise ManualBwinLinkError(f"MANUAL_BWIN_SPEC_INVALID_FORMAT:{spec!r} (esperado <FIXTURE_ID>=<URL>)")
    fid_raw, url_raw = str(spec).split("=", 1)
    fid_raw = fid_raw.strip()
    url = url_raw.strip()
    if not fid_raw.isdigit() or int(fid_raw) <= 0:
        raise ManualBwinLinkError(f"MANUAL_BWIN_INVALID_FIXTURE_ID:{fid_raw!r}")
    if not url:
        raise ManualBwinLinkError(f"MANUAL_BWIN_EMPTY_URL:fixture_id={fid_raw}")
    return int(fid_raw), url


def load_manual_bwin_file(path: Path) -> list[tuple[int, str]]:
    """Carga pares (fixture_id, bwin_url) desde JSON o CSV."""
    p = Path(path)
    if not p.is_file():
        raise ManualBwinLinkError(f"MANUAL_BWIN_FILE_NOT_FOUND:{p}")
    if p.suffix.lower() == ".csv":
        with p.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    else:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
        rows = doc if isinstance(doc, list) else (doc.get("links") or doc.get("bindings") or doc.get("fixtures") or [])
    pairs: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ManualBwinLinkError("MANUAL_BWIN_FILE_ROW_INVALID")
        fid = row.get("fixture_id")
        url = str(row.get("bwin_url") or row.get("url") or "").strip()
        if not str(fid or "").isdigit() or int(fid) <= 0 or not url:
            raise ManualBwinLinkError(f"MANUAL_BWIN_FILE_ROW_INCOMPLETE:{row!r}")
        pairs.append((int(fid), url))
    return pairs


def parse_manual_bwin_url(url: str, base_url: str = bd.BASE) -> dict[str, Any]:
    """Valida dominio/esquema HTTPS directo de Bwin (rechaza Markdown) y extrae Event ID."""
    raw = str(url or "").strip()
    if not raw:
        raise ManualBwinLinkError("MANUAL_BWIN_URL_EMPTY")
    if MARKDOWN_URL_RE.search(raw) or "[" in raw or "]" in raw:
        raise ManualBwinLinkError(
            f"MANUAL_BWIN_URL_MARKDOWN_FORMAT_FORBIDDEN:{raw} "
            "(introduce únicamente la URL HTTPS directa sin sintaxis Markdown [texto](url))"
        )
    if any(ch.isspace() for ch in raw) or raw.count("://") != 1 or "<" in raw or ">" in raw or "(" in raw or ")" in raw:
        raise ManualBwinLinkError(f"MANUAL_BWIN_URL_INVALID_SYNTAX:{raw}")
    parsed = urlparse(raw)
    base_host = (urlparse(base_url).hostname or "www.bwin.es").lower()
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or (host != base_host and not host.endswith(".bwin.es"))
    ):
        raise ManualBwinLinkError(f"MANUAL_BWIN_URL_INVALID_HOST_OR_SCHEME:{raw}")
    event_id = bd.extract_event_id(raw)
    if not event_id or not event_id.isdigit():
        raise ManualBwinLinkError(f"MANUAL_BWIN_URL_MISSING_EVENT_ID:{raw}")
    path = unquote(parsed.path or "")
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "bwin_url": raw,
        "bwin_event_id": event_id,
        "slug": slug,
        "host": host,
    }


def find_local_bwin_url_for_event_id(
    eval_dir: Path,
    event_id: str,
    *,
    catalog_snapshots: list[Path] | None = None,
) -> dict[str, Any] | None:
    """Busca una URL real de Bwin para un Event ID en catálogos locales observados (sin inventar slugs)."""
    eid = str(event_id or "").strip()
    if not eid.isdigit():
        return None
    eval_dir = Path(eval_dir)
    candidate_paths: list[Path] = []
    if catalog_snapshots:
        candidate_paths.extend(Path(p) for p in catalog_snapshots if Path(p).is_file())
    bwin_root = eval_dir / "prospective_trial/bwin"
    if bwin_root.is_dir():
        candidate_paths.extend(sorted(bwin_root.rglob("catalog_observed.json"), reverse=True))
        candidate_paths.extend(sorted(bwin_root.rglob("bwin_discovery.json"), reverse=True))
    seen_paths: set[Path] = set()
    for path in candidate_paths:
        resolved = path.resolve()
        if resolved in seen_paths or not path.is_file():
            continue
        seen_paths.add(resolved)
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        rows: list[dict[str, Any]] = []
        if isinstance(doc, list):
            rows = [r for r in doc if isinstance(r, dict)]
        elif isinstance(doc, dict):
            for key in ("catalog_observed", "events", "matches"):
                val = doc.get(key)
                if isinstance(val, list):
                    rows.extend(r for r in val if isinstance(r, dict))
        for row in rows:
            row_eid = str(row.get("bwin_event_id") or row.get("event_id") or "").strip()
            row_url = str(row.get("bwin_url") or row.get("url") or "").strip()
            if row_eid == eid and row_url.startswith("https://"):
                return {
                    "bwin_event_id": eid,
                    "bwin_url": row_url,
                    "source_path": str(path),
                    "resolution_source": "LOCAL_OBSERVED_CATALOG",
                    "observed_text": row.get("text"),
                }
    return None


def resolve_manual_bwin_target(
    eval_dir: Path,
    raw_target: str,
    *,
    fixture: dict[str, Any] | None = None,
    base_url: str = bd.BASE,
    catalog_snapshots: list[Path] | None = None,
    resolve_online: bool = False,
    headless: bool = True,
    timeout: int = 25000,
) -> dict[str, Any]:
    """Admite URL completa o Event ID numérico y resuelve su URL real sin fabricar URLs ficticias."""
    raw = str(raw_target or "").strip()
    if not raw:
        raise ManualBwinLinkError("MANUAL_BWIN_TARGET_EMPTY")
    if not raw.isdigit():
        parsed = parse_manual_bwin_url(raw, base_url=base_url)
        return {**parsed, "resolution_source": "DIRECT_URL"}

    eid = raw
    local_hit = find_local_bwin_url_for_event_id(eval_dir, eid, catalog_snapshots=catalog_snapshots)
    if local_hit:
        parsed = parse_manual_bwin_url(local_hit["bwin_url"], base_url=base_url)
        return {
            **parsed,
            "resolution_source": local_hit["resolution_source"],
            "resolution_path": local_hit["source_path"],
        }

    if resolve_online:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            try:
                catalog, _ = bd.fetch_catalog(browser, base_url, list(bd.CALENDAR), 12, timeout)
                if fixture is not None:
                    catalog, _, _ = bd.extend_catalog_for_missing(
                        browser, base_url, catalog, [fixture], 8, 12, timeout
                    )
                for row in catalog:
                    if str(row.get("bwin_event_id") or "").strip() == eid and row.get("bwin_url"):
                        parsed = parse_manual_bwin_url(str(row["bwin_url"]), base_url=base_url)
                        return {**parsed, "resolution_source": "ONLINE_BWIN_CATALOG_LOOKUP"}
            finally:
                browser.close()

    raise ManualBwinLinkError(
        f"MANUAL_BWIN_EVENT_ID_URL_NOT_RESOLVED:{eid}"
        " (no encontrado en catálogos locales; usa --confirm-external para buscar en Bwin o pasa --url con la URL completa)"
    )


def _evaluate_manual_temporal_window(
    fixture: dict[str, Any] | None,
    now_utc: datetime,
    min_lead_minutes: int,
    *,
    operation: str,
) -> tuple[bool, dict[str, Any] | None]:
    now_ref = now_utc.astimezone(timezone.utc)
    if not fixture:
        return False, {
            "blocked": True,
            "block_type": "TEMPORAL_WINDOW_BLOCK",
            "operation": operation,
            "reason": "FIXTURE_NOT_IN_EVALUATION",
            "min_lead_minutes": int(min_lead_minutes),
            "remaining_minutes": None,
            "checked_at_utc": now_ref.isoformat(),
        }
    ko = bd.local_kickoff(fixture.get("kickoff_madrid") or fixture.get("kickoff_utc"))
    if ko is None:
        return False, {
            "blocked": True,
            "block_type": "TEMPORAL_WINDOW_BLOCK",
            "operation": operation,
            "reason": "INVALID_OR_MISSING_KICKOFF",
            "min_lead_minutes": int(min_lead_minutes),
            "remaining_minutes": None,
            "checked_at_utc": now_ref.isoformat(),
        }
    remaining_min = (ko.astimezone(timezone.utc) - now_ref).total_seconds() / 60.0
    if remaining_min <= 0:
        return False, {
            "blocked": True,
            "block_type": "POST_KICKOFF_FORBIDDEN",
            "operation": operation,
            "reason": "POST_KICKOFF_PREMATCH_FORBIDDEN",
            "min_lead_minutes": int(min_lead_minutes),
            "remaining_minutes": round(remaining_min, 3),
            "checked_at_utc": now_ref.isoformat(),
        }
    if remaining_min < float(min_lead_minutes):
        return False, {
            "blocked": True,
            "block_type": "TEMPORAL_WINDOW_BLOCK",
            "operation": operation,
            "reason": "PREMATCH_MANUAL_LEAD_WINDOW_EXPIRED",
            "min_lead_minutes": int(min_lead_minutes),
            "remaining_minutes": round(remaining_min, 3),
            "checked_at_utc": now_ref.isoformat(),
        }
    return True, None


def _effective_national_context(fixture: dict[str, Any]) -> bool:
    if bd.is_national_team_context(fixture):
        return True
    h_norm = bd.norm(fixture.get("home"))
    a_norm = bd.norm(fixture.get("away"))
    return h_norm in bd._NATIONAL_TEAM_EQUIVALENCE or a_norm in bd._NATIONAL_TEAM_EQUIVALENCE


def _slug_team_check(
    fixture: dict[str, Any],
    slug: str,
    all_fixtures: dict[int, dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    """Evalúa el slug sin bloquear traducciones o alias válidos de forma arbitraria.

    Retorna:
      - ('OK', None) si el slug coincide y respeta el orden local/visitante.
      - ('CONFLICT', motivo) solo ante conflicto positivo (orden invertido o coincidencia con otro fixture).
      - ('UNVERIFIED', motivo) si el slug no coincide léxicamente offline (p. ej. traducción no catalogada).
    """
    clean_slug = bd.norm(slug.replace("-", " ").replace(":", " "))
    tokens = [w for w in clean_slug.split() if not w.isdigit() and w not in {"eventos", "sports", "es"}]
    if len(tokens) < 2:
        return "UNVERIFIED", "URL_SLUG_TOO_SHORT_FOR_OFFLINE_CHECK"
    evidence = " ".join(tokens)
    nat = _effective_national_context(fixture)
    home = str(fixture.get("home") or "")
    away = str(fixture.get("away") or "")
    h_sim = bd.team_similarity(home, evidence, national_context=nat)
    a_sim = bd.team_similarity(away, evidence, national_context=nat)
    order = bd.team_order_status(home, away, evidence, national_context=nat)
    if order.endswith("_CONFLICT") and min(h_sim, a_sim) >= 0.70:
        return "CONFLICT", f"URL_SLUG_TEAM_ORDER_REVERSED:{order}"
    if min(h_sim, a_sim) >= 0.70:
        return "OK", None
    if all_fixtures and max(h_sim, a_sim) < 0.35:
        target_fid = int(fixture.get("fixture_id") or 0)
        for other_fid, other in all_fixtures.items():
            if int(other_fid) == target_fid:
                continue
            o_nat = _effective_national_context(other)
            oh = bd.team_similarity(str(other.get("home") or ""), evidence, national_context=o_nat)
            oa = bd.team_similarity(str(other.get("away") or ""), evidence, national_context=o_nat)
            if oh >= 0.82 and oa >= 0.82:
                return "CONFLICT", f"URL_SLUG_MATCHES_OTHER_FIXTURE:{other_fid}"
    if max(h_sim, a_sim) < 0.35:
        return "UNVERIFIED", f"URL_SLUG_UNVERIFIED_OFFLINE:home_sim={round(h_sim,3)},away_sim={round(a_sim,3)}"
    return "UNVERIFIED", f"URL_SLUG_PARTIAL_MATCH_OFFLINE:home_sim={round(h_sim,3)},away_sim={round(a_sim,3)}"


def _stored_auto_discoveries(eval_dir: Path, fixture_id: int) -> list[dict[str, Any]]:
    """Lee descubrimientos automáticos previos (excluyendo manual_*) para el fixture."""
    bwin_root = eval_dir / "prospective_trial/bwin"
    if not bwin_root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for p in sorted(bwin_root.rglob("bwin_discovery.json")):
        if p.parent.name.startswith("manual_"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in doc.get("matches", []):
            if str(row.get("fixture_id")) == str(fixture_id) and row.get("bwin_event_id"):
                found.append({"path": str(p.resolve()), "row": row})
    return found


def _load_eval_fixtures(eval_dir: Path) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Carga fixtures descubiertos y estados de filtro de categoría en la evaluación."""
    fx_path = eval_dir / "fixtures.json"
    if not fx_path.is_file():
        raise ManualBwinLinkError(f"EVALUATION_FIXTURES_MISSING:{fx_path}")
    fx_doc = json.loads(fx_path.read_text(encoding="utf-8"))
    by_id: dict[int, dict[str, Any]] = {}
    for row in fx_doc.get("fixtures", []):
        if row.get("fixture_id") is not None:
            by_id[int(row["fixture_id"])] = dict(row)

    major_by_id: dict[int, dict[str, Any]] = {}
    major_path = eval_dir / "fixtures_major.json"
    if major_path.is_file():
        m_doc = json.loads(major_path.read_text(encoding="utf-8"))
        for key in ("fixtures", "review", "excluded"):
            for row in m_doc.get(key, []):
                if row.get("fixture_id") is not None:
                    major_by_id[int(row["fixture_id"])] = dict(row)
                    by_id.setdefault(int(row["fixture_id"]), dict(row))

    pre_path = eval_dir / "prospective_trial/enriched/preselection.json"
    if pre_path.is_file():
        p_doc = json.loads(pre_path.read_text(encoding="utf-8"))
        for row in p_doc.get("matches", []):
            if row.get("fixture_id") is not None:
                fid = int(row["fixture_id"])
                merged = {**by_id.get(fid, {}), **row}
                by_id[fid] = merged
    return by_id, major_by_id


def manual_links_path(eval_dir: Path) -> Path:
    return Path(eval_dir) / "prospective_trial/bwin/manual_bwin_links.json"


def load_manual_links(eval_dir: Path) -> dict[str, Any]:
    path = manual_links_path(eval_dir)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema_version": MANUAL_LINKS_SCHEMA, "links": {}}


def register_manual_bwin_urls(
    eval_dir: Path,
    bindings: list[tuple[int, str]],
    *,
    base_url: str = bd.BASE,
    now_utc: datetime | None = None,
    min_lead_minutes: int = MANUAL_BWIN_MIN_LEAD_MINUTES,
) -> dict[str, Any]:
    """Registra y valida offline una o varias URLs Bwin asociadas a fixture_id.

    No salta controles PREMATCH ni altera el ranking estadístico ni snapshots congelados.
    """
    if not bindings:
        raise ManualBwinLinkError("MANUAL_BWIN_NO_BINDINGS_PROVIDED")
    lead_floor = int(min_lead_minutes)
    if lead_floor < MANUAL_BWIN_MIN_LEAD_MINUTES:
        raise ManualBwinLinkError(
            f"MANUAL_BWIN_MIN_LEAD_BELOW_SAFETY_FLOOR:{lead_floor}<{MANUAL_BWIN_MIN_LEAD_MINUTES}"
        )
    eval_dir = Path(eval_dir)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    by_id, major_by_id = _load_eval_fixtures(eval_dir)

    # Validar duplicados y conflictos dentro del lote suministrado
    batch_by_fid: dict[int, dict[str, Any]] = {}
    batch_by_eid: dict[str, int] = {}
    parsed_items: list[tuple[int, dict[str, Any]]] = []
    for fid, raw_url in bindings:
        parsed = parse_manual_bwin_url(raw_url, base_url=base_url)
        eid = parsed["bwin_event_id"]
        if fid in batch_by_fid:
            prev = batch_by_fid[fid]
            if prev["bwin_event_id"] != eid or prev["bwin_url"] != parsed["bwin_url"]:
                raise ManualBwinLinkError(
                    f"MANUAL_BWIN_DUPLICATE_FIXTURE_CONFLICT:fixture_id={fid},events=({prev['bwin_event_id']},{eid})"
                )
            continue  # duplicado idéntico en el mismo comando: idempotente
        if eid in batch_by_eid and batch_by_eid[eid] != fid:
            raise ManualBwinLinkError(
                f"MANUAL_BWIN_DUPLICATE_EVENT_ACROSS_FIXTURES:event_id={eid},fixtures=({batch_by_eid[eid]},{fid})"
            )
        batch_by_fid[fid] = parsed
        batch_by_eid[eid] = fid
        parsed_items.append((fid, parsed))

    store = load_manual_links(eval_dir)
    links: dict[str, Any] = dict(store.get("links") or {})

    # Verificar conflicto de mismo bwin_event_id ya asignado a otro fixture distinto
    existing_eid_to_fid = {
        str(v.get("extracted_event_id")): str(k)
        for k, v in links.items()
        if v.get("extracted_event_id") and v.get("identity_status") != "BWIN_IDENTITY_CONFLICT"
    }

    registered: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    temporal_blocked: list[dict[str, Any]] = []

    for fid, parsed in parsed_items:
        fid_str = str(fid)
        eid = parsed["bwin_event_id"]
        if eid in existing_eid_to_fid and existing_eid_to_fid[eid] != fid_str:
            raise ManualBwinLinkError(
                f"MANUAL_BWIN_EVENT_ALREADY_BOUND:event_id={eid},existing_fixture={existing_eid_to_fid[eid]},new_fixture={fid}"
            )
        fixture = by_id.get(fid)
        if not fixture:
            raise ManualBwinLinkError(f"MANUAL_BWIN_FIXTURE_NOT_IN_EVALUATION:{fid}")
        if str(fixture.get("status") or "").upper() != "NS":
            raise ManualBwinLinkError(f"MANUAL_BWIN_NON_PREMATCH_STATUS:{fid}:{fixture.get('status')}")
        ko = bd.local_kickoff(fixture.get("kickoff_madrid") or fixture.get("kickoff_utc"))
        if ko is None or ko.astimezone(timezone.utc) <= now:
            raise ManualBwinLinkError(f"MANUAL_BWIN_MATCH_ALREADY_STARTED:{fid}")
        temp_ok, temp_block = _evaluate_manual_temporal_window(
            fixture,
            now,
            lead_floor,
            operation="bind_manual_bwin",
        )
        major_row = major_by_id.get(fid) or fixture
        if str(major_row.get("filter_status") or "").upper() == "EXCLUDED":
            reason = major_row.get("filter_reason") or "EXCLUDED"
            if any(tok in str(reason) for tok in ("YOUTH", "RESERVE", "NOT_PREMATCH", "CLUB_FRIENDLY")):
                raise ManualBwinLinkError(f"MANUAL_BWIN_EXCLUDED_CATEGORY:{fid}:{reason}")

        slug_state, slug_reason = _slug_team_check(fixture, parsed["slug"], all_fixtures=by_id)
        auto_rows = _stored_auto_discoveries(eval_dir, fid)
        verified_auto = next((a for a in auto_rows if a["row"].get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY"), None)
        any_auto = verified_auto or (auto_rows[0] if auto_rows else None)

        prev_entry = links.get(fid_str) or {}
        if (
            prev_entry.get("extracted_event_id") == eid
            and prev_entry.get("url_provided", {}).get("bwin_url") == parsed["bwin_url"]
            and prev_entry.get("identity_status") == "BWIN_MANUAL_LINK_VERIFIED"
            and slug_state != "CONFLICT"
            and not (verified_auto and str(verified_auto["row"].get("bwin_event_id")) != str(eid))
        ):
            if not temp_ok:
                prev_entry["operational_status"] = "OPERATIONAL_BLOCK_TEMPORAL"
                prev_entry["operational_block"] = temp_block
                links[fid_str] = prev_entry
                temporal_blocked.append(prev_entry)
                continue
            prev_entry["operational_status"] = "CLEAR"
            prev_entry["operational_block"] = None
            links[fid_str] = prev_entry
            # Ya verificada previamente de forma idéntica: conservar estado sin duplicar
            registered.append(prev_entry)
            continue

        conflict_reasons: list[str] = []
        warnings: list[str] = []
        auto_reconciliation: dict[str, Any] | None = None

        if verified_auto and str(verified_auto["row"].get("bwin_event_id")) != str(eid):
            auto_eid = str(verified_auto["row"].get("bwin_event_id"))
            conflict_reasons.append(f"AUTO_DISCOVERY_ALREADY_VERIFIED_DIFFERENT_EVENT:auto={auto_eid},manual={eid}")
            auto_reconciliation = {
                "status": "CONFLICT_WITH_ACCREDITED_AUTO",
                "auto_event_id": auto_eid,
                "manual_event_id": eid,
                "auto_discovery_path": verified_auto["path"],
                "resolution": "ACCREDITED_AUTO_DISCOVERY_PRESERVED_MANUAL_REJECTED",
            }
        elif any_auto and str(any_auto["row"].get("bwin_event_id")) != str(eid):
            auto_eid = str(any_auto["row"].get("bwin_event_id"))
            warnings.append(f"DISCREPANCY_WITH_UNVERIFIED_AUTO:auto={auto_eid},manual={eid}")
            auto_reconciliation = {
                "status": "DISCREPANCY_WITH_UNVERIFIED_AUTO",
                "auto_event_id": auto_eid,
                "manual_event_id": eid,
                "auto_discovery_path": any_auto["path"],
                "resolution": "REQUIRES_LIVE_VERIFICATION_OF_MANUAL_URL",
            }

        if slug_state == "CONFLICT" and slug_reason:
            conflict_reasons.append(slug_reason)
        elif slug_state == "UNVERIFIED" and slug_reason:
            warnings.append(slug_reason)

        identity_status = "BWIN_IDENTITY_CONFLICT" if conflict_reasons else "BWIN_MANUAL_LINK_PENDING_VALIDATION"
        classification_pending = str(major_row.get("filter_status") or "").upper() == "REVIEW"

        entry = {
            "fixture_id": fid,
            "url_provided": {
                "bwin_url": parsed["bwin_url"],
                "extracted_event_id": eid,
                "provided_at_utc": now.isoformat(),
                "source": "MANUAL_CLI",
            },
            "extracted_event_id": eid,
            "bwin_url": parsed["bwin_url"],
            "identity_status": identity_status,
            "quote_capture_status": prev_entry.get("quote_capture_status") if prev_entry.get("extracted_event_id") == eid else "NOT_CAPTURED",
            "classification_pending_warning": classification_pending,
            "conflict_reasons": conflict_reasons,
            "pending_reasons": warnings if identity_status == "BWIN_MANUAL_LINK_PENDING_VALIDATION" else [],
            "warnings": warnings,
            "auto_reconciliation": auto_reconciliation,
            "fixture_identity": {
                "fixture_id": fid,
                "home": fixture.get("home"),
                "away": fixture.get("away"),
                "home_id": fixture.get("home_id"),
                "away_id": fixture.get("away_id"),
                "league": fixture.get("league"),
                "league_id": fixture.get("league_id"),
                "season": fixture.get("season"),
                "gender": fixture.get("gender"),
                "kickoff_madrid": fixture.get("kickoff_madrid"),
                "competition_category": major_row.get("competition_category"),
            },
            "statistical_ranking_modified": False,
            "prematch_gates_bypassed": False,
            "operational_status": "CLEAR" if temp_ok else "OPERATIONAL_BLOCK_TEMPORAL",
            "operational_block": None if temp_ok else temp_block,
            "verification": prev_entry.get("verification") if prev_entry.get("extracted_event_id") == eid else None,
        }
        links[fid_str] = entry
        if not temp_ok:
            temporal_blocked.append(entry)
            if identity_status == "BWIN_IDENTITY_CONFLICT":
                conflicts.append(entry)
        elif identity_status == "BWIN_IDENTITY_CONFLICT":
            conflicts.append(entry)
        else:
            registered.append(entry)

    out_doc = {
        "schema_version": MANUAL_LINKS_SCHEMA,
        "updated_at_utc": now.isoformat(),
        "eval_dir": str(eval_dir.resolve()),
        "statistical_ranking_modified": False,
        "links": links,
    }
    _atomic_json(manual_links_path(eval_dir), out_doc)
    return {
        "status": (
            "TEMPORAL_WINDOW_BLOCKED"
            if temporal_blocked and not registered and not conflicts
            else "CONFLICTS_DETECTED"
            if conflicts and not registered
            else "REGISTERED_PENDING_LIVE_VALIDATION"
            if registered
            else "NO_CHANGES"
        ),
        "manual_links_path": str(manual_links_path(eval_dir)),
        "operation_min_lead_minutes": lead_floor,
        "registered_count": len(registered),
        "conflict_count": len(conflicts),
        "temporal_blocked_count": len(temporal_blocked),
        "registered": registered,
        "conflicts": conflicts,
        "temporal_blocked": temporal_blocked,
    }


def evaluate_observed_manual_identity(
    fixture: dict[str, Any],
    expected_event_id: str,
    res: dict[str, Any],
    obs: dict[str, Any],
) -> tuple[str, list[str], list[str], dict[str, Any]]:
    """Evalúa evidencia observada en vivo separando verificación positiva, insuficiencia y conflicto."""
    conflicts: list[str] = []
    pending: list[str] = []

    obs_status = str(obs.get("status") or "").upper()
    if obs_status != "OPENED":
        pending.append(f"LIVE_INSPECTION_NOT_OPENED:{obs_status or 'MISSING'}")
        return "BWIN_MANUAL_LINK_PENDING_VALIDATION", [], pending, {
            "page_opened": False,
            "event_id_verified": False,
            "teams_verified": False,
            "team_order_verified": False,
            "kickoff_verified": False,
            "competition_compatible": False,
            "category_compatible": False,
        }

    # 1. Event ID observado (nunca fallback `or expected_event_id`)
    observed_eid = obs.get("event_id_from_url")
    eid_ok = False
    if observed_eid is None or str(observed_eid).strip() == "":
        pending.append("OBSERVED_EVENT_ID_ABSENT")
    elif str(observed_eid).strip() != str(expected_event_id).strip():
        conflicts.append(f"OBSERVED_EVENT_ID_MISMATCH:expected={expected_event_id},observed={observed_eid}")
    else:
        eid_ok = True

    # 2. Equipos y orden local/visitante observados en el título de la página
    page_title = str(obs.get("page_title") or "").strip()
    nat = _effective_national_context(fixture)
    home = str(fixture.get("home") or "")
    away = str(fixture.get("away") or "")
    title_norm = bd.norm(page_title)
    title_hydrated = bool(title_norm and title_norm != "bwin")

    title_teams_match = obs.get("title_teams_match")
    teams_ok = False
    if not title_hydrated and title_teams_match is None:
        pending.append("PAGE_TITLE_UNAVAILABLE")
    elif title_teams_match is False:
        if not title_hydrated:
            pending.append("PAGE_TITLE_NOT_HYDRATED")
        else:
            conflicts.append("TITLE_TEAMS_MISMATCH")
    elif title_teams_match is True:
        teams_ok = True
    else:
        # title_teams_match omitido pero título presente: verificar directamente
        h_sim = bd.team_similarity(home, page_title, national_context=nat)
        a_sim = bd.team_similarity(away, page_title, national_context=nat)
        if h_sim >= 0.82 and a_sim >= 0.82:
            teams_ok = True
        else:
            conflicts.append("TITLE_TEAMS_MISMATCH")

    observed_order = "UNKNOWN"
    order_ok = False
    if title_hydrated:
        observed_order = bd.team_order_status(home, away, page_title, national_context=nat)
    elif obs.get("team_order_status"):
        observed_order = str(obs.get("team_order_status"))

    if observed_order.endswith("_CONFLICT"):
        conflicts.append(f"TEAM_ORDER_REVERSED_CONFLICT:{observed_order}")
    elif observed_order in {"HOME_AWAY", "HOME_AWAY_TOKEN", "HOME_AWAY_ALIAS"}:
        order_ok = True
    elif teams_ok:
        pending.append(f"TEAM_ORDER_UNVERIFIED:{observed_order}")

    # 3. Kickoff absoluto compatible (sin aceptar cuentas atrás ni ausencia como verificación positiva)
    obs_id_status = str(obs.get("identity_status") or "")
    obs_start = obs.get("event_start_machine_readable")
    diff_min = obs.get("kickoff_difference_minutes")
    kickoff_ok = False
    if obs_id_status in {"KICKOFF_MISMATCH_REVIEW", "KICKOFF_COUNTDOWN_MISMATCH_REVIEW", "CONFLICTING_TIME_SOURCES_REVIEW"}:
        conflicts.append(f"KICKOFF_CONFLICT:{obs_id_status}")
    elif isinstance(diff_min, (int, float)) and not isinstance(diff_min, bool) and float(diff_min) > 15.0:
        conflicts.append(f"KICKOFF_MISMATCH_MINUTES:{diff_min}")
    elif not obs_start or diff_min is None or obs_id_status in {"NAME_MATCH_TIME_NOT_MACHINE_VERIFIED", "NAME_AND_RELATIVE_TIME_CORROBORATED"}:
        pending.append("ABSOLUTE_KICKOFF_NOT_VERIFIED")
    elif obs_id_status == "NAME_AND_ABSOLUTE_TIME_MATCH" or (
        obs_start and isinstance(diff_min, (int, float)) and not isinstance(diff_min, bool) and float(diff_min) <= 15.0
    ):
        kickoff_ok = True
    else:
        pending.append(f"KICKOFF_STATUS_INSUFFICIENT:{obs_id_status or 'UNKNOWN'}")

    # 4. Competición verificable
    comp_score = obs.get("competition_match_score")
    if comp_score is None:
        comp_score = res.get("competition_similarity")
    sup_checks = obs.get("supporting_identity_checks") or {}
    comp_compat_flag = sup_checks.get("competition_compatible")
    comp_ok = False
    if fixture.get("league"):
        if comp_compat_flag is False:
            conflicts.append("COMPETITION_INCOMPATIBLE")
        elif isinstance(comp_score, (int, float)) and not isinstance(comp_score, bool):
            if float(comp_score) < 0.82:
                conflicts.append(f"COMPETITION_INCOMPATIBLE:score={comp_score}")
            else:
                comp_ok = True
        elif comp_compat_flag is True:
            comp_ok = True
        else:
            pending.append("COMPETITION_UNVERIFIED")
    else:
        comp_ok = True

    # 5. Categoría y género compatibles
    page_cat = obs.get("page_category_compatibility")
    cat_compat_flag = sup_checks.get("category_compatible")
    cat_corr = res.get("category_corroboration_status")
    cat_ok = False
    if (
        page_cat in {"GENDER_CONFLICT", "NON_ADULT_CONFLICT"}
        or cat_compat_flag is False
        or cat_corr == "CONFLICT"
        or obs_id_status == "CATEGORY_CONFLICT"
        or res.get("identity_status") == "CATEGORY_CONFLICT"
    ):
        conflicts.append(f"CATEGORY_OR_GENDER_CONFLICT:{page_cat or cat_corr or obs_id_status}")
    elif str(fixture.get("gender") or "").upper() == "F" and page_cat == "FEMALE_NOT_VISIBLE":
        pending.append("FEMALE_CATEGORY_NOT_CORROBORATED")
    elif page_cat is not None or cat_compat_flag is True or cat_corr == "CORROBORATED":
        cat_ok = True
    else:
        pending.append("CATEGORY_UNVERIFIED")

    # Respetar también discrepancias de estado global devuelto por summarize
    res_status = str(res.get("status") or "")
    if not conflicts and not pending and res_status != "IDENTITY_VERIFIED_AUTOMATICALLY":
        if res_status in {"EVENT_FOUND_NEEDS_TIME_CONFIRMATION", "EVENT_FOUND_TIME_CORROBORATED", "POSSIBLE_EVENT_FOUND", "LOW_CONFIDENCE_REVIEW", "AMBIGUOUS_MULTIPLE_EVENTS"}:
            pending.append(f"DISCOVERY_STATUS_NOT_FINAL:{res_status}")
        else:
            conflicts.append(f"DISCOVERY_STATUS_REJECTED:{res_status or 'UNKNOWN'}")

    checks = {
        "page_opened": True,
        "event_id_verified": eid_ok,
        "teams_verified": teams_ok,
        "team_order_verified": order_ok,
        "observed_team_order": observed_order,
        "kickoff_verified": kickoff_ok,
        "competition_compatible": comp_ok,
        "category_compatible": cat_ok,
    }
    if conflicts:
        return "BWIN_IDENTITY_CONFLICT", conflicts, pending, checks
    if pending:
        return "BWIN_MANUAL_LINK_PENDING_VALIDATION", [], pending, checks
    return "BWIN_MANUAL_LINK_VERIFIED", [], [], checks


def verify_manual_bwin_links_online(
    eval_dir: Path,
    fixture_ids: list[int] | None = None,
    *,
    headless: bool = True,
    timeout: int = 25000,
    min_lead_minutes: int = MANUAL_BWIN_MIN_LEAD_MINUTES,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Revalida en Bwin la identidad de URLs manuales pendientes con comprobación positiva completa."""
    from playwright.sync_api import sync_playwright

    lead_floor = int(min_lead_minutes)
    if lead_floor < MANUAL_BWIN_MIN_LEAD_MINUTES:
        raise ManualBwinLinkError(
            f"MANUAL_BWIN_MIN_LEAD_BELOW_SAFETY_FLOOR:{lead_floor}<{MANUAL_BWIN_MIN_LEAD_MINUTES}"
        )

    eval_dir = Path(eval_dir)
    store = load_manual_links(eval_dir)
    links: dict[str, Any] = dict(store.get("links") or {})
    by_id, _ = _load_eval_fixtures(eval_dir)
    now_ref_utc = (now_utc.astimezone(timezone.utc) if now_utc else trial.now_madrid().astimezone(timezone.utc))
    target_fids = [str(f) for f in fixture_ids] if fixture_ids else list(links.keys())

    verified_results: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []
    failed_results: list[dict[str, Any]] = []
    temporal_blocked_results: list[dict[str, Any]] = []

    to_check = [fid for fid in target_fids if fid in links and links[fid].get("identity_status") != "BWIN_IDENTITY_CONFLICT"]
    if not to_check:
        return {
            "verified": [],
            "pending": [],
            "failed": [],
            "temporal_blocked": [],
            "operation_min_lead_minutes": lead_floor,
            "manual_links_path": str(manual_links_path(eval_dir)),
        }

    eligible_to_check: list[str] = []
    for fid_str in to_check:
        entry = links[fid_str]
        fid = int(fid_str)
        fixture = by_id.get(fid)
        temp_ok, temp_block = _evaluate_manual_temporal_window(
            fixture,
            now_ref_utc,
            lead_floor,
            operation="verify_manual_bwin",
        )
        if not temp_ok:
            # Conservar identidad pendiente/verificada y registrar bloqueo operacional sin fabricar conflicto de identidad.
            if entry.get("identity_status") not in CANONICAL_BWIN_STATES:
                entry["identity_status"] = "BWIN_MANUAL_LINK_PENDING_VALIDATION"
            entry["operational_status"] = (
                "OPERATIONAL_BLOCK_POST_KICKOFF"
                if temp_block and temp_block["block_type"] == "POST_KICKOFF_FORBIDDEN"
                else "OPERATIONAL_BLOCK_TEMPORAL"
            )
            entry["operational_block"] = temp_block
            temporal_blocked_results.append(entry)
            continue
        entry["operational_status"] = "CLEAR"
        entry["operational_block"] = None
        eligible_to_check.append(fid_str)

    if eligible_to_check:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            try:
                for fid_str in eligible_to_check:
                    entry = links[fid_str]
                    fid = int(fid_str)
                    fixture = by_id.get(fid)
                    eid = entry["extracted_event_id"]
                    url = entry["bwin_url"]
                    slug_text = unquote(urlparse(url).path).rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace(":", " ")
                    # No fabricar text ni team_order_status del fixture como prueba previa;
                    # name_score=1.0 solo habilita abrir la URL directa para observar la página real.
                    candidate = {
                        "bwin_event_id": eid,
                        "bwin_url": url,
                        "text": slug_text,
                        "text_source": "MANUAL_USER_URL_DIRECT_REINSPECTION",
                        "text_observed_at_utc": now_ref_utc.isoformat(),
                        "source_pages": [url],
                        "name_score": 1.0,
                        "home_similarity": None,
                        "away_similarity": None,
                        "team_order_status": None,
                        "category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                        "competition_similarity": None,
                    }
                    res = bd.summarize(fixture, [candidate], browser, timeout, inspect=True, inspection_retries=1)
                    obs = res.get("inspection") or {}
                    new_status, conflicts, pending, checks = evaluate_observed_manual_identity(fixture, eid, res, obs)

                    # Verificar también si existe descubrimiento automático acreditado contradictorio
                    auto_rows = _stored_auto_discoveries(eval_dir, fid)
                    verified_auto = next((a for a in auto_rows if a["row"].get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY"), None)
                    if verified_auto and str(verified_auto["row"].get("bwin_event_id")) != str(eid):
                        auto_eid = str(verified_auto["row"].get("bwin_event_id"))
                        conflicts.append(f"AUTO_DISCOVERY_ALREADY_VERIFIED_DIFFERENT_EVENT:auto={auto_eid},manual={eid}")
                        new_status = "BWIN_IDENTITY_CONFLICT"
                        entry["auto_reconciliation"] = {
                            "status": "CONFLICT_WITH_ACCREDITED_AUTO",
                            "auto_event_id": auto_eid,
                            "manual_event_id": eid,
                            "auto_discovery_path": verified_auto["path"],
                            "resolution": "ACCREDITED_AUTO_DISCOVERY_PRESERVED_MANUAL_REJECTED",
                        }

                    stamp_str = now_ref_utc.strftime("%Y%m%dT%H%M%S%fZ")
                    disc_dir = eval_dir / "prospective_trial/bwin" / f"manual_{fid}_{stamp_str}"
                    disc_payload = {
                        "schema_version": "0.5-bwin-discovery-v3",
                        "generated_at_utc": now_ref_utc.isoformat(),
                        "source_input": str(eval_dir / "fixtures.json"),
                        "user_time_reference": "Europe/Madrid",
                        "prematch_only": True,
                        "methodology": "Manual Bwin URL direct page reinspection (observed event_id, teams+order, kickoff, competition, category)",
                        "pages": [],
                        "catalog_observed": [candidate],
                        "matches": [res],
                    }
                    bd.write_reports(disc_dir, disc_payload)
                    entry["verification"] = {
                        "verified_at_utc": now_ref_utc.isoformat(),
                        "discovery_json": str((disc_dir / "bwin_discovery.json").resolve()),
                        "discovery_status": res.get("status"),
                        "identity_status": res.get("identity_status"),
                        "observed_event_id": obs.get("event_id_from_url"),
                        "page_title": obs.get("page_title"),
                        "title_teams_match": obs.get("title_teams_match"),
                        "observed_team_order": checks.get("observed_team_order"),
                        "event_start_machine_readable": obs.get("event_start_machine_readable"),
                        "kickoff_difference_minutes": obs.get("kickoff_difference_minutes"),
                        "competition_compatible": checks.get("competition_compatible"),
                        "competition_match_score": obs.get("competition_match_score") if obs.get("competition_match_score") is not None else res.get("competition_similarity"),
                        "category_compatibility": obs.get("page_category_compatibility"),
                        "positive_checks": checks,
                    }
                    entry["identity_status"] = new_status
                    entry["conflict_reasons"] = conflicts
                    entry["pending_reasons"] = pending
                    entry["operational_status"] = "CLEAR"
                    entry["operational_block"] = None
                    if new_status == "BWIN_MANUAL_LINK_VERIFIED":
                        verified_results.append(entry)
                    elif new_status == "BWIN_MANUAL_LINK_PENDING_VALIDATION":
                        pending_results.append(entry)
                    else:
                        failed_results.append(entry)
            finally:
                browser.close()

    store["updated_at_utc"] = now_ref_utc.isoformat()
    store["links"] = links
    _atomic_json(manual_links_path(eval_dir), store)
    return {
        "verified": verified_results,
        "pending": pending_results,
        "failed": failed_results,
        "temporal_blocked": temporal_blocked_results,
        "operation_min_lead_minutes": lead_floor,
        "manual_links_path": str(manual_links_path(eval_dir)),
    }


def resolve_bwin_search_status(
    *,
    ledger_row: dict[str, Any] | None = None,
    discovery_row: dict[str, Any] | None = None,
    manual_link: dict[str, Any] | None = None,
    preflight_match: dict[str, Any] | None = None,
    scanner_row: dict[str, Any] | None = None,
) -> str:
    """Mapea el estado comercial/Bwin a la taxonomía canónica sin equiparar incompleto con ausencia."""
    if manual_link:
        m_status = manual_link.get("identity_status")
        if m_status == "BWIN_IDENTITY_CONFLICT":
            return "BWIN_IDENTITY_CONFLICT"
        if m_status == "BWIN_MANUAL_LINK_VERIFIED":
            return "BWIN_MANUAL_LINK_VERIFIED"
        if m_status == "BWIN_MANUAL_LINK_PENDING_VALIDATION":
            return "BWIN_MANUAL_LINK_PENDING_VALIDATION"

    l_state = (ledger_row or {}).get("state") or ""
    l_reason = str((ledger_row or {}).get("reason") or "")
    if l_state == "IDENTIDAD_INCORRECTA" or (preflight_match or {}).get("status") == "BLOCKED":
        if any(tok in str(preflight_match.get("reasons", []) if preflight_match else l_reason).upper()
               for tok in ("IDENTITY", "MISMATCH", "CONFLICT", "TITLE", "EVENT_ID")):
            return "BWIN_IDENTITY_CONFLICT"
    if l_state in ("DIFERIDO_POR_PRESUPUESTO", "EXCLUIDO_POR_LIMITE_ANTERIOR"):
        return "BWIN_NOT_SCANNED_BUDGET"
    if (scanner_row or {}).get("status") == "VERIFIED" or (
        (discovery_row or {}).get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY" and (preflight_match or {}).get("snapshot")
    ):
        return "BWIN_VERIFIED"
    if (discovery_row or {}).get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY":
        return "BWIN_VERIFIED"
    d_status = (discovery_row or {}).get("status") or ""
    if d_status == "NOT_FOUND_IN_SCANNED_PAGES" or "NOT_FOUND_IN_SCANNED_PAGES" in l_reason:
        return "BWIN_NOT_FOUND_IN_SCANNED_PAGES"
    if d_status in ("EVENT_FOUND_TIME_CORROBORATED", "EVENT_FOUND_NEEDS_TIME_CONFIRMATION", "POSSIBLE_EVENT_FOUND", "EVENT_REQUIRES_REVIEW") or l_state in ("SIN_BWIN", "SIN_CUOTAS"):
        return "BWIN_SEARCH_INCOMPLETE"
    return "NOT_EVALUATED"
