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
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import bwin_discovery as bd
import bwin_manual_links as bml
import discover_fixtures as df
import filter_major_leagues as fml
import prematch_prospective_trial as trial
from prematch_execution_control import require_external_confirmation

SCHEMA_VERSION = "1.0-prematch-steps"
DEFAULT_POLICY = "top-tier-and-senior-cups"
ANALYSIS_MIN_LEAD_MINUTES = 30
EARLY_SCOUT_MIN_LEAD_MINUTES = 10
PHASE1_INITIAL_LEAD_MINUTES = 45
PHASE1_SECOND_LEAD_MINUTES = 120
RESEARCH_UPDATE_MIN_LEAD_MINUTES = 15
STATS_MIN_LEAD_MINUTES = 45


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


def _preserved_output_dir(folder: Path, *filenames: str) -> Path:
    """Keep the first dossier at its published paths and put reruns beside it."""
    from tempfile import mkdtemp

    folder.mkdir(parents=True, exist_ok=True)
    if any((folder / name).exists() for name in filenames):
        revisions = folder / "revisions"
        revisions.mkdir(exist_ok=True)
        return Path(mkdtemp(prefix="revision_", dir=revisions))
    return folder


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
    latest_scan_meta: dict[str, Any] = {}
    for p in auto_paths:
        try:
            doc = _read_json(p)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not latest_scan_meta and isinstance(doc.get("latest_scan"), dict):
            latest_scan_meta = dict(doc["latest_scan"])
        for row in doc.get("matches") or []:
            fid = row.get("fixture_id")
            if isinstance(fid, int) and fid not in auto_by_fid:
                auto_by_fid[fid] = (p, row)

    # Detect cross-snapshot verified bwin_event_id contradictions
    snapshot_verified_eids: dict[int, set[str]] = {}
    scans_dir = bwin_root / "scans"
    if scans_dir.is_dir():
        for sp in sorted(scans_dir.glob("bwin_discovery_*.json")):
            try:
                sdoc = _read_json(sp)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            for row in sdoc.get("matches") or []:
                fid = row.get("fixture_id")
                if isinstance(fid, int) and row.get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY" and row.get("bwin_event_id"):
                    snapshot_verified_eids.setdefault(fid, set()).add(str(row["bwin_event_id"]))

    latest_scope = str(latest_scan_meta.get("scan_scope") or "")
    latest_targeted = {int(x) for x in (latest_scan_meta.get("targeted_fixture_ids") or []) if str(x).isdigit()}

    verified: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    pending_manual: list[dict[str, Any]] = []
    temporal_blocked: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    for fx in eligible:
        fid = int(fx["fixture_id"])
        madrid_time = bd.local_kickoff(fx.get("kickoff_madrid"))
        utc_time = bd.local_kickoff(fx.get("kickoff_utc"))
        ko = utc_time or madrid_time
        ko_utc = ko.astimezone(timezone.utc) if ko else None
        lead = (ko_utc - now_utc).total_seconds() / 60.0 if ko_utc else None
        lead_min = round(lead, 2) if lead is not None else None

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

        fixture_status = str(fx.get("status") or "").strip().upper()
        temporal_reason = None
        if madrid_time and utc_time and madrid_time != utc_time:
            temporal_reason = "KICKOFF_UTC_MADRID_MISMATCH"
        elif fixture_status and fixture_status not in {"NS", "TBD"}:
            temporal_reason = f"TARGET_STATUS_NOT_PREMATCH({fixture_status})"
        elif lead is None or lead <= 0:
            temporal_reason = "POST_KICKOFF_OR_MISSING_KICKOFF"
        elif lead < min_lead_minutes:
            temporal_reason = f"LEAD_BELOW_MIN_PREMATCH({lead_min}m<{min_lead_minutes}m)"

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

        cross_eids = snapshot_verified_eids.get(fid) or set()
        if len(cross_eids) > 1:
            state = "CONFLICT"
            reason = f"SNAPSHOT_EVENT_ID_CONTRADICTION:{','.join(sorted(cross_eids))}"
        elif manual_status == "BWIN_IDENTITY_CONFLICT":
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
            if auto_status in ("EVENT_FOUND_TIME_CORROBORATED", "EVENT_FOUND_PENDING_TIME_CHECK",
                               "EVENT_VISIBLE_PENDING_CLASSIFICATION", "POSSIBLE_EVENT_FOUND"):
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
        elif latest_scope == "INDIVIDUAL_SCAN" and fid not in latest_targeted:
            state = "PENDING_MANUAL"
            reason = "NOT_SCANNED_IN_THIS_RUN"

        scanned_in_latest = True if (not latest_scope or latest_scope == "FULL_SCAN" or fid in latest_targeted) else False
        reused_prior = bool(auto_row and not scanned_in_latest)
        verified_at = (auto_row or {}).get("scanned_at_utc") if verification_source in ("AUTO", "AUTO+MANUAL") else (manual_entry or {}).get("verified_at_utc")
        verif_age_min = None
        if verified_at:
            v_dt = bd.local_kickoff(verified_at)
            if v_dt is not None:
                verif_age_min = round((now_utc - v_dt.astimezone(timezone.utc)).total_seconds() / 60.0, 2)

        rec = {
            "fixture_id": fid,
            "kickoff_madrid": fx.get("kickoff_madrid"),
            "kickoff_utc": fx.get("kickoff_utc"),
            "lead_minutes": lead_min,
            "country": fx.get("country"),
            "league": fx.get("league"),
            "league_id": fx.get("league_id"),
            "season": fx.get("season"),
            "referee": fx.get("referee"),
            "gender": fx.get("gender"),
            "home": fx.get("home"),
            "away": fx.get("away"),
            "home_id": fx.get("home_id"),
            "away_id": fx.get("away_id"),
            "fixture_status": fixture_status,
            "identity_state": state,
            "identity_reason": reason,
            "temporal_eligible": temporal_reason is None,
            "temporal_reason": temporal_reason,
            "bwin_state": "TEMPORAL_BLOCKED" if temporal_reason else state,
            "reason": temporal_reason or reason,
            "bwin_event_id": bwin_event_id,
            "bwin_url": bwin_url,
            "verification_source": verification_source,
            "scanned_in_latest_run": scanned_in_latest,
            "reused_from_prior_scan": reused_prior,
            "verified_at_utc": verified_at,
            "verification_age_minutes": verif_age_min,
            "auto_discovery_path": auto_path,
            "candidates": candidates,
        }
        all_rows.append(rec)
        if temporal_reason:
            temporal_blocked.append(rec)
        elif state == "VERIFIED":
            verified.append(rec)
        elif state == "INCOMPLETE_SEARCH":
            incomplete.append(rec)
        elif state == "CONFLICT":
            conflicts.append(rec)
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
    return _save_filter_step(eval_dir, result, now_utc, policy)


def _save_filter_step(eval_dir, result, now_utc, policy):
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


def _persist_bwin_discovery_with_merge(
    bwin_dir: Path,
    payload: dict[str, Any],
    *,
    now_utc: datetime,
    targeted_fids: set[int] | None,
) -> None:
    """Archiva snapshot inmutable de la corrida y fusiona sin perder verificaciones previas."""
    scan_scope = "INDIVIDUAL_SCAN" if targeted_fids else "FULL_SCAN"
    scan_id = f"{now_utc:%Y%m%dT%H%M%SZ}_{scan_scope.lower()}"
    scan_iso = now_utc.isoformat()

    tagged_matches: list[dict[str, Any]] = []
    for row in payload.get("matches") or []:
        tagged_matches.append({
            **row,
            "scan_id": scan_id,
            "scanned_at_utc": scan_iso,
            "scan_scope": scan_scope,
        })

    run_snapshot = {
        **payload,
        "scan_id": scan_id,
        "scan_scope": scan_scope,
        "targeted_fixture_ids": sorted(targeted_fids) if targeted_fids else [],
        "matches": tagged_matches,
    }
    scans_dir = bwin_dir / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)
    snap_path = scans_dir / f"bwin_discovery_{scan_id}.json"
    _atomic_json(snap_path, run_snapshot)

    merged_payload = dict(run_snapshot)
    merged_payload["latest_scan"] = {
        "scan_id": scan_id,
        "scanned_at_utc": scan_iso,
        "scan_scope": scan_scope,
        "targeted_fixture_ids": sorted(targeted_fids) if targeted_fids else [],
        "snapshot_path": str(snap_path),
    }

    main_path = bwin_dir / "bwin_discovery.json"
    if targeted_fids and main_path.is_file():
        try:
            prior = _read_json(main_path)
        except (OSError, json.JSONDecodeError, ValueError):
            prior = {}

        # Merge catalog_observed by bwin_event_id
        cat_by_id: dict[str, dict[str, Any]] = {
            str(e["bwin_event_id"]): e
            for e in (prior.get("catalog_observed") or [])
            if e.get("bwin_event_id")
        }
        for e in payload.get("catalog_observed") or []:
            if e.get("bwin_event_id"):
                cat_by_id[str(e["bwin_event_id"])] = e

        # Merge pages by url
        pages_by_url: dict[str, dict[str, Any]] = {
            str(pg["url"]): pg for pg in (prior.get("pages") or []) if pg.get("url")
        }
        for pg in payload.get("pages") or []:
            if pg.get("url"):
                pages_by_url[str(pg["url"])] = pg

        # Merge matches by fixture_id with contradiction check
        matches_by_fid: dict[int, dict[str, Any]] = {}
        for row in prior.get("matches") or []:
            fid = row.get("fixture_id")
            if isinstance(fid, int):
                matches_by_fid[fid] = row
        contradictions: list[dict[str, Any]] = list(prior.get("snapshot_contradictions") or [])
        for row in tagged_matches:
            fid = row.get("fixture_id")
            if not isinstance(fid, int):
                continue
            prev = matches_by_fid.get(fid)
            if (
                prev
                and prev.get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY"
                and row.get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY"
                and prev.get("bwin_event_id")
                and row.get("bwin_event_id")
                and str(prev["bwin_event_id"]) != str(row["bwin_event_id"])
            ):
                contradictions.append({
                    "fixture_id": fid,
                    "prior_event_id": str(prev["bwin_event_id"]),
                    "new_event_id": str(row["bwin_event_id"]),
                    "detected_at_utc": scan_iso,
                })
                row = {
                    **row,
                    "status": "BWIN_IDENTITY_CONFLICT",
                    "identity_status": "BWIN_IDENTITY_CONFLICT",
                    "prior_verified_event_id": str(prev["bwin_event_id"]),
                }
            if (prev and prev.get("status") == "IDENTITY_VERIFIED_AUTOMATICALLY"
                    and row.get("status") not in {"IDENTITY_VERIFIED_AUTOMATICALLY", "IDENTITY_CONFLICT",
                                                 "BWIN_IDENTITY_CONFLICT", "GENDER_CONFLICT"}):
                continue
            matches_by_fid[fid] = row

        merged_payload["catalog_observed"] = list(cat_by_id.values())
        merged_payload["pages"] = list(pages_by_url.values())
        merged_payload["matches"] = list(matches_by_fid.values())
        if contradictions:
            merged_payload["snapshot_contradictions"] = contradictions

    bd.write_reports(bwin_dir, merged_payload)


def cmd_bwin_scan(args: argparse.Namespace) -> int:
    now_utc = _now_utc(args.asof)
    _, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    _, major_doc = _load_major_doc(eval_dir)

    candidates = list(major_doc.get("fixtures") or [])
    if args.include_review:
        candidates.extend(major_doc.get("review") or [])
    wanted_fids: set[int] | None = None
    if args.fixture_id:
        wanted_fids = {int(x) for x in args.fixture_id}
        candidates = [f for f in candidates if int(f["fixture_id"]) in wanted_fids]

    bwin_dir = eval_dir / "prospective_trial/bwin"
    bwin_dir.mkdir(parents=True, exist_ok=True)
    scan_status = "LOCAL_RECONCILED"
    scan_reason = "LOCAL_ARTIFACTS_AND_CATALOGS"

    if args.catalog_snapshot and not args.confirm_external:
        catalog = bd.load_local_catalog_snapshots(args.catalog_snapshot)
        results = [bd.summarize(fx, bd.match_catalog(fx, catalog), None, args.timeout, inspect=False) for fx in candidates]
        _persist_bwin_discovery_with_merge(
            bwin_dir,
            {
                "schema_version": "0.2-bwin-discovery",
                "generated_at_utc": now_utc.isoformat(),
                "source": "LOCAL_CATALOG_SNAPSHOT",
                "catalog_observed": catalog,
                "pages": [],
                "matches": results,
            },
            now_utc=now_utc,
            targeted_fids=wanted_fids,
        )
        scan_status = "COMPLETED_OFFLINE_SNAPSHOT"
        scan_reason = "MATCHED_AGAINST_LOCAL_CATALOG_SNAPSHOT"
    elif args.confirm_external and not args.no_browser:
        require_external_confirmation(True)
        eligible_for_scan = {
            r["fixture_id"] for r in _bwin_reconcile_records(
                eval_dir, {**major_doc, "fixtures": candidates}, now_utc, min_lead_minutes=args.min_lead,
            )["all"] if r["temporal_eligible"]
        }
        candidates = [f for f in candidates if f["fixture_id"] in eligible_for_scan]
    if args.confirm_external and not args.no_browser and candidates:
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
        _persist_bwin_discovery_with_merge(
            bwin_dir,
            {
                "schema_version": "0.5-bwin-discovery-v3",
                "generated_at_utc": now_utc.isoformat(),
                "catalog_observed": catalog,
                "pages": pages,
                "matches": results,
            },
            now_utc=now_utc,
            targeted_fids=wanted_fids,
        )
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


def _archive_legacy_preselection(
    existing_path: Path,
    out_folder: Path,
    now_utc: datetime,
) -> dict[str, Any]:
    """Conserva una copia binaria inmutable del preselection.json previo sin alterar su contenido."""
    raw_bytes = existing_path.read_bytes()
    prior_sha = hashlib.sha256(raw_bytes).hexdigest()
    archive_dir = out_folder / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"preselection_{prior_sha[:12]}.json"
    if archive_path.exists():
        if archive_path.read_bytes() != raw_bytes:
            raise StepError(f"ARCHIVE_HASH_COLLISION:{archive_path}")
    else:
        with archive_path.open("xb") as handle:
            handle.write(raw_bytes)
    return {
        "archive_path": str(archive_path),
        "archive_sha256": prior_sha,
        "archived_at_utc": now_utc.isoformat(),
    }


