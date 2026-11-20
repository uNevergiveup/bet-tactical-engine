#!/usr/bin/env python3
"""Fachada manual PREMATCH V2 para un único expediente canónico por Event ID.

No descubre partidos, no ejecuta un LLM y no genera apuestas.  Adopta el
handoff diario validado y reutiliza prematch_single/prematch_multi para las
operaciones que pueden consultar proveedores externos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import prematch_release
import prematch_single

SCHEMA = "2.0-canonical-prematch-case"
HYPOTHESIS_SCHEMA = "1.0-manual-pattern-hypotheses"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reference(path: Path) -> dict:
    path = path.resolve()
    return {"path": str(path), "sha256": sha(path), "size": path.stat().st_size}


def create_once(path: Path, data: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ValueError(f"WRITE_ONCE_CONFLICT: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def selected_context(root: Path, selected: Path):
    try:
        return prematch_single.load_context(argparse.Namespace(root=root, selected=selected, decision=None))
    except ValueError:
        # A historical bridge is intentionally unreadable by the actionable
        # single-event loader.  This facade may inspect it for a frozen audit,
        # but never changes that global safety contract.
        selection = read(selected); decision_path = selected.parent / "selection_decision.json"; decision = read(decision_path)
        if selection.get("status") != "HISTORICAL_AUDIT_ONLY" or decision.get("status") != "HISTORICAL_AUDIT_ONLY":
            raise
        active_selection = {**selection, "status": "SELECTED_FOR_DEEP_RESEARCH"}
        active_decision = {**decision, "status": "SELECTED"}
        record = prematch_single.validate_selected(active_selection, active_decision)
        record.update(root=root.resolve(), selected_path=selected.resolve(), decision_path=decision_path.resolve(),
                      selected_sha=sha(selected), decision_sha=sha(decision_path),
                      directory=root.resolve() / "outputs" / "matches" / record["event_id"])
        record["selection"] = selection
        record["historical_audit_only"] = True
        return record


def _case_entry(ctx, selected: Path, handoff: Path) -> dict:
    selection = ctx["selection"]
    entry = {
        "schema_version": SCHEMA,
        "event_id": ctx["event_id"],
        "fixture_id": ctx["fixture_id"],
        "bwin_url": ctx["url"],
        "home": {"name": ctx["home"], "api_team_id": selection.get("home_api_id")},
        "away": {"name": ctx["away"], "api_team_id": selection.get("away_api_id")},
        "competition": {"league_id": selection.get("league_id"), "name": selection.get("league"),
                        "season": selection.get("season")},
        "kickoff_madrid": ctx["kickoff"].isoformat(),
        "identity_status": "DAILY_HANDOFF_VERIFIED_CONTEXT_PENDING",
        "pipeline_version": SCHEMA,
        "selected": reference(selected),
        "source_handoff": reference(handoff),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "actionable_bet": False,
        "ready_to_bet": False,
    }
    # Carry the read-only coverage audit into the canonical case as provenance,
    # without turning an ambiguous audit candidate into an actionable identity.
    handoff_doc = read(handoff)
    entry['source_kind'] = ('MANUAL_RESEARCH_TRANSITION'
                            if handoff_doc.get('schema_version') == '1.0-research-selection'
                            else 'DAILY_HANDOFF')
    if entry['source_kind'] == 'MANUAL_RESEARCH_TRANSITION':
        entry['identity_status'] = 'VERIFIED_SELECTION_CONTEXT_PENDING'
    audit_ref = handoff_doc.get("identity_coverage_audit") or {}
    audit_path = Path(audit_ref.get("path", "")) if audit_ref.get("path") else None
    if audit_path and audit_path.is_file() and sha(audit_path) == audit_ref.get("sha256"):
        entry["identity_coverage_audit"] = reference(audit_path)
    return entry


def adopt(root: Path, handoff: Path, output_dir: Path | None = None,
          historical: bool = False, asof=None) -> Path:
    """Adopt one validated daily handoff without repeating discovery.

    The existing ``manifest.json`` remains the sole operational manifest.  A
    context-pending manifest is created only when none exists; subsequent API
    context acquisition uses the existing retry-context operation.
    """
    handoff = handoff.resolve()
    selected = prematch_release.bridge(root, handoff, output_dir, historical=historical, asof=asof)
    return adopt_selected(root, selected, handoff)


def adopt_selected(root: Path, selected: Path, handoff: Path) -> Path:
    """Shared canonical adoption for validated daily or manual selections."""
    ctx = selected_context(root, selected)
    event_dir = ctx["directory"]
    manifest_path = event_dir / "manifest.json"
    case_path = event_dir / "case.json"
    case = _case_entry(ctx, selected, handoff)
    if case_path.exists():
        existing = read(case_path)
        comparable = ("schema_version", "event_id", "fixture_id", "bwin_url", "home", "away",
                      "competition", "kickoff_madrid", "selected", "source_handoff")
        if any(existing.get(key) != case.get(key) for key in comparable):
            raise ValueError(f"WRITE_ONCE_CONFLICT: {case_path}")
    else:
        create_once(case_path, (json.dumps(case, ensure_ascii=False, indent=2) + "\n").encode())

    if manifest_path.exists():
        manifest = prematch_single.match_manifest(ctx)
        if str(manifest.get("event_id")) != ctx["event_id"] or manifest.get("url") != ctx["url"]:
            raise ValueError("EXISTING_MANIFEST_IDENTITY_CONFLICT")
        odds_ref = (manifest.get("inputs") or {}).get("odds") or {}
        if odds_ref and odds_ref.get("sha256") != ctx["selection"].get("odds_sha256"):
            raise ValueError("EXISTING_MANIFEST_DAILY_SNAPSHOT_CONFLICT")
        case_ref = reference(case_path)
        if manifest.get("canonical_case") and manifest["canonical_case"] != case_ref:
            raise ValueError("EXISTING_MANIFEST_CANONICAL_CASE_CONFLICT")
        if not manifest.get("canonical_case"):
            manifest["canonical_case"] = case_ref
            manifest["source_handoff"] = reference(handoff)
            manifest["selected_match"] = reference(selected)
            atomic_json(manifest_path, manifest)
        return selected

    odds = Path(ctx["selection"]["odds_txt"]).resolve()
    if not odds.is_file() or sha(odds) != ctx["selection"]["odds_sha256"]:
        raise ValueError("DAILY_HANDOFF_ODDS_SHA_INVALID")
    prematch_single.snapshot_info(odds, ctx)
    canonical_odds = event_dir/'inputs'/f'cuotas_bwin_{ctx["event_id"]}.txt'
    create_once(canonical_odds, odds.read_bytes())
    manifest = {
        "schema_version": SCHEMA,
        "event_id": ctx["event_id"], "url": ctx["url"],
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "status": "ERROR_CONTEXTO", "identity_status": case['identity_status'],
        "source_kind": case['source_kind'],
        "inputs": {"odds": {**reference(canonical_odds), "generated_from": str(odds)}},
        "steps": {"daily_handoff_adoption": 0, "scraper": 0},
        "kickoff_madrid": ctx['kickoff'].isoformat(),
        "canonical_case": reference(case_path),
        "research_selection": {
            "selection_tier": ctx["selection_tier"], "research_eligible": True,
            "eligible": ctx["selection"].get("eligible", True),
            "research_missing_evidence": ctx["selection"].get("research_missing_evidence", []),
            "hard_block_reasons": ctx["selection"].get("hard_block_reasons", []),
            "selected_sha256": ctx["selected_sha"], "decision_sha256": ctx["decision_sha"],
            "actionable_bet": False,
        },
        "source_handoff": reference(handoff),
        "selected_match": reference(selected),
        "next_action": "retry-context",
    }
    atomic_json(manifest_path, manifest)
    prematch_single.match_manifest(ctx)
    return selected


def promote_research(root, research, discovery, preflight, event):
    """Explicit manual choice; automatic comparison policy is unchanged.

    Revalidates current evidence, not the old exploratory ranking. Creates no
    synthetic daily handoff and never grants betting authorization.
    """
    from decimal import Decimal
    import select_single_match as selector
    research = research.resolve(); original = read(research)
    if original.get('schema_version') != '1.0-research-selection' or original.get('status') != 'SELECTED':
        raise ValueError('RESEARCH_SELECTION_REQUIRED')
    chosen = original['selected']
    if str(chosen['bwin_event_id']) != str(event):
        raise ValueError('MANUAL_EVENT_RESEARCH_CONFLICT')
    for value in original['sources'].values():
        for ref in value if isinstance(value, list) else [value]:
            if sha(Path(ref['path'])) != ref['sha256']:
                raise ValueError('RESEARCH_SOURCE_CHANGED')
    prepath = Path(original['sources']['preselection']['path'])
    pre = read(prepath); pre['_path'] = prepath
    fixtures = [f for f in pre['matches'] if f['fixture_id'] == chosen['fixture_id']]
    if len(fixtures) != 1: raise ValueError('MANUAL_FIXTURE_NOT_UNIQUE')
    fixture = fixtures[0]; pre['matches'] = fixtures
    for field in ('home_id', 'away_id', 'league_id', 'season'):
        if fixture.get(field) is None: raise ValueError('CANONICAL_ID_MISSING: ' + field)
    disc = read(discovery); disc['_path'] = discovery
    now = datetime.now(timezone.utc)
    _, row, checked_rows, _ = selector.prepare_rows(pre, disc, [(preflight, read(preflight))],
                                         now, 45, 120, 180, 1, Decimal('1.20'), False)
    if not row or row['status'] != 'QUALIFIED_FOR_COMPARISON':
        reasons = sorted({r for item in checked_rows for r in item.get('block_reasons', [])})
        raise ValueError('MANUAL_TRANSITION_BLOCKED: ' + ','.join(reasons or ['NO_CURRENT_CANDIDATE']))
    if str(row['bwin_event_id']) != str(event): raise ValueError('MANUAL_EVENT_CONFLICT')
    selection = {**row, 'schema_version': '1.0-selected-single-fixture',
        'status': 'SELECTED_FOR_DEEP_RESEARCH', 'selection_asof_madrid': now.isoformat(),
        'home_api_id': fixture['home_id'], 'away_api_id': fixture['away_id'],
        'league_id': fixture['league_id'], 'season': fixture['season'], 'gender': fixture.get('gender'),
        'market_families_ready_for_research': row['market_families_ready'],
        'selection_tier': 'A', 'eligible': True, 'research_eligible': True,
        'research_missing_evidence': [], 'hard_block_reasons': [], 'actionable_bet': False}
    decision = {'status': 'SELECTED', 'method': 'EXPLICIT_USER_EVENT_STRICT_REVALIDATION',
                'provisional_or_selected': selection, 'actionable_bet': False}
    ctx = prematch_single.validate_selected(selection, decision)
    ctx.update(root=root, selection=selection)
    prematch_single.validate_temporal_identity_evidence(ctx)
    prematch_single.freshness(ctx, min_lead=45)
    # Existing case is never overwritten by a new manual transition.
    if (root/'outputs/matches'/str(event)/'case.json').exists():
        raise ValueError('CANONICAL_CASE_ALREADY_EXISTS_USE_REFRESH')
    folder = root/'outputs/matches'/str(event)/'selection'/now.strftime('%Y%m%dT%H%M%S%fZ')
    create_once(folder/'selected_match.json', (json.dumps(selection, indent=2)+'\n').encode())
    create_once(folder/'selection_decision.json', (json.dumps(decision, indent=2)+'\n').encode())
    return adopt_selected(root, folder/'selected_match.json', research)


def _load_case(root: Path, event: str):
    directory = root / "outputs" / "matches" / str(event)
    case_path = directory / "case.json"
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"CANONICAL_CASE_NOT_PREPARED: {event}")
    manifest = read(manifest_path)
    if str(manifest.get("event_id")) != str(event):
        raise ValueError("CANONICAL_CASE_IDENTITY_CONFLICT")
    if case_path.is_file():
        case = read(case_path)
        if str(case.get("event_id")) != str(event):
            raise ValueError("CANONICAL_CASE_IDENTITY_CONFLICT")
        for label, ref in (("selected", case.get("selected")), ("source_handoff", case.get("source_handoff"))):
            path = Path((ref or {}).get("path", ""))
            if not path.is_file() or sha(path) != (ref or {}).get("sha256"):
                raise ValueError(f"CANONICAL_REFERENCE_CHANGED: {label}")
        return directory, case, manifest

    # Expedientes preparados por prematch_single.py no crean case.json, pero sí
    # manifest.json + single_execution.json e identidad verificada.
    single_path = directory / "single_execution.json"
    ident_ref = (manifest.get("inputs") or {}).get("identity") or {}
    ident_path = Path(ident_ref.get("path", "")) if ident_ref.get("path") else (directory / "inputs" / "match_identity.json")
    if not single_path.is_file() and not ident_path.is_file():
        raise ValueError(f"CANONICAL_CASE_NOT_PREPARED: {event}")
    single_doc = read(single_path) if single_path.is_file() else {}
    ident_doc = read(ident_path) if ident_path.is_file() else {}
    if single_doc and str(single_doc.get("bwin_event_id") or event) != str(event):
        raise ValueError("CANONICAL_CASE_IDENTITY_CONFLICT")
    if ident_doc and str(ident_doc.get("bwin_event_id") or event) != str(event):
        raise ValueError("CANONICAL_CASE_IDENTITY_CONFLICT")
    sel_path_str = single_doc.get("selected_path")
    sel_sha = single_doc.get("selected_sha256")
    if sel_path_str and sel_sha and Path(sel_path_str).is_file() and sha(Path(sel_path_str)) != sel_sha:
        raise ValueError("CANONICAL_REFERENCE_CHANGED: selected")
    kickoff_madrid = single_doc.get("kickoff_madrid")
    if not kickoff_madrid and ident_doc.get("kickoff_utc"):
        kickoff_madrid = datetime.fromisoformat(str(ident_doc["kickoff_utc"]).replace("Z", "+00:00")).astimezone(TZ).isoformat()
    fixture_id = single_doc.get("fixture_id") or ident_doc.get("api_fixture_id")
    if not fixture_id or not kickoff_madrid:
        raise ValueError(f"CANONICAL_CASE_NOT_PREPARED: {event}")
    home_ident = ident_doc.get("home") or {}
    away_ident = ident_doc.get("away") or {}
    case = {
        "event_id": str(event),
        "fixture_id": int(fixture_id),
        "bwin_url": single_doc.get("bwin_url") or manifest.get("url") or ident_doc.get("bwin_url"),
        "kickoff_madrid": kickoff_madrid,
        "home": {
            "name": home_ident.get("api_name") or home_ident.get("bwin_name") or "",
            "api_team_id": home_ident.get("api_team_id"),
            "bwin_name": home_ident.get("bwin_name"),
        },
        "away": {
            "name": away_ident.get("api_name") or away_ident.get("bwin_name") or "",
            "api_team_id": away_ident.get("api_team_id"),
            "bwin_name": away_ident.get("bwin_name"),
        },
        "competition": {
            "league_id": ident_doc.get("league_id"),
            "league": ident_doc.get("league_name"),
            "season": ident_doc.get("season"),
        },
        "source_mode": "prematch_single",
    }
    return directory, case, manifest


def _evaluate_closure(
    directory: Path,
    case: dict,
    manifest: dict,
    *,
    lineup_cutoff_minutes: int = 15,
    user_manual_absent: bool = False,
    now_override: datetime | None = None,
) -> dict:
    """Distinguish a preliminary evaluation from a valid final PREMATCH closure."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from bet_engine.context.lineup_manager import evaluate_fixture_lineup_state, extract_fixture_team_identity

    tz = ZoneInfo("Europe/Madrid")
    now = now_override or datetime.now(timezone.utc)
    kickoff = datetime.fromisoformat(str(case["kickoff_madrid"]).replace("Z", "+00:00"))
    remain_min = round((kickoff - now).total_seconds() / 60.0, 2)
    status_val = str(manifest.get("status") or "")
    inputs = manifest.get("inputs") or {}

    odds_capture = None
    odds_age_now = None
    odds_ref = inputs.get("odds") or {}
    if odds_ref.get("path") and Path(odds_ref["path"]).is_file():
        head = Path(odds_ref["path"]).read_text(encoding="utf-8-sig", errors="replace")[:6000]
        match_cap = prematch_single.RE_CAPTURE.search(head)
        if match_cap:
            try:
                odds_capture = datetime.fromisoformat(match_cap.group(1).replace("Z", "+00:00"))
                odds_age_now = round((now - odds_capture).total_seconds() / 60.0, 2)
            except ValueError:
                odds_capture = None

    finish_at = None
    if manifest.get("finish_completed_at"):
        try:
            finish_at = datetime.fromisoformat(str(manifest["finish_completed_at"]).replace("Z", "+00:00"))
        except ValueError:
            finish_at = None

    ctx_doc = {}
    ctx_ref = inputs.get("context_json") or {}
    if ctx_ref.get("path") and Path(ctx_ref["path"]).is_file():
        try:
            ctx_doc = read(Path(ctx_ref["path"]))
        except (OSError, ValueError, TypeError):
            ctx_doc = {}
    ident_doc = {}
    id_ref = inputs.get("identity") or {}
    if id_ref.get("path") and Path(id_ref["path"]).is_file():
        try:
            ident_doc = read(Path(id_ref["path"]))
        except (OSError, ValueError, TypeError):
            ident_doc = {}

    raw_lineups_resp = ((ctx_doc.get("context") or {}).get("lineups") or {}).get("response")
    lineups_prov = ((ctx_doc.get("context") or {}).get("lineups") or {}).get("provenance") or {}
    diff_meta = ctx_doc.get("differential_refresh") or {}
    api_acq_ts = (
        diff_meta.get("acquired_at_utc")
        or ((ctx_doc.get("context") or {}).get("lineups") or {}).get("_acquired_at_utc")
        or ctx_doc.get("generated_at")
    )
    manual_lu_doc = None
    ml_ref = inputs.get("manual_lineups") or {}
    if ml_ref.get("path") and Path(ml_ref["path"]).is_file():
        try:
            manual_lu_doc = read(Path(ml_ref["path"]))
        except (OSError, ValueError, TypeError):
            manual_lu_doc = None
    if manual_lu_doc is None and lineups_prov.get("lineup_status") in {
        "LINEUPS_MANUAL_VERIFIED",
        "LINEUPS_EXTERNAL_CONFIRMED",
    }:
        manual_lu_doc = {
            "lineup_status": lineups_prov["lineup_status"],
            "source": lineups_prov.get("source"),
            "source_url": lineups_prov.get("source_url"),
            "source_kind": lineups_prov.get("source_kind"),
            "primary_evidence_verified": lineups_prov.get("primary_evidence_verified", False),
            "captured_at_utc": lineups_prov.get("captured_at_utc"),
            "sides": {"home": {"starters_count": 11}, "away": {"starters_count": 11}},
        }
        raw_lineups_resp = None

    qual_md = None
    for qk in ("qualitative_research", "report"):
        qref = inputs.get(qk) or {}
        if qref.get("path") and Path(qref["path"]).is_file():
            try:
                qual_md = Path(qref["path"]).read_text(encoding="utf-8-sig", errors="replace")
                break
            except OSError:
                pass
    if qual_md is None:
        from prematch_single import resolve_qualitative_report_path
        eid = str(case.get("event_id") or manifest.get("event_id") or directory.name)
        root_guess = directory.parent.parent.parent if len(directory.parents) >= 3 else directory.parent
        rep_cand = resolve_qualitative_report_path(root_guess, directory, eid)
        if rep_cand.is_file():
            try:
                qual_md = rep_cand.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                pass

    team_ident = extract_fixture_team_identity(ctx_doc, case_doc=case, identity_doc=ident_doc)
    lineup_eval = evaluate_fixture_lineup_state(
        api_lineups_response=raw_lineups_resp,
        manual_lineup=manual_lu_doc,
        qualitative_markdown=qual_md,
        fixture_id=int(team_ident.get("fixture_id") or case.get("fixture_id") or 0),
        event_id=str(case.get("event_id") or ""),
        home_id=team_ident.get("home_id"),
        away_id=team_ident.get("away_id"),
        home_name=str(team_ident.get("home_name") or ""),
        away_name=str(team_ident.get("away_name") or ""),
        home_aliases=team_ident.get("home_aliases"),
        away_aliases=team_ident.get("away_aliases"),
        is_national_team=bool(team_ident.get("is_national_team")),
        kickoff_utc=kickoff,
        now_utc=now,
        lineup_cutoff_minutes=lineup_cutoff_minutes,
        user_manual_absent=user_manual_absent,
        api_acquired_at_utc=api_acq_ts,
        last_verified_state=manifest.get("lineup_state"),
    )
    lineups_status = str(lineup_eval["status"])
    lineups_confirmed_22 = bool(lineup_eval["confirmed_complete_22"])

    shadow_path = directory / "shadow" / f"shadow_summary_{case['event_id']}.json"
    shadow_present = shadow_path.is_file()
    shadow_status = "ABSENT"
    shadow_odds_sha256 = None
    shadow_odds_captured_utc = None
    shadow_ctx_sha256 = None
    shadow_prob_at_utc = None
    shadow_prices_at_utc = None
    shadow_selection_count = None
    shadow_odds_freshness = None
    shadow_ts_granularity = None
    shadow_integrity_reasons: list[str] = []
    manifest_odds_sha = (inputs.get("odds") or {}).get("sha256")
    manifest_ctx_sha = (inputs.get("context_json") or {}).get("sha256")

    if not shadow_present:
        shadow_integrity_reasons.append("SHADOW_ABSENT")
    else:
        try:
            sh_doc = read(shadow_path)
            if not isinstance(sh_doc, dict):
                raise ValueError("SHADOW_NOT_DICT")
            sh_report = sh_doc.get("shadow_report") or {}
            shadow_status = str(sh_report.get("status") or "PRESENT")
            shadow_odds_sha256 = sh_doc.get("odds_sha256")
            shadow_odds_captured_utc = sh_doc.get("odds_captured_utc")
            shadow_ctx_sha256 = sh_doc.get("context_json_sha256")
            shadow_prob_at_utc = sh_doc.get("probabilities_computed_at_utc") or sh_report.get("cutoff_utc")
            shadow_prices_at_utc = (
                sh_doc.get("prices_evaluated_at_utc")
                if "prices_evaluated_at_utc" in sh_doc
                else sh_report.get("cutoff_utc")
            )
            shadow_selection_count = sh_doc.get("odds_selection_count")
            shadow_odds_freshness = sh_doc.get("odds_freshness_status")
            shadow_ts_granularity = sh_doc.get("timestamp_granularity")
        except (OSError, ValueError, TypeError):
            shadow_status = "UNREADABLE"
            shadow_integrity_reasons.append("SHADOW_UNREADABLE")

    shadow_stale_reasons: list[str] = []
    shadow_missing_hash_reasons: list[str] = []
    if shadow_present and shadow_status == "UNREADABLE":
        shadow_stale_reasons.append("SHADOW_UNREADABLE")
    elif shadow_present:
        if not shadow_odds_sha256 or not manifest_odds_sha:
            shadow_missing_hash_reasons.append("SHADOW_ODDS_HASH_MISSING")
        elif shadow_odds_sha256 != manifest_odds_sha:
            shadow_stale_reasons.append("SHADOW_ODDS_HASH_MISMATCH")
        if not shadow_ctx_sha256 or not manifest_ctx_sha:
            shadow_missing_hash_reasons.append("SHADOW_CONTEXT_HASH_MISSING")
        elif shadow_ctx_sha256 != manifest_ctx_sha:
            shadow_stale_reasons.append("SHADOW_CONTEXT_HASH_MISMATCH")
        refresh_at = manifest.get("odds_refresh_at_utc")
        if refresh_at and shadow_prob_at_utc:
            try:
                ref_dt = datetime.fromisoformat(str(refresh_at).replace("Z", "+00:00"))
                prob_dt = datetime.fromisoformat(str(shadow_prob_at_utc).replace("Z", "+00:00"))
                if prob_dt < ref_dt:
                    shadow_stale_reasons.append("SHADOW_OLDER_THAN_ODDS_REFRESH")
            except ValueError:
                shadow_stale_reasons.append("SHADOW_TIMESTAMP_INVALID")

    shadow_stale = bool(shadow_present and shadow_stale_reasons)
    shadow_actionable_reasons = list(shadow_integrity_reasons) + shadow_stale_reasons + shadow_missing_hash_reasons
    if shadow_present and shadow_status != "UNREADABLE":
        if shadow_status != "COMPLETED_SHADOW":
            shadow_actionable_reasons.append(f"SHADOW_STATUS_{shadow_status}")
        if shadow_odds_freshness != "LOADED":
            shadow_actionable_reasons.append(f"SHADOW_ODDS_FRESHNESS_{shadow_odds_freshness or 'UNKNOWN'}")
        if not shadow_selection_count or int(shadow_selection_count) <= 0:
            shadow_actionable_reasons.append("SHADOW_NO_QUOTED_SELECTIONS")
    shadow_verified = bool(shadow_present and not shadow_actionable_reasons)

    lineup_gate_reasons = []
    if not lineups_confirmed_22:
        lineup_gate_reasons.append(f"MISSING_CONFIRMED_22_STARTERS:{lineups_status}")
    reasons = []
    if status_val == "AUDITORIA_HISTORICA":
        stage = "HISTORICAL_AUDIT"
        final_valid = False
    elif status_val != "FINALIZADO":
        stage = "PENDING_FINAL_VALIDATION"
        final_valid = False
        reasons.append(f"MANIFEST_STATUS_{status_val}")
    else:
        window_start = kickoff - timedelta(minutes=60)
        if finish_at is None:
            reasons.append("FINISH_TIMESTAMP_MISSING")
        elif finish_at < window_start:
            reasons.append(f"CLOSED_OUTSIDE_60M_FINAL_WINDOW (lead={round((kickoff - finish_at).total_seconds()/60.0, 1)}m)")
        elif finish_at > kickoff - timedelta(minutes=prematch_single.FINISH_MIN_LEAD_MINUTES):
            reasons.append("CLOSED_WITH_INSUFFICIENT_PREMATCH_LEAD")
        if odds_capture is None:
            reasons.append("ODDS_CAPTURE_TIMESTAMP_MISSING")
        else:
            if odds_capture < window_start:
                reasons.append(f"ODDS_NOT_CAPTURED_WITHIN_60M_OF_KICKOFF ({odds_capture.astimezone(tz).isoformat()})")
            if finish_at is not None and (finish_at - odds_capture).total_seconds() > 15 * 60:
                reasons.append(f"ODDS_AGE_EXCEEDED_15M_AT_FINISH ({round((finish_at - odds_capture).total_seconds()/60.0, 1)}m)")
        if odds_ref.get("path") and odds_ref.get("sha256"):
            opath = Path(odds_ref["path"])
            if not opath.is_file() or sha(opath) != odds_ref["sha256"]:
                reasons.append("ODDS_MANIFEST_HASH_MISMATCH")
        if shadow_stale:
            reasons.append("SHADOW_STALE_VS_MANIFEST")
        stage = "PRELIMINARY_EVALUATION_ONLY" if reasons else "FINAL_PREMATCH_VALID"
        final_valid = not reasons

    return {
        "closure_stage": stage,
        "final_prematch_valid": final_valid,
        "closure_reasons": reasons,
        "lineup_gate_reasons": lineup_gate_reasons,
        "minutes_to_kickoff_now": remain_min,
        "finish_completed_utc": finish_at.isoformat() if finish_at else None,
        "odds_capture_madrid": odds_capture.astimezone(tz).isoformat() if odds_capture else None,
        "odds_age_minutes_now": odds_age_now,
        "lineups_status": lineups_status,
        "lineups_confirmed_22": lineups_confirmed_22,
        "eligible_for_actionable_pick": bool(
            lineups_confirmed_22
            and final_valid
            and lineup_eval.get("eligible_for_actionable_pick", True)
            and shadow_verified
        ),
        "eligible_for_final_analysis": bool(lineups_confirmed_22),
        "official_source_tier": lineup_eval.get("official_source_tier"),
        "primary_official_verified": bool(lineup_eval.get("primary_official_verified", False)),
        "absence_interpretation": lineup_eval.get("absence_interpretation"),
        "lineup_evaluation": lineup_eval,
        "shadow_present": shadow_present,
        "shadow_status": shadow_status,
        "shadow_stale": shadow_stale,
        "shadow_verified": shadow_verified,
        "shadow_integrity_reasons": shadow_actionable_reasons,
        "shadow_odds_freshness_status": shadow_odds_freshness,
        "shadow_odds_sha256": shadow_odds_sha256,
        "shadow_odds_captured_utc": shadow_odds_captured_utc,
        "shadow_odds_selection_count": shadow_selection_count,
        "shadow_probabilities_computed_at_utc": shadow_prob_at_utc,
        "shadow_prices_evaluated_at_utc": shadow_prices_at_utc,
        "shadow_timestamp_granularity": shadow_ts_granularity,
    }


