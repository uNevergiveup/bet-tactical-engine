#!/usr/bin/env python3
"""Auditoría retrospectiva, aislada y sin reinterpretar señales como picks.

El programa no modifica artefactos PREMATCH. Solo acepta estadísticas de partidos
finalizados y conserva la evidencia descargada en un directorio propio.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Carga la configuración local de credenciales del mismo modo que el pipeline;
# la clave nunca se serializa ni se imprime.
from config import constants as _project_config  # noqa: F401
from prematch_depth_collector import ApiClient, BudgetExhausted

MADRID = ZoneInfo("Europe/Madrid")
FINAL_STATUSES = {"FT", "AET", "PEN"}
SUPPORTED = "SUPPORTED_DESCRIPTIVE_SIGNAL"
METRIC_NAMES = {
    "goals": None,
    "corners": "Corner Kicks",
    "shots": "Total Shots",
    "on_target": "Shots on Goal",
    "fouls": "Fouls",
    "yellow": "Yellow Cards",
    "offsides": "Offsides",
    "xg": "expected_goals",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_json(path: Path, value: Any) -> str:
    blob = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def in_window(value: str, audit_date: str, start: str, end: str) -> bool:
    dt = parse_dt(value).astimezone(MADRID)
    lo = datetime.combine(datetime.fromisoformat(audit_date).date(), time.fromisoformat(start), MADRID)
    hi = datetime.combine(datetime.fromisoformat(audit_date).date(), time.fromisoformat(end), MADRID)
    return lo <= dt <= hi


def source_fixture(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fixture_id": int(row["fixture_id"]),
        "kickoff_madrid": row["kickoff_madrid"],
        "kickoff_utc": row.get("kickoff_utc"),
        "home": row["home"],
        "home_id": row["home_id"],
        "away": row["away"],
        "away_id": row["away_id"],
        "league": row.get("league"),
        "league_id": row.get("league_id"),
        "season": row.get("season"),
    }


def validate_result(source: dict[str, Any], raw: dict[str, Any]) -> None:
    fixture = raw.get("fixture") or {}
    teams = raw.get("teams") or {}
    if fixture.get("id") != source["fixture_id"]:
        raise ValueError("RESULT_FIXTURE_ID_CONFLICT")
    if (teams.get("home") or {}).get("id") != source["home_id"]:
        raise ValueError("RESULT_HOME_ID_CONFLICT")
    if (teams.get("away") or {}).get("id") != source["away_id"]:
        raise ValueError("RESULT_AWAY_ID_CONFLICT")
    kickoff = parse_dt(fixture["date"])
    expected = parse_dt(source["kickoff_madrid"])
    if abs((kickoff - expected).total_seconds()) > 60:
        raise ValueError("RESULT_KICKOFF_CONFLICT")


def stat_value(stats: list[dict[str, Any]], label: str) -> float | None:
    wanted = label.casefold().replace(" ", "_")
    for item in stats:
        kind = str(item.get("type", "")).casefold().replace(" ", "_")
        if kind == wanted:
            value = item.get("value")
            if value is None:
                return None
            if isinstance(value, str) and value.endswith("%"):
                value = value[:-1]
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def extract_observed(source: dict[str, Any], result: dict[str, Any], statistics: list[dict[str, Any]]) -> tuple[dict, list[str]]:
    by_id: dict[int, list[dict[str, Any]]] = {}
    for block in statistics:
        tid = (block.get("team") or {}).get("id")
        if isinstance(tid, int):
            by_id[tid] = block.get("statistics") or []
    issues: list[str] = []
    expected = {source["home_id"], source["away_id"]}
    if statistics and set(by_id) != expected:
        issues.append("STATISTICS_TEAM_IDENTITY_CONFLICT")
        by_id = {}
    goals = result.get("goals") or {}
    observed: dict[str, dict[str, float | None]] = {"home": {}, "away": {}}
    for side in ("home", "away"):
        tid = source[f"{side}_id"]
        for metric, label in METRIC_NAMES.items():
            value = goals.get(side) if metric == "goals" else stat_value(by_id.get(tid, []), label or "")
            observed[side][metric] = float(value) if isinstance(value, (int, float)) else None
    return observed, issues


def audit_signal(signal: dict[str, Any], observed: dict[str, dict[str, float | None]], match_status: str) -> dict[str, Any]:
    side = signal["team_side"]
    metric = signal["metric"]
    value = observed.get(side, {}).get(metric)
    if match_status not in FINAL_STATUSES:
        evaluation = "PENDING_MATCH_NOT_FINAL"
    elif value is None:
        evaluation = "NO_EVALUABLE_FINAL_STAT_MISSING"
    else:
        evaluation = "NO_EVALUABLE_NO_PREDECLARED_THRESHOLD"
    own = signal.get("own") or {}
    opp = signal.get("opponent_conceded") or {}
    means = [x for x in (own.get("mean"), opp.get("mean")) if isinstance(x, (int, float))]
    midpoint = round(sum(means) / len(means), 4) if means else None
    return {
        "signal_id": signal.get("signal_id"),
        "classification": "DESCRIPTIVE_SIGNAL_NOT_PREDICTION",
        "status_at_cutoff": signal.get("status"),
        "metric": metric,
        "family": signal.get("family"),
        "team_side": side,
        "team_id": signal.get("team_id"),
        "priority_score": signal.get("priority_score"),
        "sample": {
            "own_n": own.get("n"),
            "opponent_conceded_n": opp.get("n"),
            "own_coverage_pct": own.get("historical_effective_coverage_pct", own.get("coverage_pct")),
            "opponent_coverage_pct": opp.get("historical_effective_coverage_pct", opp.get("coverage_pct")),
            "own_fixture_ids": own.get("fixture_ids") or [],
            "opponent_fixture_ids": opp.get("fixture_ids") or [],
        },
        "historical_means": {"own": own.get("mean"), "opponent_conceded": opp.get("mean")},
        "descriptive_midpoint_not_threshold": midpoint,
        "market": None,
        "threshold": None,
        "observed_value": value,
        "evaluation_status": evaluation,
        "fulfilled": None,
        "limitations": list(signal.get("limitations") or []) + ["NO_RETROFITTED_MARKET_THRESHOLD"],
    }


def render_match_md(report: dict[str, Any]) -> str:
    f = report["fixture"]
    lines = [
        f"# {f['home']} – {f['away']} ({f['fixture_id']})",
        "",
        f"- Kickoff: `{f['kickoff_madrid']}`",
        f"- Estado final consultado: `{report['result_status']}`",
        f"- Evidencia previa: `{report['prematch_evidence_status']}`",
        f"- Señales descriptivas soportadas: **{len(report['signals'])}**",
        f"- Predicciones registradas: **{report['predictions_emitted']}**",
        f"- Picks registrados: **{report['picks_emitted']}**",
        "",
    ]
    if report.get("score"):
        lines.append(f"Resultado: **{report['score']['home']}–{report['score']['away']}**.\n")
    if not report["signals"]:
        lines.append("No consta una señal descriptiva soportada previa al kickoff para este encuentro.\n")
    else:
        lines += ["| Señal | Métrica | Lado | n propio/rival | Observado | Evaluación |", "|---|---:|---|---:|---:|---|"]
        for s in report["signals"]:
            sample = s["sample"]
            lines.append(
                f"| {s['signal_id']} | {s['metric']} | {s['team_side']} | "
                f"{sample['own_n']}/{sample['opponent_conceded_n']} | {s['observed_value']} | {s['evaluation_status']} |"
            )
        lines += ["", "Las medias históricas son contexto descriptivo, no líneas de mercado. No se atribuye acierto, fallo ni rentabilidad sin un umbral y precio registrados antes del inicio.", ""]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixtures_path, preselection_path, tactical_path = map(Path, (args.fixtures, args.preselection, args.tactical))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    base_doc, pre_doc, tactical_doc = map(load_json, (fixtures_path, preselection_path, tactical_path))
    fixtures = {
        int(r["fixture_id"]): source_fixture(r)
        for r in base_doc["fixtures"]
        if in_window(r["kickoff_madrid"], args.date, args.start, args.end)
    }
    pre_ids = {
        int(r["fixture_id"])
        for r in pre_doc["matches"]
        if int(r["fixture_id"]) in fixtures
    }
    pre_rows = {int(r["fixture_id"]): r for r in pre_doc["matches"] if int(r["fixture_id"]) in fixtures}
    predictor_observations = 0
    temporal_violations: list[dict[str, Any]] = []
    for fid, row in pre_rows.items():
        target_kickoff = parse_dt(fixtures[fid]["kickoff_madrid"])
        for side in ("home", "away"):
            for observation in (row.get("statistical_observations") or {}).get(side, []):
                predictor_observations += 1
                historical_id = observation.get("fixture_id")
                historical_kickoff = observation.get("kickoff_utc")
                if historical_id == fid or not historical_kickoff or parse_dt(historical_kickoff) >= target_kickoff:
                    temporal_violations.append({
                        "target_fixture_id": fid,
                        "historical_fixture_id": historical_id,
                        "historical_kickoff": historical_kickoff,
                    })
    if temporal_violations:
        raise ValueError(f"PREDICTOR_TEMPORAL_LEAKAGE:{temporal_violations[:5]}")
    tactical = {int(k): v for k, v in tactical_doc["fixtures"].items() if int(k) in fixtures}
    ranking = {
        int(row["fixture_id"]): {"position": pos, **row}
        for pos, row in enumerate(tactical_doc.get("ranking") or [], start=1)
        if int(row["fixture_id"]) in fixtures
    }
    asof = parse_dt(tactical_doc["asof_madrid"])
    bad_cutoff = [fid for fid, f in fixtures.items() if asof >= parse_dt(f["kickoff_madrid"])]
    if bad_cutoff:
        raise ValueError(f"TACTICAL_SNAPSHOT_NOT_PREMATCH:{bad_cutoff[:5]}")

    raw_dir = output / "raw"
    cached_date = raw_dir / "fixtures_date.json"
    client = None
    if args.online:
        client = ApiClient(Path(args.cache_dir), max_calls=args.max_api_calls)
        prior_hits = client.stat.hits
        date_rows = client.get("fixtures", date=args.date, timezone="Europe/Madrid")
        dump_json(cached_date, {
            "materialized_at_utc": datetime.now(timezone.utc).isoformat(),
            "retrieval_mode": "CACHE_REUSED" if client.stat.hits > prior_hits else "NETWORK",
            "response": date_rows,
        })
    elif cached_date.exists():
        date_rows = load_json(cached_date).get("response") or []
    else:
        date_rows = []
    result_by_id = {int(r["fixture"]["id"]): r for r in date_rows if (r.get("fixture") or {}).get("id")}

    fixture_reports: list[dict[str, Any]] = []
    aggregate: dict[str, Counter] = defaultdict(Counter)
    api_issues: list[dict[str, Any]] = []
    for fid, source in sorted(fixtures.items(), key=lambda item: (item[1]["kickoff_madrid"], item[0])):
        trow = tactical.get(fid) or {}
        supported = [s for s in trow.get("signals", []) if s.get("status") == SUPPORTED]
        raw_result = result_by_id.get(fid)
        result_status = "RESULT_NOT_RETRIEVED"
        observed = {"home": {}, "away": {}}
        score = None
        stats: list[dict[str, Any]] = []
        issues: list[str] = []
        if raw_result:
            try:
                validate_result(source, raw_result)
                result_status = ((raw_result.get("fixture") or {}).get("status") or {}).get("short") or "UNKNOWN"
                score = raw_result.get("goals")
            except ValueError as exc:
                issues.append(str(exc))
                result_status = "IDENTITY_CONFLICT"
        stats_file = raw_dir / f"statistics_{fid}.json"
        if supported and result_status in FINAL_STATUSES:
            if args.online and client:
                try:
                    prior_hits = client.stat.hits
                    stats = client.get("fixtures/statistics", fixture=fid)
                    dump_json(stats_file, {
                        "materialized_at_utc": datetime.now(timezone.utc).isoformat(),
                        "retrieval_mode": "CACHE_REUSED" if client.stat.hits > prior_hits else "NETWORK",
                        "response": stats,
                    })
                except (BudgetExhausted, RuntimeError) as exc:
                    api_issues.append({"fixture_id": fid, "endpoint": "fixtures/statistics", "error": str(exc)})
            elif stats_file.exists():
                stats = load_json(stats_file).get("response") or []
        if raw_result and result_status in FINAL_STATUSES:
            observed, stat_issues = extract_observed(source, raw_result, stats)
            issues.extend(stat_issues)
        audited = [audit_signal(s, observed, result_status) for s in supported]
        for item in audited:
            aggregate[item["metric"]][item["evaluation_status"]] += 1
            aggregate[item["metric"]]["signals"] += 1
            if item["observed_value"] is not None:
                aggregate[item["metric"]]["observed_available"] += 1
        evidence = "PREMATCH_RECORDED" if fid in pre_ids else "DISCOVERED_NOT_STATISTICALLY_EVALUATED"
        report = {
            "fixture": source,
            "prematch_evidence_status": evidence,
            "tactical_snapshot_asof_madrid": tactical_doc["asof_madrid"],
            "ranking_at_cutoff": ranking.get(fid),
            "result_status": result_status,
            "score": score,
            "observed": observed,
            "signals": audited,
            "predictions_emitted": 0,
            "picks_emitted": 0,
            "odds_or_profit_evaluated": False,
            "issues": issues,
        }
        fixture_reports.append(report)
        dump_json(output / "matches" / f"{fid}.json", report)
        md_path = output / "matches" / f"{fid}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_match_md(report), encoding="utf-8")

    status_counts = Counter(r["result_status"] for r in fixture_reports)
    evidence_counts = Counter(r["prematch_evidence_status"] for r in fixture_reports)
    aggregate_rows = []
    for metric in sorted(aggregate):
        row = {"metric": metric, **aggregate[metric]}
        row.update({"fulfilled": 0, "failed": 0})
        aggregate_rows.append(row)
    csv_path = output / "aggregate_by_metric.csv"
    fieldnames = ["metric", "signals", "observed_available", "fulfilled", "failed",
                  "NO_EVALUABLE_NO_PREDECLARED_THRESHOLD", "NO_EVALUABLE_FINAL_STAT_MISSING", "PENDING_MATCH_NOT_FINAL"]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in aggregate_rows:
            writer.writerow(row)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {"path": str(p.resolve()), "sha256": sha256(p)}
            for p in (fixtures_path, preselection_path, tactical_path)
        ],
        "fresh_results": {"path": str(cached_date.resolve()), "sha256": sha256(cached_date)} if cached_date.exists() else None,
        "final_statistics": [
            {"path": str(p.resolve()), "sha256": sha256(p)}
            for p in sorted(raw_dir.glob("statistics_*.json"))
        ],
        "api_cache_entries": [
            {"path": str(p.resolve()), "sha256": sha256(p)}
            for p in sorted(Path(args.cache_dir).rglob("*.json"))
        ],
        "temporal_validation": {
            "predictor_observations_checked": predictor_observations,
            "target_or_post_kickoff_violations": len(temporal_violations),
        },
        "methodology": {
            "temporal_rule": "All predictor fixtures and the tactical snapshot must predate the evaluated kickoff.",
            "result_rule": "Detailed statistics are acquired only after an API final status.",
            "classification_rule": "No threshold is fitted after the result; descriptive signals remain non-predictions.",
            "retrospective_reconstruction": "Not used when a pre-kickoff snapshot exists; unevaluated fixtures remain unevaluated.",
        },
    }
    dump_json(output / "SOURCE_MANIFEST.json", manifest)
    api_usage = {
        "online": bool(args.online),
        "max_api_calls": args.max_api_calls,
        "new_calls": client.stat.calls if client else 0,
        "cache_hits": client.stat.hits if client else 0,
        "issues": api_issues,
    }
    dump_json(output / "api_usage.json", api_usage)
    summary = {
        "schema_version": "1.0-retrospective-descriptive-audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"date": args.date, "start": args.start, "end": args.end, "timezone": "Europe/Madrid", "inclusive_end": True},
        "counts": {
            "fixtures_discovered": len(fixtures),
            "fixtures_statistically_evaluated_prematch": len(pre_ids),
            "fixtures_not_statistically_evaluated": len(fixtures) - len(pre_ids),
            "fixtures_with_supported_descriptive_signals": sum(bool(r["signals"]) for r in fixture_reports),
            "supported_descriptive_signals": sum(len(r["signals"]) for r in fixture_reports),
            "retrospective_reconstructed": 0,
            "predictions_emitted": 0,
            "picks_emitted": 0,
            "result_statuses": dict(status_counts),
            "evidence_statuses": dict(evidence_counts),
            "predictor_observations_temporally_checked": predictor_observations,
            "predictor_temporal_violations": len(temporal_violations),
        },
        "aggregate_by_metric": aggregate_rows,
        "aggregate_by_pattern_type": [
            {
                "pattern_type": "SUPPORTED_DESCRIPTIVE_SIGNAL",
                "signals": sum(len(r["signals"]) for r in fixture_reports),
                "fulfilled": 0,
                "failed": 0,
                "non_evaluable": sum(len(r["signals"]) for r in fixture_reports),
            }
        ],
        "aggregate_by_market": [
            {
                "market": None,
                "status": "NO_PREDECLARED_MARKET_OR_THRESHOLD",
                "signals": sum(len(r["signals"]) for r in fixture_reports),
                "fulfilled": 0,
                "failed": 0,
                "non_evaluable": sum(len(r["signals"]) for r in fixture_reports),
            }
        ],
        "fixtures": fixture_reports,
        "api_usage": api_usage,
        "limitations": [
            "SUPPORTED_DESCRIPTIVE_SIGNAL did not contain a predeclared market or threshold.",
            "Fulfilled/failed is therefore intentionally null; no prices or returns are reconstructed.",
            "Fixtures not final at collection time remain pending and no live statistics are consumed.",
        ],
    }
    dump_json(output / "audit.json", summary)
    md = [
        "# Auditoría retrospectiva PREMATCH — 7 de noviembre de 2026",
        "",
        f"Ventana: **{args.start}–{args.end} Europe/Madrid** (extremo final incluido).",
        "",
        "## Cobertura",
        "",
        f"- Descubiertos: **{len(fixtures)}**",
        f"- Evaluados estadísticamente antes del kickoff: **{len(pre_ids)}**",
        f"- Con señal descriptiva soportada: **{sum(bool(r['signals']) for r in fixture_reports)}**",
        f"- Señales descriptivas: **{sum(len(r['signals']) for r in fixture_reports)}**",
        "- Predicciones emitidas: **0**",
        "- Picks emitidos: **0**",
        "",
        "## Resultado metodológico",
        "",
        "Las señales registradas eran hipótesis descriptivas sin mercado ni umbral predeclarado. Por ello no se han convertido a posteriori en aciertos o fallos. Se conserva el valor final observado y su cobertura, pero `fulfilled` permanece nulo y no se calcula rentabilidad.",
        "",
        "## Agregado por métrica",
        "",
        "| Métrica | Señales | Observado disponible | Cumplidas | Fallidas | Sin umbral | Métrica final ausente | Partido pendiente |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate_rows:
        md.append(
            f"| {row['metric']} | {row.get('signals', 0)} | {row.get('observed_available', 0)} | 0 | 0 | "
            f"{row.get('NO_EVALUABLE_NO_PREDECLARED_THRESHOLD', 0)} | "
            f"{row.get('NO_EVALUABLE_FINAL_STAT_MISSING', 0)} | {row.get('PENDING_MATCH_NOT_FINAL', 0)} |"
        )
    md += ["", "## Estados de resultados", "", *[f"- `{k}`: {v}" for k, v in sorted(status_counts.items())], "", "Los informes individuales están en `matches/`. Los partidos pendientes no incorporan datos LIVE.", ""]
    (output / "audit.md").write_text("\n".join(md), encoding="utf-8")
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--fixtures", required=True)
    p.add_argument("--preselection", required=True)
    p.add_argument("--tactical", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--date", default="2026-11-07")
    p.add_argument("--start", default="15:00")
    p.add_argument("--end", default="21:00")
    p.add_argument("--online", action="store_true")
    p.add_argument("--max-api-calls", type=int, default=240)
    return p


if __name__ == "__main__":
    result = run(parser().parse_args())
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