def _audit_preselection_compatibility(
    existing_doc: dict[str, Any],
    major_doc: dict[str, Any],
    current_filter_sha: str | None,
) -> dict[str, Any]:
    """Audita compatibilidad de fingerprint y procedencia antes de decidir reanudación o migración."""
    prior_sha = existing_doc.get("source_filter_sha256")
    prior_filter_ts = existing_doc.get("source_filter_generated_at_utc")
    current_filter_ts = major_doc.get("generated_at_utc")
    prior_matches = existing_doc.get("matches") or []
    prior_evaluated_ids = [int(m["fixture_id"]) for m in prior_matches if m.get("fixture_id") is not None]
    prior_fully_eval = (existing_doc.get("counts") or {}).get("fully_evaluated")
    prior_errors = existing_doc.get("api_errors") or {}

    if isinstance(prior_sha, str) and prior_sha and current_filter_sha and prior_sha == current_filter_sha:
        return {
            "compatible_for_merge": True,
            "migration_reason": "COMPATIBLE_SOURCE_FILTER_SHA256",
            "reasons": [],
            "reusable_stats": True,
            "prior_fingerprint_backfilled": False,
            "prior_source_filter_sha256": prior_sha,
            "current_source_filter_sha256": current_filter_sha,
            "prior_filter_generated_at_utc": prior_filter_ts,
            "current_filter_generated_at_utc": current_filter_ts,
            "prior_evaluated_ids": prior_evaluated_ids,
            "prior_fully_evaluated": prior_fully_eval,
        }

    reasons: list[str] = []
    if not prior_sha:
        migration_reason = "LEGACY_NULL_FINGERPRINT"
        reasons.append("MISSING_SOURCE_FILTER_SHA256")
        if prior_filter_ts != current_filter_ts:
            reasons.append("SOURCE_FILTER_TIMESTAMP_MISMATCH")
        else:
            reasons.append("UNPROVEN_FILTER_BYTES_CANNOT_BACKFILL_SHA256")
    else:
        migration_reason = "SOURCE_FILTER_CHANGED"
        reasons.append("SOURCE_FILTER_SHA256_MISMATCH")
        if prior_filter_ts != current_filter_ts:
            reasons.append("SOURCE_FILTER_TIMESTAMP_MISMATCH")

    if prior_errors or not prior_fully_eval:
        reasons.append("PRIOR_REPORT_HAS_NO_VALID_EVALUATED_STATS")

    return {
        "compatible_for_merge": False,
        "migration_reason": migration_reason,
        "reasons": reasons,
        "reusable_stats": False,
        "prior_fingerprint_backfilled": False,
        "prior_source_filter_sha256": prior_sha,
        "current_source_filter_sha256": current_filter_sha,
        "prior_filter_generated_at_utc": prior_filter_ts,
        "current_filter_generated_at_utc": current_filter_ts,
        "prior_evaluated_ids": prior_evaluated_ids,
        "prior_fully_evaluated": prior_fully_eval,
    }


def _same_fixture_history(match: dict[str, Any], fixture: dict[str, Any]) -> bool:
    identity_keys = ("fixture_id", "home_id", "away_id", "league_id", "season",
                     "gender", "home", "away", "kickoff_madrid")
    return all(match.get(key) is not None and match[key] == fixture.get(key) for key in identity_keys)


def _rebase_fixture_history(prior, major, fingerprint, path):
    # A legacy filter may be missing. Keep its original fingerprint with each
    # reused fixture and require an exact match against the current run's IDs.
    if (prior.get("schema_version") not in {"1.2-preselection", "1.3-preselection"}
            or not prior.get("source_filter_sha256")
            or Path(prior.get("source_filter_path") or "").resolve() != path.resolve()):
        raise StepError("SELECTIVE_HISTORY_REUSE_SOURCE_FILTER_UNPROVEN")
    eligible = {m["fixture_id"]: m for m in major["fixtures"]}
    retained = [
        {**m, "history_origin_filter_sha256": m.get("history_origin_filter_sha256") or prior["source_filter_sha256"]}
        for m in prior.get("matches", [])
        if _same_fixture_history(m, eligible.get(m["fixture_id"], {}))
    ]
    return {**prior, "source_filter_sha256": fingerprint,
            "source_filter_generated_at_utc": major.get("generated_at_utc"), "matches": retained}


def cmd_stats_fetch(args: argparse.Namespace) -> int:
    import preselect_matches as pm

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    major_path, major_doc = _load_major_doc(eval_dir)
    current_filter_sha = _sha256_file(major_path)

    local_reuse_only = getattr(args, "local_reuse_only", False)
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc,
                                   min_lead_minutes=0 if local_reuse_only else STATS_MIN_LEAD_MINUTES)
    verified_ids = {int(r["fixture_id"]) for r in recon["verified"]}
    verified_identity_ids = {r["fixture_id"] for r in recon["all"] if r["identity_state"] == "VERIFIED"}
    temporal_ids = {r["fixture_id"] for r in recon["all"] if r["temporal_eligible"]}

    if args.research_without_bwin:
        target_ids = temporal_ids & set(args.fixture_id) if args.fixture_id else temporal_ids
        out_folder = eval_dir / "prospective_trial/research_only"
        route_tag = "RESEARCH_WITHOUT_BWIN"
    else:
        target_ids = verified_ids & set(args.fixture_id) if args.fixture_id else verified_ids
        out_folder = eval_dir / "prospective_trial/enriched"
        route_tag = "BWIN_ACCREDITED_COMMERCIAL"

    if not target_ids:
        if args.fixture_id:
            blocked = [f"{r['fixture_id']}:{r['reason']}" for r in recon["all"] if r["fixture_id"] in args.fixture_id]
            if blocked:
                raise StepError("STATS_FETCH_TARGET_BLOCKED:" + ",".join(blocked))
        raise StepError(
            "NO_ELIGIBLE_FIXTURES_FOR_STATS_FETCH "
            "(ningún fixture con Bwin verificado; ejecuta `bwin-scan`/`bwin-bind`/`bwin-verify` o usa `--research-without-bwin`)"
        )

    existing_path = out_folder / "preselection.json"
    existing_doc: dict[str, Any] | None = None
    migration_meta: dict[str, Any] | None = None
    if existing_path.is_file():
        prior_doc = _read_json(existing_path)
        compat = _audit_preselection_compatibility(prior_doc, major_doc, current_filter_sha)
        if getattr(args, "reuse_matching_history", False) and not compat["compatible_for_merge"]:
            if _now_utc(prior_doc["generated_at_utc"]) > now_utc:
                raise StepError("PRESELECTION_AFTER_CUTOFF")
            existing_doc = _rebase_fixture_history(prior_doc, major_doc, current_filter_sha, major_path)
            migration_meta = {
                **compat,
                "migration_reason": "REBASED_MATCHING_FIXTURE_IDENTITIES",
                "stats_reused_from_prior_report": bool(existing_doc["matches"]),
                "retained_fixture_ids": [m["fixture_id"] for m in existing_doc["matches"]],
                **_archive_legacy_preselection(existing_path, out_folder, now_utc),
            }
        elif args.refresh or not compat["compatible_for_merge"]:
            archive_info = _archive_legacy_preselection(existing_path, out_folder, now_utc)
            migration_reason = "MANUAL_REFRESH" if (args.refresh and compat["compatible_for_merge"]) else compat["migration_reason"]
            migration_meta = {
                **compat,
                "migration_reason": migration_reason,
                "stats_reused_from_prior_report": False,
                **archive_info,
            }
            existing_doc = None
        else:
            existing_doc = prior_doc

    already_ids = {int(m["fixture_id"]) for m in (existing_doc or {}).get("matches", [])}
    missing_ids = sorted(target_ids - already_ids)
    if local_reuse_only and missing_ids:
        raise StepError(f"SELECTIVE_HISTORY_REUSE_MISSING_FIXTURES:{missing_ids}")

    if not missing_ids and existing_doc is not None:
        report = existing_doc
        if migration_meta:
            report["snapshot_migration"] = migration_meta
            pm.assign_ranks(report["matches"])
            report["counts"].update(
                evaluated=len(report["matches"]),
                fully_evaluated=sum(m.get("data_readiness_score") is not None for m in report["matches"]),
                details_requested=sum(bool(m.get("details_checked")) for m in report["matches"]),
                pending_detail=sum(not m.get("details_checked") for m in report["matches"]),
            )
            pm.save_report(report, out_folder)
        status = "REUSED_EXISTING"
        reason = "ALL_TARGET_FIXTURES_ALREADY_FETCHED"
    else:
        from config.constants import API_KEY, FOOTBALL_BASE_URL, REQUEST_TIMEOUT

        if not API_KEY:
            raise StepError("FOOTBALL_API_KEY no configurada (revisa config/.env)")
        if existing_doc is not None and existing_path.is_file():
            # Conserva también snapshot previo antes de fusionar nuevos fixtures.
            _archive_legacy_preselection(existing_path, out_folder, now_utc)
        client_asof = now_utc if args.asof else None
        client = pm.ApiFootball(
            API_KEY,
            FOOTBALL_BASE_URL,
            root,
            timeout=REQUEST_TIMEOUT,
            refresh=args.refresh,
            asof=client_asof,
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
                source_filter_sha256=current_filter_sha,
            )
        finally:
            client.close()
        if fresh.get("source_filter_sha256") is None:
            fresh["source_filter_sha256"] = current_filter_sha
        elif current_filter_sha and fresh.get("source_filter_sha256") != current_filter_sha:
            raise StepError(
                f"STATS_FETCH_FILTER_SHA_MISMATCH:{fresh.get('source_filter_sha256')}!={current_filter_sha}"
            )
        if not fresh.get("source_filter_generated_at_utc"):
            fresh["source_filter_generated_at_utc"] = major_doc.get("generated_at_utc")

        report = pm.merge_preselection_reports(existing_doc, fresh, allow_new_fixtures=True) if existing_doc else fresh
        if existing_doc and getattr(args, "reuse_matching_history", False):
            report["api_request_ledger"] = [*(existing_doc.get("api_request_ledger") or []),
                                            *(fresh.get("api_request_ledger") or [])]
            ledger = {r["fixture_id"]: r for r in existing_doc.get("fixture_evaluation_ledger") or []}
            ledger.update({r["fixture_id"]: r for r in fresh.get("fixture_evaluation_ledger") or []
                           if r["fixture_id"] in missing_ids})
            report["fixture_evaluation_ledger"] = list(ledger.values())
        for m in report.get("matches", []):
            fid = int(m["fixture_id"])
            identity_verified = fid in verified_identity_ids
            m["bwin_accredited"] = identity_verified
            m["research_only_without_bwin"] = not identity_verified
            m["commercial_eligible"] = identity_verified and not args.research_without_bwin
        report["route_mode"] = route_tag
        report["source_filter_path"] = str(major_path.resolve())
        if migration_meta is not None:
            report["snapshot_migration"] = migration_meta
        pm.save_report(report, out_folder)
        status = "COMPLETED"
        reason = (
            f"MIGRATED_{migration_meta['migration_reason']}_FETCHED_{len(missing_ids)}_FIXTURES"
            if migration_meta is not None
            else f"FETCHED_{len(missing_ids)}_FIXTURES"
        )

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
        "source_filter_path": str(major_path),
        "source_filter_sha256": current_filter_sha,
        "preselection_path": str(existing_path),
        "preselection_sha256": _sha256_file(existing_path),
        "snapshot_migration": migration_meta or report.get("snapshot_migration"),
    }
    md_lines = [
        "# Paso 6: Stats Fetch (API-Football)",
        "",
        f"- Estado: **{status}** ({reason}) | Ruta: `{route_tag}`",
        f"- Partidos en `preselection.json`: **{counts['matches_in_report']}**",
        f"- Filtro origen (`fixtures_major.json`): `{current_filter_sha}`",
        f"- Artefacto: `{existing_path}` (`{payload['preselection_sha256']}`)",
    ]
    if migration_meta is not None:
        md_lines.append(
            f"- Migración de snapshot previo: **{migration_meta['migration_reason']}** -> copia inmutable `{migration_meta['archive_path']}` (`{migration_meta['archive_sha256']}`)"
        )
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
    pre_doc = _read_json(pre_path)
    pre_sha = _sha256_file(pre_path)
    tactical_json_path = tactical_out / "tactical_signal_engine.json"
    tactical_sha = _sha256_file(tactical_json_path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "patterns",
        "status": "COMPLETED",
        "reason": "DESCRIPTIVE_PATTERNS_EVALUATED",
        "generated_at_utc": now_utc.isoformat(),
        "counts": counts,
        "signal_summary": signal_summary,
        "preselection_path": str(pre_path),
        "preselection_sha256": pre_sha,
        "source_filter_sha256": pre_doc.get("source_filter_sha256"),
        "tactical_json": str(tactical_json_path),
        "tactical_json_sha256": tactical_sha,
        "tactical_md": str(tactical_out / "TACTICAL_SELECTION_REPORT.md"),
    }
    md_lines = [
        "# Paso 7: Patrones Estadísticos y Señales Descriptivas",
        "",
        f"- Partidos evaluados: **{counts['fixtures']}** | Señales sustentadas: **{counts['supported_total']}** | Sin mercado Bwin: **{counts['without_bwin_market']}**",
        f"- Preselección entrada: `{pre_path}` (`{pre_sha}`) | Filtro origen: `{pre_doc.get('source_filter_sha256')}`",
        f"- Reporte motor: `{tactical_out / 'TACTICAL_SELECTION_REPORT.md'}` (`{tactical_sha}`)",
        "",
        "Nota: Los candidatos descriptivos se conservan aunque no tengan probabilidad calibrada ni oferta Bwin.",
    ]
    step_json, step_md = _write_step_artifacts(eval_dir, "patterns", payload, md_lines)
    _print_step_summary("patterns", "COMPLETED", "DESCRIPTIVE_PATTERNS_EVALUATED", counts, {"tactical_json": tactical_out / "tactical_signal_engine.json", "step_json": step_json, "step_md": step_md})
    return 0


def _write_phase1_list(
    eval_dir: Path,
    *,
    now_utc: datetime,
    min_lead: int,
    filename: str,
    excluded_ids: frozenset[int] = frozenset(),
    no_history_reason: str | None = None,
) -> Path:
    _, major_doc = _load_major_doc(eval_dir)
    discovery_path = eval_dir / "fixtures.json"
    window = (_read_json(discovery_path).get("window") if discovery_path.is_file() else None) or major_doc.get("source_window") or {}
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc, min_lead_minutes=min_lead)
    verified = {row["fixture_id"]: row for row in recon["verified"]}
    ranking = []
    ranking_cutoff = None
    pre_path = eval_dir / "prospective_trial/enriched/preselection.json"
    rank_path = eval_dir / "prospective_trial/tactical/tactical_signal_engine.json"
    if no_history_reason is None and not (pre_path.is_file() or rank_path.is_file()):
        no_history_reason = "La corrida carece de histórico y ranking. Revisa los motivos de exclusión."
    if no_history_reason is None:
        _require_file(pre_path, "stats-fetch")
        _require_file(rank_path, "patterns")
        tactical = _read_json(rank_path)
        if (tactical.get("sources") or {}).get("preselection_sha256") != _sha256_file(pre_path):
            raise StepError("PHASE1_RANKING_HISTORY_MISMATCH: vuelve a generar patterns para esta corrida")
        ranking = tactical.get("statistical_ranking")
        if not isinstance(ranking, list):
            raise StepError("PHASE1_STATISTICAL_RANKING_MISSING: vuelve a generar patterns para esta corrida")
        ranking_cutoff = bd.local_kickoff(tactical.get("asof_madrid"))
        if ranking_cutoff and ranking_cutoff > now_utc:
            raise StepError("PHASE1_RANKING_AFTER_QUERY_CUTOFF")

    def madrid_label(value: datetime) -> str:
        return value.astimezone(trial.TZ).strftime("%d/%m/%Y %H:%M %Z")

    def cell(value: Any) -> str:
        return " ".join(str(value if value is not None else "—").split()).replace("|", "&#124;")

    ranking_ids = {int(stat["fixture_id"]) for stat in ranking}
    exclusions = Counter()
    for rec in recon["all"]:
        fid = rec["fixture_id"]
        if fid in excluded_ids:
            exclusions["manual"] += 1
        elif not rec["temporal_eligible"]:
            exclusions["horario/estado"] += 1
        elif rec["identity_state"] != "VERIFIED":
            exclusions["vínculo Bwin sin verificar"] += 1
        elif fid not in ranking_ids:
            exclusions["ausencia del ranking"] += 1

    rows = []
    for position, stat in enumerate(ranking, 1):
        fid = int(stat["fixture_id"])
        if fid not in verified or fid in excluded_ids:
            continue
        rec = verified[fid]
        kickoff = bd.local_kickoff(rec.get("kickoff_madrid") or rec.get("kickoff_utc"))
        if kickoff is None:  # Same timestamp required by reconciliation.
            continue
        fields = (
            stat.get("position", position), madrid_label(kickoff), rec["lead_minutes"],
            rec["country"], rec["league"], f"{rec['home']} – {rec['away']}",
            f"{stat['statistical_research_score']:.2f}", stat.get("signal_count", 0),
            ", ".join(stat.get("signal_families") or []) or "—",
            fid, rec["bwin_event_id"],
        )
        rows.append((kickoff.astimezone(timezone.utc), position, "| " + " | ".join(map(cell, fields)) + " |"))

    lines = [
        "# Fase 1 · Partidos para revisar",
        "",
        f"- Corrida: `{eval_dir}`.",
        f"- Ventana original: **{window.get('start_madrid', 'sin registrar')} → {window.get('end_madrid', 'sin registrar')}**.",
        f"- Consulta: **{madrid_label(now_utc)}** (`{now_utc.astimezone(trial.TZ).isoformat()}`, Europe/Madrid) · margen mínimo **{min_lead} min** · partidos en la lista **{len(rows)}**.",
        f"- Corte del ranking: **{madrid_label(ranking_cutoff) if ranking_cutoff else '—'}**.",
        f"- Identidad Bwin verificada y con margen en los registros locales: **{len(verified)}** antes de cruzar el ranking y las exclusiones.",
        "- Exclusiones entre elegibles del filtro (primer motivo aplicable): " + " · ".join(
            f"{label} **{exclusions[label]}**"
            for label in ("manual", "horario/estado", "vínculo Bwin sin verificar", "ausencia del ranking")
        ) + ".",
        f"- Fuera del conjunto elegible deportivo: revisión **{len(major_doc.get('review') or [])}** · excluidos **{len(major_doc.get('excluded') or [])}**.",
        "- El puesto y la puntuación proceden del ranking estadístico original. El margen se calcula en esta consulta.",
    ]
    if excluded_ids:
        lines.append("- Fixtures excluidos manualmente: " + ", ".join(map(str, sorted(excluded_ids))) + ".")
    if not rows:
        lines.extend(["", no_history_reason or "Ningún partido del ranking cumple los filtros de esta consulta."])
    else:
        header = "| Puesto original | Inicio en Madrid | Margen (min) | País | Liga | Partido | Puntuación | Señales | Familias | Fixture ID | Event ID Bwin |"
        separator = "|---:|---|---:|---|---|---|---:|---:|---|---:|---:|"
        for title, ordered in (
            ("Por ranking estadístico", rows),
            ("Por hora de inicio", sorted(rows)),
        ):
            lines.extend(["", f"## {title}", "", header, separator])
            lines.extend(row[2] for row in ordered)

    report = eval_dir / filename
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  lista_markdown: {report}")
    return report