def _recommended_next_action(manifest: dict, closure: dict) -> str:
    """Compute a valid, state-consistent next action instead of trusting stale flags."""
    status_val = str(manifest.get("status") or "")
    if status_val == "CASE_CREATED_PENDING_ACQUISITION":
        return "acquire-context-and-odds"
    if status_val in {"ERROR_CONTEXTO", "ERROR_ARCHIVOS", "PREPARADO_PROVISIONAL"}:
        return "acquire-context"
    if status_val == "PARCIAL_SIN_CUOTAS":
        return "retry-scraper"
    stage = closure.get("closure_stage")
    if stage == "FINAL_PREMATCH_VALID":
        return "export-handoff"
    if stage == "HISTORICAL_AUDIT":
        return "historical-audit-only"
    remain = closure.get("minutes_to_kickoff_now")
    odds_age = closure.get("odds_age_minutes_now")
    cap_iso = closure.get("odds_capture_madrid")
    if remain is not None and remain <= prematch_single.FINISH_MIN_LEAD_MINUTES:
        return "historical-audit-only-after-kickoff"
    odds_in_final_window = False
    if cap_iso and odds_age is not None and 0 <= odds_age <= 15 and remain is not None and remain <= 60:
        # Ensure capture itself happened within T-60 of kickoff.
        cap_dt = datetime.fromisoformat(cap_iso)
        reasons = closure.get("closure_reasons") or []
        if not any("ODDS_NOT_CAPTURED_WITHIN_60M" in r for r in reasons):
            odds_in_final_window = True
    return "finalize" if odds_in_final_window else "refresh-odds"


