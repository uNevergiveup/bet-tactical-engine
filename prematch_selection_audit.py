#!/usr/bin/env python3
"""Auditoría offline del embudo PREMATCH y catálogo descriptivo de patrones.

Lee exclusivamente artefactos ya existentes bajo un ``--run-dir`` concreto.
No sigue punteros ``latest``, no consume red, no modifica fuentes y no produce
picks, probabilidades, EV, cuotas justas ni stakes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SCHEMA = "1.0-selection-audit"
CATALOG_SCHEMA = "1.0-pattern-catalog"
MADRID = ZoneInfo("Europe/Madrid")
METRICS = ("goals", "corners", "shots", "on_target", "fouls", "yellow", "offsides", "xg")
FAMILY = {
    "goals": "goals", "corners": "corners", "shots": "shots",
    "on_target": "shots", "fouls": "fouls", "yellow": "cards",
    "offsides": "offsides", "xg": "goals",
}
V2_NOT_IMPLEMENTED = (
    "PLAYER_METRICS_NOT_IMPLEMENTED_REQUIRES_PLAYER_ID_MINUTES_LINEUP_AND_STAT_ENDPOINTS",
    "PERIOD_METRICS_NOT_IMPLEMENTED_REQUIRES_PERIOD_SPLITS_AND_MARKET_SETTLEMENT_RULES",
    "UNOBSERVED_TACTICS_NOT_SUPPORTED_REQUIRES_DATED_IDENTIFIED_PRIMARY_EVIDENCE",
)


class AuditError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AuditError(f"La raíz JSON no es un objeto: {path}")
    return value


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def fixture_id(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    value = row.get("fixture_id")
    if isinstance(value, bool) or value is None:
        return None
    return str(value)


def rows(value: Any, *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        candidate = value.get(key) if isinstance(value, dict) else None
        if isinstance(candidate, list):
            return [x for x in candidate if isinstance(x, dict)]
    return []


def fixture_map(value: Any, *keys: str) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict) and "fixtures" in keys and isinstance(value.get("fixtures"), dict):
        return {str(k): v for k, v in value["fixtures"].items() if isinstance(v, dict)}
    result: dict[str, dict[str, Any]] = {}
    for row in rows(value, *keys):
        fid = fixture_id(row)
        if fid is not None and fid not in result:
            result[fid] = row
    return result


def duplicate_ids(value: Any, *keys: str) -> list[str]:
    ids = [fixture_id(x) for x in rows(value, *keys)]
    count = Counter(x for x in ids if x is not None)
    return sorted(x for x, n in count.items() if n > 1)


def norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def identity_conflicts(canonical: dict[str, dict[str, Any]], artifact: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts = []
    for fid, row in artifact.items():
        base = canonical.get(fid)
        if not base:
            continue
        fields = []
        for key in ("home", "away"):
            if row.get(key) is not None and base.get(key) is not None and norm(row[key]) != norm(base[key]):
                fields.append(key)
        a, b = parse_dt(row.get("kickoff_madrid")), parse_dt(base.get("kickoff_madrid"))
        if a and b and abs((a - b).total_seconds()) > 180:
            fields.append("kickoff_madrid")
        for key in ("home_id", "away_id", "league_id", "season"):
            if row.get(key) is not None and base.get(key) is not None and str(row[key]) != str(base[key]):
                fields.append(key)
        if fields:
            conflicts.append({"fixture_id": fid, "fields": sorted(set(fields))})
    return conflicts


def resolve_reference(raw: Any, run_dir: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or raw.strip().casefold() == "latest":
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        direct = (run_dir / path).resolve()
        root_relative = (run_dir.parent.parent / path).resolve()
        path = direct if direct.exists() else root_relative
    return path.resolve()


def temporal_status(document: dict[str, Any], asof: datetime | None) -> str:
    if asof is None:
        return "NO_VERIFICABLE"
    stamps = [document.get(k) for k in (
        "generated_at_utc", "generated_madrid", "asof_madrid", "evaluation_as_of_utc"
    )]
    parsed = [parse_dt(x) for x in stamps]
    parsed = [x for x in parsed if x is not None]
    if not parsed:
        return "NO_VERIFICABLE"
    return "POST_ASOF" if any(x > asof for x in parsed) else "VERIFIED_NOT_AFTER_ASOF"


def find_scan_run(trial_dir: Path) -> Path | None:
    base_scanner = trial_dir / "opportunity" / "opportunity_scan.json"
    if base_scanner.is_file():
        return trial_dir
    daily = trial_dir / "daily_runs"
    candidates = sorted(
        p for p in daily.iterdir()
        if p.is_dir() and (p / "opportunity" / "opportunity_scan.json").is_file()
    ) if daily.is_dir() else []
    return candidates[-1] if candidates else None


def load_artifacts(run_dir: Path) -> tuple[dict[str, dict[str, Any]], Path | None]:
    trial = run_dir / "prospective_trial"
    scan_run = find_scan_run(trial)
    paths = {
        "fixtures": run_dir / "fixtures.json",
        "fixture_audit": run_dir / "fixture_audit.json",
        "major": run_dir / "fixtures_major.json",
        "league_review": run_dir / "league_filter_review.json",
        "trial_scope": trial / "fixtures_major_test.json",
        "preselection": trial / "enriched" / "preselection.json",
        "pattern_engine": trial / "patterns" / "pattern_engine.json",
        "tactical": trial / "tactical" / "tactical_signal_engine.json",
        "ledger": trial / "candidate_ledger.json",
        "summary": trial / "prospective_test_summary.json",
        "budget": trial / "budget_assignment.json",
        "bwin": trial / "bwin" / "bwin_discovery.json",
        "handoff": trial / "handoff_prematch.json",
    }
    if scan_run:
        paths.update({
            "scanner": scan_run / "opportunity" / "opportunity_scan.json",
            "scan_tactical": scan_run / "tactical" / "tactical_signal_engine.json",
            "scan_ledger": scan_run / "candidate_ledger.json",
            "scan_handoff": scan_run / "handoff_prematch.json",
        })
    result = {}
    for name, path in paths.items():
        if path.is_file():
            result[name] = {"path": str(path.resolve()), "sha256": sha256(path), "data": read_json(path)}
    # Some discovery versions embed fixture_audit instead of writing a separate file.
    embedded_audit = result.get("fixtures", {}).get("data", {}).get("fixture_audit")
    if "fixture_audit" not in result and isinstance(embedded_audit, (dict, list)):
        result["fixture_audit"] = {
            "path": result["fixtures"]["path"] + "#fixture_audit",
            "sha256": result["fixtures"]["sha256"],
            "data": embedded_audit if isinstance(embedded_audit, dict) else {"records": embedded_audit},
        }
    # Once a concrete scanner run is selected, its tactical artifact is the
    # canonical one for the catalog. Preserve the preparation copy separately.
    if "scan_tactical" in result:
        if "tactical" in result:
            result["prepare_tactical"] = result["tactical"]
        result["tactical"] = result["scan_tactical"]
    return result, scan_run


def data(artifacts: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    return artifacts.get(name, {}).get("data", {})


def verify_hash_links(artifacts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    checks = []
    relations = (
        ("major", "source_sha256", "fixtures"),
        ("trial_scope", "source_sha256", "fixtures"),
        ("preselection", "source_filter_sha256", "trial_scope"),
        ("bwin", "source_input_sha256", "preselection"),
    )
    for source, field, target in relations:
        if source not in artifacts:
            continue
        expected = data(artifacts, source).get(field)
        actual = artifacts.get(target, {}).get("sha256")
        status = "NO_VERIFICABLE" if not expected or not actual else "MATCH" if expected == actual else "MISMATCH"
        checks.append({"artifact": source, "field": field, "target": target, "expected": expected,
                       "actual": actual, "status": status})
    tactical = data(artifacts, "tactical")
    tactical_sources = tactical.get("sources")
    if isinstance(tactical_sources, dict):
        for field, target in (("preselection_sha256", "preselection"), ("pattern_sha256", "pattern_engine")):
            expected = tactical_sources.get(field)
            actual = artifacts.get(target, {}).get("sha256")
            checks.append({"artifact": "tactical", "field": f"sources.{field}", "target": target,
                           "expected": expected, "actual": actual,
                           "status": "NO_VERIFICABLE" if not expected or not actual else "MATCH" if expected == actual else "MISMATCH"})
    elif isinstance(tactical_sources, list):
        for source in tactical_sources:
            if not isinstance(source, dict):
                continue
            raw = source.get("path")
            expected = source.get("sha256")
            path = Path(raw) if isinstance(raw, str) else None
            actual = sha256(path) if path and path.is_file() else None
            checks.append({"artifact": "tactical", "field": "sources[].sha256", "target": raw,
                           "expected": expected, "actual": actual,
                           "status": "NO_VERIFICABLE" if not expected or not actual else "MATCH" if expected == actual else "MISMATCH"})
    return checks


def set_from(doc: dict[str, Any], key: str) -> set[str]:
    values = doc.get(key)
    if not isinstance(values, list):
        return set()
    return {str(x.get("fixture_id") if isinstance(x, dict) else x) for x in values}


def scanner_fixture_sets(scanner: dict[str, Any]) -> tuple[set[str], set[str]]:
    audits = rows(scanner, "audits")
    captured = {fixture_id(x) for x in audits if x.get("captured", 0) or x.get("snapshot")}
    verified = {fixture_id(x) for x in audits if str(x.get("status", "")).upper() == "VERIFIED"}
    return {x for x in captured if x}, {x for x in verified if x}


def overall_classification(row: dict[str, Any]) -> str:
    state = str(row.get("state") or "").upper()
    reason = str(row.get("reason") or "").upper()
    joined = state + " " + reason
    if "DIFERIDO" in joined or "BUDGET" in joined:
        return "DIFERIDO_POR_PRESUPUESTO"
    if "SELECCION" in joined or "FINALIST" in joined or "HANDOFF" in joined:
        return "SELECCIONADO"
    if "SIN_BWIN" in joined or "NOT_FOUND" in joined:
        return "SIN_BWIN"
    if "SIN_COBERTURA" in joined or "API_ERROR" in joined or "NO_STAT" in joined:
        return "SIN_DATOS_API"
    if "ERROR" in joined or "FAIL" in joined:
        return "ERROR_TECNICO"
    if "EXCLUID" in joined or "FILTRADO" in joined or "IDENTIDAD_INCORRECTA" in joined:
        return "EXCLUIDO"
    if state:
        return "PENDIENTE"
    return "NO_VERIFICABLE"


def stage(name: str, ids: set[str] | None, denominator: set[str] | None, note: str = "") -> dict[str, Any]:
    if ids is None:
        return {"stage": name, "status": "NO_VERIFICABLE", "numerator": None,
                "denominator": len(denominator) if denominator is not None else None, "fixture_ids": [], "note": note}
    return {"stage": name, "status": "OBSERVED", "numerator": len(ids),
            "denominator": len(denominator) if denominator is not None else None,
            "fixture_ids": sorted(ids), "note": note}


def build_selection_audit(run_dir: Path, artifacts: dict[str, dict[str, Any]], scan_run: Path | None) -> dict[str, Any]:
    fixtures_doc = data(artifacts, "fixtures")
    discovered_map = fixture_map(fixtures_doc, "fixtures")
    discovered = set(discovered_map)
    ns_window = {fid for fid, x in discovered_map.items() if x.get("status") == "NS"}
    major_doc = data(artifacts, "major")
    eligible_map = fixture_map(major_doc, "fixtures")
    review_map = fixture_map(major_doc, "review")
    excluded_map = fixture_map(major_doc, "excluded")
    trial_map = fixture_map(data(artifacts, "trial_scope"), "fixtures")
    pre_map = fixture_map(data(artifacts, "preselection"), "matches")
    details = {fid for fid, x in pre_map.items() if x.get("details_checked")}
    pattern_doc = data(artifacts, "pattern_engine")
    pattern_map = fixture_map(pattern_doc, "fixtures")
    patterns_with_evidence = {
        str(x["fixture_id"]) for x in rows(pattern_doc, "patterns")
        if x.get("status") not in ("SIN_DATOS", "INSUFFICIENT_DATA")
    }
    tactical_map = fixture_map(data(artifacts, "tactical"), "fixtures")
    bwin_map = fixture_map(data(artifacts, "bwin"), "matches")
    bwin_found = {fid for fid, x in bwin_map.items() if x.get("bwin_event_id")}
    scanner = data(artifacts, "scanner")
    captured, scanner_verified = scanner_fixture_sets(scanner)
    handoff = data(artifacts, "scan_handoff") or data(artifacts, "handoff")
    finalists = set()
    for key in ("ready", "matches", "fixtures", "selected", "candidates"):
        finalists |= set_from(handoff, key)
    selected_id = handoff.get("selected_fixture_id")
    if selected_id is not None:
        finalists.add(str(selected_id))
    if handoff.get("fixture_id") is not None and str(handoff.get("status", "")).upper() in (
        "READY_FOR_MANUAL_RESEARCH", "SELECTED", "READY"
    ):
        finalists.add(str(handoff["fixture_id"]))
    ledger_doc = data(artifacts, "scan_ledger") or data(artifacts, "ledger")
    ledger_map = fixture_map(ledger_doc, "fixtures")
    summary = data(artifacts, "summary")
    eval_deferred = set_from(summary, "evaluation_deferred_ids")
    discovery_deferred = set_from(summary, "discovery_deferred_ids")
    budget = data(artifacts, "budget")
    evaluation_ids = set_from(budget, "evaluation_fixture_ids")
    bwin_order_ids = set_from(budget, "bwin_order_fixture_ids")
    all_ids = set().union(discovered, eligible_map, review_map, excluded_map, trial_map, pre_map,
                          pattern_map, tactical_map, bwin_map, ledger_map, captured, scanner_verified, finalists)
    fixture_rows = []
    for fid in sorted(all_ids, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x)):
        sources = [m.get(fid) for m in (discovered_map, eligible_map, review_map, excluded_map, trial_map,
                                       pre_map, tactical_map, bwin_map, ledger_map) if m.get(fid)]
        base = next(iter(sources), {})
        ledger = ledger_map.get(fid, {})
        stages = {
            "discovered": fid in discovered, "ns_in_window": fid in ns_window,
            "competition_eligible": fid in eligible_map, "competition_review": fid in review_map,
            "competition_excluded": fid in excluded_map, "within_scope_horizon": fid in trial_map,
            "basic_evaluation": fid in pre_map, "details_statistics": fid in details,
            "pattern_engine": fid in pattern_map, "pattern_evidence": fid in patterns_with_evidence,
            "tactical_engine": fid in tactical_map, "bwin_discovered": fid in bwin_found,
            "snapshot_captured": fid in captured, "scanner_verified": fid in scanner_verified,
            "finalist_or_handoff": fid in finalists,
        }
        classification = ("DIFERIDO_POR_PRESUPUESTO" if fid in eval_deferred | discovery_deferred else
                          "SELECCIONADO" if fid in finalists else overall_classification(ledger))
        fixture_rows.append({
            "fixture_id": base.get("fixture_id", int(fid) if fid.isdigit() else fid),
            "home": base.get("home"), "away": base.get("away"), "kickoff_madrid": base.get("kickoff_madrid"),
            "league": base.get("league"), "league_id": base.get("league_id"), "gender": base.get("gender"),
            "stages": stages, "original_state": ledger.get("state"), "original_stage": ledger.get("stage"),
            "original_reason": ledger.get("reason"), "history": ledger.get("history", []),
            "classification": classification,
        })
    stages = [
        stage("discovered", discovered if "fixtures" in artifacts else None, None),
        stage("ns_in_operational_window", ns_window if "fixtures" in artifacts else None, discovered),
        stage("competition_eligible", set(eligible_map) if "major" in artifacts else None, ns_window),
        stage("competition_review", set(review_map) if "major" in artifacts else None, ns_window,
              "REVIEW se conserva; no se trata como evaluado ni excluido."),
        stage("within_scope_horizon", set(trial_map) if "trial_scope" in artifacts else None, set(eligible_map)),
        stage("basic_evaluation", set(pre_map) if "preselection" in artifacts else None, set(trial_map)),
        stage("statistics_details", details if "preselection" in artifacts else None, set(pre_map)),
        stage("patterns_with_evidence", patterns_with_evidence if "pattern_engine" in artifacts else None, set(pre_map)),
        stage("bwin_discovery", bwin_found if "bwin" in artifacts else None, set(pre_map)),
        stage("snapshot_capture", captured if "scanner" in artifacts else None, bwin_found),
        stage("scanner_verified", scanner_verified if "scanner" in artifacts else None, captured),
        stage("finalists_or_handoff", finalists if handoff else None, scanner_verified),
    ]
    asof = parse_dt(data(artifacts, "tactical").get("asof_madrid")) or parse_dt(scanner.get("asof_madrid"))
    identity = {}
    for name, key in (("major", "fixtures"), ("trial_scope", "fixtures"), ("preselection", "matches"),
                      ("pattern_engine", "fixtures"), ("tactical", "fixtures"), ("bwin", "matches"),
                      ("ledger", "fixtures")):
        doc = data(artifacts, name)
        if doc:
            identity[name] = {
                "duplicate_fixture_ids": duplicate_ids(doc, key),
                "conflicts": identity_conflicts(discovered_map, fixture_map(doc, key)),
            }
    classification_counts = Counter(x["classification"] for x in fixture_rows)
    return {
        "schema_version": SCHEMA, "run_dir": str(run_dir),
        "selected_scan_run": str(scan_run) if scan_run else None,
        "asof_madrid": asof.astimezone(MADRID).isoformat() if asof else None,
        "offline_read_only_sources": True, "actionable_bet": False,
        "artifacts": {
            name: {"path": meta["path"], "sha256": meta["sha256"],
                   "temporal_status": temporal_status(meta["data"], asof)}
            for name, meta in artifacts.items()
        },
        "missing_artifacts": [name for name in (
            "fixtures", "fixture_audit", "major", "trial_scope", "preselection", "pattern_engine", "tactical",
            "ledger", "summary", "budget", "bwin", "scanner", "handoff"
        ) if name not in artifacts and not (name == "handoff" and "scan_handoff" in artifacts)],
        "hash_checks": verify_hash_links(artifacts), "identity_checks": identity,
        "stage_summary": stages,
        "budget": {
            "max_matches_is_batch_size": data(artifacts, "ledger").get("scope", {}).get("max_matches_batch_size"),
            "evaluation_fixture_ids": sorted(evaluation_ids), "bwin_order_fixture_ids": sorted(bwin_order_ids),
            "evaluation_deferred_ids": sorted(eval_deferred), "discovery_deferred_ids": sorted(discovery_deferred),
            "deferred_is_not_excluded": True,
        },
        "classification_counts_exclusive": dict(sorted(classification_counts.items())),
        "fixtures": fixture_rows,
        "limitations": [
            "Las categorías de cada etapa pueden solaparse; no deben sumarse entre etapas.",
            "Las clasificaciones terminales son exclusivas y conservan estado/motivo original.",
            "NO_VERIFICABLE se usa cuando falta el artefacto o el vínculo necesario; no se estima.",
        ],
    }


def market_state(signal: dict[str, Any], deferred: bool = False) -> str:
    if deferred:
        return "DEFERRED"
    if signal.get("markets"):
        return "MARKET_VERIFIED_NO_CALIBRATED_MODEL"
    if signal.get("status") in ("SUPPORTED_DESCRIPTIVE_SIGNAL", "PARTIAL_EVIDENCE",
                                 "UNSTABLE_PATTERN", "CONTRADICTORY_SIGNAL"):
        return "PATTERN_DETECTED_NO_MARKET"
    return "INSUFFICIENT_DATA"


def pattern_lookup(pattern_doc: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result = {}
    for row in rows(pattern_doc, "patterns"):
        fid = fixture_id(row)
        side, metric = row.get("team_side"), row.get("metric")
        if fid and side in ("home", "away") and metric in METRICS:
            result[(fid, side, metric)] = row
    return result


def build_pattern_catalog(audit: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tactical = data(artifacts, "tactical")
    tactical_map = fixture_map(tactical, "fixtures")
    pre_map = fixture_map(data(artifacts, "preselection"), "matches")
    patterns = pattern_lookup(data(artifacts, "pattern_engine"))
    deferred = set(audit.get("budget", {}).get("evaluation_deferred_ids", [])) | set(audit.get("budget", {}).get("discovery_deferred_ids", []))
    output = []
    seen = set()
    for fid, fixture in sorted(tactical_map.items()):
        pre = pre_map.get(fid, {})
        for signal in fixture.get("signals", []):
            if not isinstance(signal, dict):
                continue
            side, metric = signal.get("team_side"), signal.get("metric")
            key = (fid, side, metric)
            if side not in ("home", "away") or metric not in METRICS or key in seen:
                continue
            seen.add(key)
            own = signal.get("own") if isinstance(signal.get("own"), dict) else {}
            opp = signal.get("opponent_conceded") if isinstance(signal.get("opponent_conceded"), dict) else {}
            prior = patterns.get(key, {})
            markets = signal.get("markets") if isinstance(signal.get("markets"), list) else []
            limitations = list(dict.fromkeys([*(signal.get("limitations") or []), *V2_NOT_IMPLEMENTED]))
            output.append({
                "fixture_id": int(fid) if fid.isdigit() else fid,
                "home": fixture.get("home") or pre.get("home"), "away": fixture.get("away") or pre.get("away"),
                "league": pre.get("league"), "gender": pre.get("gender"),
                "kickoff_madrid": fixture.get("kickoff_madrid") or pre.get("kickoff_madrid"),
                "team_side": side, "team_id": signal.get("team_id"), "metric": metric,
                "family": signal.get("family") or FAMILY[metric], "evidence_status": signal.get("status", "INSUFFICIENT_DATA"),
                "source_mode": signal.get("source_mode"), "n_own": own.get("n"),
                "n_opponent_conceded": opp.get("n"),
                "history_n": {"own": own.get("history_n"), "opponent_conceded": opp.get("history_n")},
                "details_n": {"own": own.get("expected_n"), "opponent_conceded": opp.get("expected_n")},
                "history_not_detailed_n": {"own": own.get("history_not_detailed_n"), "opponent_conceded": opp.get("history_not_detailed_n")},
                "mean_own": own.get("mean"), "mean_opponent_conceded": opp.get("mean"),
                "cv": {"own": own.get("cv"), "opponent_conceded": opp.get("cv")},
                "coverage_requested_pct": {"own": own.get("requested_data_coverage_pct"),
                                             "opponent_conceded": opp.get("requested_data_coverage_pct")},
                "coverage_effective_pct": {"own": own.get("historical_effective_coverage_pct"),
                                             "opponent_conceded": opp.get("historical_effective_coverage_pct")},
                "venue": {"own": own.get("venue"), "opponent_conceded": opp.get("venue")},
                "recency": {"own_recent": own.get("recent"), "own_previous": own.get("previous"),
                            "opponent_recent": opp.get("recent"), "opponent_previous": opp.get("previous")},
                "contradictions": signal.get("contradictions") or [],
                "descriptive_priority": signal.get("priority_score"),
                "pattern_engine_status": prior.get("status"), "pattern_engine_flag": prior.get("pattern_flag"),
                "market_status": market_state(signal, fid in deferred),
                "line_status": "VERIFIED_IN_SOURCE_MARKET" if markets else "LINE_NOT_VERIFIED",
                "verified_markets": markets, "limitations": limitations,
                "not_a_pick": True,
            })
    audit_fixtures = audit.get("fixtures", [])
    not_evaluated = []
    for row in audit_fixtures:
        stages = row.get("stages", {})
        if stages.get("competition_review") or (stages.get("competition_eligible") and not stages.get("tactical_engine")):
            not_evaluated.append({
                "fixture_id": row.get("fixture_id"), "home": row.get("home"), "away": row.get("away"),
                "reason": "COMPETITION_REVIEW_NOT_EVALUATED" if stages.get("competition_review") else "TACTICAL_ARTIFACT_UNAVAILABLE",
                "metrics_invented": False,
            })
    status_counts = Counter(x["evidence_status"] for x in output)
    market_counts = Counter(x["market_status"] for x in output)
    return {
        "schema_version": CATALOG_SCHEMA, "asof_madrid": audit.get("asof_madrid"),
        "source_refs": {name: {"path": meta["path"], "sha256": meta["sha256"]}
                        for name, meta in artifacts.items() if name in ("preselection", "pattern_engine", "tactical", "bwin", "scanner")},
        "methodology": {
            "metrics": list(METRICS), "reuse_only": "TACTICAL_SIGNAL_ENGINE_PLUS_PATTERN_ENGINE_LINKAGE",
            "null_is_zero": False, "probability_or_ev": False,
            "ordering": "Solo se comparan prioridades dentro del mismo evidence_status y source_mode.",
            "market_states": ["PATTERN_DETECTED_NO_MARKET", "MARKET_VERIFIED_NO_CALIBRATED_MODEL", "INSUFFICIENT_DATA", "DEFERRED"],
        },
        "counts": {"rows": len(output), "fixtures": len({str(x["fixture_id"]) for x in output}),
                   "evidence_status": dict(status_counts), "market_status": dict(market_counts),
                   "not_evaluated_fixtures": len(not_evaluated)},
        "patterns": output, "fixtures_not_evaluated": not_evaluated,
        "v2_requirements": list(V2_NOT_IMPLEMENTED), "actionable_bet": False,
    }


def esc(value: Any) -> str:
    return str(value if value is not None else "N/D").replace("|", "\\|")


def selection_markdown(audit: dict[str, Any]) -> str:
    lines = ["# Auditoría offline del selector", "",
             f"Run: `{audit['run_dir']}`", "",
             "Auditoría descriptiva y de trazabilidad. No contiene picks, probabilidades, EV ni stake.", "",
             "## Embudo por etapa", "", "| Etapa | Numerador | Denominador | Estado | Nota |", "|---|---:|---:|---|---|"]
    for item in audit["stage_summary"]:
        lines.append(f"| {esc(item['stage'])} | {esc(item['numerator'])} | {esc(item['denominator'])} | {esc(item['status'])} | {esc(item['note'])} |")
    lines += ["", "Los conteos entre etapas pueden solaparse y no se suman como categorías exclusivas.", "",
              "## Clasificación terminal exclusiva", "", "| Clasificación | Fixtures |", "|---|---:|"]
    for name, count in audit["classification_counts_exclusive"].items():
        lines.append(f"| {esc(name)} | {count} |")
    lines += ["", "## Dónde desaparece cada candidato", "",
              "| Fixture | Partido | Competición | Alcance | API/detalles | Patrones | Bwin | Captura/scanner | Estado original | Clasificación |",
              "|---:|---|---|---|---|---|---|---|---|---|"]
    displayed = [row for row in audit["fixtures"]
                 if row.get("stages", {}).get("discovered") or row.get("classification") != "EXCLUIDO"]
    for row in displayed:
        s = row["stages"]
        competition = "ELIGIBLE" if s["competition_eligible"] else "REVIEW" if s["competition_review"] else "EXCLUDED" if s["competition_excluded"] else "N/V"
        api = "DETALLE" if s["details_statistics"] else "BÁSICO" if s["basic_evaluation"] else "NO"
        bwin = "SÍ" if s["bwin_discovered"] else "NO"
        capture = "VERIFIED" if s["scanner_verified"] else "CAPTURADO" if s["snapshot_captured"] else "NO"
        lines.append(f"| {esc(row['fixture_id'])} | {esc(row['home'])}–{esc(row['away'])} | {competition} | {'SÍ' if s['within_scope_horizon'] else 'NO'} | {api} | {'SÍ' if s['pattern_evidence'] else 'NO'} | {bwin} | {capture} | {esc(row['original_state'])}: {esc(row['original_reason'])} | {esc(row['classification'])} |")
    hidden = [row for row in audit["fixtures"] if row not in displayed]
    if hidden:
        lines += ["", f"Se omiten de esta vista **{len(hidden)}** filas ya clasificadas como EXCLUIDO y ajenas al conjunto descubierto en la ventana; permanecen íntegramente en `selection_audit.json`.", "",
                  "| Motivo original de exclusión omitida | Fixtures |", "|---|---:|"]
        grouped = Counter(str(row.get("original_reason") or "NO_VERIFICABLE") for row in hidden)
        for reason, count in sorted(grouped.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {esc(reason)} | {count} |")
    lines += ["", "## Integridad", ""]
    mismatches = [x for x in audit["hash_checks"] if x["status"] == "MISMATCH"]
    conflicts = sum(len(x.get("conflicts", [])) for x in audit["identity_checks"].values())
    duplicates = sum(len(x.get("duplicate_fixture_ids", [])) for x in audit["identity_checks"].values())
    lines += [f"- Hashes con discrepancia: **{len(mismatches)}**.", f"- Conflictos de identidad: **{conflicts}**.",
              f"- IDs duplicados: **{duplicates}**.",
              f"- Artefactos faltantes: {', '.join(audit['missing_artifacts']) or 'ninguno de los esperados'}.",
              f"- Run de scanner elegido explícitamente: `{audit.get('selected_scan_run') or 'NO_VERIFICABLE'}`.", "",
              "`max_matches` se audita como tamaño de lote; los diferidos por presupuesto no se convierten en exclusiones.", ""]
    return "\n".join(lines)


def pattern_markdown(catalog: dict[str, Any]) -> str:
    lines = ["# Catálogo offline de patrones descriptivos", "",
             f"Corte Madrid: `{catalog.get('asof_madrid') or 'NO_VERIFICABLE'}`", "",
             "No es un ranking de apuestas: no hay probabilidad calibrada, EV, cuota justa ni stake.", "",
             "## Vista investigable (máximo 30)", ""]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in catalog["patterns"]:
        groups[(row["evidence_status"], str(row["source_mode"]))].append(row)
    shown = []
    preferred = ("SUPPORTED_DESCRIPTIVE_SIGNAL", "PARTIAL_EVIDENCE", "CONTRADICTORY_SIGNAL", "UNSTABLE_PATTERN")
    for status in preferred:
        for key in sorted(k for k in groups if k[0] == status):
            comparable = sorted(groups[key], key=lambda x: (-(x["descriptive_priority"] or 0), str(x["fixture_id"]), x["team_side"], x["metric"]))
            shown.extend(comparable[:max(0, 30 - len(shown))])
            if len(shown) >= 30:
                break
        if len(shown) >= 30:
            break
    lines += ["| Fixture | Partido | Lado/métrica | Evidencia | n propia/concesión | Media propia/concesión | Cobertura efectiva | Prioridad descriptiva | Mercado |",
              "|---:|---|---|---|---:|---:|---|---:|---|"]
    for row in shown:
        coverage = row["coverage_effective_pct"]
        lines.append(f"| {esc(row['fixture_id'])} | {esc(row['home'])}–{esc(row['away'])} | {row['team_side']} / {row['metric']} | {row['evidence_status']} | {esc(row['n_own'])}/{esc(row['n_opponent_conceded'])} | {esc(row['mean_own'])}/{esc(row['mean_opponent_conceded'])} | {esc(coverage['own'])}%/{esc(coverage['opponent_conceded'])}% | {esc(row['descriptive_priority'])} | {row['market_status']} · {row['line_status']} |")
    if not shown:
        lines.append("| — | No existen hipótesis que cumplan la vista investigable | — | — | — | — | — | — | — |")
    lines += ["", "## Cobertura del catálogo", "",
              f"- Filas preservadas en JSON: **{catalog['counts']['rows']}**.",
              f"- Fixtures evaluados: **{catalog['counts']['fixtures']}**.",
              f"- Fixtures no evaluados conservados: **{catalog['counts']['not_evaluated_fixtures']}**.",
              f"- Estados de evidencia: `{json.dumps(catalog['counts']['evidence_status'], ensure_ascii=False, sort_keys=True)}`.",
              f"- Estados de mercado: `{json.dumps(catalog['counts']['market_status'], ensure_ascii=False, sort_keys=True)}`.", "",
              "Las filas débiles, contradictorias, de baja muestra o sin datos siguen en `pattern_catalog.json`; no se eliminan por fallar calidad. Una línea ausente se marca `LINE_NOT_VERIFIED`.", "",
              "## Límites V1 / requisitos V2", ""]
    for item in catalog["v2_requirements"]:
        lines.append(f"- `{item}`")
    lines += ["", "No se traducen faltas concedidas a faltas recibidas por jugador, ni offsides a altura de línea. No se inventan jugadores, periodos o tácticas no observadas.", ""]
    return "\n".join(lines)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_audit(run_dir: Path, output_dir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], Path]:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise AuditError(f"No existe --run-dir: {run_dir}")
    if run_dir.name.casefold() == "latest" or run_dir.is_symlink():
        raise AuditError("--run-dir debe ser una ejecución concreta; no se aceptan symlinks/punteros latest")
    artifacts, scan_run = load_artifacts(run_dir)
    if not artifacts:
        raise AuditError(f"No se encontraron artefactos PREMATCH reconocibles en {run_dir}")
    audit = build_selection_audit(run_dir, artifacts, scan_run)
    catalog = build_pattern_catalog(audit, artifacts)
    destination = (output_dir.expanduser().resolve() if output_dir else run_dir / "selection_audit")
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "selection_audit.json", audit)
    (destination / "selection_audit.md").write_text(selection_markdown(audit), encoding="utf-8")
    write_json(destination / "pattern_catalog.json", catalog)
    (destination / "pattern_catalog.md").write_text(pattern_markdown(catalog), encoding="utf-8")
    return audit, catalog, destination


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path, help="Carpeta concreta outputs/discovery/YYYY-MM-DD_HHMM_HHMM")
    parser.add_argument("--output-dir", type=Path, help="Por defecto <run-dir>/selection_audit")
    args = parser.parse_args(argv)
    try:
        audit, catalog, destination = run_audit(args.run_dir, args.output_dir)
    except (AuditError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Auditoría: {len(audit['fixtures'])} fixtures | catálogo: {catalog['counts']['rows']} filas")
    print(f"Archivos: {destination / 'selection_audit.json'}, {destination / 'selection_audit.md'}, "
          f"{destination / 'pattern_catalog.json'}, {destination / 'pattern_catalog.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