def cmd_phase1_run(args: argparse.Namespace) -> int:
    if args.asof:
        raise StepError("PHASE1_RUN_REQUIRES_REAL_CLOCK: omite --asof")
    if args.min_lead < 0:
        raise StepError("PHASE1_MIN_LEAD_MUST_BE_NONNEGATIVE")
    require_external_confirmation(args.confirm_external)
    started = _now_utc()
    args.date = args.date or started.astimezone(trial.TZ).date().isoformat()
    root, eval_dir, begin, end = _resolve_eval_dir(args, started)
    if args.eval_dir is None:
        stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
        eval_dir = eval_dir.with_name(f"{eval_dir.name}_fase1_{stamp}")
    try:
        eval_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise StepError(f"PHASE1_RUN_DIRECTORY_EXISTS: {eval_dir}") from exc
    print(f"[phase1-run] nueva corrida: {eval_dir}", flush=True)
    _atomic_json(eval_dir / "phase1_run.json", {
        "started_at_utc": started.isoformat(),
        "window_date_madrid": args.date,
        "window": {"start_madrid": begin.isoformat(), "end_madrid": end.isoformat()},
    })

    common = ["--root", str(root), "--eval-dir", str(eval_dir)]
    parser = build_parser()

    def run_step(name: str, *options: str) -> None:
        stage = parser.parse_args([name, *common, *options])
        if stage.func(stage):
            raise StepError(f"PHASE1_STAGE_FAILED: {name}")

    run_step("discover", "--date", args.date, "--start", begin.strftime("%H:%M"),
             "--end", end.strftime("%H:%M"), "--refresh")
    run_step("filter", "--policy", args.policy)
    _, major_doc = _load_major_doc(eval_dir)
    scan_options = ["--confirm-external"] if major_doc["fixtures"] else []
    run_step("bwin-scan", *scan_options, "--max-country-pages", str(args.max_country_pages),
             "--max-scrolls", str(args.max_scrolls), "--timeout", str(args.timeout))
    run_step("bwin-list", "--include-verified")
    recon = _bwin_reconcile_records(eval_dir, major_doc, _now_utc())
    no_history_reason = None
    if recon["verified"]:
        run_step("stats-fetch")
        run_step("patterns")
    else:
        no_history_reason = "No había partidos verificados con margen para recoger histórico y generar el ranking."
    _write_phase1_list(
        eval_dir, now_utc=_now_utc(), min_lead=args.min_lead,
        filename="ranking_fase1.md", no_history_reason=no_history_reason,
    )
    return 0


def cmd_phase1_list(args: argparse.Namespace) -> int:
    now = _now_utc(args.asof)
    if args.eval_dir:
        if args.date:
            raise StepError("PHASE1_LIST_DATE_REQUIRES_FIRST_RUN")
        eval_dir = args.eval_dir.expanduser().resolve()
    else:
        day = df.parse_day(args.date) if args.date else now.astimezone(trial.TZ).date()
        eval_dir = _first_phase1_run(args.root.expanduser().resolve(), day, now)
    if args.show_run:
        print(eval_dir)
        return 0
    print(f"  RUN: {eval_dir}")
    cutoff_suffix = f"_{now:%Y%m%dT%H%M%SZ}_replay" if args.asof else ""
    _write_phase1_list(
        eval_dir,
        now_utc=now, min_lead=args.min_lead,
        filename=f"segunda_eleccion{cutoff_suffix}.md", excluded_ids=frozenset(args.exclude_fixture_id),
    )
    return 0


def _first_phase1_run(root: Path, day, now: datetime) -> Path:
    candidates = []
    for folder in (root / "outputs/discovery").glob("*"):
        metadata = folder / "phase1_run.json"
        discovery = folder / "fixtures.json"
        if metadata.is_file():
            doc = _read_json(metadata)
            window_day = doc.get("window_date_madrid")
            started = doc.get("started_at_utc")
        elif discovery.is_file() and (folder / "steps/discover.json").is_file():
            doc = _read_json(discovery)
            window_day = str((doc.get("window") or {}).get("start_madrid") or "")[:10]
            started = doc.get("generated_at_utc")
        else:
            continue
        if window_day == day.isoformat() and started and _now_utc(started) <= now:
            candidates.append((_now_utc(started), folder))
    if not candidates:
        raise StepError(f"PHASE1_RUN_NOT_FOUND_FOR_MADRID_DATE:{day}")
    return min(candidates)[1]


def _odds_header(path: Path, event_id: str) -> str | None:
    from motor_probabilistico import parse_odds

    parsed = parse_odds(path)
    if parsed["event_id"] != event_id:
        raise StepError(f"ODDS_SNAPSHOT_EVENT_ID_MISMATCH:{parsed['event_id']}!={event_id}")
    header = path.read_text(encoding="utf-8-sig", errors="replace")[:3000]
    url = re.search(r"^URL:[ \t]*(\S+)", header, re.MULTILINE)
    if url:
        url_id = re.search(r":(\d+)(?:[/?#]|$)", url[1])
        if url_id and url_id[1] != event_id:
            raise StepError("ODDS_SNAPSHOT_URL_EVENT_ID_MISMATCH")
    legacy_date = re.search(r"^(?:Generado|captured_at_utc|timestamp_utc|fecha_captura)\s*[:=]\s*(\S+)", header, re.MULTILINE | re.IGNORECASE)
    return parsed["capture"] or (legacy_date[1] if legacy_date else None)


def _validate_snapshot_times(stamps, target, cutoff, *, source="ODDS_SNAPSHOT"):
    kickoff = _now_utc(target.get("kickoff_utc") or target["kickoff_madrid"])
    for value in stamps:
        if value:
            stamp = _now_utc(value)
            if stamp >= kickoff:
                raise StepError(f"{source}_AT_OR_AFTER_KICKOFF:{value}")
            if stamp > cutoff:
                raise StepError(f"{source}_AFTER_ANALYSIS_CUTOFF:{value}")