def status(root: Path, event: str) -> dict:
    if not (root/'outputs/matches'/str(event)/'case.json').is_file():
        research = []
        for path in (root/'outputs/discovery').rglob('research_selection.json'):
            doc = read(path)
            if str((doc.get('selected') or {}).get('bwin_event_id')) == str(event):
                research.append(reference(path))
        return {'event_id': str(event), 'status': 'RESEARCH_ONLY' if research else 'BLOCKED',
                'research_selections': research, 'actionable_bet': False,
                'reason': 'CANONICAL_CASE_NOT_PREPARED', 'next_action': 'promote-research' if research else None}
    directory, case, manifest = _load_case(root, event)
    inputs = {}
    for name, ref in (manifest.get("inputs") or {}).items():
        if not isinstance(ref, dict) or not ref.get("path"): continue
        path = Path(ref["path"])
        inputs[name] = {"exists": path.is_file(), "hash_valid": path.is_file() and sha(path) == ref.get("sha256")}
    closure = _evaluate_closure(directory, case, manifest)
    return {
        "event_id": event, "fixture_id": case["fixture_id"], "status": manifest.get("status"),
        "identity_status": manifest.get("identity_status"), "kickoff_madrid": case["kickoff_madrid"],
        "closure_stage": closure["closure_stage"], "final_prematch_valid": closure["final_prematch_valid"],
        "lineup_status": closure["lineups_status"],
        "lineups_confirmed_22": closure["lineups_confirmed_22"],
        "eligible_for_actionable_pick": closure["eligible_for_actionable_pick"],
        "eligible_for_final_analysis": closure["eligible_for_final_analysis"],
        "closure_checks": closure,
        "inputs": inputs, "next_action": _recommended_next_action(manifest, closure),
        "actionable_bet": False, "directory": str(directory.resolve()),
    }


def attach_hypotheses(root: Path, event: str, source: Path) -> Path:
    directory, case, manifest = _load_case(root, event)
    payload = read(source)
    if payload.get("schema_version") != HYPOTHESIS_SCHEMA:
        raise ValueError(f"HYPOTHESIS_SCHEMA_REQUIRED: {HYPOTHESIS_SCHEMA}")
    if str(payload.get("event_id")) != event or int(payload.get("fixture_id") or 0) != int(case["fixture_id"]):
        raise ValueError("HYPOTHESIS_IDENTITY_MISMATCH")
    hypotheses = payload.get("hypotheses")
    if not isinstance(hypotheses, list): raise ValueError("HYPOTHESES_MUST_BE_A_LIST")
    required = {"id", "variables", "evidence", "conditions", "refutation", "prematch_availability"}
    for item in hypotheses:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("HYPOTHESIS_CONTRACT_INCOMPLETE")
        if item["prematch_availability"] not in {"OBSERVABLE", "HISTORICAL_AGGREGATE", "XI_DEPENDENT", "POSTMATCH_ONLY"}:
            raise ValueError("HYPOTHESIS_PREMATCH_CLASS_INVALID")
    target = directory / "research" / "hypotheses" / sha(source) / "llm_pattern_hypotheses.json"
    create_once(target, source.read_bytes())
    manifest.setdefault("inputs", {})["manual_hypotheses"] = reference(target)
    manifest["hypothesis_validation_status"] = "PENDING_PYTHON_VALIDATION" if hypotheses else "NO_HYPOTHESES_SUBMITTED"
    atomic_json(directory / "manifest.json", manifest)
    return target


def attach_research(root: Path, event: str, source: Path) -> Path:
    from bet_engine.context.research_parser import validate_research_identity
    from bet_engine.context.lineup_manager import evaluate_fixture_lineup_state, extract_confirmed_lineup_from_research

    directory, case, manifest = _load_case(root, event)
    body = source.read_text(encoding="utf-8-sig")
    parsed = validate_research_identity(
        body,
        expected_event_id=str(event),
        expected_fixture_id=int(case["fixture_id"]),
    )
    if "STATUS: PENDIENTE_MANUAL" in body:
        raise ValueError("QUALITATIVE_RESEARCH_PENDING")
    target = directory / "research" / "qualitative" / sha(source) / source.name
    create_once(target, source.read_bytes())
    ref = reference(target)
    ref.update(
        {
            "sources_attributed": parsed["sources_attributed"],
            "sha256_references_in_report": parsed["sha256_references"][:20],
            "report_kickoff_utc": parsed["kickoff_utc"].isoformat() if parsed["kickoff_utc"] else None,
            "report_cutoff_utc": parsed["cutoff_utc"].isoformat() if parsed["cutoff_utc"] else None,
            "report_generated_utc": parsed["generated_utc"].isoformat() if parsed["generated_utc"] else None,
            "imported_at_utc": datetime.now(timezone.utc).isoformat(),
            "temporal_traceability": {
                "cutoff_declared_utc": parsed["cutoff_utc"].isoformat() if parsed["cutoff_utc"] else None,
                "completion_declared_utc": parsed["generated_utc"].isoformat() if parsed["generated_utc"] else None,
                "runtime_completion_verified": False,
                "import_is_not_creation_proof": True,
            },
        }
    )
    manifest.setdefault("inputs", {})["qualitative_research"] = ref
    manifest["qualitative_research_status"] = "ATTACHED_NOT_YET_VALIDATED"

    # Inspección P0 de XI dentro de la investigación cualitativa (confirmado verifiable vs solo probable)
    home_meta = case.get("home") or {}
    away_meta = case.get("away") or {}
    qual_confirmed = extract_confirmed_lineup_from_research(
        body,
        expected_fixture_id=int(case["fixture_id"]),
        expected_event_id=str(event),
        home_name=str(home_meta.get("name") or ""),
        away_name=str(away_meta.get("name") or ""),
        home_id=home_meta.get("api_team_id"),
        away_id=away_meta.get("api_team_id"),
        kickoff_utc=case["kickoff_madrid"],
    )
    if qual_confirmed is not None:
        qual_lu_path = directory / "lineups" / f"qualitative_confirmed_{sha(target)[:16]}.json"
        create_once(qual_lu_path, (json.dumps(qual_confirmed, ensure_ascii=False, indent=2) + "\n").encode())
        manifest["inputs"]["manual_lineups"] = reference(qual_lu_path)
        manifest["lineup_state"] = {
            "status": qual_confirmed["lineup_status"],
            "confirmed_complete_22": True,
            "source_origin": qual_confirmed["source"],
            "source_url": qual_confirmed.get("source_url"),
            "captured_at_utc": qual_confirmed["captured_at_utc"],
            "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
        }
        # Si ya existe contexto API en manifest, propagar actualización diferencial y reevaluar
        if all(k in manifest["inputs"] for k in ("context", "context_json", "identity")):
            _apply_validated_lineup_and_reevaluate(root, directory, case, manifest, qual_confirmed, reevaluate=True)
            return target
    elif not (manifest.get("lineup_state") or {}).get("confirmed_complete_22"):
        lu_eval = evaluate_fixture_lineup_state(
            qualitative_markdown=body,
            fixture_id=int(case["fixture_id"]),
            event_id=str(event),
            home_id=home_meta.get("api_team_id"),
            away_id=away_meta.get("api_team_id"),
            home_name=str(home_meta.get("name") or ""),
            away_name=str(away_meta.get("name") or ""),
            kickoff_utc=case["kickoff_madrid"],
        )
        manifest["lineup_state"] = {
            "status": lu_eval["status"],
            "confirmed_complete_22": False,
            "probable_counts": lu_eval.get("probable_counts", {}),
            "reason": lu_eval.get("reason"),
            "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
        }

    atomic_json(directory / "manifest.json", manifest)
    return target


def _apply_validated_lineup_and_reevaluate(
    root: Path,
    directory: Path,
    case: dict,
    manifest: dict,
    validated_manual: dict,
    *,
    now_utc: datetime | None = None,
    reevaluate: bool = True,
) -> dict[str, Any]:
    from prematch_context_delta import apply_manual_lineups_to_context

    now = now_utc or datetime.now(timezone.utc)
    ctx_info = {
        "root": root.resolve(),
        "directory": directory.resolve(),
        "event_id": str(case["event_id"]),
        "fixture_id": int(case["fixture_id"]),
        "url": str(case["bwin_url"]),
        "kickoff": datetime.fromisoformat(str(case["kickoff_madrid"]).replace("Z", "+00:00")),
    }
    if all(k in (manifest.get("inputs") or {}) for k in ("context", "context_json", "identity")):
        updated_manifest, update_dir = apply_manual_lineups_to_context(ctx_info, manifest, validated_manual, now)
        update_dir_str = str(update_dir.resolve())
    else:
        updated_manifest = dict(manifest)
        updated_manifest["lineup_state"] = {
            "status": validated_manual["lineup_status"],
            "confirmed_complete_22": True,
            "source_origin": validated_manual.get("source"),
            "source_url": validated_manual.get("source_url"),
            "captured_at_utc": validated_manual.get("captured_at_utc"),
            "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
        }
        update_dir_str = None

    atomic_json(directory / "manifest.json", updated_manifest)

    re_eval_result: dict[str, Any] = {"executed": False}
    if reevaluate and all(k in (updated_manifest.get("inputs") or {}) for k in ("context_json",)):
        from bet_engine.orchestration import run_shadow_for_event_dir

        sh_res = run_shadow_for_event_dir(
            root,
            str(case["event_id"]),
            historical=False,
            now_utc=now.isoformat(),
            min_lead_minutes=0,
        )
        re_eval_result = {"executed": True, "shadow": sh_res}

    audit_path = directory / "lineups" / f"lineup_audit_{case['event_id']}.json"
    atomic_json(
        audit_path,
        {
            "event_id": str(case["event_id"]),
            "fixture_id": int(case["fixture_id"]),
            "lineup_state": updated_manifest.get("lineup_state"),
            "context_update_dir": update_dir_str,
            "reevaluation": re_eval_result,
            "original_inputs_untouched": True,
            "recorded_at_utc": now.isoformat(),
        },
    )
    return {
        "event_id": str(case["event_id"]),
        "fixture_id": int(case["fixture_id"]),
        "lineup_state": updated_manifest.get("lineup_state"),
        "context_update_dir": update_dir_str,
        "reevaluation": re_eval_result,
        "audit_path": str(audit_path.resolve()),
    }


