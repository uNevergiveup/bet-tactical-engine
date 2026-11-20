#!/usr/bin/env python3
"""Ejecutor modular del pipeline PREMATCH (BET ENGINE V4 - P0).

Permite ejecutar cada etapa de forma independiente, reanudable y auditable,
manteniendo intactos los modelos matemáticos, calibración y gates temporales.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bwin_discovery as bd
import bwin_manual_links as bml
import discover_fixtures as df
import filter_major_leagues as fml
import prematch_prospective_trial as trial
from prematch_execution_control import require_external_confirmation

SCHEMA_VERSION = "1.0-prematch-steps"
DEFAULT_POLICY = "top-tier-and-senior-cups"


class StepError(RuntimeError):
    """Error controlado en una etapa del pipeline modular PREMATCH."""


def _now_utc(asof: str | None = None) -> datetime:
    if asof:
        parsed = datetime.fromisoformat(asof.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise StepError("ASOF_REQUIRES_TIMEZONE_OFFSET")
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_step_artifacts(
    eval_dir: Path,
    step: str,
    payload: dict[str, Any],
    md_lines: list[str],
) -> tuple[Path, Path]:
    steps_dir = eval_dir / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    json_path = steps_dir / f"{step}.json"
    md_path = steps_dir / f"{step}.md"
    _atomic_json(json_path, payload)
    md_path.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
    return json_path, md_path


def _resolve_eval_dir(args: argparse.Namespace, now_utc: datetime) -> tuple[Path, Path, datetime, datetime]:
    root = args.root.expanduser().resolve()
    cfg, _, _ = trial.load_window_config(root)
    _, start_t, end_t = trial.resolve_window(cfg, getattr(args, "window", None), getattr(args, "start", None), getattr(args, "end", None))
    day = df.parse_day(args.date) if getattr(args, "date", None) else trial.automatic_day(now_utc, start_t, end_t)
    begin, end = df.build_window(day, start_t, end_t)
    eval_dir = (
        args.eval_dir.expanduser().resolve()
        if getattr(args, "eval_dir", None)
        else df.output_folder(root, begin, end)
    )
    return root, eval_dir, begin, end


def _require_file(path: Path, stage_hint: str) -> Path:
    if not path.is_file():
        raise StepError(f"MISSING_REQUIRED_ARTIFACT:{path} (ejecuta primero `{stage_hint}`)")
    return path


def _load_major_doc(eval_dir: Path) -> tuple[Path, dict[str, Any]]:
    major_path = _require_file(eval_dir / "fixtures_major.json", "filter")
    doc = _read_json(major_path)
    if doc.get("schema_version") != fml.VERSION:
        raise StepError(f"INVALID_FIXTURES_MAJOR_SCHEMA:{major_path}")
    return major_path, doc


def _print_step_summary(
    step: str,
    status: str,
    reason: str,
    counts: dict[str, Any],
    paths: dict[str, Path | str | None],
) -> None:
    print(f"[{step}] status={status} | reason={reason}")
    if counts:
        print("  counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    for label, p in paths.items():
        if p:
            print(f"  {label}: {p}")


def _bwin_reconcile_records(
    eval_dir: Path,
    major_doc: dict[str, Any],
    now_utc: datetime,
    *,
    min_lead_minutes: int = 45,
) -> dict[str, Any]:
    """Reconcilia el estado Bwin de los fixtures ELIGIBLE a partir de artefactos locales."""
    eligible = list(major_doc.get("fixtures") or [])
    bwin_root = eval_dir / "prospective_trial/bwin"
    manual_store = bml.load_manual_links(eval_dir)
    manual_links = dict(manual_store.get("links") or {})

    auto_paths = sorted(
        [p for p in bwin_root.rglob("bwin_discovery.json") if not p.parent.name.startswith("manual_")],
        reverse=True,
    ) if bwin_root.is_dir() else []

    auto_by_fid: dict[int, tuple[Path, dict[str, Any]]] = {}
    for p in auto_paths:
        try:
            doc = _read_json(p)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        for row in doc.get("matches") or []:
            fid = row.get("fixture_id")
            if isinstance(fid, int) and fid not in auto_by_fid:
                auto_by_fid[fid] = (p, row)

    verified: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    pending_manual: list[dict[str, Any]] = []
    temporal_blocked: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    for fx in eligible:
        fid = int(fx["fixture_id"])
        ko = bd.local_kickoff(fx.get("kickoff_madrid") or fx.get("kickoff_utc"))
        ko_utc = ko.astimezone(timezone.utc) if ko else None
        lead_min = round((ko_utc - now_utc).total_seconds() / 60.0, 2) if ko_utc else None

        auto_hit = auto_by_fid.get(fid)
        auto_path = str(auto_hit[0]) if auto_hit else None
        auto_row = auto_hit[1] if auto_hit else None
        manual_entry = manual_links.get(str(fid))

        bwin_event_id: str | None = None
        bwin_url: str | None = None
        verification_source: str | None = None
        state = "PENDING_MANUAL"
        reason = "NO_BWIN_SCAN_OR_MANUAL_BINDING"
        candidates = (auto_row or {}).get("alternatives") or []

        if ko_utc is None or ko_utc <= now_utc:
            state = "TEMPORAL_BLOCKED"
            reason = "POST_KICKOFF_OR_MISSING_KICKOFF"
        elif lead_min is not None and lead_min < min_lead_minutes:
            state = "TEMPORAL_BLOCKED"
            reason = f"LEAD_BELOW_MIN_PREMATCH({lead_min}m<{min_lead_minutes}m)"

        auto_verified = bool(
            auto_row
            and auto_row.get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY"
            and auto_row.get("bwin_event_id")
            and auto_row.get("bwin_url")
        )
        manual_status = (manual_entry or {}).get("identity_status")
        manual_verified = bool(
            manual_entry
            and manual_status == "BWIN_MANUAL_LINK_VERIFIED"
            and manual_entry.get("extracted_event_id")
            and manual_entry.get("bwin_url")
        )

        if state != "TEMPORAL_BLOCKED":
            if manual_status == "BWIN_IDENTITY_CONFLICT":
                state = "CONFLICT"
                reason = ",".join(manual_entry.get("conflict_reasons") or ["BWIN_IDENTITY_CONFLICT"])
                bwin_event_id = manual_entry.get("extracted_event_id")
                bwin_url = manual_entry.get("bwin_url")
            elif auto_verified and manual_verified:
                if str(auto_row["bwin_event_id"]) != str(manual_entry["extracted_event_id"]):
                    state = "CONFLICT"
                    reason = f"AUTO_MANUAL_EVENT_MISMATCH:auto={auto_row['bwin_event_id']},manual={manual_entry['extracted_event_id']}"
                else:
                    state = "VERIFIED"
                    reason = "AUTO_AND_MANUAL_VERIFIED_AGREE"
                    bwin_event_id = str(auto_row["bwin_event_id"])
                    bwin_url = str(auto_row["bwin_url"])
                    verification_source = "AUTO+MANUAL"
            elif auto_verified:
                state = "VERIFIED"
                reason = "IDENTITY_VERIFIED_AUTOMATICALLY"
                bwin_event_id = str(auto_row["bwin_event_id"])
                bwin_url = str(auto_row["bwin_url"])
                verification_source = "AUTO"
            elif manual_verified:
                state = "VERIFIED"
                reason = "BWIN_MANUAL_LINK_VERIFIED"
                bwin_event_id = str(manual_entry["extracted_event_id"])
                bwin_url = str(manual_entry["bwin_url"])
                verification_source = "MANUAL"
            elif manual_entry and manual_status == "BWIN_MANUAL_LINK_PENDING_VALIDATION":
                state = "INCOMPLETE_SEARCH"
                reason = "MANUAL_LINK_PENDING_ONLINE_VERIFICATION"
                bwin_event_id = manual_entry.get("extracted_event_id")
                bwin_url = manual_entry.get("bwin_url")
            elif auto_row:
                auto_status = str(auto_row.get("status") or "")
                if auto_status in ("EVENT_FOUND_TIME_CORROBORATED", "EVENT_FOUND_PENDING_TIME_CHECK", "EVENT_VISIBLE_PENDING_CLASSIFICATION"):
                    state = "INCOMPLETE_SEARCH"
                    reason = auto_status
                    bwin_event_id = auto_row.get("bwin_event_id")
                    bwin_url = auto_row.get("bwin_url")
                elif auto_status in ("IDENTITY_CONFLICT", "BWIN_IDENTITY_CONFLICT", "GENDER_CONFLICT"):
                    state = "CONFLICT"
                    reason = auto_status
                else:
                    state = "PENDING_MANUAL"
                    reason = auto_status or "NOT_FOUND_IN_SCANNED_PAGES"

        rec = {
            "fixture_id": fid,
            "kickoff_madrid": fx.get("kickoff_madrid"),
            "kickoff_utc": fx.get("kickoff_utc"),
            "lead_minutes": lead_min,
            "country": fx.get("country"),
            "league": fx.get("league"),
            "league_id": fx.get("league_id"),
            "gender": fx.get("gender"),
            "home": fx.get("home"),
            "away": fx.get("away"),
            "bwin_state": state,
            "reason": reason,
            "bwin_event_id": bwin_event_id,
            "bwin_url": bwin_url,
            "verification_source": verification_source,
            "auto_discovery_path": auto_path,
            "candidates": candidates,
        }
        all_rows.append(rec)
        if state == "VERIFIED":
            verified.append(rec)
        elif state == "INCOMPLETE_SEARCH":
            incomplete.append(rec)
        elif state == "CONFLICT":
            conflicts.append(rec)
        elif state == "TEMPORAL_BLOCKED":
            temporal_blocked.append(rec)
        else:
            pending_manual.append(rec)

    return {
        "schema_version": "1.0-bwin-reconciliation",
        "generated_at_utc": now_utc.isoformat(),
        "eval_dir": str(eval_dir),
        "counts": {
            "eligible": len(eligible),
            "verified": len(verified),
            "incomplete_search": len(incomplete),
            "conflict": len(conflicts),
            "pending_manual": len(pending_manual),
            "temporal_blocked": len(temporal_blocked),
        },
        "verified": verified,
        "incomplete_search": incomplete,
        "conflicts": conflicts,
        "pending_manual": pending_manual,
        "temporal_blocked": temporal_blocked,
        "all": all_rows,
    }


def cmd_discover(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    root, eval_dir, begin, end = _resolve_eval_dir(args, now_utc)
    fixtures_json = eval_dir / "fixtures.json"

    if fixtures_json.is_file() and not args.refresh:
        report = _read_json(fixtures_json)
        status = "REUSED_EXISTING"
        reason = "EXISTING_FIXTURES_JSON_REUSED"
        sources = report.get("request_days") or []
    else:
        import requests
        from config.constants import API_KEY, FOOTBALL_BASE_URL, REQUEST_TIMEOUT

        if not API_KEY:
            raise StepError("FOOTBALL_API_KEY no configurada (revisa config/.env)")
        days = sorted({begin.date(), end.date()})
        responses: list[dict[str, Any]] = []
        sources = []
        with requests.Session() as session:
            session.headers.update({
                "x-apisports-key": API_KEY,
                "Accept": "application/json",
                "User-Agent": f"prematch-discovery/{df.VERSION}",
            })
            for d in days:
                raw, origin = df.request_fixtures(
                    session, root, d, API_KEY, FOOTBALL_BASE_URL, REQUEST_TIMEOUT, now_utc, args.refresh
                )
                responses.extend(raw)
                sources.append({"date": d.isoformat(), "source": origin, "returned": len(raw)})
        fixture_audit: list[dict[str, Any]] = []
        selected, excluded, warnings = df.filter_fixtures(responses, begin, end, now_utc, fixture_audit)
        league_keys = {(x["league_id"], x["country"], x["season"]) for x in selected}
        report = {
            "schema_version": df.VERSION,
            "generated_at_utc": now_utc.isoformat(),
            "source": "API-Football v3 /fixtures?date=YYYY-MM-DD&timezone=Europe/Madrid",
            "scope": "ALL_AVAILABLE_COMPETITIONS_UNFILTERED",
            "window": {
                "start_madrid": begin.isoformat() + " Europe/Madrid (hora civil)",
                "end_madrid": end.isoformat() + " Europe/Madrid (hora civil)",
                "timezone": "Europe/Madrid",
                "inclusive_end": True,
            },
            "request_days": sources,
            "counts": {
                "api_records_received": len(responses),
                "prematch_matches": len(selected),
                "competitions": len(league_keys),
                "excluded": excluded,
            },
            "fixtures": selected,
            "fixture_audit": fixture_audit,
            "warnings": warnings,
            "note": "Descubrimiento solamente; no se ha valorado calidad de datos, rentabilidad ni cuotas Bwin.",
        }
        df.write_outputs(eval_dir, report)
        status = "COMPLETED"
        reason = "DISCOVERED_FROM_API_OR_CACHE"

    counts = report.get("counts") or {}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "discover",
        "status": status,
        "reason": reason,
        "generated_at_utc": now_utc.isoformat(),
        "eval_dir": str(eval_dir),
        "counts": counts,
        "fixtures_path": str(fixtures_json),
        "fixtures_sha256": _sha256_file(fixtures_json),
    }
    md_lines = [
        "# Paso 1: Discover (API-Football)",
        "",
        f"- Estado: **{status}** ({reason})",
        f"- Ventana Madrid: `{begin.isoformat()}` → `{end.isoformat()}`",
        f"- PREMATCH encontrados: **{counts.get('prematch_matches', 0)}** | Competiciones: **{counts.get('competitions', 0)}** | Excluidos ventana/estado: **{counts.get('excluded', 0)}**",
        f"- Artefacto: `{fixtures_json}` (`{_sha256_file(fixtures_json)}`)",
        "",
        "Sin descarga de microestadísticas ni 20 partidos históricos en esta etapa.",
    ]
    step_json, step_md = _write_step_artifacts(eval_dir, "discover", payload, md_lines)
    _print_step_summary("discover", status, reason, counts, {"fixtures_json": fixtures_json, "step_json": step_json, "step_md": step_md})
    return 0


def cmd_filter(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    fixtures_path = _require_file(eval_dir / "fixtures.json", "discover")
    raw_bytes = fixtures_path.read_bytes()
    data = json.loads(raw_bytes)
    policy = args.policy or DEFAULT_POLICY
    result = fml.filter_document(data, raw_bytes, policy=policy)
    fml.save_outputs(result, eval_dir)

    counts = result["counts"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "filter",
        "status": "COMPLETED",
        "reason": f"POLICY_{policy.upper()}",
        "generated_at_utc": now_utc.isoformat(),
        "eval_dir": str(eval_dir),
        "policy": policy,
        "counts": counts,
        "fixtures_major_path": str(eval_dir / "fixtures_major.json"),
        "fixtures_major_sha256": _sha256_file(eval_dir / "fixtures_major.json"),
        "review_path": str(eval_dir / "league_filter_review.json"),
    }

    md_lines = [
        f"# Paso 2: Filtro Deportivo (`{policy}`)",
        "",
        f"- Entrada: **{counts['input']}**",
        f"- ELIGIBLE: **{counts['eligible']}** (M={counts['eligible_male']}, F={counts['eligible_female']}, UNKNOWN={counts['eligible_gender_unknown']})",
        f"- REVIEW: **{counts['review']}**",
        f"- EXCLUDED: **{counts['excluded']}**",
        "",
        "## Todos los partidos y razones",
        "",
        "| Estado | Fixture ID | Hora Madrid | País | Liga | Género | Partido | Razón |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for bucket_name, rows in (("ELIGIBLE", result["fixtures"]), ("REVIEW", result["review"]), ("EXCLUDED", result["excluded"])):
        for r in rows:
            esc = lambda v: str(v or "").replace("|", "\\|")
            md_lines.append(
                f"| {bucket_name} | `{r['fixture_id']}` | {esc(r.get('kickoff_madrid'))} | {esc(r.get('country'))} | "
                f"{esc(r.get('league'))} | {esc(r.get('gender'))} | {esc(r.get('home'))} – {esc(r.get('away'))} | `{esc(r.get('filter_reason'))}` |"
            )

    step_json, step_md = _write_step_artifacts(eval_dir, "filter", payload, md_lines)
    _print_step_summary(
        "filter",
        "COMPLETED",
        f"POLICY_{policy.upper()}",
        {"input": counts["input"], "eligible": counts["eligible"], "review": counts["review"], "excluded": counts["excluded"]},
        {"fixtures_major": eval_dir / "fixtures_major.json", "step_json": step_json, "step_md": step_md},
    )
    return 0


def cmd_bwin_scan(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)

    candidates = list(major_doc.get("fixtures") or [])
    if args.include_review:
        candidates.extend(major_doc.get("review") or [])
    if args.fixture_id:
        wanted = set(args.fixture_id)
        candidates = [f for f in candidates if int(f["fixture_id"]) in wanted]

    bwin_dir = eval_dir / "prospective_trial/bwin"
    bwin_dir.mkdir(parents=True, exist_ok=True)
    scan_status = "LOCAL_RECONCILED"
    scan_reason = "LOCAL_ARTIFACTS_AND_CATALOGS"

    if args.catalog_snapshot and not args.confirm_external:
        catalog = bd.load_local_catalog_snapshots(args.catalog_snapshot)
        results = [bd.summarize(fx, bd.match_catalog(fx, catalog), None, args.timeout, inspect=False) for fx in candidates]
        bd.write_reports(bwin_dir, {
            "schema_version": "0.2-bwin-discovery",
            "generated_at_utc": now_utc.isoformat(),
            "source": "LOCAL_CATALOG_SNAPSHOT",
            "catalog_observed": catalog,
            "pages": [],
            "matches": results,
        })
        scan_status = "COMPLETED_OFFLINE_SNAPSHOT"
        scan_reason = "MATCHED_AGAINST_LOCAL_CATALOG_SNAPSHOT"
    elif args.confirm_external and not args.no_browser:
        require_external_confirmation(True)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless, args=["--disable-blink-features=AutomationControlled"])
            try:
                catalog, pages = bd.fetch_catalog(browser, bd.BASE, list(bd.CALENDAR), args.max_scrolls, args.timeout)
                if args.catalog_snapshot:
                    archived = bd.load_local_catalog_snapshots(args.catalog_snapshot)
                    byid = {e["bwin_event_id"]: e for e in archived}
                    byid.update({e["bwin_event_id"]: e for e in catalog})
                    catalog = list(byid.values())
                catalog, extended_pages, _ = bd.extend_catalog_for_missing(
                    browser, bd.BASE, catalog, candidates, args.max_country_pages, args.max_scrolls, args.timeout
                )
                pages.extend(extended_pages)
                results = [
                    bd.summarize(fx, bd.match_catalog(fx, catalog), browser, args.timeout, inspect=True)
                    for fx in candidates
                ]
            finally:
                browser.close()
        bd.write_reports(bwin_dir, {
            "schema_version": "0.5-bwin-discovery-v3",
            "generated_at_utc": now_utc.isoformat(),
            "catalog_observed": catalog,
            "pages": pages,
            "matches": results,
        })
        scan_status = "COMPLETED_ONLINE"
        scan_reason = "BWIN_ONLINE_DISCOVERY_AND_INSPECTION"

    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc, min_lead_minutes=args.min_lead)
    recon_path = bwin_dir / "bwin_scan_reconciliation.json"
    _atomic_json(recon_path, recon)

    counts = recon["counts"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "bwin-scan",
        "status": scan_status,
        "reason": scan_reason,
        "generated_at_utc": now_utc.isoformat(),
        "eval_dir": str(eval_dir),
        "counts": counts,
        "reconciliation_path": str(recon_path),
    }
    md_lines = [
        "# Paso 3: Bwin Scan y Reconciliación",
        "",
        f"- Estado: **{scan_status}** ({scan_reason})",
        f"- Elegibles: **{counts['eligible']}** | Verificados: **{counts['verified']}** | Búsqueda incompleta: **{counts['incomplete_search']}** | Conflictos: **{counts['conflict']}** | Pendientes manuales: **{counts['pending_manual']}** | Bloqueo temporal: **{counts['temporal_blocked']}**",
        "",
        "| Fixture ID | Partido | Estado Bwin | Event ID | Razón |",
        "|---:|---|---|---:|---|",
    ]
    for r in recon["all"]:
        md_lines.append(
            f"| `{r['fixture_id']}` | {r['home']} – {r['away']} | `{r['bwin_state']}` | `{r.get('bwin_event_id') or '—'}` | {r['reason']} |"
        )
    step_json, step_md = _write_step_artifacts(eval_dir, "bwin_scan", payload, md_lines)
    _print_step_summary("bwin-scan", scan_status, scan_reason, counts, {"reconciliation": recon_path, "step_json": step_json, "step_md": step_md})
    return 0


def cmd_bwin_list(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc)
    queue_path = eval_dir / "prospective_trial/bwin/pending_manual_bwin_queue.json"
    _atomic_json(queue_path, recon)

    pending_all = recon["pending_manual"] + recon["incomplete_search"] + recon["conflicts"]
    md_lines = [
        "# Cola de Reconciliación Manual Bwin",
        "",
        f"- Verificados: **{recon['counts']['verified']}**",
        f"- Pendientes manuales: **{recon['counts']['pending_manual']}** | Búsqueda incompleta: **{recon['counts']['incomplete_search']}** | Conflictos: **{recon['counts']['conflict']}**",
        "",
        "## Partidos pendientes / en conflicto",
        "",
        "| Fixture ID | Hora Madrid | País / Liga | Género | Partido | Estado | Razón |",
        "|---:|---|---|---|---|---|---|",
    ]
    for r in pending_all:
        md_lines.append(
            f"| `{r['fixture_id']}` | {r.get('kickoff_madrid')} | {r.get('country')} / {r.get('league')} | {r.get('gender')} | {r['home']} – {r['away']} | `{r['bwin_state']}` | {r['reason']} |"
        )
    if args.include_verified and recon["verified"]:
        md_lines.extend(["", "## Verificados", "", "| Fixture ID | Partido | Event ID | Fuente |", "|---:|---|---:|---|"])
        for r in recon["verified"]:
            md_lines.append(f"| `{r['fixture_id']}` | {r['home']} – {r['away']} | `{r['bwin_event_id']}` | {r.get('verification_source')} |")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "bwin-list",
        "status": "COMPLETED",
        "reason": "QUEUE_LISTED",
        "generated_at_utc": now_utc.isoformat(),
        "counts": recon["counts"],
        "queue_path": str(queue_path),
    }
    step_json, step_md = _write_step_artifacts(eval_dir, "bwin_list", payload, md_lines)
    _print_step_summary("bwin-list", "COMPLETED", "QUEUE_LISTED", recon["counts"], {"queue_json": queue_path, "step_json": step_json, "step_md": step_md})
    return 0


def cmd_bwin_bind(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)
    major_by_id = {int(f["fixture_id"]): f for f in (major_doc.get("fixtures") or []) + (major_doc.get("review") or [])}

    raw_bindings: list[tuple[int, str]] = []
    if args.fixture_id is not None:
        target = args.url or args.event_id
        if not target:
            raise StepError("BWIN_BIND_REQUIRES_URL_OR_EVENT_ID")
        raw_bindings.append((int(args.fixture_id), str(target).strip()))
    for spec in args.spec or []:
        raw_bindings.append(bml.parse_manual_bwin_spec(spec))
    if args.file:
        raw_bindings.extend(bml.load_manual_bwin_file(args.file))
    if not raw_bindings:
        raise StepError("BWIN_BIND_NO_INPUTS_PROVIDED")

    if args.confirm_external:
        require_external_confirmation(True)

    resolved_bindings: list[tuple[int, str]] = []
    resolutions: list[dict[str, Any]] = []
    for fid, raw_target in raw_bindings:
        fx = major_by_id.get(fid)
        resolved = bml.resolve_manual_bwin_target(
            eval_dir,
            raw_target,
            fixture=fx,
            catalog_snapshots=args.catalog_snapshot or None,
            resolve_online=bool(args.confirm_external),
            headless=args.headless,
            timeout=args.timeout,
        )
        resolved_bindings.append((fid, resolved["bwin_url"]))
        resolutions.append({"fixture_id": fid, **resolved})

    result = bml.register_manual_bwin_urls(
        eval_dir,
        resolved_bindings,
        now_utc=now_utc,
        min_lead_minutes=args.min_lead,
    )
    counts = {
        "registered": len(result["registered"]),
        "conflicts": len(result["conflicts"]),
        "temporal_blocked": len(result["temporal_blocked"]),
    }
    status = "COMPLETED" if not result["conflicts"] and not result["temporal_blocked"] else "PARTIAL_OR_CONFLICT"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "bwin-bind",
        "status": status,
        "reason": "MANUAL_LINKS_PROCESSED",
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "resolutions": resolutions,
        "result": result,
    }
    md_lines = [
        "# Paso 4: Bwin Bind Manual",
        "",
        f"- Estado: **{status}**",
        f"- Registrados/pendientes verificación: **{counts['registered']}** | Conflictos: **{counts['conflicts']}** | Bloqueo temporal: **{counts['temporal_blocked']}**",
        f"- Archivo manual: `{result['manual_links_path']}`",
    ]
    step_json, step_md = _write_step_artifacts(eval_dir, "bwin_bind", payload, md_lines)
    _print_step_summary("bwin-bind", status, "MANUAL_LINKS_PROCESSED", counts, {"manual_links": result["manual_links_path"], "step_json": step_json, "step_md": step_md})
    return 0 if not result["conflicts"] else 2


def cmd_bwin_verify(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _require_file(bml.manual_links_path(eval_dir), "bwin-bind")
    require_external_confirmation(args.confirm_external)

    result = bml.verify_manual_bwin_links_online(
        eval_dir,
        fixture_ids=args.fixture_id or None,
        headless=args.headless,
        timeout=args.timeout,
        min_lead_minutes=args.min_lead,
        now_utc=now_utc,
    )
    counts = {
        "verified": len(result["verified"]),
        "pending": len(result["pending"]),
        "failed": len(result["failed"]),
        "temporal_blocked": len(result["temporal_blocked"]),
    }
    status = "COMPLETED" if not result["failed"] else "CONFLICTS_DETECTED"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "bwin-verify",
        "status": status,
        "reason": "ONLINE_MANUAL_VERIFICATION",
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "result": result,
    }
    md_lines = [
        "# Paso 5: Bwin Verify Online",
        "",
        f"- Verificados: **{counts['verified']}** | Pendientes: **{counts['pending']}** | Fallidos/Conflictos: **{counts['failed']}** | Bloqueo temporal: **{counts['temporal_blocked']}**",
        f"- Archivo manual: `{result['manual_links_path']}`",
    ]
    step_json, step_md = _write_step_artifacts(eval_dir, "bwin_verify", payload, md_lines)
    _print_step_summary("bwin-verify", status, "ONLINE_MANUAL_VERIFICATION", counts, {"manual_links": result["manual_links_path"], "step_json": step_json, "step_md": step_md})
    return 0


def cmd_stats_fetch(args: argparse.Namespace) -> int:
    import preselect_matches as pm

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)

    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc)
    verified_ids = {int(r["fixture_id"]) for r in recon["verified"]}

    if args.research_without_bwin:
        target_ids = set(args.fixture_id) if args.fixture_id else {int(f["fixture_id"]) for f in major_doc.get("fixtures") or []}
        out_folder = eval_dir / "prospective_trial/research_only"
        route_tag = "RESEARCH_WITHOUT_BWIN"
    else:
        target_ids = verified_ids & set(args.fixture_id) if args.fixture_id else verified_ids
        out_folder = eval_dir / "prospective_trial/enriched"
        route_tag = "BWIN_ACCREDITED_COMMERCIAL"

    if not target_ids:
        raise StepError(
            "NO_ELIGIBLE_FIXTURES_FOR_STATS_FETCH "
            "(ningún fixture con Bwin verificado; ejecuta `bwin-scan`/`bwin-bind`/`bwin-verify` o usa `--research-without-bwin`)"
        )

    existing_path = out_folder / "preselection.json"
    existing_doc = _read_json(existing_path) if existing_path.is_file() and not args.refresh else None
    already_ids = {int(m["fixture_id"]) for m in (existing_doc or {}).get("matches", [])}
    missing_ids = sorted(target_ids - already_ids)

    if not missing_ids and existing_doc is not None:
        report = existing_doc
        status = "REUSED_EXISTING"
        reason = "ALL_TARGET_FIXTURES_ALREADY_FETCHED"
    else:
        from config.constants import API_KEY, FOOTBALL_BASE_URL, REQUEST_TIMEOUT

        if not API_KEY:
            raise StepError("FOOTBALL_API_KEY no configurada (revisa config/.env)")
        client = pm.ApiFootball(
            API_KEY,
            FOOTBALL_BASE_URL,
            root,
            timeout=REQUEST_TIMEOUT,
            refresh=args.refresh,
            asof=now_utc,
        )
        try:
            fresh = pm.run_evaluator(
                major_doc,
                client,
                now_utc,
                history_limit=args.history,
                detail_limit=args.details,
                max_matches=args.max_matches,
                history_seasons=args.history_seasons,
                mode=args.mode,
                fixture_ids=set(missing_ids),
            )
        finally:
            client.close()
        report = pm.merge_preselection_reports(existing_doc, fresh) if existing_doc else fresh
        for m in report.get("matches", []):
            fid = int(m["fixture_id"])
            m["bwin_accredited"] = fid in verified_ids
            m["research_only_without_bwin"] = fid not in verified_ids
            m["commercial_eligible"] = (fid in verified_ids) and not args.research_without_bwin
        report["route_mode"] = route_tag
        pm.save_report(report, out_folder)
        status = "COMPLETED"
        reason = f"FETCHED_{len(missing_ids)}_FIXTURES"

    family_cov = {
        str(m["fixture_id"]): {
            fam: info.get("status")
            for fam, info in (m.get("family_evidence") or {}).items()
            if isinstance(info, dict)
        }
        for m in report.get("matches", [])
    }
    counts = {
        "matches_in_report": len(report.get("matches", [])),
        "newly_fetched": len(missing_ids),
        "bwin_verified_pool": len(verified_ids),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "stats-fetch",
        "status": status,
        "reason": reason,
        "route_mode": route_tag,
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "family_coverage": family_cov,
        "preselection_path": str(existing_path),
        "preselection_sha256": _sha256_file(existing_path),
    }
    md_lines = [
        "# Paso 6: Stats Fetch (API-Football)",
        "",
        f"- Estado: **{status}** ({reason}) | Ruta: `{route_tag}`",
        f"- Partidos en `preselection.json`: **{counts['matches_in_report']}**",
        f"- Artefacto: `{existing_path}`",
    ]
    step_json, step_md = _write_step_artifacts(eval_dir, "stats_fetch", payload, md_lines)
    _print_step_summary("stats-fetch", status, reason, counts, {"preselection": existing_path, "step_json": step_json, "step_md": step_md})
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    import prematch_tactical_signal_engine as ptse

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    pre_path = _require_file(eval_dir / "prospective_trial/enriched/preselection.json", "stats-fetch")
    tactical_out = eval_dir / "prospective_trial/tactical"
    profiles_path = args.profiles or (root / "config/tactical_profiles/profiles.json")
    pattern_path = args.pattern if args.pattern and args.pattern.is_file() else None

    preflights = tuple((eval_dir / "prospective_trial/preflight").rglob("market_preflight.json"))
    doc = ptse.run_engine(
        pre_path,
        pattern_path,
        tactical_out,
        root=root,
        preflights=preflights,
        profiles=profiles_path if profiles_path.is_file() else None,
        asof=now_utc.astimezone(trial.TZ),
        use_cache=not args.no_cache,
    )

    signal_summary: dict[str, Any] = {}
    for fid_str, fx_res in (doc.get("fixtures") or {}).items():
        sigs = fx_res.get("signals") or []
        supported = [s for s in sigs if s.get("status") == "SUPPORTED_DESCRIPTIVE_SIGNAL"]
        contradictory = fx_res.get("contradictions") or []
        missing = [s for s in sigs if s.get("status") != "SUPPORTED_DESCRIPTIVE_SIGNAL"]
        signal_summary[fid_str] = {
            "status": fx_res.get("status"),
            "supported_signals": len(supported),
            "signals_without_bwin_market": sum(1 for s in supported if not s.get("markets")),
            "contradictions": len(contradictory),
            "missing_or_insufficient": len(missing),
            "coverage_summary": fx_res.get("coverage_summary"),
        }

    counts = {
        "fixtures": len(doc.get("fixtures") or {}),
        "supported_total": sum(v["supported_signals"] for v in signal_summary.values()),
        "without_bwin_market": sum(v["signals_without_bwin_market"] for v in signal_summary.values()),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "patterns",
        "status": "COMPLETED",
        "reason": "DESCRIPTIVE_PATTERNS_EVALUATED",
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "signal_summary": signal_summary,
        "tactical_json": str(tactical_out / "tactical_signal_engine.json"),
        "tactical_md": str(tactical_out / "TACTICAL_SELECTION_REPORT.md"),
    }
    md_lines = [
        "# Paso 7: Patrones Estadísticos y Señales Descriptivas",
        "",
        f"- Partidos evaluados: **{counts['fixtures']}** | Señales sustentadas: **{counts['supported_total']}** | Sin mercado Bwin: **{counts['without_bwin_market']}**",
        f"- Reporte motor: `{tactical_out / 'TACTICAL_SELECTION_REPORT.md'}`",
        "",
        "Nota: Los candidatos descriptivos se conservan aunque no tengan probabilidad calibrada ni oferta Bwin.",
    ]
    step_json, step_md = _write_step_artifacts(eval_dir, "patterns", payload, md_lines)
    _print_step_summary("patterns", "COMPLETED", "DESCRIPTIVE_PATTERNS_EVALUATED", counts, {"tactical_json": tactical_out / "tactical_signal_engine.json", "step_json": step_json, "step_md": step_md})
    return 0


def cmd_odds_capture(args: argparse.Namespace) -> int:
    from bet_engine.orchestration.shadow_pipeline import audit_bwin_catalog_txt

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc, min_lead_minutes=args.min_lead)

    verified = recon["verified"]
    if args.fixture_id:
        wanted = set(args.fixture_id)
        verified = [r for r in verified if int(r["fixture_id"]) in wanted]
    if args.event_id:
        verified = [r for r in verified if str(r.get("bwin_event_id")) == str(args.event_id)]
    if not verified:
        raise StepError("NO_VERIFIED_BWIN_FIXTURES_FOR_ODDS_CAPTURE")

    captured: list[dict[str, Any]] = []
    for rec in verified:
        eid = str(rec["bwin_event_id"])
        fid = int(rec["fixture_id"])
        inputs_dir = root / "outputs/matches" / eid / "inputs"
        canonical_txt = inputs_dir / f"cuotas_bwin_{eid}.txt"
        capture_dir = eval_dir / "prospective_trial/captures" / eid
        capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
        target_txt: Path | None = None
        capture_mode = "MISSING"

        if args.snapshot_file:
            src = Path(args.snapshot_file).expanduser().resolve()
            if not src.is_file():
                raise StepError(f"SNAPSHOT_FILE_NOT_FOUND:{src}")
            if canonical_txt.is_file():
                target_txt = capture_dir / f"cuotas_bwin_{eid}_{stamp}.txt"
                target_txt.write_bytes(src.read_bytes())
                capture_mode = "COPIED_TO_IMMUTABLE_CAPTURE_CANONICAL_PRESERVED"
            else:
                inputs_dir.mkdir(parents=True, exist_ok=True)
                canonical_txt.write_bytes(src.read_bytes())
                target_txt = canonical_txt
                capture_mode = "COPIED_FROM_SNAPSHOT_FILE"
        elif args.confirm_external:
            require_external_confirmation(True)
            import scraper_bwin as sb

            dest = capture_dir / f"cuotas_bwin_{eid}_{stamp}.txt" if canonical_txt.is_file() else canonical_txt
            dest.parent.mkdir(parents=True, exist_ok=True)
            sb.scrape_event(rec["bwin_url"], dest, headless=args.headless, timeout=args.timeout)
            target_txt = dest
            capture_mode = "SCRAPED_LIVE_BWIN"
        elif canonical_txt.is_file():
            target_txt = canonical_txt
            capture_mode = "EXISTING_LOCAL_SNAPSHOT"
        else:
            raise StepError(f"ODDS_SNAPSHOT_MISSING_FOR_EVENT:{eid} (usa `--confirm-external` o `--snapshot-file`)")

        audit = audit_bwin_catalog_txt(target_txt, 0)
        captured.append({
            "fixture_id": fid,
            "bwin_event_id": eid,
            "snapshot_path": str(target_txt),
            "snapshot_sha256": _sha256_file(target_txt),
            "capture_mode": capture_mode,
            "captured_at_utc": now_utc.isoformat(),
            "total_markets": audit.get("total_markets_in_txt"),
            "total_selections": audit.get("total_selections_in_txt"),
        })

    counts = {"events_captured": len(captured)}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "odds-capture",
        "status": "COMPLETED",
        "reason": "BWIN_ODDS_SNAPSHOT_CAPTURED",
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "captures": captured,
    }
    md_lines = [
        "# Paso 8: Captura de Cuotas Bwin",
        "",
        f"- Eventos capturados: **{len(captured)}**",
        "",
        "| Fixture ID | Event ID | Modo | Selecciones | Snapshot |",
        "|---:|---:|---|---:|---|",
    ]
    for c in captured:
        md_lines.append(f"| `{c['fixture_id']}` | `{c['bwin_event_id']}` | {c['capture_mode']} | {c.get('total_selections')} | `{c['snapshot_path']}` |")
    step_json, step_md = _write_step_artifacts(eval_dir, "odds_capture", payload, md_lines)
    _print_step_summary("odds-capture", "COMPLETED", "BWIN_ODDS_SNAPSHOT_CAPTURED", counts, {"step_json": step_json, "step_md": step_md})
    return 0


def cmd_reasoning_package(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc)

    target_rec: dict[str, Any] | None = None
    for r in recon["all"]:
        if args.fixture_id and int(r["fixture_id"]) == int(args.fixture_id):
            target_rec = r
            break
        if args.event_id and str(r.get("bwin_event_id")) == str(args.event_id):
            target_rec = r
            break
    if not target_rec:
        raise StepError("TARGET_FIXTURE_OR_EVENT_NOT_FOUND_IN_EVAL_DIR")

    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    if not eid and not args.research_without_bwin:
        raise StepError(f"FIXTURE_WITHOUT_VERIFIED_BWIN_EVENT_ID:{fid} (usa `--research-without-bwin` para expediente solo investigación)")

    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    match_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id)
    match_dir.mkdir(parents=True, exist_ok=True)

    pre_path = eval_dir / ("prospective_trial/research_only/preselection.json" if args.research_without_bwin and (eval_dir / "prospective_trial/research_only/preselection.json").is_file() else "prospective_trial/enriched/preselection.json")
    _require_file(pre_path, "stats-fetch")
    pre_doc = _read_json(pre_path)
    pre_match = next((m for m in pre_doc.get("matches", []) if int(m["fixture_id"]) == fid), None)
    if not pre_match:
        raise StepError(f"FIXTURE_NOT_IN_PRESELECTION:{fid}")

    tactical_path = eval_dir / "prospective_trial/tactical/tactical_signal_engine.json"
    tactical_doc = _read_json(tactical_path) if tactical_path.is_file() else {}
    tactical_fx = (tactical_doc.get("fixtures") or {}).get(str(fid)) or {}


    odds_txt = None
    capture_manifest = eval_dir / "steps" / "odds_capture.json"

    if eid and capture_manifest.is_file():
        capture_doc = _read_json(capture_manifest)

        matching = [
            c for c in capture_doc.get("captures", [])
            if int(c.get("fixture_id", -1)) == fid
               and str(c.get("bwin_event_id")) == str(eid)
        ]

        if len(matching) != 1:
            raise StepError(
                f"EXPECTED_ONE_ODDS_CAPTURE:{fid}:{eid}:"
                f"found={len(matching)}"
            )

        capture = matching[0]
        odds_txt = Path(capture["snapshot_path"]).resolve()

        allowed_dir = (
                eval_dir / "prospective_trial" / "captures" / str(eid)
        ).resolve()

        if not odds_txt.is_relative_to(allowed_dir):
            raise StepError("ODDS_SNAPSHOT_OUTSIDE_EVALUATION")

        if not odds_txt.is_file():
            raise StepError("ODDS_SNAPSHOT_NOT_FOUND")

        if _sha256_file(odds_txt) != capture["snapshot_sha256"]:
            raise StepError("ODDS_SNAPSHOT_HASH_MISMATCH")

    odds_exists = odds_txt is not None

    sources: dict[str, Any] = {
        "fixtures_major": {"path": str(eval_dir / "fixtures_major.json"), "sha256": _sha256_file(eval_dir / "fixtures_major.json")},
        "preselection": {"path": str(pre_path), "sha256": _sha256_file(pre_path)},
        "tactical_signals": {"path": str(tactical_path), "sha256": _sha256_file(tactical_path)} if tactical_path.is_file() else None,
        "odds_bwin": {"path": str(odds_txt), "sha256": _sha256_file(odds_txt)} if odds_exists else None,
    }

    ko_madrid = target_rec.get("kickoff_madrid")
    ko_utc = target_rec.get("kickoff_utc")

    if args.import_qualitative:
        qpath = Path(args.import_qualitative).expanduser().resolve()
        if not qpath.is_file():
            raise StepError(f"QUALITATIVE_ARTIFACT_NOT_FOUND:{qpath}")
        qdoc = _read_json(qpath)
        if not isinstance(qdoc, dict):
            raise StepError("QUALITATIVE_ARTIFACT_INVALID_SCHEMA:expected JSON object")
        q_fid = qdoc.get("fixture_id")
        q_eid = qdoc.get("event_id")
        if q_fid is not None and int(q_fid) != fid:
            raise StepError(f"QUALITATIVE_IDENTITY_MISMATCH:fixture_id={q_fid}!={fid}")
        if eid and q_eid is not None and str(q_eid) != str(eid):
            raise StepError(f"QUALITATIVE_IDENTITY_MISMATCH:event_id={q_eid}!={eid}")
        q_ts = qdoc.get("analysis_cutoff_utc") or qdoc.get("generated_at_utc")
        if not q_ts:
            raise StepError("QUALITATIVE_MISSING_TIMESTAMP")
        q_dt = bd.local_kickoff(q_ts)
        ko_dt = bd.local_kickoff(ko_utc or ko_madrid)
        if q_dt is None or (ko_dt is not None and q_dt.astimezone(timezone.utc) > ko_dt.astimezone(timezone.utc)):
            raise StepError("QUALITATIVE_TIMESTAMP_AFTER_KICKOFF_OR_INVALID")
        if not qdoc.get("provenance") and not qdoc.get("sources"):
            raise StepError("QUALITATIVE_MISSING_PROVENANCE")
        if qdoc.get("actionable_bet") is True or qdoc.get("calibrated_p_model") is not None:
            raise StepError("QUALITATIVE_CANNOT_AUTHORIZE_BET_OR_INVENT_P_MODEL")
        dest_q = match_dir / "qualitative_imported.json"
        _atomic_json(dest_q, {
            **qdoc,
            "imported_at_utc": now_utc.isoformat(),
            "imported_source_sha256": _sha256_file(qpath),
            "verified_fixture_id": fid,
            "verified_event_id": eid,
        })
        payload = {
            "schema_version": SCHEMA_VERSION,
            "step": "reasoning-package",
            "status": "COMPLETED",
            "reason": "QUALITATIVE_ARTIFACT_IMPORTED",
            "generated_at_utc": now_utc.isoformat(),
            "counts": {"imported": 1},
            "imported_path": str(dest_q),
        }
        step_json, step_md = _write_step_artifacts(eval_dir, "reasoning_package", payload, [f"# Importación Cualitativa · {dossier_id}", "", f"- Destino: `{dest_q}`"])
        _print_step_summary("reasoning-package", "COMPLETED", "QUALITATIVE_ARTIFACT_IMPORTED", payload["counts"], {"imported_path": dest_q, "step_json": step_json})
        return 0

    package_json_path = match_dir / "codex_reasoning_package.json"
    codex_md_path = match_dir / "codex_reasoning_input.md"
    ko_madrid = target_rec.get("kickoff_madrid")
    ko_utc = target_rec.get("kickoff_utc")

    package = {
        "schema_version": "1.0-codex-reasoning-package",
        "generated_at_utc": now_utc.isoformat(),
        "identity": {
            "event_id": eid,
            "dossier_id": dossier_id,
            "fixture_id": fid,
            "home": target_rec["home"],
            "away": target_rec["away"],
            "country": target_rec.get("country"),
            "league": target_rec.get("league"),
            "gender": target_rec.get("gender"),
            "bwin_state": target_rec.get("bwin_state"),
            "bwin_url": target_rec.get("bwin_url"),
            "kickoff_madrid": ko_madrid,
            "kickoff_utc": ko_utc,
        },
        "family_coverage": pre_match.get("family_evidence") or {},
        "quantitative_patterns": tactical_fx.get("signals") or [],
        "contradictions": tactical_fx.get("contradictions") or [],
        "lineups_and_coaches": {
            "lineups_status": "NO_VERIFICADO",
            "coaches_status": "NO_VERIFICADO",
            "note": "Procedencia explícita requerida antes de cualquier inferencia de XI o entrenador.",
        },
        "bwin_markets": {
            "captured": odds_exists,
            "snapshot_path": str(odds_txt) if odds_exists else None,
            "snapshot_sha256": _sha256_file(odds_txt) if odds_exists else None,
        },
        "warnings": tactical_fx.get("block_reasons") or [],
        "sources": sources,
        "llm_constraints": {
            "auto_llm_invoked": False,
            "cannot_invent_calibrated_probabilities": True,
            "cannot_override_engine_or_authorize_bets": True,
        },
    }
    _atomic_json(package_json_path, package)

    md_lines = [
        f"# Expediente Codex PREMATCH · {dossier_id}",
        "",
        "## 1. Identidad y Timestamps",
        f"- Fixture ID: `{fid}` | Bwin Event ID: `{eid or 'SIN_BWIN_SOLO_INVESTIGACION'}`",
        f"- Partido: **{target_rec['home']} – {target_rec['away']}** ({target_rec.get('country')} / {target_rec.get('league')}, género `{target_rec.get('gender')}`)",
        f"- Kickoff Madrid: `{ko_madrid}` | Kickoff UTC: `{ko_utc}`",
        f"- Generación del paquete (UTC): `{now_utc.isoformat()}`",
        f"- Estado Bwin: `{target_rec.get('bwin_state')}` ({target_rec.get('reason')})",
        "",
        "## 2. Estadísticas y Cobertura por Familia",
    ]

    for sig in tactical_fx.get("signals") or []:
        own = sig.get("own") or {}
        opp = sig.get("opponent_conceded") or {}

        md_lines.append(
            f"- {sig.get('metric')} / {sig.get('team_side')}: "
            f"producción n={own.get('n')}, "
            f"cobertura={own.get('historical_effective_coverage_pct')}%; "
            f"rival concede n={opp.get('n')}, "
            f"cobertura={opp.get('historical_effective_coverage_pct')}%"
        )

    md_lines.extend(["", "## 3. Patrones Cuantitativos Encontrados"])

    for sig in tactical_fx.get("signals") or []:
        own = sig.get("own") or {}
        opp = sig.get("opponent_conceded") or {}

        side = sig.get("team_side")
        team = (
            target_rec["home"] if side == "home"
            else target_rec["away"] if side == "away"
            else "NO_IDENTIFICADO"
        )

        market_status = (
                sig.get("markets")
                or "PENDIENTE_DE_CRUCE_CON_BWIN"
        )

        md_lines.append(
            f"- [{sig.get('status')}] {team} · "
            f"{sig.get('metric')} · "
            f"produce={own.get('mean')} (n={own.get('n')}) · "
            f"rival_concede={opp.get('mean')} (n={opp.get('n')}) · "
            f"robustez={sig.get('priority_score')} · "
            f"mercado={market_status}"
        )

    if not tactical_fx.get("signals"):
        md_lines.append("- Sin señales cuantitativas registradas.")
    md_lines.extend([
        "",
        "## 4. XI y Entrenadores (Procedencia)",
        "- XI: `NO_VERIFICADO` | Entrenadores: `NO_VERIFICADO` (no asumir sin fuente oficial/prepartido acreditada).",
        "",
        "## 5. Mercados Bwin Capturados",
        f"- Captura disponible: `{odds_exists}`" + (f" (`{odds_txt}`, SHA-256 `{_sha256_file(odds_txt)}`)" if odds_exists else " (`PRECIO_PENDIENTE_DE_VERIFICACION`)"),
        "",
        "## 6. Advertencias, Carencias y Contradicciones",
        f"- Contradicciones: {tactical_fx.get('contradictions') or ['Ninguna registrada']}",
        f"- Bloqueos/carencias: {tactical_fx.get('block_reasons') or ['Ninguno registrado']}",
        "",
        "## 7. Fuentes y Hashes SHA-256",
    ])
    for name, ref in sources.items():
        if ref:
            md_lines.append(f"- `{name}`: `{ref['path']}` (`{ref['sha256']}`)")
    md_lines.extend([
        "",
        "## 8. Reglas de Razonamiento",
        "- No invocar APIs externas ni inventar probabilidades calibradas, cuota justa ni EV.",
        "- No sustituir al motor cuantitativo ni autorizar apuestas.",
    ])
    codex_md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "reasoning-package",
        "status": "COMPLETED",
        "reason": "CODEX_PACKAGE_CREATED",
        "generated_at_utc": now_utc.isoformat(),
        "counts": {"signals": len(tactical_fx.get("signals") or []), "sources": sum(1 for v in sources.values() if v)},
        "codex_reasoning_input": str(codex_md_path),
        "codex_reasoning_package_json": str(package_json_path),
    }
    step_json, step_md = _write_step_artifacts(eval_dir, "reasoning_package", payload, md_lines)
    _print_step_summary("reasoning-package", "COMPLETED", "CODEX_PACKAGE_CREATED", payload["counts"], {"codex_input": codex_md_path, "package_json": package_json_path, "step_json": step_json})
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    from bet_engine.context.observation_store import ObservationStore
    from bet_engine.orchestration.shadow_pipeline import ShadowPipelineRunner, audit_bwin_catalog_txt, parse_bwin_odds_txt

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc)

    target_rec = next(
        (
            r for r in recon["all"]
            if (args.fixture_id and int(r["fixture_id"]) == int(args.fixture_id))
            or (args.event_id and str(r.get("bwin_event_id")) == str(args.event_id))
        ),
        None,
    )
    if not target_rec:
        raise StepError("TARGET_FIXTURE_OR_EVENT_NOT_FOUND_FOR_MODEL")
    eid = str(target_rec.get("bwin_event_id") or "")
    if not eid:
        raise StepError(f"MODEL_REQUIRES_VERIFIED_BWIN_EVENT_ID:fixture={target_rec['fixture_id']}")
    fid = int(target_rec["fixture_id"])

    pre_path = _require_file(eval_dir / "prospective_trial/enriched/preselection.json", "stats-fetch")
    pre_doc = _read_json(pre_path)
    pre_match = next((m for m in pre_doc.get("matches", []) if int(m["fixture_id"]) == fid), None)
    if not pre_match:
        raise StepError(f"FIXTURE_NOT_IN_PRESELECTION:{fid}")

    import dataclasses
    bundle = ObservationStore.from_preselection_match(pre_match, cutoff_utc=now_utc.isoformat())
    bundle = dataclasses.replace(bundle, event_id=eid)
    bundle_dict = bundle.to_dict()

    odds_path = args.odds_file.expanduser().resolve() if args.odds_file else (root / "outputs/matches" / eid / "inputs" / f"cuotas_bwin_{eid}.txt")
    odds_map = parse_bwin_odds_txt(odds_path, home_name=target_rec["home"], away_name=target_rec["away"], gender=target_rec.get("gender")) if odds_path.is_file() else {}

    runner = ShadowPipelineRunner(min_lead_minutes=args.min_lead)
    report = runner.run_shadow_on_bundle(
        bundle,
        target_status="NS",
        odds_map=odds_map or None,
        bwin_txt_path=odds_path if odds_path.is_file() else None,
    )

    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / eid / "model")
    out_dir.mkdir(parents=True, exist_ok=True)
    model_json = out_dir / f"model_report_{eid}.json"
    model_md = out_dir / f"model_report_{eid}.md"
    report_dict = report.to_dict() if hasattr(report, "to_dict") else {"decision": report.decision, "status": report.status, "no_bet_reasons": list(report.no_bet_reasons), "decision_classification": report.decision_classification, "formal_decision_breakdown_counts": report.formal_decision_breakdown_counts, "model_version": report.model_version, "bundle": bundle_dict}
    _atomic_json(model_json, report_dict)

    md_lines = [
        f"# Paso 10: Modelado (B0 Operativo + CAND_2B3B_V1 Experimental) · {eid}",
        "",
        f"- Fixture: `{fid}` ({target_rec['home']} – {target_rec['away']})",
        f"- Decisión: **{report.decision}** (`{report.decision_classification}`)",
        f"- Estado Shadow: `{report.status}` | Versión modelo: `{report.model_version}`",
        f"- Razones NO BET: {list(report.no_bet_reasons)}",
        f"- Desglose formal: `{report.formal_decision_breakdown_counts}`",
    ]
    model_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    counts = {
        "market_comparisons": len(getattr(report, "market_comparisons", ())),
        "decision": report.decision,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "model",
        "status": report.status,
        "reason": report.decision_classification,
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "model_json": str(model_json),
        "model_md": str(model_md),
    }
    step_json, step_md = _write_step_artifacts(eval_dir, "model", payload, md_lines)
    _print_step_summary("model", report.status, report.decision_classification, counts, {"model_json": model_json, "model_md": model_md, "step_json": step_json})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, begin, end = _resolve_eval_dir(args, now_utc)
    artifacts = {
        "fixtures_json": eval_dir / "fixtures.json",
        "fixtures_major_json": eval_dir / "fixtures_major.json",
        "manual_bwin_links": bml.manual_links_path(eval_dir),
        "bwin_reconciliation": eval_dir / "prospective_trial/bwin/bwin_scan_reconciliation.json",
        "preselection_json": eval_dir / "prospective_trial/enriched/preselection.json",
        "tactical_signals_json": eval_dir / "prospective_trial/tactical/tactical_signal_engine.json",
    }
    present = {k: p.is_file() for k, p in artifacts.items()}
    next_cmd = "discover"
    if present["fixtures_json"] and not present["fixtures_major_json"]:
        next_cmd = "filter"
    elif present["fixtures_major_json"] and not present["bwin_reconciliation"]:
        next_cmd = "bwin-scan"
    elif present["bwin_reconciliation"] and not present["preselection_json"]:
        next_cmd = "stats-fetch"
    elif present["preselection_json"] and not present["tactical_signals_json"]:
        next_cmd = "patterns"
    elif present["tactical_signals_json"]:
        next_cmd = "odds-capture / reasoning-package / model"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "status",
        "status": "COMPLETED",
        "reason": f"NEXT_SUGGESTED:{next_cmd}",
        "generated_at_utc": now_utc.isoformat(),
        "eval_dir": str(eval_dir),
        "window_madrid": [begin.isoformat(), end.isoformat()],
        "artifacts_present": present,
        "next_suggested_command": next_cmd,
    }
    md_lines = [
        "# Estado del Pipeline Modular PREMATCH",
        "",
        f"- Directorio: `{eval_dir}`",
        f"- Siguiente etapa sugerida: `{next_cmd}`",
        "",
        "## Artefactos",
    ]
    for k, ok in present.items():
        md_lines.append(f"- `{k}`: **{'PRESENTE' if ok else 'AUSENTE'}** (`{artifacts[k]}`)")
    step_json, step_md = _write_step_artifacts(eval_dir, "status", payload, md_lines)
    _print_step_summary("status", "COMPLETED", f"NEXT:{next_cmd}", {k: int(v) for k, v in present.items()}, {"step_json": step_json, "step_md": step_md})
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="Raíz del proyecto")
    common.add_argument("--eval-dir", type=Path, help="Carpeta explícita de evaluación")
    common.add_argument("--date", help="Fecha Madrid YYYY-MM-DD")
    common.add_argument("--window", help="Perfil de ventana en config/prematch_windows.json")
    common.add_argument("--start", type=df.parse_hour, default=None)
    common.add_argument("--end", type=df.parse_hour, default=None)
    common.add_argument("--asof", help="Timestamp de referencia ISO-8601 con offset")

    parser = argparse.ArgumentParser(description="Ejecutor modular PREMATCH (BET ENGINE V4 P0)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", parents=[common], help="Descubrir fixtures PREMATCH en API-Football")
    p_disc.add_argument("--refresh", action="store_true")
    p_disc.add_argument("--show", type=int, default=40)
    p_disc.set_defaults(func=cmd_discover)

    p_filt = sub.add_parser("filter", parents=[common], help="Aplicar filtro deportivo top-tier-and-senior-cups")
    p_filt.add_argument("--policy", choices=fml.VALID_POLICIES, default=DEFAULT_POLICY)
    p_filt.add_argument("--show", type=int, default=0)
    p_filt.set_defaults(func=cmd_filter)

    p_bscan = sub.add_parser("bwin-scan", parents=[common], help="Buscar y reconciliar identidad Bwin para elegibles")
    p_bscan.add_argument("--fixture-id", type=int, action="append", default=[])
    p_bscan.add_argument("--catalog-snapshot", type=Path, action="append", default=[])
    p_bscan.add_argument("--confirm-external", action="store_true")
    p_bscan.add_argument("--headless", action="store_true", default=True)
    p_bscan.add_argument("--no-browser", action="store_true")
    p_bscan.add_argument("--max-country-pages", type=int, default=8)
    p_bscan.add_argument("--max-scrolls", type=int, default=12)
    p_bscan.add_argument("--timeout", type=int, default=25000)
    p_bscan.add_argument("--min-lead", type=int, default=45)
    p_bscan.add_argument("--include-review", action="store_true")
    p_bscan.set_defaults(func=cmd_bwin_scan)

    p_blist = sub.add_parser("bwin-list", parents=[common], help="Listar cola Bwin pendiente/conflictos/verificados")
    p_blist.add_argument("--include-verified", action="store_true")
    p_blist.set_defaults(func=cmd_bwin_list)

    p_bbind = sub.add_parser("bwin-bind", parents=[common], help="Vincular manualmente Event ID o URL Bwin")
    p_bbind.add_argument("--fixture-id", type=int)
    p_bbind.add_argument("--event-id")
    p_bbind.add_argument("--url")
    p_bbind.add_argument("--spec", action="append", default=[])
    p_bbind.add_argument("--file", type=Path)
    p_bbind.add_argument("--catalog-snapshot", type=Path, action="append", default=[])
    p_bbind.add_argument("--confirm-external", action="store_true")
    p_bbind.add_argument("--headless", action="store_true", default=True)
    p_bbind.add_argument("--timeout", type=int, default=25000)
    p_bbind.add_argument("--min-lead", type=int, default=bml.MANUAL_BWIN_MIN_LEAD_MINUTES)
    p_bbind.set_defaults(func=cmd_bwin_bind)

    p_bver = sub.add_parser("bwin-verify", parents=[common], help="Verificar online vinculaciones manuales Bwin")
    p_bver.add_argument("--fixture-id", type=int, action="append", default=[])
    p_bver.add_argument("--confirm-external", action="store_true")
    p_bver.add_argument("--headless", action="store_true", default=True)
    p_bver.add_argument("--timeout", type=int, default=25000)
    p_bver.add_argument("--min-lead", type=int, default=bml.MANUAL_BWIN_MIN_LEAD_MINUTES)
    p_bver.set_defaults(func=cmd_bwin_verify)

    p_stats = sub.add_parser("stats-fetch", parents=[common], help="Recuperar historial API-Football y cobertura")
    p_stats.add_argument("--fixture-id", type=int, action="append", default=[])
    p_stats.add_argument("--history", type=int, default=20)
    p_stats.add_argument("--details", type=int, default=8)
    p_stats.add_argument("--history-seasons", type=int, default=3)
    p_stats.add_argument("--mode", choices=("full", "quick", "adaptive"), default="full")
    p_stats.add_argument("--max-matches", type=int, default=0)
    p_stats.add_argument("--research-without-bwin", action="store_true")
    p_stats.add_argument("--refresh", action="store_true")
    p_stats.set_defaults(func=cmd_stats_fetch)

    p_pat = sub.add_parser("patterns", parents=[common], help="Ejecutar motor de señales y patrones descriptivos")
    p_pat.add_argument("--profiles", type=Path)
    p_pat.add_argument("--pattern", type=Path)
    p_pat.add_argument("--no-cache", action="store_true")
    p_pat.set_defaults(func=cmd_patterns)

    p_odds = sub.add_parser("odds-capture", parents=[common], help="Capturar o validar snapshot fresco de cuotas Bwin")
    p_odds.add_argument("--fixture-id", type=int, action="append", default=[])
    p_odds.add_argument("--event-id")
    p_odds.add_argument("--snapshot-file", type=Path)
    p_odds.add_argument("--confirm-external", action="store_true")
    p_odds.add_argument("--headless", action="store_true", default=True)
    p_odds.add_argument("--timeout", type=int, default=25000)
    p_odds.add_argument("--min-lead", type=int, default=45)
    p_odds.set_defaults(func=cmd_odds_capture)

    p_pack = sub.add_parser("reasoning-package", parents=[common], help="Generar expediente autocontenido para Codex")
    p_pack.add_argument("--fixture-id", type=int)
    p_pack.add_argument("--event-id")
    p_pack.add_argument("--output-dir", type=Path)
    p_pack.add_argument("--research-without-bwin", action="store_true")
    p_pack.add_argument("--import-qualitative", type=Path)
    p_pack.set_defaults(func=cmd_reasoning_package)

    p_mod = sub.add_parser("model", parents=[common], help="Ejecutar modelo B0 operativo y shadow CAND_2B3B_V1")
    p_mod.add_argument("--fixture-id", type=int)
    p_mod.add_argument("--event-id")
    p_mod.add_argument("--odds-file", type=Path)
    p_mod.add_argument("--output-dir", type=Path)
    p_mod.add_argument("--min-lead", type=int, default=45)
    p_mod.set_defaults(func=cmd_model)

    p_stat = sub.add_parser("status", parents=[common], help="Consultar estado del pipeline modular")
    p_stat.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (StepError, bml.ManualBwinLinkError, df.DiscoveryError, ValueError, OSError) as exc:
        print(f"ERROR [{args.command}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