def _resolve_odds_snapshot(eval_dir, target, cutoff, *, explicit=None, required=False):
    """Resolve the run's current capture with its original TXT and diagnostics."""
    eid = str(target.get("bwin_event_id") or "")
    fid = target["fixture_id"]
    if target["identity_state"] != "VERIFIED":
        if required or explicit:
            raise StepError(f"ODDS_REQUIRES_VERIFIED_BWIN_IDENTITY:{fid}")
        return None
    capture_dir = eval_dir / "prospective_trial/captures" / eid
    manifest = capture_dir / "capture_manifest.json"
    if capture_dir.resolve() != capture_dir or manifest.resolve() != manifest:
        raise StepError("ODDS_CAPTURE_MANIFEST_OUTSIDE_EVALUATION")
    capture = {}
    diagnostics = None
    if manifest.is_file():
        doc = _read_json(manifest)
        if doc.get("eval_dir") and Path(doc["eval_dir"]).resolve() != eval_dir:
            raise StepError("ODDS_CAPTURE_MANIFEST_RUN_MISMATCH")
        rows = doc.get("captures") or []
        if str(doc.get("fixture_id")) != str(fid) or str(doc.get("bwin_event_id")) != eid or len(rows) != 1:
            raise StepError(f"ODDS_CAPTURE_MANIFEST_IDENTITY_OR_COUNT_MISMATCH:{fid}:{eid}")
        capture = rows[0]
        if str(capture.get("fixture_id")) != str(fid) or str(capture.get("bwin_event_id")) != eid:
            raise StepError(f"ODDS_CAPTURE_RECORD_IDENTITY_MISMATCH:{fid}:{eid}")

        def manifest_file(name, suffix):
            value = capture.get(name + "_path")
            if not value:
                raise StepError(f"ODDS_CAPTURE_MANIFEST_PATH_MISSING:{name}")
            path = (capture_dir / value).resolve()
            if path.parent != capture_dir or path.suffix.lower() != suffix:
                raise StepError(f"ODDS_SNAPSHOT_OUTSIDE_EVALUATION:{path}")
            actual = _sha256_file(path)
            if actual is None:
                raise StepError(f"ODDS_SNAPSHOT_NOT_FOUND:{path}")
            expected = capture.get(name + "_sha256")
            if expected and expected != actual:
                raise StepError(f"ODDS_SNAPSHOT_HASH_MISMATCH:{path}")
            return path

        path = manifest_file("snapshot", ".txt")
        if explicit and explicit.expanduser().resolve() != path:
            raise StepError(f"ODDS_OVERRIDE_DIFFERS_FROM_RUN_MANIFEST:{path}")
        if capture.get("capture_diagnostics_path"):
            diagnostics = manifest_file("capture_diagnostics", ".json")
            diag = _read_json(diagnostics)
            if (str(diag.get("event_id")) != eid
                    or (diag.get("snapshot_sha256") and diag["snapshot_sha256"] != _sha256_file(path))):
                raise StepError("ODDS_CAPTURE_DIAGNOSTICS_IDENTITY_MISMATCH")
    elif explicit:
        path = explicit.expanduser().resolve()
    elif required:
        raise StepError(f"ODDS_CAPTURE_MANIFEST_MISSING:{manifest} (ejecuta odds-capture)")
    else:
        return None

    _require_file(path, "odds-capture")
    header_stamp = _odds_header(path, eid)
    registered = capture.get("captured_at_utc")
    _validate_snapshot_times((doc.get("generated_at_utc") if capture else None, registered, header_stamp), target, cutoff)
    effective_stamp = header_stamp or (registered if capture.get("capture_mode") == "SCRAPED_LIVE_BWIN" else None)
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "manifest_path": str(manifest) if capture else None,
        "manifest_hash_verified": bool(capture.get("snapshot_sha256")),
        "captured_at_utc": _now_utc(effective_stamp).isoformat() if effective_stamp else None,
        "registered_at_utc": registered,
        "diagnostics_path": str(diagnostics) if diagnostics else None,
    }


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
        scoped = [r for r in recon["all"] if (not args.fixture_id or r["fixture_id"] in args.fixture_id)
                  and (not args.event_id or str(r["bwin_event_id"]) == str(args.event_id))]
        for target in scoped:
            if not target["temporal_eligible"]:
                _record_temporal_block(eval_dir, args, target, now_utc, args.min_lead)
        if (args.fixture_id or args.event_id) and scoped:
            raise StepError("ODDS_CAPTURE_TARGET_BLOCKED:" + ",".join(f"{r['fixture_id']}:{r['reason']}" for r in scoped))
        raise StepError("NO_VERIFIED_BWIN_FIXTURES_FOR_ODDS_CAPTURE")
    if args.snapshot_file and len(verified) != 1:
        raise StepError("ODDS_SNAPSHOT_FILE_REQUIRES_ONE_TARGET")

    captured: list[dict[str, Any]] = []
    for rec in verified:
        eid = str(rec["bwin_event_id"])
        fid = int(rec["fixture_id"])
        inputs_dir = root / "outputs/matches" / eid / "inputs"
        canonical_txt = inputs_dir / f"cuotas_bwin_{eid}.txt"
        capture_dir = eval_dir / "prospective_trial/captures" / eid
        capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_utc.strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:8]
        immutable_txt = capture_dir / f"cuotas_bwin_{eid}_{stamp}.txt"
        target_txt: Path | None = None
        target_diagnostics: Path | None = None
        capture_mode = "MISSING"

        if args.snapshot_file:
            src = Path(args.snapshot_file).expanduser().resolve()
            if not src.is_file():
                raise StepError(f"SNAPSHOT_FILE_NOT_FOUND:{src}")
            _validate_snapshot_times((_odds_header(src, eid),), rec, now_utc)
            snapshot_bytes = src.read_bytes()
            immutable_txt.write_bytes(snapshot_bytes)
            target_txt = immutable_txt
            if not canonical_txt.is_file():
                inputs_dir.mkdir(parents=True, exist_ok=True)
                canonical_txt.write_bytes(snapshot_bytes)
                capture_mode = "COPIED_TO_CANONICAL_AND_IMMUTABLE_CAPTURE"
            else:
                capture_mode = "COPIED_TO_IMMUTABLE_CAPTURE_CANONICAL_PRESERVED"
        elif args.confirm_external:
            require_external_confirmation(True)
            import scraper_bwin as sb

            if hasattr(sb, "scrape_event"):
                sb.scrape_event(rec["bwin_url"], immutable_txt, headless=args.headless, timeout=args.timeout)
                target_txt = immutable_txt
            else:
                stage_stamp = now_utc.strftime("%Y%m%dT%H%M%S%fZ")
                stage_inputs = canonical_txt.parent.parent / "staging" / stage_stamp / str(eid) / "inputs"
                stage_inputs.mkdir(parents=True, exist_ok=True)
                prev_env = {k: os.environ.get(k) for k in ("PREMATCH_EVENT_DATA_DIR", "PREMATCH_IMMUTABLE_CAPTURE", "PREMATCH_EVENT_ID")}
                try:
                    os.environ["PREMATCH_EVENT_DATA_DIR"] = str(stage_inputs)
                    os.environ["PREMATCH_IMMUTABLE_CAPTURE"] = "1"
                    os.environ["PREMATCH_EVENT_ID"] = str(eid)
                    scraper_status = sb.main(["--url", rec["bwin_url"], "--confirm-external"])
                finally:
                    for k, v in prev_env.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                if scraper_status:
                    raise StepError(f"BWIN_SCRAPER_FAILED:{scraper_status}:{stage_inputs.parent / 'scraper'}")
                staged_txt = stage_inputs / f"cuotas_bwin_{eid}.txt"
                if not staged_txt.is_file():
                    raise StepError(f"STAGED_ODDS_SNAPSHOT_NOT_FOUND:{staged_txt}")
                immutable_txt.write_bytes(staged_txt.read_bytes())
                target_txt = immutable_txt
                staged_diagnostics = stage_inputs.parent / "scraper" / f"cuotas_bwin_{eid}.json"
                if staged_diagnostics.is_file():
                    target_diagnostics = capture_dir / f"cuotas_bwin_{eid}_{stamp}.json"
                    target_diagnostics.write_bytes(staged_diagnostics.read_bytes())
            captured_at = _now_utc()
            _validate_snapshot_times((_odds_header(target_txt, eid), captured_at.isoformat()), rec, captured_at)
            if not canonical_txt.is_file():
                inputs_dir.mkdir(parents=True, exist_ok=True)
                canonical_txt.write_bytes(target_txt.read_bytes())
                if target_diagnostics is not None:
                    canonical_diagnostics = canonical_txt.parent.parent / "scraper" / f"cuotas_bwin_{eid}.json"
                    canonical_diagnostics.parent.mkdir(parents=True, exist_ok=True)
                    canonical_diagnostics.write_bytes(target_diagnostics.read_bytes())
            capture_mode = "SCRAPED_LIVE_BWIN"
        elif canonical_txt.is_file():
            _validate_snapshot_times((_odds_header(canonical_txt, eid),), rec, now_utc)
            immutable_txt.write_bytes(canonical_txt.read_bytes())
            target_txt = immutable_txt
            capture_mode = "COPIED_EXISTING_LOCAL_TO_IMMUTABLE_CAPTURE"
        else:
            raise StepError(f"ODDS_SNAPSHOT_MISSING_FOR_EVENT:{eid} (usa `--confirm-external` o `--snapshot-file`)")

        audit = audit_bwin_catalog_txt(target_txt, 0)
        registered_at = captured_at if capture_mode == "SCRAPED_LIVE_BWIN" else now_utc
        capture_record = {
            "fixture_id": fid,
            "bwin_event_id": eid,
            "snapshot_path": str(target_txt),
            "snapshot_sha256": _sha256_file(target_txt),
            "capture_diagnostics_path": str(target_diagnostics) if target_diagnostics else None,
            "capture_diagnostics_sha256": _sha256_file(target_diagnostics) if target_diagnostics else None,
            "capture_mode": capture_mode,
            "captured_at_utc": registered_at.isoformat(),
            "total_markets": audit.get("total_markets_in_txt"),
            "total_selections": audit.get("total_selections_in_txt"),
        }
        captured.append(capture_record)
        # Autoridad por evento: evita que dos capturas de fixtures diferentes
        # compitan por el resumen global steps/odds_capture.json.
        manifest = capture_dir / "capture_manifest.json"
        if manifest.is_file():
            prior_bytes = manifest.read_bytes()
            prior_path = capture_dir / "manifests" / f"capture_manifest_{hashlib.sha256(prior_bytes).hexdigest()}.json"
            prior_path.parent.mkdir(exist_ok=True)
            try:
                with prior_path.open("xb") as archive:
                    archive.write(prior_bytes)
            except FileExistsError:
                pass
        _atomic_json(manifest, {
            "schema_version": SCHEMA_VERSION,
            "step": "odds-capture",
            "generated_at_utc": registered_at.isoformat(),
            "eval_dir": str(eval_dir),
            "fixture_id": fid,
            "bwin_event_id": eid,
            "captures": [capture_record],
        })
        print(f"  snapshot [{fid}/{eid}]: {target_txt}")
        print(f"  capture_manifest [{eid}]: {capture_dir / 'capture_manifest.json'}")

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
    min_lead = RESEARCH_UPDATE_MIN_LEAD_MINUTES if args.import_qualitative else ANALYSIS_MIN_LEAD_MINUTES
    target_rec = _resolve_target_rec_from_eval(eval_dir, args, now_utc, min_lead_minutes=min_lead)

    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    if not eid and not args.research_without_bwin:
        raise StepError(f"FIXTURE_WITHOUT_VERIFIED_BWIN_EVENT_ID:{fid} (usa `--research-without-bwin` para expediente solo investigación)")

    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    match_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id)
    research_dir = root / "outputs/matches" / dossier_id / "research"
    reports = _prepare_research_inbox(research_dir, dossier_id)

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
        if str(q_fid) != str(fid):
            raise StepError(f"QUALITATIVE_IDENTITY_MISMATCH:fixture_id={q_fid}!={fid}")
        if (eid or q_eid) and str(q_eid) != str(eid):
            raise StepError(f"QUALITATIVE_IDENTITY_MISMATCH:event_id={q_eid}!={eid}")
        q_ts = qdoc.get("analysis_cutoff_utc") or qdoc.get("generated_at_utc")
        if not q_ts:
            raise StepError("QUALITATIVE_MISSING_TIMESTAMP")
        timestamps = [qdoc[k] for k in ("analysis_cutoff_utc", "generated_at_utc") if qdoc.get(k)]
        ko_dt = _now_utc(ko_utc or ko_madrid)
        if any(_now_utc(value) >= ko_dt or _now_utc(value) > now_utc for value in timestamps):
            raise StepError("QUALITATIVE_TIMESTAMP_AFTER_CUTOFF_OR_AT_KICKOFF")
        if not qdoc.get("provenance") and not qdoc.get("sources"):
            raise StepError("QUALITATIVE_MISSING_PROVENANCE")
        if qdoc.get("actionable_bet") is True or qdoc.get("calibrated_p_model") is not None:
            raise StepError("QUALITATIVE_CANNOT_AUTHORIZE_BET_OR_INVENT_P_MODEL")
        dest_q = _preserved_output_dir(match_dir, "qualitative_imported.json") / "qualitative_imported.json"
        _atomic_json(dest_q, {
            **qdoc,
            "imported_at_utc": now_utc.isoformat(),
            "imported_source_sha256": _sha256_file(qpath),
            "verified_fixture_id": fid,
            "verified_event_id": eid,
            "eval_dir": str(eval_dir),
            "execution_mode": "REPLAY" if args.asof else "PREMATCH",
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

    pre_path, pre_match, tactical_path, tactical_fx = _fixture_statistical_inputs(
        eval_dir, target_rec, now_utc, research_only=args.research_without_bwin, required=True,
    )
    odds = _resolve_odds_snapshot(eval_dir, target_rec, now_utc)
    odds_txt = odds["path"] if odds else None
    odds_exists = bool(odds)
    external = _evaluate_research_inbox(reports, target_rec, now_utc)
    imported = []
    for path in [match_dir / "qualitative_imported.json", *sorted(match_dir.glob("revisions/*/qualitative_imported.json"))]:
        if path.is_file():
            doc = _read_json(path)
            if (doc.get("verified_fixture_id") == fid and str(doc.get("verified_event_id")) == str(eid)
                    and doc.get("eval_dir") in (None, str(eval_dir))
                    and doc.get("imported_at_utc") and _now_utc(doc["imported_at_utc"]) <= now_utc
                    and (args.asof or doc.get("execution_mode") != "REPLAY")):
                imported.append({"path": str(path), "sha256": _sha256_file(path),
                                 "imported_at_utc": doc["imported_at_utc"]})
    sources: dict[str, Any] = {
        "fixtures_major": {"path": str(eval_dir / "fixtures_major.json"), "sha256": _sha256_file(eval_dir / "fixtures_major.json")},
        "preselection": {"path": str(pre_path), "sha256": _sha256_file(pre_path)},
        "tactical_signals": {"path": str(tactical_path), "sha256": _sha256_file(tactical_path)} if tactical_path else None,
        "odds_bwin": odds,
    }
    match_dir = _preserved_output_dir(match_dir, "codex_reasoning_package.json", "codex_reasoning_input.md")
    package_json_path = match_dir / "codex_reasoning_package.json"
    codex_md_path = match_dir / "codex_reasoning_input.md"

    package = {
        "schema_version": "1.0-codex-reasoning-package",
        "generated_at_utc": now_utc.isoformat(),
        "execution_mode": "REPLAY" if args.asof else "PREMATCH",
        "eval_dir": str(eval_dir),
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
        "external_reports": external,
        "qualitative_imports": imported,
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
            "snapshot_sha256": odds["sha256"] if odds else None,
            "captured_at_utc": odds["captured_at_utc"] if odds else None,
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
        f"- Corte del paquete (UTC): `{now_utc.isoformat()}` · modo `{package['execution_mode']}`",
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
        f"- Captura disponible: `{odds_exists}`" + (f" (`{odds_txt}`, SHA-256 `{odds['sha256']}`, captura UTC `{odds['captured_at_utc'] or 'sin acreditar'}`)" if odds_exists else ""),
        "- Precio `PRECIO_PENDIENTE_DE_VERIFICACION` hasta comprobar vigencia.",
        "",
        "## 6. Advertencias, Carencias y Contradicciones",
        f"- Contradicciones: {tactical_fx.get('contradictions') or ['Ninguna registrada']}",
        f"- Bloqueos/carencias: {tactical_fx.get('block_reasons') or ['Ninguno registrado']}",
        f"- Investigación externa opcional: {len(external['accepted'])} aceptados, {len(external['rejected'])} rechazados y {len(external['ignored'])} pendientes.",
        "- Sin informes válidos, usa la evidencia local y declara cobertura cualitativa reducida.",
        "",
        "## 7. Fuentes y Hashes SHA-256",
    ])
    for name, ref in sources.items():
        if ref:
            md_lines.append(f"- `{name}`: `{ref['path']}` (`{ref['sha256']}`)")
    for key in ("accepted", "rejected", "ignored"):
        for report in external[key]:
            md_lines.append(f"- {report['source']} · {key} · `{report.get('path')}` · {report.get('reason') or report.get('generated_utc')}")
    for report in imported:
        md_lines.append(f"- Importación JSON · `{report['path']}` · SHA-256 `{report['sha256']}`.")
    md_lines.extend([f"- Contradicciones externas: {external['contradictions']}",
                     f"- Fuentes compartidas (una publicación por URL): {external['repeated_sources']}"])
    md_lines.extend([
        "",
        "## 8. Reglas de Razonamiento",
        "- No invocar APIs externas ni inventar probabilidades calibradas, cuota justa ni EV.",
        "- Separa mecanismo táctico y contraescenario, estimación probabilística acreditada, incertidumbre, liquidación literal y precio observado.",
        "- Si falta un dato crítico, identifica el mercado afectado. La ausencia de informes externos solo reduce cobertura.",
        "- En una revisión intermedia, devuelve hipótesis, contraevidencia y faltantes por mercado. Reserva PICK y NO BET para Shadow y para el análisis final. Usa el prompt final únicamente en esa etapa.",
        "- Distingue hipótesis exploratorias de decisiones financieras. Usa el expediente en Codex o Argon.",
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
    _print_step_summary("reasoning-package", "COMPLETED", "CODEX_PACKAGE_CREATED", payload["counts"],
                        {"codex_input": codex_md_path, "package_json": package_json_path, "step_json": step_json,
                         "snapshot": odds_txt, "research": research_dir})
    return 0


def _load_current_lineup_refresh(path, eval_dir, target, now_utc):
    available = []
    revisions = sorted((path.parent / "revisions").glob(f"*/{path.name}"))
    for candidate in [path, *revisions]:
        if not candidate.is_file():
            continue
        refresh = _read_json(candidate)
        if refresh.get("eval_dir") != str(eval_dir):
            continue
        for field, expected in (
            ("fixture_id", target.get("fixture_id")), ("event_id", target.get("bwin_event_id")),
            ("home_id", target.get("home_id")), ("away_id", target.get("away_id")),
        ):
            if expected is not None and str(refresh.get(field)) != str(expected):
                raise StepError(f"LINEUP_REFRESH_IDENTITY_MISMATCH:{field}")
        kickoff = _now_utc(target.get("kickoff_utc") or target["kickoff_madrid"])
        captured = refresh.get("refreshed_at_utc")
        if not captured or not refresh.get("kickoff_utc") or _now_utc(refresh["kickoff_utc"]) != kickoff:
            raise StepError("LINEUP_REFRESH_TEMPORAL_MISMATCH")
        stamp = _now_utc(captured)
        if stamp < kickoff and stamp <= now_utc:
            available.append((stamp, {**refresh, "artifact_path": str(candidate)}))
    return max(available, key=lambda row: row[0])[1] if available else None


def cmd_model(args: argparse.Namespace) -> int:
    from bet_engine.context.observation_store import ObservationStore
    from bet_engine.orchestration.shadow_pipeline import ShadowPipelineRunner, parse_bwin_odds_txt

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    target_rec = _resolve_target_rec_from_eval(
        eval_dir, args, now_utc, min_lead_minutes=args.min_lead
    )
    eid = str(target_rec.get("bwin_event_id") or "")
    if not eid or target_rec["identity_state"] != "VERIFIED":
        raise StepError(f"MODEL_REQUIRES_VERIFIED_BWIN_EVENT_ID:fixture={target_rec['fixture_id']}")
    fid = int(target_rec["fixture_id"])

    _, pre_match, _, _ = _fixture_statistical_inputs(eval_dir, target_rec, now_utc, required=True)

    import dataclasses
    bundle = ObservationStore.from_preselection_match(pre_match, cutoff_utc=now_utc.isoformat())
    bundle = dataclasses.replace(bundle, event_id=eid)
    bundle_dict = bundle.to_dict()

    odds = _resolve_odds_snapshot(eval_dir, target_rec, now_utc, explicit=args.odds_file)
    odds_path = Path(odds["path"]) if odds else None
    odds_map = getattr(args, "verified_catalog_odds", None)
    uses_verified_catalog = odds_map is not None
    if odds_map is None:
        odds_map = parse_bwin_odds_txt(odds_path, home_name=target_rec["home"],
                                     away_name=target_rec["away"], gender=target_rec.get("gender"))
    lineup_path = root / "outputs/matches" / eid / "market_phase2" / f"lineup_refresh_{eid}.json"
    lineup_refresh = _load_current_lineup_refresh(lineup_path, eval_dir, target_rec, now_utc)
    lineup_kwargs = {}
    if lineup_refresh and lineup_refresh.get("confirmed_complete_22"):
        lineup_kwargs["lineup_evaluation"] = lineup_refresh["lineup_evaluation"]
    early_keys = getattr(args, "lineup_independent_keys", frozenset())
    if early_keys:
        lineup_kwargs["lineup_independent_keys"] = early_keys
    accredited_fams = getattr(args, "calibration_accredited_families", None)
    if accredited_fams:
        lineup_kwargs["calibration_accredited_families"] = frozenset(accredited_fams)

    runner = ShadowPipelineRunner(min_lead_minutes=args.min_lead)
    report = runner.run_shadow_on_bundle(
        bundle,
        target_status=target_rec["fixture_status"] or "NS",
        odds_map=odds_map or None,
        bwin_txt_path=odds_path,
        **lineup_kwargs,
    )

    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / eid / "model")
    json_name, md_name = f"model_report_{eid}.json", f"model_report_{eid}.md"
    out_dir = _preserved_output_dir(out_dir, json_name, md_name)
    model_json = out_dir / json_name
    model_md = out_dir / md_name
    report_dict = report.to_dict() if hasattr(report, "to_dict") else {"decision": report.decision, "status": report.status, "no_bet_reasons": list(report.no_bet_reasons), "decision_classification": report.decision_classification, "formal_decision_breakdown_counts": report.formal_decision_breakdown_counts, "model_version": report.model_version, "bundle": bundle_dict}
    report_dict["execution"] = {
        "mode": "REPLAY" if args.asof else "PREMATCH", "evaluated_at_utc": now_utc.isoformat(),
        "eval_dir": str(eval_dir), "fixture_id": fid, "event_id": eid, "snapshot": odds,
        "odds_input": "VERIFIED_BATCH_CATALOG" if uses_verified_catalog else "BWIN_SNAPSHOT_PARSER",
        "price_status": "PRECIO_PENDIENTE_DE_VERIFICACION",
        "operational_status": "REPLAY_NOT_EXECUTABLE" if args.asof else "MODEL_COMPLETED",
    }
    if "lineup_evaluation" in lineup_kwargs:
        report_dict["lineup_refresh_ref"] = {
            "path": lineup_refresh["artifact_path"], "refreshed_at_utc": lineup_refresh["refreshed_at_utc"],
        }
    _atomic_json(model_json, report_dict)

    md_lines = [
        f"# Paso 10: Modelado (B0 Operativo + CAND_2B3B_V1 Experimental) · {eid}",
        "",
        f"- Fixture: `{fid}` ({target_rec['home']} – {target_rec['away']})",
        f"- Ejecución: `{report_dict['execution']['operational_status']}` | margen mínimo: {args.min_lead} min.",
        f"- Snapshot: `{odds_path or 'sin captura'}` · precio `PRECIO_PENDIENTE_DE_VERIFICACION`.",
        f"- Diagnóstico Shadow: **{report.decision}** (`{report.decision_classification}`)",
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
    console_counts = {"market_comparisons": counts["market_comparisons"], "shadow_decision": report.decision}
    _print_step_summary("model", report.status, report.decision_classification, console_counts,
                        {"model_json": model_json, "model_md": model_md, "step_json": step_json, "snapshot": odds_path})
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

    p_phase1 = sub.add_parser("phase1-run", parents=[common], help="Nueva corrida hasta el ranking estadístico")
    p_phase1.add_argument("--policy", choices=fml.VALID_POLICIES, default=DEFAULT_POLICY)
    p_phase1.add_argument("--confirm-external", action="store_true", help="Autorizar API-Football y Bwin")
    p_phase1.add_argument("--max-country-pages", type=int, default=12)
    p_phase1.add_argument("--max-scrolls", type=int, default=20)
    p_phase1.add_argument("--timeout", type=int, default=30000)
    p_phase1.add_argument("--min-lead", type=int, default=PHASE1_INITIAL_LEAD_MINUTES,
                          help="Margen mínimo en minutos para el listado al terminar (45 por defecto)")
    p_phase1.set_defaults(func=cmd_phase1_run)

    p_second = sub.add_parser("phase1-list", help="Siguientes elecciones desde la corrida original, sin red")
    run_source = p_second.add_mutually_exclusive_group(required=True)
    run_source.add_argument("--eval-dir", type=Path, help="Carpeta de la corrida existente")
    run_source.add_argument("--first-run", action="store_true", help="Recuperar la primera corrida de la fecha Madrid")
    p_second.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    p_second.add_argument("--date", help="Fecha de inicio de la ventana Madrid, por defecto hoy")
    p_second.add_argument("--asof", help="Corte de consulta con offset para replay")
    p_second.add_argument("--show-run", action="store_true", help="Mostrar solo la ruta del RUN")
    p_second.add_argument("--min-lead", type=int, default=PHASE1_SECOND_LEAD_MINUTES,
                          help="Margen mínimo en minutos desde ahora (120 por defecto)")
    p_second.add_argument("--exclude-fixture-id", type=int, action="append", default=[],
                          help="Fixture ID ya elegido, se puede repetir")
    p_second.set_defaults(func=cmd_phase1_list)

    for command, help_text in (
        ("hour-batch-prepare", "Cruzar señales y cuotas por hora de inicio Madrid"),
        ("hour-batch-refresh", "Actualizar XI, cuotas y Shadow para los prefinalistas del lote"),
    ):
        batch = sub.add_parser(command, help=help_text)
        batch.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
        batch.add_argument("--eval-dir", required=True, type=Path, help="Corrida existente")
        batch.add_argument("--hour", required=True, type=df.parse_hour,
                           help="Inicio Madrid: dos horas; desde 23:00 cubre el resto de la ventana hasta su cierre")
        batch.add_argument("--date", help="Fecha Madrid del lote si hace falta desambiguar")
        batch.add_argument("--confirm-external", action="store_true", help="Autorizar capturas Bwin y, en el cierre, consulta de XI")
        batch.add_argument("--bankroll", type=float, default=1000.0,
                           help="Bankroll de referencia para calcular el monto exacto por apuesta (1000.0 por defecto)")
        batch.add_argument("--currency", default="EUR",
                           help="Moneda del bankroll para la boleta de ejecución (EUR por defecto)")
        if command == "hour-batch-refresh":
            batch.add_argument("--shortlist", required=True, type=Path, help="JSON de prefinalistas devuelto por Codex o Argon")
            batch.add_argument("--manual-lineups-json", type=Path,
                               help="XI confirmados externos del fixture, con fuente, capturas y hora verificables")
            batch.add_argument("--lineup-mode", choices=("sin-xi", "xi-confirmado", "auto"), default="xi-confirmado",
                               help="sin-xi ignora XI; xi-confirmado los exige; auto consulta cada fixture y usa la vía disponible")
            batch.add_argument("--fixture-id", type=int, action="append", default=[],
                               help="Cerrar solo estos fixtures de la lista; opción repetible")
        else:
            batch.add_argument("--fixture-id", type=int, action="append", default=[],
                               help="Preparar solo estos fixtures de la hora; opción repetible")
            batch.add_argument("--refresh-todo", action="store_true",
                               help="Renovar las capturas que carezcan de verificación de Todo en el DOM")
            batch.add_argument("--export-gemini", action="store_true",
                               help="Crear un encargo y un inventario de cuotas para adjuntar a Gemini")
        batch.set_defaults(func=cmd_hour_batch, start=None, end=None, window=None, asof=None)

    batch_research = sub.add_parser("hour-batch-research", help="Preparar investigación cualitativa por finalista del lote")
    batch_research.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    batch_research.add_argument("--eval-dir", required=True, type=Path, help="Corrida existente")
    batch_research.add_argument("--hour", required=True, type=df.parse_hour,
                                help="Hora inicial del paquete en Europe/Madrid")
    batch_research.add_argument("--date", help="Fecha Madrid del lote si hace falta desambiguar")
    batch_research.add_argument("--shortlist", required=True, type=Path, help="JSON de prefinalistas devuelto por Codex o Argon")
    batch_research.add_argument("--provider", choices=("gemini", "grok", "perplexity"), default="gemini")
    batch_research.add_argument("--research-mode", choices=("focused", "deep", "update"), default="focused",
                                help="focused: preguntas prioritarias; deep: investigación amplia; update: novedades")
    batch_research.add_argument("--fixture-id", type=int, action="append", default=[], help="Limitar a fixtures concretos; se puede repetir")
    batch_research.set_defaults(func=cmd_hour_batch, start=None, end=None, window=None, asof=None,
                                confirm_external=False, refresh_todo=False)

    p_sel = sub.add_parser("select-fixture", parents=[common], help="Seleccionar fixture y preparar recepción de informes")
    p_sel.add_argument("--fixture-id", type=int)
    p_sel.add_argument("--event-id")
    p_sel.add_argument("--research-without-bwin", action="store_true")
    p_sel.set_defaults(func=cmd_select_fixture)

    p_update = sub.add_parser("fixture-update", parents=[common], help="Diagnosticar y actualizar selectivamente un fixture omitido")
    p_update.add_argument("--fixture-id", required=True, type=int)
    p_update.add_argument("--apply", action="store_true", help="Aplicar cambios locales y regenerar ranking si corresponde")
    p_update.add_argument("--confirm-external", action="store_true", help="Autorizar Bwin y/o histórico API únicamente para este fixture")
    p_update.set_defaults(func=cmd_fixture_update)

    p_audit = sub.add_parser("audit-event", parents=[common], help="Inventario de archivos y procedencia por evento")
    p_audit.add_argument("--fixture-id", type=int)
    p_audit.add_argument("--event-id")
    p_audit.add_argument("--output-dir", type=Path)
    p_audit.set_defaults(func=cmd_audit_event)

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
    p_mod.add_argument("--min-lead", type=int, default=ANALYSIS_MIN_LEAD_MINUTES,
                       help="Mínimo efectivo 30 min. Esta opción permite exigir un margen mayor")
    p_mod.set_defaults(func=cmd_model)

    p_stat = sub.add_parser("status", parents=[common], help="Consultar estado del pipeline modular")
    p_stat.set_defaults(func=cmd_status)

    p_es = sub.add_parser("early-scout", parents=[common], help="EARLY_TACTICAL_SCOUT independiente sin cuotas (EXPERIMENTAL_SHADOW)")
    p_es.add_argument("--fixture-id", type=int)
    p_es.add_argument("--event-id")
    p_es.add_argument("--external-report", action="append", default=[], type=Path, help="Ruta a informe cualitativo externo (.md)")
    p_es.add_argument("--import-analysis", type=Path, help="Ruta a JSON de analisis tactico generado por Codex/IA")
    p_es.add_argument("--output-dir", type=Path)
    p_es.add_argument("--research-without-bwin", action="store_true")
    p_es.add_argument("--research-phase", choices=("deep", "update"), help="Prepara encargo manual en T−6/T−4 h o T−60/T−15 min")
    p_es.add_argument("--research-provider", choices=("perplexity", "grok"), help="Proveedor único para novedades; deep usa Gemini")
    p_es.add_argument("--research-question", action="append", default=[], help="Pregunta pendiente; se puede repetir")
    p_es.add_argument("--source-registry", action="append", default=[], type=Path,
                      help="Registro JSON del mismo fixture o encargo previo; se puede repetir")
    p_es.set_defaults(func=cmd_early_scout)

    p_mc = sub.add_parser("market-catalog", parents=[common], help="Extraer catalogo comercial Bwin completo (EXPERIMENTAL_SHADOW)")
    p_mc.add_argument("--fixture-id", type=int)
    p_mc.add_argument("--event-id")
    p_mc.add_argument("--odds-file", type=Path, help="Override histórico si el RUN carece de manifiesto de captura")
    p_mc.add_argument("--capture-diagnostics", type=Path, help="JSON del scraper correspondiente al snapshot")
    p_mc.add_argument("--captured-at-utc")
    p_mc.add_argument("--min-lead", type=int, default=ANALYSIS_MIN_LEAD_MINUTES,
                      help="Override explícito del margen analítico; nunca permite post-kickoff")
    p_mc.add_argument("--output-dir", type=Path)
    p_mc.set_defaults(func=cmd_market_catalog)

    p_mm = sub.add_parser("market-match", parents=[common], help="Vincular hipotesis tacticas con catalogo Bwin (EXPERIMENTAL_SHADOW)")
    p_mm.add_argument("--fixture-id", type=int)
    p_mm.add_argument("--event-id")
    p_mm.add_argument("--scout-json", type=Path, required=True)
    p_mm.add_argument("--catalog-json", type=Path, required=True)
    p_mm.add_argument("--min-lead", type=int, default=ANALYSIS_MIN_LEAD_MINUTES,
                      help="Override explícito del margen analítico; nunca permite post-kickoff")
    p_mm.add_argument("--output-dir", type=Path)
    p_mm.set_defaults(func=cmd_market_match)

    p_cmp = sub.add_parser("market-compare", parents=[common], help="Comparador tactico de alternativas Bwin en 2 etapas (EXPERIMENTAL_SHADOW)")
    p_cmp.add_argument("--fixture-id", type=int)
    p_cmp.add_argument("--event-id")
    p_cmp.add_argument("--scout-json", type=Path, required=True)
    p_cmp.add_argument("--catalog-json", type=Path, required=True)
    p_cmp.add_argument("--match-json", type=Path, required=True)
    p_cmp.add_argument("--import-comparison", type=Path, help="Ruta a JSON de evaluacion comparativa generado por Codex/IA")
    p_cmp.add_argument("--max-price-age", type=int, default=180)
    p_cmp.add_argument("--min-lead", type=int, default=ANALYSIS_MIN_LEAD_MINUTES,
                       help="Override explícito del margen analítico; nunca permite post-kickoff")
    p_cmp.add_argument("--output-dir", type=Path)
    p_cmp.set_defaults(func=cmd_market_compare)

    p_lr = sub.add_parser("lineup-refresh", parents=[common], help="Refrescar expediente con XI oficiales o probables (EXPERIMENTAL_SHADOW)")
    p_lr.add_argument("--fixture-id", type=int)
    p_lr.add_argument("--event-id")
    p_lr.add_argument("--scout-json", type=Path, required=True)
    p_lr.add_argument("--api-lineups-json", type=Path, help="Ruta a respuesta JSON /fixtures/lineups de API-Football")
    p_lr.add_argument("--qualitative-md", type=Path, help="Ruta a informe cualitativo con XI probable")
    p_lr.add_argument("--watch-player", action="append", default=[], help="Jugador a seguir en exposicion (titular/suplente/ausente)")
    p_lr.add_argument("--output-dir", type=Path)
    p_lr.set_defaults(func=cmd_lineup_refresh)

    return parser


def _resolve_target_rec_from_eval(
    eval_dir: Path,
    args: argparse.Namespace,
    now_utc: datetime,
    *,
    min_lead_minutes: int = ANALYSIS_MIN_LEAD_MINUTES,
    audit: bool = False,
) -> dict[str, Any]:
    if audit and not (eval_dir / "fixtures_major.json").is_file():
        major_doc = _read_json(_require_file(eval_dir / "fixtures.json", "discover"))
    else:
        _, major_doc = _load_major_doc(eval_dir)
    if audit:
        major_doc = {**major_doc, "fixtures": [
            *(major_doc.get("fixtures") or []), *(major_doc.get("review") or []), *(major_doc.get("excluded") or []),
        ]}
    recon = _bwin_reconcile_records(eval_dir, major_doc, now_utc, min_lead_minutes=min_lead_minutes)
    fid = getattr(args, "fixture_id", None)
    eid = getattr(args, "event_id", None)
    matches = [
        r for r in recon["all"]
        if (fid or eid)
        and (not fid or int(r["fixture_id"]) == int(fid))
        and (not eid or str(r.get("bwin_event_id")) == str(eid))
    ]
    if len(matches) != 1:
        raise StepError("TARGET_FIXTURE_OR_EVENT_NOT_FOUND_IN_EVAL_DIR" if not matches else "AMBIGUOUS_TARGET")
    target = matches[0]
    if audit:
        return target
    if not target["temporal_eligible"]:
        if getattr(args, "command", None):
            _record_temporal_block(eval_dir, args, target, now_utc, min_lead_minutes)
        raise StepError(target["temporal_reason"])
    if target["identity_state"] == "CONFLICT" or (eid and target["identity_state"] != "VERIFIED"):
        raise StepError(f"TARGET_IDENTITY_NOT_VERIFIED:{target['identity_reason']}")
    if (getattr(args, "command", None) in {"reasoning-package", "early-scout", "lineup-refresh"}
            and target["identity_state"] != "VERIFIED"):
        if not getattr(args, "research_without_bwin", False):
            raise StepError(f"TARGET_IDENTITY_NOT_VERIFIED:{target['identity_reason']}")
        return {**target, "bwin_event_id": None, "bwin_url": None}
    return target


def _match_dir(root, target) -> Path:
    eid = target["bwin_event_id"] if target["identity_state"] == "VERIFIED" else None
    return root / "outputs/matches" / (str(eid) if eid else f"research_fixture_{target['fixture_id']}")


def _temporal_actions(target, now):
    from bet_engine.analytics.early_tactical_scout import RESEARCH_WINDOWS

    kickoff = _now_utc(target.get("kickoff_utc") or target.get("kickoff_madrid")) if target.get("kickoff_utc") or target.get("kickoff_madrid") else None
    lead = (kickoff - now).total_seconds() / 60 if kickoff else None
    ranges = {
        "select-fixture": (0, None), "bwin-scan": (45, None), "bwin-verify": (bml.MANUAL_BWIN_MIN_LEAD_MINUTES, None),
        "stats-fetch": (STATS_MIN_LEAD_MINUTES, None), "odds-capture": (45, None),
        "reasoning-package": (ANALYSIS_MIN_LEAD_MINUTES, None), "model": (ANALYSIS_MIN_LEAD_MINUTES, None),
        "early-scout": (EARLY_SCOUT_MIN_LEAD_MINUTES, None), "import-qualitative": (RESEARCH_UPDATE_MIN_LEAD_MINUTES, None),
        "lineup-refresh": (5, None),
        "market-catalog/match/compare (por defecto)": (ANALYSIS_MIN_LEAD_MINUTES, None),
        **{f"research-{phase}": window for phase, window in RESEARCH_WINDOWS.items()},
    }
    return {
        name: bool(target.get("fixture_status") in {"", "NS", "TBD"} and lead and lead > 0
                   and lead >= minimum and (maximum is None or lead <= maximum))
        for name, (minimum, maximum) in ranges.items()
    } | {"audit-event": True}


def _record_temporal_block(eval_dir, args, target, now, minimum):
    dossier = _match_dir(args.root.expanduser().resolve(), target)
    path = dossier / "operations" / f"{args.command}_{now:%Y%m%dT%H%M%S%fZ}_{uuid4().hex[:8]}.json"
    actions = _temporal_actions(target, now)
    _atomic_json(path, {
        "action": args.command, "eval_dir": str(eval_dir), "fixture_id": target["fixture_id"],
        "bwin_event_id": target["bwin_event_id"], "identity_state": target["identity_state"],
        "status": "OPERATION_BLOCKED_BY_TIME", "reason": target["temporal_reason"], "model_decision": None,
        "minimum_lead_minutes": minimum, "lead_minutes": target["lead_minutes"],
        "kickoff_utc": target["kickoff_utc"], "evaluated_at_utc": now.isoformat(),
        "execution_mode": "REPLAY" if args.asof else "PREMATCH",
        "available_by_time": actions,
    })
    print(f"  bloqueo_temporal: {path}")
    print("  Acciones por margen (sujetas a identidad e insumos): " + ", ".join(name for name, ok in actions.items() if ok))


def _prepare_research_inbox(folder, dossier_id):
    from bet_engine.analytics.early_tactical_scout import RESEARCH_PLACEHOLDER, RESEARCH_PROVIDERS

    folder.mkdir(parents=True, exist_ok=True)
    reports = []
    for provider in RESEARCH_PROVIDERS:
        path = folder / f"{provider}_{dossier_id}.md"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(RESEARCH_PLACEHOLDER + "\n")
        except FileExistsError:
            pass
        reports.append({"source": provider, "path": str(path), "discovered": True})
    return reports


def _evaluate_research_inbox(reports, target, now):
    from bet_engine.analytics.early_tactical_scout import evaluate_external_reports

    return evaluate_external_reports(
        reports, fixture_id=target["fixture_id"], event_id=target["bwin_event_id"],
        home=target["home"], away=target["away"],
        kickoff_utc=_now_utc(target.get("kickoff_utc") or target["kickoff_madrid"]),
        evidence_cutoff_utc=now,
    )


def _fixture_statistical_inputs(eval_dir, target, cutoff, *, research_only=False, required=False):
    fid = target["fixture_id"]
    base = eval_dir / "prospective_trial"
    pre_path = base / "enriched/preselection.json"
    research_path = base / "research_only/preselection.json"
    if research_only and research_path.is_file():
        pre_path = research_path
    pre_match = None
    if pre_path.is_file():
        pre = _read_json(pre_path)
        pre_match = next((m for m in pre.get("matches") or [] if int(m["fixture_id"]) == fid), None)
        if pre_match:
            _validate_snapshot_times((pre.get("generated_at_utc"), pre_match.get("analysis_cutoff_utc")),
                                     target, cutoff, source="PRESELECTION")
            for key in ("home_id", "away_id", "league_id", "season", "gender", "kickoff_madrid"):
                if pre_match.get(key) is not None and target.get(key) is not None and pre_match[key] != target[key]:
                    raise StepError(f"PRESELECTION_FIXTURE_IDENTITY_MISMATCH:{fid}:{key}")
    elif required:
        _require_file(pre_path, "stats-fetch")
    if required and pre_match is None:
        raise StepError(f"FIXTURE_NOT_IN_PRESELECTION:{fid}")

    tactical_path = base / "tactical/tactical_signal_engine.json"
    if not pre_match or not tactical_path.is_file():
        return pre_path, pre_match, None, {}
    tactical = _read_json(tactical_path)
    signals = (tactical.get("fixtures") or {}).get(str(fid)) or {}
    if signals and (tactical.get("sources") or {}).get("preselection_sha256") == _sha256_file(pre_path):
        rank_at = tactical.get("asof_madrid")
        if rank_at and _now_utc(rank_at) <= cutoff:
            return pre_path, pre_match, tactical_path, signals
    return pre_path, pre_match, None, {}


def cmd_select_fixture(args):
    now = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now)
    target = _resolve_target_rec_from_eval(eval_dir, args, now, min_lead_minutes=0)
    if target["identity_state"] != "VERIFIED" and not args.research_without_bwin:
        raise StepError(f"SELECTION_REQUIRES_VERIFIED_BWIN_IDENTITY:{target['identity_reason']}")
    dossier = _match_dir(root, target)
    reports = _prepare_research_inbox(dossier / "research", dossier.name)
    _print_step_summary("select-fixture", "COMPLETED", "RESEARCH_INBOX_READY", {},
                        {"event_dir": dossier, **{r["source"]: r["path"] for r in reports}})
    return 0


def _filter_row(doc, fid):
    return next((row for key in ("fixtures", "review", "excluded") for row in doc.get(key, [])
                 if row["fixture_id"] == fid), {})


def cmd_fixture_update(args):
    """Plan from local data, then apply only the stages the target still needs."""
    from shutil import copytree

    now = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now)
    fid = args.fixture_id
    discovery_path = _require_file(eval_dir / "fixtures.json", "discover")
    discovery = _read_json(discovery_path)
    fixture = next((f for f in discovery["fixtures"] if f["fixture_id"] == fid), None)
    if fixture is None:
        raise StepError(f"FIXTURE_NOT_IN_ORIGINAL_DISCOVERY_WINDOW:{fid}")
    major_path, major = _load_major_doc(eval_dir)
    old_row = _filter_row(major, fid)
    policy = (major.get("policy") or {}).get("competition_tier", "TOP_TIER_AND_SENIOR_CUPS")
    policy = policy.casefold().replace("_", "-")
    refreshed = fml.filter_document(discovery, discovery_path.read_bytes(), policy,
                                    refresh_fixture_id=fid, previous=major)
    new_row = _filter_row(refreshed, fid)
    candidate_doc = {**major, "fixtures": [fixture]}
    target = _bwin_reconcile_records(eval_dir, candidate_doc, now, min_lead_minutes=0)["all"][0]
    pre_path = eval_dir / "prospective_trial/enriched/preselection.json"
    rank_path = eval_dir / "prospective_trial/tactical/tactical_signal_engine.json"
    pre = _read_json(pre_path) if pre_path.is_file() else {}
    rank = _read_json(rank_path) if rank_path.is_file() else {}
    history_match = next((m for m in pre.get("matches", []) if m["fixture_id"] == fid), None)
    history_present = history_match is not None and _same_fixture_history(history_match, new_row)
    history_filter_changed = bool(pre and pre.get("source_filter_sha256") != _sha256_file(major_path))
    rank_present = any(m["fixture_id"] == fid for m in rank.get("statistical_ranking", []))
    rank_current = bool(rank) and (rank.get("sources") or {}).get("preselection_sha256") == _sha256_file(pre_path)
    filter_changed = old_row != new_row
    eligible = new_row.get("filter_status") == "ELIGIBLE"
    bwin_dir = eval_dir / "prospective_trial/bwin"
    catalog = bwin_dir / "catalog_observed.json"
    observed = []
    if catalog.is_file() and eligible:
        local_events = bd.load_local_catalog_snapshots([catalog])
        match = bd.summarize(new_row, bd.match_catalog(new_row, local_events), None, 25000, inspect=False)
        observed = [str(match["bwin_event_id"])] if match.get("bwin_event_id") else []
    actions = _temporal_actions(target, now)
    print(f"[fixture-update] RUN={eval_dir} | fixture={fid} | margen={target['lead_minutes']} min")
    print(f"  filtro: {old_row.get('filter_status', 'MISSING')} / {old_row.get('filter_reason')} → {new_row.get('filter_status')} / {new_row.get('filter_reason')} | {major_path}")
    print(f"  Bwin: {target['identity_state']} ({target['identity_reason']}) | Event ID observado={','.join(observed) or target['bwin_event_id'] or '—'}")
    print(f"  histórico: {'PRESENTE' if history_present else 'AUSENTE_O_IDENTIDAD_DISTINTA'} | fuente filtro {'DIFERENTE' if history_filter_changed else 'SIN_DIVERGENCIA_DE_HASH'} | {pre_path}")
    print(f"  ranking: {'PRESENTE' if rank_present else 'AUSENTE'} | enlace al histórico {'VIGENTE' if rank_current else 'DESACTUALIZADO_O_FALTANTE'} | {rank_path}")
    origin = ("FILTRO" if not eligible or not old_row or old_row.get("filter_status") != "ELIGIBLE"
              else "VINCULO_BWIN" if target["identity_state"] != "VERIFIED"
              else "HISTORICO" if not history_present else "RANKING" if not rank_present or not rank_current else "NINGUNA")
    print(f"  primera etapa pendiente: {origin}")
    print(f"  margen por acción: scan={'OK' if actions['bwin-scan'] else 'BLOQUEADO'} (45 min), "
          f"verificación manual={'OK' if actions['bwin-verify'] else 'BLOQUEADA'} ({bml.MANUAL_BWIN_MIN_LEAD_MINUTES} min), "
          f"histórico={'OK' if actions['stats-fetch'] else 'BLOQUEADO'} ({STATS_MIN_LEAD_MINUTES} min), "
          f"expediente/modelo={'OK' if actions['model'] else 'BLOQUEADO'} ({ANALYSIS_MIN_LEAD_MINUTES} min).")
    if not args.apply:
        return 0
    if not target["temporal_eligible"]:
        _record_temporal_block(eval_dir, args, target, now, 0)
        raise StepError(target["temporal_reason"])

    stage_args = ["--root", str(root), "--eval-dir", str(eval_dir)]
    if args.asof:
        stage_args.extend(("--asof", args.asof))
    parser = build_parser()

    def run_stage(name, *options, **overrides):
        stage = parser.parse_args([name, *stage_args, *options])
        vars(stage).update(overrides)
        return stage.func(stage)

    if filter_changed:
        archive = eval_dir / "filter_archive" / f"fixtures_major_{_sha256_file(major_path)}.json"
        archive.parent.mkdir(exist_ok=True)
        if not archive.exists():
            archive.write_bytes(major_path.read_bytes())
        _save_filter_step(eval_dir, refreshed, now, policy)
        print(f"  filtro actualizado: {major_path} | versión anterior: {archive}")
    if not eligible:
        return 0
    if target["identity_state"] not in {"VERIFIED", "CONFLICT"}:
        if args.confirm_external and actions["bwin-scan"]:
            run_stage("bwin-scan", "--fixture-id", str(fid), "--confirm-external")
        elif observed and (filter_changed or target["bwin_event_id"] is None):
            run_stage("bwin-scan", "--fixture-id", str(fid), "--catalog-snapshot", str(catalog))
        _, major = _load_major_doc(eval_dir)
        target = _bwin_reconcile_records(eval_dir, {**major, "fixtures": [new_row]}, _now_utc(args.asof),
                                         min_lead_minutes=0)["all"][0]
    if target["identity_state"] != "VERIFIED":
        print(f"  histórico/ranking pendientes de verificación Bwin: {target['identity_reason']}")
        return 0
    if target["temporal_eligible"] and (not history_present or history_filter_changed or filter_changed):
        if not history_present and (not args.confirm_external or not _temporal_actions(target, _now_utc(args.asof))["stats-fetch"]):
            print("  histórico pendiente: se requiere confirmación externa y al menos 45 minutos antes del inicio.")
            return 0
        run_stage("stats-fetch", "--fixture-id", str(fid), reuse_matching_history=True, local_reuse_only=history_present)
        history_present = any(m["fixture_id"] == fid for m in _read_json(pre_path).get("matches", []))
        if not history_present:
            raise StepError(f"FIXTURE_HISTORY_NOT_ACQUIRED:{fid}")
    if history_present and (not rank_present or not rank_current or (rank.get("sources") or {}).get("preselection_sha256") != _sha256_file(pre_path)):
        current = _now_utc(args.asof)
        _resolve_target_rec_from_eval(eval_dir, args, current, min_lead_minutes=0)
        if rank_path.is_file():
            archive = eval_dir / "ranking_archive" / _sha256_file(rank_path)
            if not archive.exists():
                copytree(rank_path.parent, archive)
            print(f"  ranking anterior: {archive}")
        run_stage("patterns")
    return 0


def _batch_audit_files(eval_dir, fid, eid, kickoff):
    references, recorded = {}, {}
    for lot_path in sorted(eval_dir.glob("hour_batches/*/packets/*/lote.json")):
        lot = _read_json(lot_path)
        if lot.get("eval_dir") != str(eval_dir) or not any(
            row.get("fixture_id") == fid and str(row.get("bwin_event_id")) == str(eid) for row in lot.get("fixtures", [])
        ):
            continue
        packet = lot_path.parent
        event_dir = packet / "events" / str(fid)
        paths = [lot_path, event_dir / "catalogo.json", packet / "prefinalistas.json"]
        paths.extend(sorted((event_dir / "research" / "responses").glob("*.md")))
        for close_path in sorted(packet.glob("refresh/*/cierre.json")):
            close = _read_json(close_path)
            if close.get("packet_id") != lot.get("packet_id") or not any(
                row.get("fixture_id") == fid and str(row.get("bwin_event_id")) == str(eid) for row in close.get("fixtures", [])
            ):
                continue
            event = close_path.parent / str(fid)
            named = event / f"instruccion_cierre_llm_{fid}.txt"
            paths.extend([close_path, event / "estado.json", event / "revision_final_codex_argon.md",
                          named if named.is_file() else event / "instruccion_agente_local.txt",
                          event / ("resultado_cierre_llm.md" if named.is_file() else "resultado_final.md")])
            paths.extend(path for path in (event / "catalogo_cierre.json", event / "auditoria_informes.json",
                                          event / "model" / f"model_report_{eid}.json") if path.is_file())
            shortlist = Path(close["shortlist_path"]) if close.get("shortlist_path") else packet / "prefinalistas.json"
            if shortlist.is_relative_to(eval_dir):
                paths.append(shortlist)
                if close.get("evaluated_at_utc") and _now_utc(close["evaluated_at_utc"]) < kickoff:
                    recorded[(shortlist, close.get("shortlist_sha256"))] = str(close_path)
        for path in paths:
            references[str(path.relative_to(eval_dir))] = path
    return references, recorded


def cmd_audit_event(args):
    from bet_engine.analytics.early_tactical_scout import RESEARCH_PLACEHOLDER, RESEARCH_PROVIDERS

    now = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now)
    target = _resolve_target_rec_from_eval(eval_dir, args, now, min_lead_minutes=0, audit=True)
    fid = target["fixture_id"]
    eid = target["bwin_event_id"]
    dossier = _match_dir(root, target)
    key = dossier.name
    kickoff = _now_utc(target.get("kickoff_utc") or target["kickoff_madrid"])
    live_record = not args.asof and target["temporal_eligible"]
    cutoff = min(now, kickoff)
    capture_dir = eval_dir / "prospective_trial/captures" / str(eid)
    base = eval_dir / "prospective_trial"
    audit_dir = args.output_dir.expanduser().resolve() if args.output_dir else dossier / "audit"
    references = {
        "discovery": eval_dir / "fixtures.json", "filter": eval_dir / "fixtures_major.json",
        "Bwin discovery": base / "bwin/bwin_discovery.json",
        "Bwin catalog observado": base / "bwin/catalog_observed.json",
        "vínculos manuales": base / "bwin/manual_bwin_links.json",
        "reconciliación": base / "bwin/bwin_scan_reconciliation.json",
        "histórico": base / "enriched/preselection.json",
        "ranking": base / "tactical/tactical_signal_engine.json",
        "señales del fixture": base / "tactical/fixtures" / f"{fid}.json",
        "manifiesto de captura": capture_dir / "capture_manifest.json",
        "TXT de cuotas": capture_dir / f"cuotas_bwin_{eid}_<STAMP>.txt",
        "diagnóstico de captura": capture_dir / f"cuotas_bwin_{eid}_<STAMP>.json",
        "copia canónica de cuotas": dossier / "inputs" / f"cuotas_bwin_{eid}.txt",
        "entrada Codex solicitada": dossier / f"CODEX_{key}.md",
        "manifiesto del expediente": dossier / "manifest.json",
        "validación del expediente": dossier / "validation" / f"validacion_{key}.json",
        "expediente": dossier / "codex_reasoning_package.json",
        "entrada de razonamiento": dossier / "codex_reasoning_input.md",
        "scout": dossier / "early_scout" / f"early_scout_input_{key}.json",
        "catálogo comercial": dossier / "market_phase2" / f"bwin_catalog_{key}.json",
        "cruce de hipótesis": dossier / "market_phase2" / f"market_match_{key}.json",
        "modelo": dossier / "model" / f"model_report_{key}.json",
        "análisis original": dossier / "analysis" / f"analisis_{key}.md",
        "cualitativo fusionado": dossier / "research" / f"qualitative_merged_{key}.md",
        **{provider: dossier / "research" / f"{provider}_{key}.md" for provider in RESEARCH_PROVIDERS},
    }
    alerts = []
    odds = None
    if target["identity_state"] == "VERIFIED":
        try:
            odds = _resolve_odds_snapshot(eval_dir, target, cutoff)
        except (StepError, ValueError, OSError) as exc:
            alerts.append(str(exc))
        else:
            if odds:
                references["TXT de cuotas"] = Path(odds["path"])
                if odds["diagnostics_path"]:
                    references["diagnóstico de captura"] = Path(odds["diagnostics_path"])
    else:
        alerts.append(f"BWIN_IDENTITY_{target['identity_state']}:{target['identity_reason']}")
    batch_files, recorded_shortlists = _batch_audit_files(eval_dir, fid, eid, kickoff)
    references.update(batch_files)
    for path in sorted((capture_dir / "manifests").glob("capture_manifest_*.json")):
        references[f"manifiesto anterior {path.stem}"] = path
    for folder, suffixes in (
        ("analysis", ("*.md",)), ("operations", ("*.json",)),
        ("early_scout", ("*.md", "*.json")), ("research/requests", ("*.md", "*.json")),
        ("market_phase2", ("*.md", "*.json")), ("model", ("*.md", "*.json")),
        ("revisions", ("*.md", "*.json")),
    ):
        for suffix in suffixes:
            for path in sorted((dossier / folder).rglob(suffix)):
                if path not in references.values():
                    references[str(path.relative_to(dossier))] = path

    pre_records = {}
    for path in audit_dir.glob("audit_event_*.json"):
        previous = _read_json(path)
        if (previous.get("mode") == "PREMATCH_INVENTORY"
                and previous.get("fixture_id") == fid and previous.get("bwin_event_id") == eid
                and previous.get("eval_dir") == str(eval_dir)
                and previous.get("observed_at_utc") and _now_utc(previous["observed_at_utc"]) < kickoff):
            for item in previous.get("files", []):
                if item.get("candidate_provenance") == "PREFINALISTA_REGISTRADO_PREKICKOFF":
                    pre_records[(item["path"], item["sha256"])] = str(path)

    files = []
    for label, path in references.items():
        digest = _sha256_file(path)
        state = "PRESENTE" if digest else "FALTANTE"
        if digest and label in {"histórico", "ranking"}:
            collection = "matches" if label == "histórico" else "statistical_ranking"
            if not any(row.get("fixture_id") == fid for row in _read_json(path).get(collection, [])):
                state = "SIN_FIXTURE_OBJETIVO"
        elif digest and label == "catálogo comercial" and odds:
            if _read_json(path).get("source_txt_sha256") != odds["sha256"]:
                state = "SNAPSHOT_DISTINTO_AL_MANIFIESTO"
        elif digest and label in RESEARCH_PROVIDERS and not path.read_text(encoding="utf-8-sig", errors="replace").replace(RESEARCH_PLACEHOLDER, "").strip():
            state = "PENDIENTE"
        entry = {"label": label, "path": str(path), "state": state, "sha256": digest}
        if digest and (record := recorded_shortlists.get((path, digest))):
            entry.update(candidate_provenance="PREFINALISTA_REGISTRADO_PREKICKOFF", inventory_record=record)
        elif digest and path.name.startswith("prefinalistas"):
            entry["candidate_provenance"] = "DOCUMENTO_EXISTENTE_PREKICKOFF_NO_ACREDITADO"
        elif digest and (path.parent.name == "analysis" or path.name.startswith(("market_comparison", "model_report", "resultado_final", "resultado_cierre_llm"))):
            metadata = _read_json(path) if path.suffix == ".json" else {}
            execution = metadata.get("execution") or {}
            provenance = pre_records.get((str(path), digest))
            if "auditoria" in path.stem or execution.get("mode") == "REPLAY" or metadata.get("execution_mode") == "REPLAY":
                entry["candidate_provenance"] = "RECONSTRUCCION_O_REVISION_RETROSPECTIVA"
            elif provenance or (live_record and path.parent.name == "analysis"):
                entry.update(candidate_provenance="PREFINALISTA_REGISTRADO_PREKICKOFF", inventory_record=provenance)
            else:
                entry["candidate_provenance"] = "DOCUMENTO_EXISTENTE_PREKICKOFF_NO_ACREDITADO"
        files.append(entry)
    audit = {
        "mode": "PREMATCH_INVENTORY" if live_record else "RETROSPECTIVE_INVENTORY",
        "fixture_id": fid, "bwin_event_id": eid, "bwin_identity_state": target["identity_state"],
        "eval_dir": str(eval_dir), "kickoff_utc": kickoff.isoformat(),
        "evidence_cutoff_utc": cutoff.isoformat(), "observed_at_utc": _now_utc().isoformat(),
        "available_by_time": _temporal_actions(target, now), "alerts": alerts, "files": files,
    }
    stamp = f"{now:%Y%m%dT%H%M%S%fZ}_{uuid4().hex[:8]}"
    json_path = audit_dir / f"audit_event_{stamp}.json"
    _atomic_json(json_path, audit)
    md_path = json_path.with_suffix(".md")
    lines = [
        f"# Inventario de auditoría · fixture {fid} · Bwin {eid or 'sin verificar'}", "",
        f"- Modo {audit['mode']} · corte admisible {cutoff.isoformat()} · kickoff {kickoff.isoformat()}.",
        f"- Corrida `{eval_dir}` · identidad Bwin `{target['identity_state']}`.",
        "- El inventario registra presencia y huella. La tabla conserva archivos de revisión posterior y marca por separado los candidatos con registro local previo.",
        *[f"- Incidencia `{alert}`." for alert in alerts], "",
        "| Archivo | Estado | Ruta | Procedencia de candidatos |", "|---|---|---|---|",
        *[f"| {r['label']} | {r['state']} | `{r['path']}` | {r.get('candidate_provenance', '—')} |" for r in files],
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _print_step_summary("audit-event", "COMPLETED", audit["mode"], dict(Counter(r["state"] for r in files)),
                        {"audit_md": md_path, "audit_json": json_path})
    return 0


def cmd_market_catalog(args: argparse.Namespace) -> int:
    from bet_engine.markets.bwin_catalog import extract_bwin_catalog

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    target_rec = _resolve_target_rec_from_eval(
        eval_dir, args, now_utc, min_lead_minutes=max(0, args.min_lead)
    )
    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id / "market_phase2")
    odds = _resolve_odds_snapshot(eval_dir, target_rec, now_utc, explicit=args.odds_file, required=True)
    diagnostics = Path(odds["diagnostics_path"]) if odds["diagnostics_path"] else None
    if args.capture_diagnostics:
        supplied = args.capture_diagnostics.expanduser().resolve()
        if odds["manifest_path"] and supplied != diagnostics:
            raise StepError("ODDS_DIAGNOSTICS_OVERRIDE_DIFFERS_FROM_RUN_MANIFEST")
        diagnostics = supplied
    if args.captured_at_utc:
        _validate_snapshot_times((args.captured_at_utc,), target_rec, now_utc)
        if odds["captured_at_utc"] and _now_utc(args.captured_at_utc) != _now_utc(odds["captured_at_utc"]):
            raise StepError("ODDS_TIMESTAMP_OVERRIDE_DIFFERS_FROM_SNAPSHOT")

    cat = extract_bwin_catalog(
        Path(odds["path"]),
        event_id=str(eid) if eid else None,
        fixture_id=fid,
        home_name=target_rec["home"],
        away_name=target_rec["away"],
        captured_at_utc=odds["captured_at_utc"] or args.captured_at_utc,
        diagnostics_path=diagnostics,
    )
    cat["temporal_gate"] = {
        "min_lead_minutes": max(0, args.min_lead),
        "default_min_lead_minutes": ANALYSIS_MIN_LEAD_MINUTES,
        "explicit_override": args.min_lead != ANALYSIS_MIN_LEAD_MINUTES,
        "evaluated_at_utc": now_utc.isoformat(),
    }
    cat["snapshot_ref"] = odds
    cat["eval_dir"] = str(eval_dir)
    cat["execution_mode"] = "REPLAY" if args.asof else "PREMATCH"
    filename = f"bwin_catalog_{dossier_id}.json"
    cat_path = _preserved_output_dir(out_dir, filename) / filename
    _atomic_json(cat_path, cat)
    _print_step_summary("market-catalog", "COMPLETED", "BWIN_CATALOG_EXTRACTED", cat["counts_by_commercial_coverage"],
                        {"catalog_json": cat_path, "snapshot": odds["path"], "capture_manifest": odds["manifest_path"]})
    return 0