def attach_manual_lineups(
    root: Path,
    event: str,
    source: Path,
    *,
    now_utc: str | None = None,
    reevaluate: bool = True,
) -> dict[str, Any]:
    """Incorpora XI confirmados mediante archivo JSON estructurado con procedencia, capturas preservadas y auditoría."""
    from bet_engine.context.lineup_manager import load_and_validate_manual_lineup_file, scan_manual_lineup_inbox

    directory, case, manifest = _load_case(root, event)
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")
    home_meta = case.get("home") or {}
    away_meta = case.get("away") or {}
    validated = load_and_validate_manual_lineup_file(
        source,
        expected_fixture_id=int(case["fixture_id"]),
        expected_event_id=str(event),
        expected_home_id=home_meta.get("api_team_id"),
        expected_away_id=away_meta.get("api_team_id"),
        expected_home_name=home_meta.get("name"),
        expected_away_name=away_meta.get("name"),
        kickoff_utc=case["kickoff_madrid"],
        now_utc=now_dt,
        allow_probable_only=False,
    )
    # Verifica y conserva hashes de capturas declaradas o presentes en lineups/manual/
    inbox = scan_manual_lineup_inbox(directory, event_id=str(event))
    manual_dir = directory / "lineups" / "manual"
    verified_screenshots: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for ref in validated.get("screenshots") or []:
        raw_p = Path(ref["path"])
        cand = raw_p if raw_p.is_absolute() else (manual_dir / raw_p)
        if not cand.is_file():
            cand = directory / raw_p
        if not cand.is_file():
            raise ValueError(f"LINEUP_SCREENSHOT_MISSING:{ref['path']}")
        actual_sha = sha(cand)
        if ref.get("sha256") and ref["sha256"] != actual_sha:
            raise ValueError(f"LINEUP_SCREENSHOT_SHA_MISMATCH:{ref['path']}")
        resolved = str(cand.resolve())
        seen_paths.add(resolved)
        verified_screenshots.append({"path": resolved, "name": cand.name, "sha256": actual_sha, "size": cand.stat().st_size})
    for item in inbox.get("screenshots") or []:
        if item["path"] not in seen_paths:
            verified_screenshots.append(item)
            seen_paths.add(item["path"])
    if verified_screenshots:
        validated["screenshots"] = verified_screenshots
        manifest["manual_lineup_screenshots"] = verified_screenshots

    pinned = directory / "lineups" / f"manual_lineup_{validated['source_file_sha256'][:16]}.json"
    create_once(pinned, (json.dumps(validated, ensure_ascii=False, indent=2) + "\n").encode())
    manifest.setdefault("inputs", {})["manual_lineups"] = reference(pinned)
    res = _apply_validated_lineup_and_reevaluate(
        root,
        directory,
        case,
        manifest,
        validated,
        now_utc=now_dt,
        reevaluate=reevaluate,
    )
    res["manual_lineup_pinned"] = str(pinned.resolve())
    res["screenshots_preserved"] = verified_screenshots
    return res


def sync_manual_lineups(
    root: Path,
    event: str,
    *,
    now_utc: str | None = None,
    lineup_cutoff_minutes: int = 15,
    reevaluate: bool = True,
) -> dict[str, Any]:
    """Inspecciona `outputs/matches/<EVENT_ID>/lineups/manual/`, preserva capturas y valida/incorpora JSON manual."""
    from bet_engine.context.lineup_manager import (
        LineupStatus,
        load_and_validate_manual_lineup_file,
        prepare_manual_lineup_inbox,
        scan_manual_lineup_inbox,
    )

    directory, case, manifest = _load_case(root, event)
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")
    ko_dt = datetime.fromisoformat(str(case["kickoff_madrid"]).replace("Z", "+00:00"))
    remain_min = round((ko_dt - now_dt).total_seconds() / 60.0, 2)
    cutoff_reached = remain_min <= float(lineup_cutoff_minutes)

    home_meta = case.get("home") or {}
    away_meta = case.get("away") or {}
    inbox_setup = prepare_manual_lineup_inbox(
        directory,
        event_id=str(event),
        fixture_id=int(case["fixture_id"]),
        home_name=str(home_meta.get("name") or ""),
        away_name=str(away_meta.get("name") or ""),
        home_id=home_meta.get("api_team_id"),
        away_id=away_meta.get("api_team_id"),
        kickoff_iso=str(case["kickoff_madrid"]),
    )
    inbox = scan_manual_lineup_inbox(directory, event_id=str(event))
    if inbox["screenshots"]:
        manifest["manual_lineup_screenshots"] = inbox["screenshots"]

    json_candidates = inbox["json_candidates"]
    if not json_candidates:
        status_val = (
            LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value if cutoff_reached else LineupStatus.LINEUPS_PENDING.value
        )
        inbox_state = (
            "AWAITING_CODEX_VISUAL_INTERPRETATION" if inbox["screenshots"] else "MANUAL_INBOX_EMPTY"
        )
        manifest["lineup_state"] = {
            "status": status_val,
            "confirmed_complete_22": False,
            "inbox_state": inbox_state,
            "lineup_cutoff_minutes": int(lineup_cutoff_minutes),
            "minutes_to_kickoff": remain_min,
            "screenshots_count": len(inbox["screenshots"]),
            "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
        }
        atomic_json(directory / "manifest.json", manifest)
        return {
            "event_id": str(event),
            "fixture_id": int(case["fixture_id"]),
            "lineup_status": status_val,
            "confirmed_complete_22": False,
            "inbox_state": inbox_state,
            "screenshots_preserved": inbox["screenshots"],
            "codex_prompt_path": inbox_setup["prompt_path"],
            "template_path": inbox_setup["template_path"],
            "expected_json_path": inbox_setup["expected_json_path"],
        }

    json_path = Path(json_candidates[0])
    try:
        res = attach_manual_lineups(
            root,
            event,
            json_path,
            now_utc=now_dt.isoformat(),
            reevaluate=reevaluate,
        )
        return {
            **res,
            "inbox_state": "CONFIRMED_MANUAL_LINEUP_INGESTED",
            "lineup_status": res["lineup_state"]["status"],
            "confirmed_complete_22": True,
        }
    except ValueError as exc:
        if "LINEUP_PROBABLE_CANNOT_BE_PROMOTED_TO_CONFIRMED" in str(exc):
            prob_doc = load_and_validate_manual_lineup_file(
                json_path,
                expected_fixture_id=int(case["fixture_id"]),
                expected_event_id=str(event),
                expected_home_id=home_meta.get("api_team_id"),
                expected_away_id=away_meta.get("api_team_id"),
                expected_home_name=home_meta.get("name"),
                expected_away_name=away_meta.get("name"),
                kickoff_utc=case["kickoff_madrid"],
                now_utc=now_dt,
                allow_probable_only=True,
            )
            status_val = (
                LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value if cutoff_reached else LineupStatus.LINEUPS_PROBABLE_ONLY.value
            )
            manifest["lineup_state"] = {
                "status": status_val,
                "confirmed_complete_22": False,
                "inbox_state": "PROBABLE_ONLY_REJECTED_FOR_CONFIRMATION",
                "source_origin": prob_doc.get("source"),
                "captured_at_utc": prob_doc.get("captured_at_utc"),
                "uncertainties": prob_doc.get("uncertainties", []),
                "lineup_cutoff_minutes": int(lineup_cutoff_minutes),
                "minutes_to_kickoff": remain_min,
                "absence_interpretation": "NOT_INFERRED_AS_INJURY_OR_SUSPENSION",
            }
            atomic_json(directory / "manifest.json", manifest)
            audit_path = directory / "lineups" / f"lineup_audit_{event}.json"
            atomic_json(
                audit_path,
                {
                    "event_id": str(event),
                    "fixture_id": int(case["fixture_id"]),
                    "evaluated_at_utc": now_dt.isoformat(),
                    "lineup_state": manifest["lineup_state"],
                    "screenshots_preserved": inbox["screenshots"],
                    "rejected_probable_json": str(json_path),
                    "original_inputs_untouched": True,
                },
            )
            return {
                "event_id": str(event),
                "fixture_id": int(case["fixture_id"]),
                "lineup_status": status_val,
                "confirmed_complete_22": False,
                "inbox_state": "PROBABLE_ONLY_REJECTED_FOR_CONFIRMATION",
                "screenshots_preserved": inbox["screenshots"],
                "audit_path": str(audit_path.resolve()),
            }
        raise


def refresh_odds_from_snapshot(
    root: Path,
    event: str,
    snapshot_txt: Path,
    *,
    now_utc: str | None = None,
    max_odds_age_minutes: int = 60,
    reevaluate_shadow: bool = True,
) -> dict[str, Any]:
    """Refresco offline/simulado de cuotas Bwin desde TXT local fechado sin ejecutar scraper."""
    directory, case, manifest = _load_case(root, event)
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")
    ko_dt = datetime.fromisoformat(str(case["kickoff_madrid"]).replace("Z", "+00:00"))
    if now_dt >= ko_dt:
        raise ValueError("ODDS_REFRESH_AFTER_KICKOFF")

    src_path = Path(snapshot_txt).resolve()
    if not src_path.is_file():
        raise ValueError(f"ODDS_SNAPSHOT_NOT_FOUND:{src_path}")
    raw_txt = src_path.read_text(encoding="utf-8-sig", errors="replace")
    m_eid = prematch_single.RE_EVENT.search(raw_txt[:6000])
    if not m_eid or m_eid.group(1) != str(event):
        raise ValueError(f"ODDS_SNAPSHOT_EVENT_MISMATCH:expected={event}")
    m_cap = prematch_single.RE_CAPTURE.search(raw_txt[:6000])
    if not m_cap:
        raise ValueError("ODDS_SNAPSHOT_CAPTURE_MISSING")
    cap_dt = datetime.fromisoformat(m_cap.group(1).replace("Z", "+00:00"))
    if cap_dt.tzinfo is None:
        raise ValueError("ODDS_CAPTURE_TIMEZONE_REQUIRED")
    if cap_dt > now_dt or cap_dt >= ko_dt:
        raise ValueError("ODDS_CAPTURE_AFTER_NOW_OR_KICKOFF")
    age_min = round((now_dt - cap_dt).total_seconds() / 60.0, 2)
    if age_min > float(max_odds_age_minutes):
        raise ValueError(f"ODDS_SNAPSHOT_TOO_OLD:{age_min}m>{max_odds_age_minutes}m")

    # Conservar snapshot previo intacto en archivo y calcular delta real
    odds_dest = directory / "inputs" / f"cuotas_bwin_{event}.txt"
    archive = directory / "archives" / f"refresh_{now_dt.strftime('%Y%m%dT%H%M%S%fZ')}"
    archive.mkdir(parents=True, exist_ok=True)
    prev_ref = (manifest.get("inputs") or {}).get("odds") or {}
    preserved = None
    delta: dict[str, Any] = {"note": "FIRST_ODDS_PINNED"}
    if odds_dest.is_file():
        prev_txt = odds_dest.read_text(encoding="utf-8-sig", errors="replace")
        m_prev = prematch_single.RE_CAPTURE.search(prev_txt[:6000])
        if m_prev:
            prev_cap = datetime.fromisoformat(m_prev.group(1).replace("Z", "+00:00"))
            if cap_dt <= prev_cap:
                raise ValueError("ODDS_REFRESH_NOT_NEWER_THAN_PREVIOUS")
        preserved = archive / f"cuotas_bwin_{event}.prev.txt"
        preserved.write_bytes(odds_dest.read_bytes())
    odds_dest.write_bytes(src_path.read_bytes())
    if preserved is not None:
        delta = prematch_single.odds_changes(preserved, odds_dest, str(event))
        atomic_json(archive / "odds_delta.json", delta)

    manifest.setdefault("inputs", {})["odds"] = {
        **reference(odds_dest),
        "generated_from": str(src_path),
        "refreshed_at_utc": now_dt.isoformat(),
        "simulated_offline": True,
    }
    manifest["odds_refresh_at_utc"] = now_dt.isoformat()
    if preserved is not None:
        manifest["odds_delta"] = reference(archive / "odds_delta.json")
    manifest["invalidated_by_odds_refresh"] = {
        "previous_sha256": prev_ref.get("sha256"),
        "invalidated_at_utc": now_dt.isoformat(),
        "stages": ["validation", "probabilistic", "shadow", "codex_analysis", "final_review"],
    }
    for step in ("validacion", "motor"):
        (manifest.get("steps") or {}).pop(step, None)
    manifest.pop("codex_prompt", None)
    manifest.pop("validation_state", None)
    for stale_path in (
        directory / "shadow" / f"shadow_summary_{event}.json",
        directory / f"CODEX_{event}.md",
    ):
        if stale_path.is_file():
            backup = archive / stale_path.name
            if not backup.exists():
                backup.write_bytes(stale_path.read_bytes())
            stale_path.unlink()
    atomic_json(directory / "manifest.json", manifest)

    shadow_res: dict[str, Any] = {"executed": False}
    if reevaluate_shadow and (manifest.get("inputs") or {}).get("context_json"):
        from bet_engine.orchestration import run_shadow_for_event_dir

        shadow_res = {
            "executed": True,
            **run_shadow_for_event_dir(
                root,
                str(event),
                historical=False,
                now_utc=now_dt.isoformat(),
                max_odds_age_minutes=max_odds_age_minutes,
                min_lead_minutes=0,
            ),
        }
    return {
        "event_id": str(event),
        "odds_path": str(odds_dest.resolve()),
        "odds_sha256": sha(odds_dest),
        "capture_utc": cap_dt.astimezone(timezone.utc).isoformat(),
        "age_minutes": age_min,
        "preserved_previous": str(preserved.resolve()) if preserved else None,
        "delta": delta,
        "shadow_reevaluation": shadow_res,
    }