def _validate_catalog_snapshot(catalog, eval_dir, target, cutoff):
    if (str(catalog.get("fixture_id")) != str(target["fixture_id"])
            or str(catalog.get("event_id")) != str(target["bwin_event_id"])):
        raise StepError("MARKET_CATALOG_FIXTURE_OR_EVENT_MISMATCH")
    if catalog.get("eval_dir") and catalog["eval_dir"] != str(eval_dir):
        raise StepError("MARKET_CATALOG_RUN_MISMATCH")
    path = catalog.get("source_txt_path")
    snapshot = _resolve_odds_snapshot(eval_dir, target, cutoff, explicit=Path(path) if path else None, required=True)
    if snapshot["sha256"] != catalog.get("source_txt_sha256"):
        raise StepError("MARKET_CATALOG_SNAPSHOT_HASH_MISMATCH")
    _validate_snapshot_times((catalog.get("captured_at_utc"),), target, cutoff)
    return snapshot


def cmd_market_match(args: argparse.Namespace) -> int:
    from bet_engine.context.evidence_aggregator import build_statistical_evidence_bundle
    from bet_engine.markets.tactical_market_matcher import match_hypotheses_to_catalog

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    target_rec = _resolve_target_rec_from_eval(
        eval_dir, args, now_utc, min_lead_minutes=max(0, args.min_lead)
    )
    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id / "market_phase2")

    scout_doc = _read_json(args.scout_json.expanduser().resolve())
    cat_doc = _read_json(args.catalog_json.expanduser().resolve())
    snapshot = _validate_catalog_snapshot(cat_doc, eval_dir, target_rec, now_utc)

    _, pre_match, tactical_path, tactical_fx = _fixture_statistical_inputs(eval_dir, target_rec, now_utc)
    tactical_doc = _read_json(tactical_path) if tactical_path else {}
    raw_sources = (tactical_doc.get("sources") or {}).get("raw_cache_sources") or []

    home_id = int((pre_match or {}).get("home_id") or target_rec.get("home_id") or 0)
    away_id = int((pre_match or {}).get("away_id") or target_rec.get("away_id") or 0)

    ev_bundle = build_statistical_evidence_bundle(
        home_id=home_id,
        away_id=away_id,
        kickoff_utc=target_rec.get("kickoff_utc"),
        raw_cache_sources=raw_sources,
        tactical_fx=tactical_fx,
        pre_match=pre_match,
    )
    matched = match_hypotheses_to_catalog(
        scout_report=scout_doc,
        bwin_catalog=cat_doc,
        evidence_bundle=ev_bundle,
    )
    matched["temporal_gate"] = {
        "min_lead_minutes": max(0, args.min_lead),
        "default_min_lead_minutes": ANALYSIS_MIN_LEAD_MINUTES,
        "explicit_override": args.min_lead != ANALYSIS_MIN_LEAD_MINUTES,
        "evaluated_at_utc": now_utc.isoformat(),
    }
    match_name, evidence_name = f"market_match_{dossier_id}.json", f"evidence_bundle_{dossier_id}.json"
    out_dir = _preserved_output_dir(out_dir, match_name, evidence_name)
    ev_path = out_dir / evidence_name
    _atomic_json(ev_path, ev_bundle)
    matched["snapshot_ref"] = snapshot
    matched["evidence_ref"] = {"path": str(ev_path), "sha256": _sha256_file(ev_path)}
    match_path = out_dir / match_name
    _atomic_json(match_path, matched)
    _print_step_summary("market-match", "COMPLETED", "HYPOTHESES_MATCHED_TO_BWIN", {"matched": matched["matched_count"], "total": matched["hypotheses_total"]},
                        {"match_json": match_path, "evidence_json": ev_path, "snapshot": snapshot["path"]})
    return 0


def cmd_market_compare(args: argparse.Namespace) -> int:
    from bet_engine.markets.tactical_market_comparator import (
        MarketComparisonError,
        finalize_market_comparison,
        prepare_market_comparison_package,
    )

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    target_rec = _resolve_target_rec_from_eval(
        eval_dir, args, now_utc, min_lead_minutes=max(0, args.min_lead)
    )
    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id / "market_phase2")

    scout_doc = _read_json(args.scout_json.expanduser().resolve())
    cat_doc = _read_json(args.catalog_json.expanduser().resolve())
    snapshot = _validate_catalog_snapshot(cat_doc, eval_dir, target_rec, now_utc)
    match_path = args.match_json.expanduser().resolve()
    match_doc = _read_json(match_path)
    ev_path = match_path.with_name(f"evidence_bundle_{dossier_id}.json")
    if match_doc.get("evidence_ref") and match_doc["evidence_ref"].get("sha256") != _sha256_file(ev_path):
        raise StepError("MARKET_MATCH_EVIDENCE_HASH_MISMATCH")
    ev_doc = _read_json(ev_path) if ev_path.is_file() else None
    lineup_path = out_dir / f"lineup_refresh_{dossier_id}.json"
    if not lineup_path.is_file():
        lineup_path = root / "outputs/matches" / dossier_id / "market_phase2" / lineup_path.name
    lineup_refresh = _load_current_lineup_refresh(lineup_path, eval_dir, target_rec, now_utc)
    out_dir = _preserved_output_dir(out_dir, f"market_comparison_input_{dossier_id}.json")

    try:
        prep = prepare_market_comparison_package(
            scout_report=scout_doc,
            bwin_catalog=cat_doc,
            matcher_result=match_doc,
            evidence_bundle=ev_doc,
            output_dir=out_dir,
            asof_utc=now_utc,
            max_price_age_minutes=args.max_price_age,
            lineup_refresh=lineup_refresh,
        )
        artifacts = {
            "comparison_input": prep["input_json_path"],
            "comparison_prompt": prep["prompt_md_path"],
            "snapshot": snapshot["path"],
        }
        reason = "MARKET_COMPARISON_PACKAGE_CREATED"
        if args.import_comparison:
            ai_doc = _read_json(args.import_comparison.expanduser().resolve())
            fin = finalize_market_comparison(comp_input=prep["comparison_input"], ai_evaluation=ai_doc, output_dir=out_dir)
            result_path = Path(fin["report_json_path"])
            final_report = _read_json(result_path)
            _atomic_json(result_path, {**final_report, "execution_mode": "REPLAY" if args.asof else "PREMATCH",
                                       "eval_dir": str(eval_dir), "snapshot_ref": snapshot})
            artifacts["comparison_json"] = fin["report_json_path"]
            artifacts["comparison_md"] = fin["report_md_path"]
            reason = "MARKET_COMPARISON_FINALIZED"
    except MarketComparisonError as exc:
        raise StepError(str(exc)) from exc

    _print_step_summary("market-compare", "COMPLETED", reason, {"options": len(prep["comparison_input"]["candidate_options"])}, artifacts)
    return 0