def prepare_fallback_candidate_expediente(
    root: Path,
    candidate_row: dict[str, Any],
    *,
    discarded_event_id: str,
    discarded_audit_path: Path,
    handoff_path: Path | None = None,
    odds_txt_path: Path | None = None,
    context_json_path: Path | None = None,
    context_txt_path: Path | None = None,
    manual_lineup_path: Path | None = None,
    report_path: Path | None = None,
    now_utc: str | None = None,
    run_safe_shadow: bool = True,
) -> dict[str, Any]:
    """Prepara de forma offline e idempotente el expediente del candidato B tras descartar A."""
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")

    eid = str(candidate_row.get("bwin_event_id") or candidate_row.get("event_id") or "").strip()
    fid = int(candidate_row.get("fixture_id") or 0)
    if not eid or fid <= 0:
        raise ValueError("FALLBACK_CANDIDATE_MISSING_EVENT_OR_FIXTURE_ID")

    eff_handoff = handoff_path or (Path(candidate_row["handoff_path"]) if candidate_row.get("handoff_path") else None)
    if eff_handoff and eff_handoff.is_file():
        selected_path = adopt(root, eff_handoff)
        directory, case, manifest = _load_case(root, eid)
    else:
        mdir = root / "outputs" / "matches" / eid
        inputs_dir = mdir / "inputs"
        sel_dir = mdir / "selection" / f"fallback_{now_dt.strftime('%Y%m%dT%H%M%SZ')}"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        sel_dir.mkdir(parents=True, exist_ok=True)

        eff_ctx_json = context_json_path or (Path(candidate_row["context_json_path"]) if candidate_row.get("context_json_path") else None)
        eff_ctx_txt = context_txt_path or (Path(candidate_row["context_txt_path"]) if candidate_row.get("context_txt_path") else None)
        eff_odds = odds_txt_path or (Path(candidate_row["odds_txt"]) if candidate_row.get("odds_txt") else None)
        eff_report = report_path or (Path(candidate_row["report_path"]) if candidate_row.get("report_path") else None)
        if not eff_ctx_json or not eff_ctx_json.is_file():
            raise ValueError(f"FALLBACK_CANDIDATE_MISSING_CONTEXT_JSON:{eid}")
        if not eff_odds or not eff_odds.is_file():
            raise ValueError(f"FALLBACK_CANDIDATE_MISSING_ODDS_TXT:{eid}")

        from match_identity import identity_from_payload

        ctx_payload = read(eff_ctx_json)
        ident_obj = identity_from_payload(ctx_payload)
        h_ident = ident_obj.get("home") or {}
        a_ident = ident_obj.get("away") or {}
        home_name = str(candidate_row.get("home") or h_ident.get("name") or "")
        away_name = str(candidate_row.get("away") or a_ident.get("name") or "")
        home_id = int(candidate_row.get("home_api_id") or candidate_row.get("home_id") or h_ident.get("api_team_id") or 0)
        away_id = int(candidate_row.get("away_api_id") or candidate_row.get("away_id") or a_ident.get("api_team_id") or 0)
        bwin_url = str(candidate_row.get("bwin_url") or ident_obj.get("bwin_url") or (ctx_payload.get("match") or {}).get("url") or f"https://www.bwin.es/es/sports/eventos/fallback:{eid}")
        kickoff_iso = str(candidate_row.get("kickoff_madrid") or candidate_row.get("kickoff_utc") or ident_obj.get("kickoff_utc") or (ctx_payload.get("fixture") or {}).get("date") or "")

        odds_dst = inputs_dir / f"cuotas_bwin_{eid}.txt"
        create_once(odds_dst, eff_odds.read_bytes())
        ctx_json_dst = inputs_dir / f"match_engine_{eid}.json"
        create_once(ctx_json_dst, eff_ctx_json.read_bytes())

        ident_dst = inputs_dir / "match_identity.json"
        create_once(ident_dst, (json.dumps(ident_obj, ensure_ascii=False, indent=2) + "\n").encode())
        inputs_manifest = {
            "odds": reference(odds_dst),
            "context_json": reference(ctx_json_dst),
            "identity": reference(ident_dst),
        }
        if eff_ctx_txt and eff_ctx_txt.is_file():
            ctx_txt_dst = inputs_dir / f"match_engine_{eid}.txt"
            create_once(ctx_txt_dst, eff_ctx_txt.read_bytes())
            inputs_manifest["context"] = reference(ctx_txt_dst)
        if eff_report and eff_report.is_file():
            rep_dst = inputs_dir / f"reporte_cualitativo_{eid}.md"
            create_once(rep_dst, eff_report.read_bytes())
            inputs_manifest["report"] = reference(rep_dst)
            inputs_manifest["qualitative_research"] = reference(rep_dst)

        selected_doc = {
            "schema_version": "1.0-selected-single-fixture",
            "status": "SELECTED_FOR_DEEP_RESEARCH",
            "fixture_id": fid,
            "bwin_event_id": eid,
            "bwin_url": bwin_url,
            "home": home_name,
            "away": away_name,
            "home_api_id": home_id,
            "away_api_id": away_id,
            "league_id": int(candidate_row.get("league_id") or ((ctx_payload.get("fixture") or {}).get("league") or {}).get("id") or 0),
            "season": int(candidate_row.get("season") or ((ctx_payload.get("fixture") or {}).get("league") or {}).get("season") or 0),
            "kickoff_madrid": kickoff_iso,
            "odds_txt": str(odds_dst.resolve()),
            "odds_sha256": sha(odds_dst),
            "actionable_bet": False,
        }
        decision_doc = {
            "schema_version": "1.0-fallback-continuation",
            "status": "SELECTED",
            "method": "AUTOMATIC_FALLBACK_AFTER_MISSING_LINEUPS",
            "discarded_event_id": str(discarded_event_id),
            "provisional_or_selected": selected_doc,
            "actionable_bet": False,
        }
        selected_path = sel_dir / "selected_match.json"
        decision_path = sel_dir / "selection_decision.json"
        create_once(selected_path, (json.dumps(selected_doc, ensure_ascii=False, indent=2) + "\n").encode())
        create_once(decision_path, (json.dumps(decision_doc, ensure_ascii=False, indent=2) + "\n").encode())

        case = {
            "schema_version": SCHEMA,
            "event_id": eid,
            "fixture_id": fid,
            "bwin_url": bwin_url,
            "home": {"name": home_name, "api_team_id": home_id},
            "away": {"name": away_name, "api_team_id": away_id},
            "competition": {
                "league_id": selected_doc["league_id"],
                "name": str(candidate_row.get("league") or ""),
                "season": selected_doc["season"],
            },
            "kickoff_madrid": kickoff_iso,
            "identity_status": "FALLBACK_OFFLINE_PREPARED",
            "selected": reference(selected_path),
            "source_handoff": reference(decision_path),
            "created_utc": now_dt.isoformat(),
            "actionable_bet": False,
        }
        create_once(mdir / "case.json", (json.dumps(case, ensure_ascii=False, indent=2) + "\n").encode())
        manifest = {
            "schema_version": SCHEMA,
            "event_id": eid,
            "fixture_id": fid,
            "url": bwin_url,
            "prepared_at": now_dt.isoformat(),
            "status": "PREPARADO",
            "identity_status": "FALLBACK_OFFLINE_PREPARED",
            "kickoff_madrid": kickoff_iso,
            "inputs": inputs_manifest,
            "steps": {"fallback_offline_prepare": 0},
            "canonical_case": reference(mdir / "case.json"),
            "selected_match": reference(selected_path),
        }
        atomic_json(mdir / "manifest.json", manifest)
        directory = mdir

    case["fallback_from_discarded_event"] = {
        "discarded_event_id": str(discarded_event_id),
        "discarded_audit_path": str(discarded_audit_path.resolve()),
        "promoted_at_utc": now_dt.isoformat(),
    }
    atomic_json(directory / "case.json", case)
    manifest["fallback_from_discarded_event"] = dict(case["fallback_from_discarded_event"])
    atomic_json(directory / "manifest.json", manifest)

    manual_attach_res = None
    eff_manual_lu = manual_lineup_path or (Path(candidate_row["manual_lineup_path"]) if candidate_row.get("manual_lineup_path") else None)
    if eff_manual_lu and eff_manual_lu.is_file():
        manual_attach_res = attach_manual_lineups(root, eid, eff_manual_lu, now_utc=now_dt.isoformat(), reevaluate=False)
        _, case, manifest = _load_case(root, eid)

    closure = _evaluate_closure(directory, case, manifest, now_override=now_dt)
    manifest["lineup_state"] = closure["lineup_evaluation"]
    atomic_json(directory / "manifest.json", manifest)

    shadow_res: dict[str, Any] = {"executed": False}
    if run_safe_shadow:
        from bet_engine.orchestration import run_shadow_for_event_dir

        shadow_res = {
            "executed": True,
            **run_shadow_for_event_dir(
                root,
                eid,
                historical=False,
                now_utc=now_dt.isoformat(),
                min_lead_minutes=0,
            ),
        }

    return {
        "event_id": eid,
        "fixture_id": fid,
        "directory": str(directory.resolve()),
        "selected_path": str(selected_path.resolve()),
        "lineup_status": closure["lineups_status"],
        "lineups_confirmed_22": closure["lineups_confirmed_22"],
        "eligible_for_actionable_pick": closure["eligible_for_actionable_pick"],
        "manual_lineup_attached": manual_attach_res,
        "safe_shadow_step": shadow_res,
        "fallback_from_discarded_event": dict(case["fallback_from_discarded_event"]),
    }


def resolve_lineups_or_fallback(
    root: Path,
    event: str,
    *,
    ranking_path: Path | None = None,
    lineup_cutoff_minutes: int = 15,
    min_lead_minutes: int = 45,
    user_manual_absent: bool = False,
    now_utc: str | None = None,
    auto_prepare_next: bool = False,
    next_context_json: Path | None = None,
    next_context_txt: Path | None = None,
    next_odds_txt: Path | None = None,
    next_manual_lineups: Path | None = None,
    next_report: Path | None = None,
) -> dict[str, Any]:
    """Verifica el estado de XI confirmados y, si no procede antes del corte, descarta para auditoría, busca el siguiente candidato y opcionalmente prepara su expediente."""
    from bet_engine.context.lineup_manager import select_next_eligible_ranked_candidate

    directory, case, manifest = _load_case(root, event)
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")

    closure = _evaluate_closure(
        directory,
        case,
        manifest,
        lineup_cutoff_minutes=lineup_cutoff_minutes,
        user_manual_absent=user_manual_absent,
        now_override=now_dt,
    )
    # Antes de descartar, revisar automáticamente la bandeja manual lineups/manual/ por si existen XI manuales confirmados
    if not closure["lineups_confirmed_22"] and not user_manual_absent:
        from bet_engine.context.lineup_manager import scan_manual_lineup_inbox

        inbox = scan_manual_lineup_inbox(directory, event_id=str(event))
        if inbox["json_candidates"]:
            sync_manual_lineups(
                root,
                event,
                now_utc=now_dt.isoformat(),
                lineup_cutoff_minutes=lineup_cutoff_minutes,
                reevaluate=True,
            )
            directory, case, manifest = _load_case(root, event)
            closure = _evaluate_closure(
                directory,
                case,
                manifest,
                lineup_cutoff_minutes=lineup_cutoff_minutes,
                user_manual_absent=user_manual_absent,
                now_override=now_dt,
            )

    lineup_eval = dict(closure["lineup_evaluation"])
    manifest["lineup_state"] = {
        k: v
        for k, v in lineup_eval.items()
        if k != "extracted_manual_payload"
    }

    discarded = bool(not lineup_eval["confirmed_complete_22"] and (lineup_eval.get("cutoff_reached") or user_manual_absent))
    next_candidate: dict[str, Any] = {"status": "NOT_REQUESTED"}
    if discarded:
        manifest["actionable_bet"] = False
        manifest["final_eligibility"] = "BLOCKED_MISSING_CONFIRMED_22_STARTERS"
        manifest["eligible_for_actionable_pick"] = False
        manifest["eligible_for_final_analysis"] = False
        manifest["discarded_for_missing_lineups"] = {
            "event_id": str(event),
            "fixture_id": int(case["fixture_id"]),
            "discarded_at_utc": now_dt.isoformat(),
            "lineup_status": lineup_eval["status"],
            "reason": lineup_eval.get("reason"),
            "original_inputs_untouched": True,
        }
        if ranking_path is not None:
            rdoc = read(ranking_path)
            if isinstance(rdoc, list):
                rows = rdoc
            else:
                rows = rdoc.get("ranked_matches") or rdoc.get("tactical_selection_ranking") or rdoc.get("ranking") or rdoc.get("matches") or []
            next_candidate = select_next_eligible_ranked_candidate(
                rows,
                excluded_event_ids={str(event)},
                excluded_fixture_ids={int(case["fixture_id"])},
                now_utc=now_dt,
                min_lead_minutes=min_lead_minutes,
            )
    elif lineup_eval["confirmed_complete_22"]:
        manifest["eligible_for_final_analysis"] = True
        manifest["eligible_for_actionable_pick"] = bool(closure["eligible_for_actionable_pick"])
        if closure["final_prematch_valid"] and closure["eligible_for_actionable_pick"]:
            manifest["final_eligibility"] = "ELIGIBLE_ACTIONABLE_CLOSURE"
        elif closure["closure_stage"] == "PRELIMINARY_EVALUATION_ONLY":
            manifest["final_eligibility"] = "PRELIMINARY_EVALUATION_ONLY"
        elif manifest.get("final_eligibility") == "BLOCKED_MISSING_CONFIRMED_22_STARTERS":
            manifest["final_eligibility"] = "ELIGIBLE_PENDING_VALIDATION_AND_FINISH"

    atomic_json(directory / "manifest.json", manifest)
    audit_path = directory / "lineups" / f"lineup_audit_{event}.json"
    audit_doc = {
        "event_id": str(event),
        "fixture_id": int(case["fixture_id"]),
        "evaluated_at_utc": now_dt.isoformat(),
        "lineup_evaluation": lineup_eval,
        "discarded_from_actionable_queue": discarded,
        "original_inputs_untouched": True,
        "next_candidate_reconsideration": next_candidate,
    }

    prepared_next: dict[str, Any] | None = None
    if discarded and auto_prepare_next and next_candidate.get("status") == "NEXT_CANDIDATE_SELECTED":
        atomic_json(audit_path, audit_doc)
        prepared_next = prepare_fallback_candidate_expediente(
            root,
            next_candidate["selected_candidate"],
            discarded_event_id=str(event),
            discarded_audit_path=audit_path,
            odds_txt_path=next_odds_txt,
            context_json_path=next_context_json,
            context_txt_path=next_context_txt,
            manual_lineup_path=next_manual_lineups,
            report_path=next_report,
            now_utc=now_dt.isoformat(),
            run_safe_shadow=True,
        )
        audit_doc["automatic_continuation_prepared"] = prepared_next

    atomic_json(audit_path, audit_doc)
    return {
        "event_id": str(event),
        "fixture_id": int(case["fixture_id"]),
        "lineup_status": lineup_eval["status"],
        "confirmed_complete_22": lineup_eval["confirmed_complete_22"],
        "eligible_for_actionable_pick": lineup_eval["eligible_for_actionable_pick"],
        "eligible_for_final_analysis": lineup_eval["eligible_for_final_analysis"],
        "discarded": discarded,
        "audit_path": str(audit_path.resolve()),
        "next_candidate_reconsideration": next_candidate,
        "prepared_next_candidate": prepared_next,
    }