def cmd_lineup_refresh(args: argparse.Namespace) -> int:
    from bet_engine.markets.tactical_market_comparator import (
        MarketComparisonError,
        refresh_expedient_with_lineups,
    )

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    from prematch_single import REFRESH_MIN_LEAD_MINUTES

    target_rec = _resolve_target_rec_from_eval(
        eval_dir, args, now_utc, min_lead_minutes=REFRESH_MIN_LEAD_MINUTES
    )
    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id / "market_phase2")
    out_dir.mkdir(parents=True, exist_ok=True)

    scout_doc = _read_json(args.scout_json.expanduser().resolve())
    pre_path = eval_dir / "prospective_trial/enriched/preselection.json"
    pre_match = None
    if pre_path.is_file():
        pre_doc = _read_json(pre_path)
        pre_match = next((m for m in pre_doc.get("matches", []) if int(m.get("fixture_id", -1)) == fid), None)
    home_id = int((pre_match or {}).get("home_id") or target_rec.get("home_id") or 0)
    away_id = int((pre_match or {}).get("away_id") or target_rec.get("away_id") or 0)

    api_lu = _read_json(args.api_lineups_json.expanduser().resolve()) if args.api_lineups_json else None
    if isinstance(api_lu, dict) and "response" in api_lu:
        reported_fixture = (api_lu.get("parameters") or {}).get("fixture")
        if reported_fixture is not None and str(reported_fixture) != str(fid):
            raise StepError("LINEUP_API_FIXTURE_MISMATCH")
        acquired = api_lu.get("_acquired_at_utc")
        if acquired and _now_utc(acquired) > now_utc:
            raise StepError("LINEUP_API_ACQUISITION_AFTER_REFRESH")
        api_lu = api_lu["response"]
    qual_md = args.qualitative_md.expanduser().resolve().read_text(encoding="utf-8", errors="replace") if args.qualitative_md else None

    try:
        ref = refresh_expedient_with_lineups(
            scout_report=scout_doc,
            fixture_id=fid,
            event_id=str(eid) if eid else None,
            home_id=home_id,
            away_id=away_id,
            home_name=target_rec["home"],
            away_name=target_rec["away"],
            kickoff_utc=target_rec.get("kickoff_utc") or target_rec.get("kickoff_madrid"),
            now_utc=now_utc,
            api_lineups_response=api_lu,
            qualitative_markdown=qual_md,
            watch_players=args.watch_player or [],
        )
    except MarketComparisonError as exc:
        raise StepError(str(exc)) from exc

    filename = f"lineup_refresh_{dossier_id}.json"
    ref_path = _preserved_output_dir(out_dir, filename) / filename
    ref["eval_dir"] = str(eval_dir)
    ref["dependent_reports_require_refresh_if_older"] = ["model", "market-compare"]
    _atomic_json(ref_path, ref)
    _print_step_summary("lineup-refresh", "COMPLETED", str(ref["lineup_status"]), {"official_confirmed": int(ref["official_confirmed_22"]), "changes": len(ref["lineup_changes"])}, {"refresh_json": ref_path})
    return 0