def reconcile_case_state(
    root: Path,
    event: str,
    *,
    now_utc: str | None = None,
    lineup_cutoff_minutes: int = 15,
) -> dict[str, Any]:
    """Reconcilia `manifest.json` y `lineups/lineup_audit_<E>.json` sin modificar los inputs originales."""
    directory, case, manifest = _load_case(root, event)
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")
    closure = _evaluate_closure(
        directory,
        case,
        manifest,
        lineup_cutoff_minutes=lineup_cutoff_minutes,
        now_override=now_dt,
    )
    lineup_eval = dict(closure["lineup_evaluation"])
    manifest["lineup_state"] = {
        k: v
        for k, v in lineup_eval.items()
        if k != "extracted_manual_payload"
    }
    manifest["eligible_for_final_analysis"] = bool(closure["eligible_for_final_analysis"])
    manifest["eligible_for_actionable_pick"] = bool(closure["eligible_for_actionable_pick"])
    if lineup_eval["confirmed_complete_22"]:
        if closure["final_prematch_valid"] and closure["eligible_for_actionable_pick"]:
            manifest["final_eligibility"] = "ELIGIBLE_ACTIONABLE_CLOSURE"
        elif closure["closure_stage"] == "PRELIMINARY_EVALUATION_ONLY":
            manifest["final_eligibility"] = "PRELIMINARY_EVALUATION_ONLY"
        elif closure["closure_stage"] == "HISTORICAL_AUDIT":
            manifest["final_eligibility"] = "HISTORICAL_AUDIT_NO_LIVE_BET"
        elif manifest.get("final_eligibility") == "BLOCKED_MISSING_CONFIRMED_22_STARTERS":
            manifest["final_eligibility"] = "ELIGIBLE_PENDING_VALIDATION_AND_FINISH"
    else:
        manifest["final_eligibility"] = "BLOCKED_MISSING_CONFIRMED_22_STARTERS"
    manifest["reconciled_at_utc"] = now_dt.isoformat()
    atomic_json(directory / "manifest.json", manifest)

    audit_path = directory / "lineups" / f"lineup_audit_{event}.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    prev_audit = read(audit_path) if audit_path.is_file() else {}
    prev_audit.update(
        {
            "event_id": str(event),
            "fixture_id": int(case["fixture_id"]),
            "reconciled_at_utc": now_dt.isoformat(),
            "lineup_state": manifest["lineup_state"],
            "closure_checks": closure,
            "original_inputs_untouched": True,
        }
    )
    atomic_json(audit_path, prev_audit)
    return {
        "event_id": str(event),
        "fixture_id": int(case["fixture_id"]),
        "status": manifest.get("status"),
        "lineup_status": lineup_eval["status"],
        "lineups_confirmed_22": lineup_eval["confirmed_complete_22"],
        "eligible_for_final_analysis": manifest["eligible_for_final_analysis"],
        "eligible_for_actionable_pick": manifest["eligible_for_actionable_pick"],
        "final_eligibility": manifest.get("final_eligibility"),
        "closure_stage": closure["closure_stage"],
        "audit_path": str(audit_path.resolve()),
    }


def validate_hypotheses(root: Path, event: str, packet_path: Path, sealed_path: Path,
                        ranking_path: Path | None = None) -> Path:
    """Contrast attached manual proposals with the existing sealed Python validator."""
    from prematch_llm_research import validate_hypotheses as run_validation
    directory, case, manifest = _load_case(root, event)
    attached = (manifest.get("inputs") or {}).get("manual_hypotheses") or {}
    hypothesis_path = Path(attached.get("path", ""))
    if not hypothesis_path.is_file() or sha(hypothesis_path) != attached.get("sha256"):
        raise ValueError("MANUAL_HYPOTHESES_NOT_ATTACHED_OR_CHANGED")
    packet = read(packet_path); sealed = read(sealed_path); source = read(hypothesis_path)
    target_ids = {int(row.get("fixture_id")) for row in packet.get("targets", []) if row.get("fixture_id") is not None}
    if int(case["fixture_id"]) not in target_ids:
        raise ValueError("STATISTICAL_PACKET_FIXTURE_MISMATCH")
    proposals = []
    for item in source["hypotheses"]:
        test = dict(item.get("python_test") or {})
        test.update(hypothesis_id=item["id"], mechanism=item.get("mechanism"), falsifier=item["refutation"])
        proposals.append(test)
    validation = run_validation(packet, sealed, proposals)
    registry = {"schema_version": "1.0-pattern-registry", "event_id": event,
                "fixture_id": case["fixture_id"], "rules": validation["results"],
                "actionable_bet": False}
    if ranking_path:
        base = read(ranking_path)
        base_rows = base.get("ranking", base) if isinstance(base, dict) else base
        if not isinstance(base_rows, list): raise ValueError("BASE_RANKING_MUST_BE_A_LIST")
    else:
        base_rows = [{"fixture_id": case["fixture_id"]}]
    statuses = [{"hypothesis_id": row["hypothesis"].get("hypothesis_id"),
                 "status": row["status"], "reason": row["reason"]} for row in validation["results"]]
    enriched = {"schema_version": "1.0-ranking-enriched", "event_id": event,
                "ranking": [{**row, "manual_hypothesis_evidence": statuses if int(row.get("fixture_id") or 0) == int(case["fixture_id"]) else []}
                            for row in base_rows],
                "ranking_policy": "ANNOTATION_ONLY_BASE_ORDER_PRESERVED", "actionable_bet": False}
    folder = directory / "research" / "validation" / hashlib.sha256(
        (sha(hypothesis_path) + sha(packet_path) + sha(sealed_path)).encode()).hexdigest()
    artifacts = {"llm_pattern_hypotheses.json": source, "pattern_validation.json": validation,
                 "pattern_registry.json": registry, "ranking_enriched.json": enriched}
    for name, value in artifacts.items():
        create_once(folder / name, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
    manifest.setdefault("inputs", {})["pattern_validation"] = reference(folder / "pattern_validation.json")
    manifest["inputs"]["pattern_registry"] = reference(folder / "pattern_registry.json")
    manifest["inputs"]["ranking_enriched"] = reference(folder / "ranking_enriched.json")
    manifest["hypothesis_validation_status"] = "PYTHON_VALIDATION_COMPLETE"
    atomic_json(directory / "manifest.json", manifest)
    return folder


def _resolve_quick_context_target(root: Path, identifier: str, preselection: Path | None = None):
    """Resolve either an existing canonical Event ID case or a RESEARCH_PENDING fixture ID."""
    clean_id = str(identifier).strip()
    if clean_id.startswith("fixture_"):
        clean_fid = clean_id[len("fixture_"):]
    else:
        clean_fid = clean_id

    case_dir = root / "outputs" / "matches" / clean_id
    if (case_dir / "case.json").is_file() and (case_dir / "manifest.json").is_file():
        directory, case, manifest = _load_case(root, clean_id)
        return {
            "kind": "CANONICAL_CASE",
            "case_dir": directory,
            "manifest_path": directory / "manifest.json",
            "manifest": manifest,
            "fixture_id": int(case["fixture_id"]),
            "event_id": str(case["event_id"]),
            "home": case["home"]["name"],
            "away": case["away"]["name"],
            "kickoff_utc": case["kickoff_madrid"],
        }

    # Research-pending fixture without Bwin Event ID yet: search preselection.json.
    if not clean_fid.isdigit():
        raise ValueError(f"QUICK_CONTEXT_TARGET_NOT_FOUND: {identifier}")
    target_fid = int(clean_fid)
    candidates = [preselection.resolve()] if preselection else sorted(
        (root / "outputs" / "discovery").glob("*/evaluations/*/prospective_trial/enriched/preselection.json"),
        reverse=True,
    )
    found_match = None
    found_pre = None
    for pre_path in candidates:
        if not pre_path.is_file():
            continue
        doc = read(pre_path)
        for m in doc.get("matches", []):
            if int(m.get("fixture_id") or 0) == target_fid:
                found_match = m
                found_pre = pre_path
                break
        if found_match:
            break
    if not found_match:
        raise ValueError(f"QUICK_CONTEXT_FIXTURE_NOT_FOUND: {identifier}")

    research_dir = root / "outputs" / "matches" / f"fixture_{target_fid}"
    research_case_path = research_dir / "research_case.json"
    if not research_case_path.is_file():
        create_once(
            research_case_path,
            (json.dumps({
                "schema_version": "1.0-research-pending-quick-context-case",
                "fixture_id": target_fid,
                "bwin_event_id": None,
                "home": found_match.get("home"),
                "away": found_match.get("away"),
                "competition": found_match.get("league"),
                "season": found_match.get("season"),
                "kickoff_madrid": found_match.get("kickoff_madrid"),
                "source_preselection": reference(found_pre),
                "actionable_bet": False,
            }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    return {
        "kind": "RESEARCH_PENDING_FIXTURE",
        "case_dir": research_dir,
        "manifest_path": None,
        "manifest": None,
        "fixture_id": target_fid,
        "event_id": None,
        "home": str(found_match.get("home") or ""),
        "away": str(found_match.get("away") or ""),
        "kickoff_utc": str(found_match.get("kickoff_madrid") or ""),
    }


def import_quick_context(root: Path, event: str, source: Path, preselection: Path | None = None) -> dict:
    """Importa Markdown de Quick Context con validación temporal/identidad, sin alterar p_model."""
    from bet_engine.context.quick_context import import_quick_context_markdown
    if not source.is_file():
        raise ValueError(f"QUICK_CONTEXT_FILE_NOT_FOUND: {source}")
    target = _resolve_quick_context_target(root, event, preselection)
    result = import_quick_context_markdown(
        target["case_dir"],
        source,
        expected_fixture_id=target["fixture_id"],
        expected_event_id=target["event_id"],
        expected_home=target["home"],
        expected_away=target["away"],
        expected_kickoff_utc=target["kickoff_utc"],
    )
    if target["manifest_path"] is not None and not result.get("duplicate"):
        manifest = read(target["manifest_path"])
        preserved = Path(result["preserved_md"])
        manifest.setdefault("inputs", {})["quick_context_latest"] = reference(preserved)
        manifest.setdefault("quick_contexts", []).append({
            "sha256": result["sha256"],
            "preserved_md": str(preserved),
            "evidence_count": result["evidence_count"],
            "contradictions": result["contradictions"],
            "p_model_modified": False,
        })
        manifest["quick_context_status"] = "ATTACHED_PREMATCH_EXPLORATORY"
        atomic_json(target["manifest_path"], manifest)
    return {
        **result,
        "target_kind": target["kind"],
        "fixture_id": target["fixture_id"],
        "bwin_event_id": target["event_id"],
        "case_dir": str(target["case_dir"].resolve()),
    }


def export_handoff(root: Path, event: str) -> Path:
    directory, case, manifest = _load_case(root, event)
    refs = {}
    for name, ref in (manifest.get("inputs") or {}).items():
        if isinstance(ref, dict) and ref.get("path"):
            path = Path(ref["path"])
            refs[name] = {**ref, "hash_valid": path.is_file() and sha(path) == ref.get("sha256")}
    invalid = [name for name, ref in refs.items() if not ref["hash_valid"]]
    if invalid: raise ValueError("DOSSIER_INPUT_HASH_INVALID: " + ",".join(invalid))
    from bet_engine.context.quick_context import load_quick_context_bundle
    qc_evidences, qc_summaries = load_quick_context_bundle(directory)
    coverage = {"statistical": "AVAILABLE" if any(k in refs for k in ("context", "context_json")) else "PENDING_CONTEXT",
                "manual_hypotheses": manifest.get("hypothesis_validation_status", "NOT_SUPPLIED"),
                "qualitative": manifest.get("qualitative_research_status", "NOT_SUPPLIED"),
                "quick_context": f"ATTACHED ({len(qc_summaries)} doc / {len(qc_evidences)} hechos)" if qc_summaries else "NOT_SUPPLIED",
                "bwin": "AVAILABLE" if "odds" in refs else "NOT_AVAILABLE"}
    qc_contradictions = sorted({c for doc in qc_summaries for c in doc.get("contradictions", [])})
    qc_affected = [h for doc in qc_summaries for h in doc.get("affected_hypotheses", [])]
    closure = _evaluate_closure(directory, case, manifest)
    dossier = {
        "schema_version": "2.0-final-prematch-dossier", "generated_utc": datetime.now(timezone.utc).isoformat(),
        "identity": {k: case[k] for k in ("event_id", "fixture_id", "bwin_url", "home", "away", "competition", "kickoff_madrid")},
        "pipeline_version": SCHEMA, "manifest_status": manifest.get("status"),
        "closure_stage": closure["closure_stage"], "final_prematch_valid": closure["final_prematch_valid"],
        "closure_checks": closure,
        "timestamps": {
            "odds_captured_utc": closure.get("shadow_odds_captured_utc") or (
                datetime.fromisoformat(closure["odds_capture_madrid"]).astimezone(timezone.utc).isoformat()
                if closure.get("odds_capture_madrid") else None
            ),
            "odds_sha256": (refs.get("odds") or {}).get("sha256"),
            "probabilities_computed_at_utc": closure.get("shadow_probabilities_computed_at_utc"),
            "prices_evaluated_at_utc": closure.get("shadow_prices_evaluated_at_utc"),
            "timestamp_granularity": closure.get("shadow_timestamp_granularity"),
            "finish_completed_utc": closure.get("finish_completed_utc"),
            "shadow_odds_sha256": closure.get("shadow_odds_sha256"),
            "shadow_odds_selection_count": closure.get("shadow_odds_selection_count"),
            "shadow_stale": closure.get("shadow_stale"),
            "shadow_verified": closure.get("shadow_verified"),
        },
        "coverage": coverage,
        "inputs": refs, "validation_state": manifest.get("validation_state"),
        "quick_context": {
            "count": len(qc_summaries),
            "evidences_count": len(qc_evidences),
            "contradictions": qc_contradictions,
            "affected_hypotheses": qc_affected,
            "sources": [{"sha256": d.get("sha256"), "urls": d.get("source_urls", []), "cutoff_utc": d.get("analysis_cutoff_utc")} for d in qc_summaries],
            "p_model_modified": False,
        },
        "codex_prompt": manifest.get("codex_prompt"), "actionable_bet": False,
        "limitations": ["Las cuotas son snapshots", "No se acredita valor financiero sin modelo validado",
                        "Las hipótesis manuales no son patrones validados hasta su contraste Python"],
    }
    folder = directory / "dossier"
    json_path = folder / f"dossier_{event}.json"
    md_path = folder / f"dossier_{event}.md"
    atomic_json(json_path, dossier)
    lines = [f"# Expediente PREMATCH canónico · {event}", "", "## Identidad", "",
             f"- Fixture ID: {case['fixture_id']}", f"- Partido: {case['home']['name']} – {case['away']['name']}",
             f"- Kickoff Madrid: {case['kickoff_madrid']}", f"- Estado manifest: {manifest.get('status')}",
             f"- Clasificación cierre: {closure['closure_stage']} (final_prematch_valid={closure['final_prematch_valid']})",
             f"- XI API: {closure['lineups_status']} | Shadow: {closure['shadow_status']} (stale={closure.get('shadow_stale')})",
             f"- Timestamp precios Bwin (UTC): {dossier['timestamps']['odds_captured_utc']} | SHA-256: `{dossier['timestamps']['odds_sha256']}`",
             f"- Timestamp probabilidades Shadow (UTC): {dossier['timestamps']['probabilities_computed_at_utc']} | Evaluación precios (UTC): {dossier['timestamps']['prices_evaluated_at_utc']}",
             "", "## Cobertura", ""]
    lines += [f"- {name}: {value}" for name, value in coverage.items()]
    if qc_summaries:
        lines += ["", "## Quick Context Cualitativo (No altera p_model)", ""]
        for d in qc_summaries:
            lines.append(f"- SHA `{str(d.get('sha256',''))[:12]}` · corte `{d.get('analysis_cutoff_utc')}` · fuentes: {', '.join(d.get('source_urls', [])[:5])}")
        if qc_contradictions:
            lines += ["", "### Contradicciones registradas", ""] + [f"- {c}" for c in qc_contradictions]
        if qc_affected:
            lines += ["", "### Hipótesis estadísticas afectadas", ""] + [f"- `{h['hypothesis_area']}`: {h['impact_note']}" for h in qc_affected]
    lines += ["", "## Fuentes verificadas", ""] + [f"- {name}: `{ref['path']}` · SHA-256 `{ref['sha256']}`" for name, ref in refs.items()]
    lines += ["", "## Límites", "", "- No es una apuesta ni una recomendación automática.",
              "- La decisión final requiere control temporal, XI, precio fresco y evidencia compatible.", ""]
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path


def delegate(root: Path, selected: Path, action: str, extra: list[str], confirm_external: bool) -> int:
    command = [sys.executable, str(root / "prematch_single.py"), "--root", str(root), "--selected", str(selected)]
    if confirm_external: command.append("--confirm-external")
    if extra[:1] == ["--"]: extra = extra[1:]
    command += [action, *extra]
    return subprocess.run(command, cwd=root, check=False).returncode


def prepare_research(selection: Path, output: Path):
    """Freeze a local qualitative dossier without creating an executable case."""
    doc=read(selection)
    if doc.get('status')!='SELECTED' or not doc.get('comparison',{}).get('comparison_complete'):
        raise ValueError('COMPARISON_INCOMPLETE_OR_NO_RESEARCH_SELECTION')
    for value in doc['sources'].values():
        for ref in value if isinstance(value,list) else [value]:
            if sha(Path(ref['path']))!=ref['sha256']:raise ValueError('RESEARCH_SOURCE_CHANGED')
    winner=doc['selected'];pre=read(Path(doc['sources']['preselection']['path']))
    fixtures=[f for f in pre['matches'] if f['fixture_id']==winner['fixture_id']]
    if len(fixtures)!=1:raise ValueError('SELECTED_FIXTURE_NOT_UNIQUE')
    fixture=fixtures[0];now=datetime.now(timezone.utc)
    kickoff=datetime.fromisoformat(fixture['kickoff_madrid'])
    if now>=kickoff:raise ValueError('SELECTED_FIXTURE_ALREADY_STARTED')
    odds=Path(winner['odds_txt'])
    if sha(odds)!=winner['odds_sha256']:raise ValueError('SELECTED_ODDS_CHANGED')
    preflight=Path(winner['preflight_path'])
    if sha(preflight)!=winner['preflight_sha256']:raise ValueError('SELECTED_PREFLIGHT_CHANGED')
    output.mkdir(parents=True,exist_ok=False)
    packet={'schema_version':'1.0-research-packet','fixture':fixture,'selection':doc,
            'selection_source':reference(selection),'odds':reference(odds),'preflight':reference(preflight),
            'created_at_utc':now.isoformat(),'actionable_bet':False,'final_validation_pending':True,
            'next_steps':['qualitative research with dated evidence','resolve identity/time/category warnings',
                          'refresh odds and revalidate PREMATCH','official XI/context','existing final validator and model']}
    atomic_json(output/'research_packet.json',packet)
    (output/'research_brief.md').write_text('\n'.join([
        f"# Investigación: {fixture['home']}–{fixture['away']}",
        f"Fixture {fixture['fixture_id']} / Bwin {winner['bwin_event_id']}",
        f"Inicio Europe/Madrid: {fixture['kickoff_madrid']}",
        'Estado: investigación; validación final pendiente. No es una apuesta.',
        'Las cuotas son snapshots. Revalidar vigencia y PREMATCH antes de avanzar.',
        f"Advertencias finales: {doc['final_validation_reasons']}",
        f"Estadística, coberturas y referencias: {output/'research_packet.json'}",'']),encoding='utf-8')
    return output/'research_packet.json'


def prepare_independent_event(
    root: Path,
    *,
    event_id: str,
    preselection_path: Path | None = None,
    bwin_discovery_path: Path | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Prepara offline un expediente canónico independiente (sin ocupar slots del orquestador)."""
    from zoneinfo import ZoneInfo
    from bet_engine.context.lineup_manager import prepare_manual_lineup_inbox

    root = Path(root).resolve()
    eid = str(event_id).strip()
    if not eid.isdigit() or len(eid) < 6:
        raise ValueError(f"INVALID_EVENT_ID:{event_id}")
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) if now_utc else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("NOW_UTC_TIMEZONE_REQUIRED")

    bwin_cands = [bwin_discovery_path.resolve()] if bwin_discovery_path else sorted(
        (root / "outputs" / "discovery").glob("*/evaluations/*/prospective_trial/bwin/bwin_discovery.json"),
        reverse=True,
    ) + sorted(
        (root / "outputs" / "discovery").glob("*/prospective_trial/bwin/bwin_discovery.json"),
        reverse=True,
    )
    bwin_row = None
    bwin_src = None
    for bp in bwin_cands:
        if not bp.is_file():
            continue
        bdoc = read(bp)
        hits = [r for r in (bdoc.get("matches") or []) if str(r.get("bwin_event_id") or "") == eid]
        if len(hits) == 1:
            bwin_row = hits[0]
            bwin_src = bp
            break
    if bwin_row is None or bwin_src is None:
        raise ValueError(f"INDEPENDENT_BWIN_EVENT_NOT_FOUND:{eid}")

    fid = int(bwin_row.get("fixture_id") or 0)
    if fid <= 0:
        raise ValueError(f"INDEPENDENT_FIXTURE_ID_MISSING:{eid}")

    pre_cands = [preselection_path.resolve()] if preselection_path else [
        bwin_src.parent.parent / "enriched" / "preselection.json",
        *sorted(
            (root / "outputs" / "discovery").glob("*/evaluations/*/prospective_trial/enriched/preselection.json"),
            reverse=True,
        ),
    ]
    pre_row = None
    pre_src = None
    for pp in pre_cands:
        if not pp.is_file():
            continue
        pdoc = read(pp)
        hits = [m for m in (pdoc.get("matches") or []) if int(m.get("fixture_id") or 0) == fid]
        if len(hits) == 1:
            pre_row = hits[0]
            pre_src = pp
            break
    if pre_row is None or pre_src is None:
        raise ValueError(f"INDEPENDENT_PRESELECTION_MATCH_NOT_FOUND:{fid}")

    ko_raw = str(pre_row.get("kickoff_madrid") or bwin_row.get("kickoff_madrid") or "")
    ko_madrid_dt = datetime.fromisoformat(ko_raw.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Madrid"))
    if now_dt >= ko_madrid_dt:
        raise ValueError("INDEPENDENT_EVENT_ALREADY_STARTED")

    bwin_url = str(bwin_row.get("bwin_url") or "")
    home_id = int(pre_row.get("home_id") or bwin_row.get("home_id") or 0)
    away_id = int(pre_row.get("away_id") or bwin_row.get("away_id") or 0)
    home_name = str(pre_row.get("home") or bwin_row.get("home") or "")
    away_name = str(pre_row.get("away") or bwin_row.get("away") or "")
    league_id = int(pre_row.get("league_id") or bwin_row.get("league_id") or 0)
    league_name = str(pre_row.get("league") or bwin_row.get("league") or "")
    season = int(pre_row.get("season") or bwin_row.get("season") or 0)
    gender = str(pre_row.get("gender") or bwin_row.get("gender") or "M")
    if home_id <= 0 or away_id <= 0 or home_id == away_id or league_id <= 0:
        raise ValueError("INDEPENDENT_CANONICAL_IDS_INVALID")

    bwin_status = str(bwin_row.get("status") or bwin_row.get("bwin_match_status") or "")
    time_verified = bwin_status == "IDENTITY_VERIFIED_AUTOMATICALLY"
    sel_tier = "A" if time_verified else "B"
    sel_eligible = bool(time_verified)
    missing_ev = [] if time_verified else [f"BWIN_KICKOFF_NOT_MACHINE_VERIFIED:{bwin_status or 'UNVERIFIED'}"]

    mdir = root / "outputs" / "matches" / eid
    sel_dir = mdir / "selection"
    sel_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = sel_dir / "independent_handoff.json"
    selected_path = sel_dir / "selected_match.json"
    decision_path = sel_dir / "selection_decision.json"
    case_path = mdir / "case.json"
    manifest_path = mdir / "manifest.json"

    if not handoff_path.is_file():
        atomic_json(handoff_path, {
            "schema_version": "1.0-independent-event-handoff",
            "event_id": eid,
            "fixture_id": fid,
            "bwin_url": bwin_url,
            "bwin_discovery": reference(bwin_src),
            "preselection": reference(pre_src),
            "created_utc": now_dt.isoformat(),
        })
    asof_madrid = now_dt.astimezone(ZoneInfo("Europe/Madrid")).isoformat()
    common_payload = {
        "home_api_id": home_id,
        "away_api_id": away_id,
        "league_id": league_id,
        "season": season,
        "gender": gender,
        "fixture_id": fid,
        "bwin_event_id": eid,
        "bwin_url": bwin_url,
        "home": home_name,
        "away": away_name,
        "kickoff_madrid": ko_madrid_dt.isoformat(),
        "selection_asof_madrid": asof_madrid,
        "selection_score_not_probability": pre_row.get("research_priority_score") or bwin_row.get("research_priority_score") or 0.0,
        "selection_tier": sel_tier,
        "eligible": sel_eligible,
        "research_eligible": True,
        "research_missing_evidence": missing_ev,
        "hard_block_reasons": [],
        "actionable_bet": False,
        **{
            k: pre_row[k]
            for k in ("round", "category_level", "category_evidence_status", "category_provenance", "national_team_evidence")
            if pre_row.get(k) is not None
        },
    }
    decision_doc = {
        "schema_version": "1.0-independent-selection-decision",
        "status": "SELECTED",
        "method": "INDEPENDENT_SINGLE_EVENT_BOOTSTRAP",
        "asof_madrid": asof_madrid,
        "provisional_or_selected": {**common_payload, "market_families_ready": []},
        "actionable_bet": False,
    }
    selection_doc = {
        "schema_version": "1.0-selected-single-fixture",
        "status": "SELECTED_FOR_DEEP_RESEARCH",
        **common_payload,
        "market_families_ready_for_research": [],
        "source_decision": str(decision_path.resolve()),
        "source_handoff": str(handoff_path.resolve()),
        "warning": "Expediente independiente fuera del orquestador de franjas; requiere adquisición Bwin + API-Football y XI P0.",
    }
    prematch_single.validate_selected(selection_doc, decision_doc)
    if not decision_path.is_file() or not selected_path.is_file():
        atomic_json(decision_path, decision_doc)
        atomic_json(selected_path, selection_doc)

    if case_path.is_file():
        existing_case = read(case_path)
        if str(existing_case.get("event_id") or "") != eid or int(existing_case.get("fixture_id") or 0) != fid:
            raise ValueError(f"CANONICAL_CASE_IDENTITY_CONFLICT:{eid}")
    else:
        atomic_json(case_path, {
            "schema_version": SCHEMA,
            "event_id": eid,
            "fixture_id": fid,
            "bwin_url": bwin_url,
            "home": {"name": home_name, "api_team_id": home_id},
            "away": {"name": away_name, "api_team_id": away_id},
            "competition": {"league_id": league_id, "name": league_name, "season": season},
            "kickoff_utc": ko_madrid_dt.astimezone(timezone.utc).isoformat(),
            "kickoff_madrid": ko_madrid_dt.isoformat(),
            "identity_status": (
                "INDEPENDENT_DISCOVERY_VERIFIED_CONTEXT_PENDING"
                if time_verified
                else "INDEPENDENT_BWIN_TIME_UNVERIFIED_CONTEXT_PENDING"
            ),
            "pipeline_version": SCHEMA,
            "source_kind": "INDEPENDENT_EVENT_SELECTION",
            "selected": reference(selected_path),
            "source_handoff": reference(handoff_path),
            "created_utc": now_dt.isoformat(),
            "actionable_bet": False,
            "ready_to_bet": False,
            "qualitative_research": {"attached": False, "sources": []},
        })

    sel_doc = read(selected_path)
    dec_doc = read(decision_path)
    res_sel = {
        "selection_tier": sel_doc.get("selection_tier", sel_tier),
        "research_eligible": True,
        "eligible": sel_doc.get("eligible", sel_eligible),
        "research_missing_evidence": sel_doc.get("research_missing_evidence", missing_ev),
        "hard_block_reasons": sel_doc.get("hard_block_reasons", []),
        "selected_sha256": sha(selected_path),
        "decision_sha256": sha(decision_path),
        "actionable_bet": False,
    }
    if not manifest_path.is_file():
        manifest_doc = {
            "schema_version": SCHEMA,
            "event_id": eid,
            "fixture_id": fid,
            "url": bwin_url,
            "prepared_at": now_dt.isoformat(),
            "status": "CASE_CREATED_PENDING_ACQUISITION",
            "identity_status": read(case_path)["identity_status"],
            "source_kind": "INDEPENDENT_EVENT_SELECTION",
            "inputs": {},
            "steps": {},
            "kickoff_madrid": ko_madrid_dt.isoformat(),
            "canonical_case": reference(case_path),
            "selected_match": reference(selected_path),
            "source_handoff": reference(handoff_path),
            "research_selection": res_sel,
        }
    else:
        manifest_doc = read(manifest_path)
        manifest_doc["canonical_case"] = reference(case_path)
        manifest_doc["selected_match"] = reference(selected_path)
        manifest_doc["source_handoff"] = reference(handoff_path)
        manifest_doc["kickoff_madrid"] = manifest_doc.get("kickoff_madrid") or ko_madrid_dt.isoformat()
        manifest_doc.setdefault("research_selection", res_sel)
    atomic_json(manifest_path, manifest_doc)

    inbox = prepare_manual_lineup_inbox(
        mdir,
        event_id=eid,
        fixture_id=fid,
        home_id=home_id,
        away_id=away_id,
        home_name=home_name,
        away_name=away_name,
        kickoff_iso=ko_madrid_dt.isoformat(),
    )
    return {
        "event_id": eid,
        "fixture_id": fid,
        "status": manifest_doc.get("status"),
        "bwin_url": bwin_url,
        "selected_match_path": str(selected_path.resolve()),
        "case_path": str(case_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manual_lineup_inbox": inbox["manual_dir"],
        "requires_external_acquisition": not all(
            k in (manifest_doc.get("inputs") or {}) for k in ("odds", "context", "context_json", "identity")
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser('prepare-research');p.add_argument('--selection',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p = sub.add_parser("prepare"); p.add_argument("--handoff", type=Path, required=True); p.add_argument("--historical", action="store_true"); p.add_argument("--asof")
    p = sub.add_parser('promote-research')
    p.add_argument('--research-selection', type=Path, required=True)
    p.add_argument('--discovery', type=Path, required=True)
    p.add_argument('--preflight', type=Path, required=True)
    p.add_argument('--event-id', required=True)
    for name in ("status", "export-handoff"):
        p = sub.add_parser(name); p.add_argument("--event-id", required=True)
    for name in ("attach-hypotheses", "attach-research"):
        p = sub.add_parser(name); p.add_argument("--event-id", required=True); p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("attach-manual-lineups", help="Incorpora XI confirmados manuales/externos en JSON estructurado y reevalúa")
    p.add_argument("--event-id", required=True)
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--now-utc", help="Reloj UTC explícito para pruebas offline")
    p.add_argument("--no-reevaluate", action="store_true", help="No reejecutar Shadow inmediatamente tras adjuntar XI")
    p = sub.add_parser("sync-manual-lineups", help="Prepara o sincroniza la bandeja `lineups/manual/` (capturas + JSON asistido por Codex)")
    p.add_argument("--event-id", required=True)
    p.add_argument("--lineup-cutoff-minutes", type=int, default=15)
    p.add_argument("--now-utc", help="Reloj UTC explícito para pruebas offline")
    p.add_argument("--no-reevaluate", action="store_true")
    p = sub.add_parser("refresh-odds-offline", help="Refresco simulado/offline de cuotas desde TXT Bwin fechado sin scraper")
    p.add_argument("--event-id", required=True)
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--now-utc", help="Reloj UTC explícito para pruebas offline")
    p.add_argument("--max-odds-age-minutes", type=int, default=60)
    p.add_argument("--no-reevaluate", action="store_true")
    p = sub.add_parser("resolve-lineups", help="Audita XI confirmados frente a corte configurable y reconsidera siguiente candidato del ranking si falta")
    p.add_argument("--event-id", required=True)
    p.add_argument("--ranking", type=Path, help="Archivo ranking/selection_decision/preselection JSON para continuación automática")
    p.add_argument("--lineup-cutoff-minutes", type=int, default=15)
    p.add_argument("--min-lead-minutes", type=int, default=45)
    p.add_argument("--user-manual-absent", action="store_true", help="Marcar ausencia de intervención manual del usuario")
    p.add_argument("--auto-prepare-next", action="store_true", help="Prepara automáticamente el expediente del siguiente candidato elegible y ejecuta Shadow seguro")
    p.add_argument("--next-context-json", type=Path)
    p.add_argument("--next-context-txt", type=Path)
    p.add_argument("--next-odds-txt", type=Path)
    p.add_argument("--next-manual-lineups", type=Path)
    p.add_argument("--next-report", type=Path)
    p.add_argument("--now-utc", help="Reloj UTC explícito para pruebas offline")
    p = sub.add_parser("import-quick-context")
    p.add_argument("--event-id", required=True, help="Bwin Event ID canónico o Fixture ID para candidatos RESEARCH_PENDING")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--preselection", type=Path, help="preselection.json opcional si aún no hay Event ID Bwin")
    p = sub.add_parser("validate-hypotheses"); p.add_argument("--event-id", required=True)
    p.add_argument("--packet", type=Path, required=True); p.add_argument("--sealed", type=Path, required=True)
    p.add_argument("--ranking", type=Path)
    p = sub.add_parser("reconcile", help="Reconcilia manifest.json y lineup_audit_<E>.json desde snapshots verificados sin re-adquirir")
    p.add_argument("--event-id", required=True)
    p.add_argument("--lineup-cutoff-minutes", type=int, default=15)
    p.add_argument("--now-utc", help="Reloj UTC explícito para auditoría/reproducción offline")
    for name in ("acquire-context", "acquire-lineups", "refresh-context", "refresh-odds", "finalize", "close"):
        p = sub.add_parser(name); p.add_argument("--selected", type=Path, required=True)
        p.add_argument("--event-id", help="Opcional: valida coincidencia con selected_match.json sin reenviarlo a prematch_single.py")
        p.add_argument("args", nargs=argparse.REMAINDER)
    pi = sub.add_parser("prepare-independent", help="Adopta offline un evento único desde discovery/preselection congelados sin ocupar franjas del orquestador")
    pi.add_argument("--event-id", required=True)
    pi.add_argument("--preselection", type=Path, help="Ruta explícita a preselection.json")
    pi.add_argument("--bwin-discovery", type=Path, help="Ruta explícita a bwin_discovery.json")
    pi.add_argument("--now-utc", help="Reloj UTC explícito para pruebas offline")
    args, unknown = parser.parse_known_args(argv); root = args.root.resolve()
    delegated = {"acquire-context", "acquire-lineups", "refresh-context", "refresh-odds", "finalize", "close"}
    if unknown and args.action not in delegated:
        parser.error("unrecognized arguments: " + " ".join(unknown))
    try:
        if args.action=='prepare-research':
            print(prepare_research(args.selection.resolve(),args.output_dir.resolve()));return 0
        if args.action == 'promote-research':
            print(promote_research(root, args.research_selection, args.discovery.resolve(), args.preflight.resolve(), args.event_id)); return 0
        if args.action == "prepare":
            selected = adopt(root, args.handoff, historical=args.historical,
                             asof=datetime.fromisoformat(args.asof) if args.asof else None)
            print(selected); return 0
        if args.action == "prepare-independent":
            result = prepare_independent_event(
                root,
                event_id=args.event_id,
                preselection_path=args.preselection.resolve() if args.preselection else None,
                bwin_discovery_path=args.bwin_discovery.resolve() if args.bwin_discovery else None,
                now_utc=args.now_utc,
            )
        elif args.action == "status": result = status(root, args.event_id)
        elif args.action == "attach-hypotheses": result = attach_hypotheses(root, args.event_id, args.file.resolve())
        elif args.action == "attach-research": result = attach_research(root, args.event_id, args.file.resolve())
        elif args.action == "attach-manual-lineups":
            result = attach_manual_lineups(
                root,
                args.event_id,
                args.file.resolve(),
                now_utc=args.now_utc,
                reevaluate=not args.no_reevaluate,
            )
        elif args.action == "sync-manual-lineups":
            result = sync_manual_lineups(
                root,
                args.event_id,
                now_utc=args.now_utc,
                lineup_cutoff_minutes=args.lineup_cutoff_minutes,
                reevaluate=not args.no_reevaluate,
            )
        elif args.action == "refresh-odds-offline":
            result = refresh_odds_from_snapshot(
                root,
                args.event_id,
                args.file.resolve(),
                now_utc=args.now_utc,
                max_odds_age_minutes=args.max_odds_age_minutes,
                reevaluate_shadow=not args.no_reevaluate,
            )
        elif args.action == "resolve-lineups":
            result = resolve_lineups_or_fallback(
                root,
                args.event_id,
                ranking_path=args.ranking.resolve() if args.ranking else None,
                lineup_cutoff_minutes=args.lineup_cutoff_minutes,
                min_lead_minutes=args.min_lead_minutes,
                user_manual_absent=args.user_manual_absent,
                now_utc=args.now_utc,
                auto_prepare_next=args.auto_prepare_next,
                next_context_json=args.next_context_json.resolve() if args.next_context_json else None,
                next_context_txt=args.next_context_txt.resolve() if args.next_context_txt else None,
                next_odds_txt=args.next_odds_txt.resolve() if args.next_odds_txt else None,
                next_manual_lineups=args.next_manual_lineups.resolve() if args.next_manual_lineups else None,
                next_report=args.next_report.resolve() if args.next_report else None,
            )
        elif args.action == "import-quick-context":
            result = import_quick_context(root, args.event_id, args.file.resolve(),
                                          args.preselection.resolve() if args.preselection else None)
        elif args.action == "validate-hypotheses":
            result = validate_hypotheses(root, args.event_id, args.packet.resolve(), args.sealed.resolve(),
                                         args.ranking.resolve() if args.ranking else None)
        elif args.action == "reconcile":
            result = reconcile_case_state(
                root,
                args.event_id,
                now_utc=args.now_utc,
                lineup_cutoff_minutes=args.lineup_cutoff_minutes,
            )
        elif args.action == "export-handoff": result = export_handoff(root, args.event_id)
        else:
            sel_path = args.selected.resolve()
            if getattr(args, "event_id", None):
                sel_doc = read(sel_path)
                if str(sel_doc.get("bwin_event_id") or "") != str(args.event_id):
                    raise ValueError(
                        f"DELEGATED_EVENT_ID_MISMATCH:expected={args.event_id},selected={sel_doc.get('bwin_event_id')}"
                    )
            mapped = {"acquire-context": "retry-context", "acquire-lineups": "acquire-lineups",
                      "refresh-context": "refresh-context", "refresh-odds": "refresh-odds",
                      "finalize": "finish", "close": "close"}[args.action]
            forwarded = [*unknown, *args.args]
            return delegate(root, sel_path, mapped, forwarded,
                            confirm_external=mapped in {"retry-context", "acquire-lineups", "refresh-odds", "close"})
        print(json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else result)
        return 0
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"BLOQUEADO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