def cmd_early_scout(args: argparse.Namespace) -> int:
    from bet_engine.analytics.early_tactical_scout import (
        EarlyScoutError,
        build_research_handoff,
        build_research_handoff_prompt,
        finalize_early_scout,
        fixture_research_context,
        prepare_early_scout_package,
    )

    now_utc = _now_utc(args.asof)
    root, eval_dir, _, _ = _resolve_eval_dir(args, now_utc)
    phase = getattr(args, "research_phase", None)
    provider = getattr(args, "research_provider", None)
    questions = args.research_question
    registries = args.source_registry
    target_rec = _resolve_target_rec_from_eval(
        eval_dir,
        args,
        now_utc,
        min_lead_minutes=({"deep": 240, "update": RESEARCH_UPDATE_MIN_LEAD_MINUTES}.get(phase)
                          or EARLY_SCOUT_MIN_LEAD_MINUTES),
    )

    fid = int(target_rec["fixture_id"])
    eid = target_rec.get("bwin_event_id")
    dossier_id = str(eid) if eid else f"research_fixture_{fid}"
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else (root / "outputs/matches" / dossier_id / "early_scout")

    _, pre_match, _, tactical_fx = _fixture_statistical_inputs(
        eval_dir, target_rec, now_utc, research_only=args.research_without_bwin,
    )
    research_dir = root / "outputs/matches" / dossier_id / "research"
    ext_reports = _prepare_research_inbox(research_dir, dossier_id)
    known = {Path(report["path"]).resolve() for report in ext_reports}
    for path in args.external_report:
        path = path.expanduser().resolve()
        if path not in known:
            ext_reports.append({"source": path.stem, "path": str(path)})
            known.add(path)
    identity = {
        "fixture_id": fid,
        "event_id": eid,
        "home": target_rec["home"],
        "away": target_rec["away"],
        "country": target_rec.get("country"),
        "league": target_rec.get("league"),
        "kickoff_utc": target_rec.get("kickoff_utc"),
        "kickoff_madrid": target_rec.get("kickoff_madrid"),
    }

    try:
        handoff = None
        if phase:
            sources = {"fixture_id": fid, "event_id": eid, "sources": []}
            for path in registries:
                doc = _read_json(path.expanduser().resolve())
                if any(str(doc.get(key)) != str(identity.get(key)) for key in ("fixture_id", "event_id")):
                    raise EarlyScoutError("RESEARCH_SOURCE_REGISTRY_IDENTITY_MISMATCH")
                rows = doc.get("sources")
                if rows is None:
                    groups = doc.get("source_registry")
                    if not isinstance(groups, list) or any(
                        not isinstance(group, dict) or not isinstance(group.get("citations"), list)
                        for group in groups
                    ):
                        raise EarlyScoutError("RESEARCH_SOURCE_REGISTRY_INVALID")
                    rows = [row for group in groups for row in group["citations"]]
                if not isinstance(rows, list):
                    raise EarlyScoutError("RESEARCH_SOURCE_REGISTRY_INVALID")
                sources["sources"].extend(rows)
            defaults, context = fixture_research_context(identity, pre_match, tactical_fx, phase, provider)
            handoff = build_research_handoff(
                identity=identity, asof_utc=now_utc, phase=phase,
                questions=questions or defaults, sources=sources, provider=provider, context=context,
            )
            handoff["execution_mode"] = "REPLAY" if args.asof else "MANUAL_PREMATCH"
            handoff["response_path"] = next(r["path"] for r in ext_reports if r["source"] == handoff["provider"])
            from tempfile import mkdtemp

            requests_dir = out_dir if args.output_dir else research_dir / "requests"
            requests_dir.mkdir(parents=True, exist_ok=True)
            prefix = f"research_{phase}_" if args.output_dir else f"{phase}_{handoff['provider']}_"
            out_dir = Path(mkdtemp(prefix=prefix, dir=requests_dir))
        else:
            out_dir = _preserved_output_dir(out_dir, f"early_scout_input_{dossier_id}.json",
                                            f"early_scout_{dossier_id}.json")
        prep = prepare_early_scout_package(
            identity=identity,
            output_dir=out_dir,
            pre_match=pre_match,
            tactical_fx=tactical_fx,
            external_reports=ext_reports,
            asof_utc=now_utc,
            enforce_report_cutoff=True,
        )
        artifacts: dict[str, Any] = {
            "early_scout_input": prep["input_json_path"],
            "early_scout_prompt": prep["prompt_md_path"],
        }
        reason = "EARLY_SCOUT_PACKAGE_CREATED"
        if handoff:
            handoff_path = out_dir / "research_handoff.json"
            prompt_name = (f"research_{handoff['provider']}.md" if args.output_dir
                           else f"encargo_{handoff['provider']}_{dossier_id}.md")
            research_prompt = out_dir / prompt_name
            _atomic_json(handoff_path, handoff)
            research_prompt.write_text(build_research_handoff_prompt(identity, handoff), encoding="utf-8")
            artifacts.update(research_handoff=str(handoff_path), research_prompt=str(research_prompt),
                             research_response=handoff["response_path"])
            reason = "OPTIONAL_RESEARCH_HANDOFF_CREATED"
        if args.import_analysis:
            ana_doc = _read_json(args.import_analysis.expanduser().resolve())
            fin = finalize_early_scout(scout_input=prep["scout_input"], analysis=ana_doc, output_dir=out_dir)
            _atomic_json(Path(fin["report_json_path"]), {
                **fin["report"], "eval_dir": str(eval_dir), "input_cutoff_utc": now_utc.isoformat(),
                "execution_mode": "REPLAY" if args.asof else "PREMATCH",
                "analysis_source": {"path": str(args.import_analysis.expanduser().resolve()),
                                    "sha256": _sha256_file(args.import_analysis.expanduser().resolve())},
            })
            artifacts["early_scout_json"] = fin["report_json_path"]
            artifacts["early_scout_md"] = fin["report_md_path"]
            reason = "EARLY_SCOUT_REPORT_FINALIZED"
    except EarlyScoutError as exc:
        raise StepError(str(exc)) from exc

    payload = {
        "schema_version": SCHEMA_VERSION,
        "step": "early-scout",
        "status": "COMPLETED",
        "mode": "EXPERIMENTAL_SHADOW",
        "reason": reason,
        "generated_at_utc": now_utc.isoformat(),
        "counts": {
            "external_accepted": len(prep["scout_input"]["external_reports"]["accepted"]),
            "external_rejected": len(prep["scout_input"]["external_reports"]["rejected"]),
            "external_pending": len(prep["scout_input"]["external_reports"]["ignored"]),
            "contradictions": len(prep["scout_input"]["external_reports"]["contradictions"]),
        },
        **artifacts,
    }
    step_json, _ = _write_step_artifacts(
        out_dir if phase else eval_dir,
        "early_scout",
        payload,
        [f"# EARLY TACTICAL SCOUT · {dossier_id}", "", f"- Estado: `{reason}`", f"- Cobertura: `{prep['scout_input']['coverage']['coverage_level']}`"],
    )
    _print_step_summary("early-scout", "COMPLETED", reason, payload["counts"],
                        {**artifacts, "step_json": step_json, "research": research_dir})
    return 0


def cmd_hour_batch(args):
    from prematch_hour_batch import prepare, refresh, research

    if args.hour.minute:
        raise StepError("HOUR_BATCH_REQUIRES_HH_00")
    if getattr(args, "refresh_todo", False) and not args.confirm_external:
        raise StepError("HOUR_BATCH_REFRESH_TODO_REQUIRES_CONFIRM_EXTERNAL")
    action = {"hour-batch-prepare": prepare, "hour-batch-refresh": refresh,
              "hour-batch-research": research}[args.command]
    return action(args, sys.modules[__name__])


def _validate_cli(args):
    command = args.command
    if getattr(args, "min_lead", 0) < 0:
        raise StepError(f"MIN_LEAD_MUST_BE_NONNEGATIVE:{command}")
    if command == "model" and args.min_lead < ANALYSIS_MIN_LEAD_MINUTES:
        raise StepError(f"MODEL_MIN_LEAD_FLOOR:{ANALYSIS_MIN_LEAD_MINUTES}m (omite --min-lead o usa un margen mayor)")
    if command in {"select-fixture", "audit-event", "reasoning-package", "model", "early-scout",
                   "market-catalog", "market-match", "market-compare", "lineup-refresh"}:
        if not (args.fixture_id or args.event_id):
            raise StepError("TARGET_REQUIRED: indica --fixture-id o --event-id; si usas ambos deben coincidir")
        if getattr(args, "research_without_bwin", False) and args.event_id:
            raise StepError("RESEARCH_WITHOUT_BWIN_REQUIRES_FIXTURE_ID_ONLY")
    if command == "early-scout":
        if args.research_phase == "update" and not args.research_provider:
            raise StepError("RESEARCH_UPDATE_REQUIRES_ONE_PROVIDER: usa --research-provider grok o perplexity")
        if args.research_provider and args.research_phase != "update":
            raise StepError("RESEARCH_PROVIDER_REQUIRES_UPDATE: deep usa Gemini")
        if not args.research_phase and (args.research_question or args.source_registry):
            raise StepError("RESEARCH_QUESTIONS_AND_SOURCES_REQUIRE_PHASE")
        if args.research_phase and args.import_analysis:
            raise StepError("RESEARCH_PHASE_AND_IMPORT_ANALYSIS_ARE_SEPARATE_ACTIONS")
    if command == "odds-capture":
        if args.snapshot_file and args.confirm_external:
            raise StepError("ODDS_SNAPSHOT_FILE_AND_CONFIRM_EXTERNAL_ARE_MUTUALLY_EXCLUSIVE")
        if args.confirm_external and args.asof:
            raise StepError("LIVE_ODDS_CAPTURE_REQUIRES_REAL_CLOCK: omite --asof")
    if command == "fixture-update" and args.confirm_external and (not args.apply or args.asof):
        raise StepError("FIXTURE_UPDATE_EXTERNAL_REQUIRES_APPLY_AND_REAL_CLOCK")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_cli(args)
        return int(args.func(args))
    except (StepError, bml.ManualBwinLinkError, df.DiscoveryError, ValueError, OSError) as exc:
        print(f"ERROR [{args.command}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
