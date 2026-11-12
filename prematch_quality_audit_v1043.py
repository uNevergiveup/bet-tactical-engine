#!/usr/bin/env python3
"""PREMATCH v1.0.4.3: auditoría reproducible de calidad; lectura local exclusivamente.

No usa red ni modifica ningún input, JSON de Fase 2 o caché. Este informe NO
verifica por sí solo la exactitud externa de API-Football ni autoriza apuestas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = "prematch-quality-audit-1.0.4.3"
METRICS = {
    "goals_for": (None, "goals_for"),
    "shots_for": ("own", "shots"),
    "on_target_for": ("own", "on_target"),
    "corners_for": ("own", "corners"),
    "xg_for": ("own", "xg"),
    "shots_against": ("conceded", "shots"),
    "on_target_against": ("conceded", "on_target"),
    "corners_against": ("conceded", "corners"),
    "xg_against": ("conceded", "xg"),
}
ADDITIVE_STATS = {
    "Shots on Goal", "Shots off Goal", "Total Shots", "Blocked Shots",
    "Shots insidebox", "Shots outsidebox", "Fouls", "Corner Kicks",
    "Offsides", "Yellow Cards", "Red Cards", "Goalkeeper Saves",
    "Total passes", "Passes accurate",
}


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def value(row: dict, key: str) -> float | None:
    source, field = METRICS[key]
    val = (row.get(source) or {}).get(field) if source else row.get(field)
    if isinstance(val, bool) or not isinstance(val, (float, int)) or not math.isfinite(val):
        return None
    return float(val)


def stat_map(values: Any) -> dict[str, float]:
    out = {}
    if not isinstance(values, list):
        return out
    for entry in values:
        if not isinstance(entry, dict):
            continue
        key, val = entry.get("type"), entry.get("value")
        if (isinstance(key, str) and isinstance(val, (int, float))
                and not isinstance(val, bool) and math.isfinite(val)):
            out[key] = float(val)
    return out


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Archivo necesario no encontrado: {path}")
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError(f"JSON raíz debe ser objeto: {path}")
    return obj


def audit_bundle(folder: Path, *, event_id: str, identity_path: Path | None = None) -> dict:
    """Audita archivos ya recuperados. No consulta API; admite ejecución pospartido.

    El corte y la procedencia de captura son los que declaran los artefactos,
    no una atestación independiente de su hora de adquisición.
    """
    folder = Path(folder)
    names = {
        "enriched": f"contexto_ampliado_{event_id}.json",
        "performance": f"rendimiento_profundo_{event_id}.json",
        "trends": f"tendencias_profundas_{event_id}.json",
        "depth": f"profundidad_historica_{event_id}.json",
        "players": f"jugadores_historicos_{event_id}.json",
    }
    paths = {k: folder / v for k, v in names.items()}
    docs = {k: load(p) for k, p in paths.items()}
    enriched, perf, trends, depth, players = (docs[k] for k in names)
    issues: list[dict] = []

    def issue(severity: str, code: str, **details: Any) -> None:
        issues.append({"severity": severity, "code": code, **details})

    identity = load(identity_path) if identity_path and identity_path.exists() else None
    ids = [d.get("target_fixture_id") for d in (perf, trends, depth, players)]
    target = (enriched.get("fixture") or {}).get("fixture_id")
    if any(fid != target for fid in ids) or (identity and identity.get("api_fixture_id") != target):
        issue("CRITICAL", "TARGET_FIXTURE_MISMATCH", expected=target, observed=ids,
              identity_fixture_id=identity.get("api_fixture_id") if identity else None)
    for src in (perf, trends, depth, players):
        if str(src.get("event_id_bwin")) != event_id:
            issue("CRITICAL", "EVENT_ID_MISMATCH", schema=src.get("schema"),
                  observed=str(src.get("event_id_bwin")), expected=event_id)
    if str((enriched.get("match") or {}).get("event_id")) != event_id:
        issue("CRITICAL", "EVENT_ID_MISMATCH", schema="enriched", expected=event_id)
    if identity and str(identity.get("bwin_event_id")) != event_id:
        issue("CRITICAL", "IDENTITY_EVENT_ID_MISMATCH", expected=event_id)
    if identity is None:
        issue("WARNING", "IDENTITY_FILE_MISSING", note="No se pudo contrastar el vínculo Bwin")

    kickoff = parse_utc((enriched.get("fixture") or {}).get("date"))
    cutoff = parse_utc(perf.get("analysis_cutoff_utc"))
    generated = parse_utc(enriched.get("generated_at"))
    snapshot = parse_utc(perf.get("snapshot_generated_at_utc"))
    if not all((kickoff, cutoff, generated, snapshot)):
        issue("CRITICAL", "INVALID_OR_NAIVE_CUTOFF", fields={
            "kickoff": bool(kickoff), "cutoff": bool(cutoff),
            "enriched_generated_at": bool(generated), "snapshot": bool(snapshot)})
    else:
        if not (snapshot <= cutoff < kickoff and generated < kickoff):
            issue("CRITICAL", "TIME_BOUNDARY_VIOLATION", kickoff_utc=kickoff.isoformat(),
                  cutoff_utc=cutoff.isoformat(), snapshot_utc=snapshot.isoformat(),
                  enriched_generated_at_utc=generated.isoformat())
        if generated > cutoff + timedelta(seconds=5):
            issue("WARNING", "ENRICHED_AFTER_ANALYSIS_CUTOFF", delta_seconds=(generated-cutoff).total_seconds())
    for label, doc in (("trends", trends), ("depth", depth), ("players", players)):
        field = "cutoff_utc" if label == "players" else "analysis_cutoff_utc"
        other = parse_utc(doc.get(field))
        if cutoff and other != cutoff:
            issue("CRITICAL", "INCONSISTENT_ANALYSIS_CUTOFF", source=label,
                  observed=doc.get(field), expected=cutoff.isoformat())
    if identity:
        if parse_utc(identity.get("kickoff_utc")) != kickoff:
            issue("CRITICAL", "IDENTITY_KICKOFF_MISMATCH", identity=identity.get("kickoff_utc"))
        identity_status = identity.get("identity_status")
        if identity_status != "FIXTURE_AND_BWIN_TIME_VERIFIED":
            issue("WARNING", "BWIN_TIME_LINK_NOT_VERIFIED", identity_status=identity_status,
                  consequence="NO_FINISH_WITH_ODDS")
    else:
        identity_status = "UNKNOWN"
    if (enriched.get("match") or {}).get("identity_status") != identity_status and identity:
        issue("WARNING", "ENRICHED_IDENTITY_STATUS_MISMATCH",
              enriched=(enriched.get("match") or {}).get("identity_status"),
              identity=identity_status)

    summary = {}
    all_halves_discrepancies: list[dict] = []
    for side in ("home", "away"):
        performance_team = (perf.get("teams") or {}).get(side) or {}
        trends_team = (trends.get("teams") or {}).get(side) or {}
        depth_team = (depth.get("teams") or {}).get(side) or {}
        players_team = (players.get("teams") or {}).get(side) or {}
        hist = ((enriched.get("context") or {}).get(side + "_history_deep") or {})
        raw = hist.get("detailed_fixtures") or []
        rows = performance_team.get("historical_fixtures") or []
        team_id = (enriched.get("fixture") or {}).get(side + "_id")
        if not isinstance(team_id, int):
            issue("CRITICAL", "MISSING_TEAM_ID", side=side)
        if any(t.get("team_id") != team_id for t in (performance_team, trends_team, depth_team, players_team)):
            issue("CRITICAL", "TEAM_ID_MISMATCH", side=side, expected=team_id)
        valid_ids = [r.get("fixture_id") for r in rows]
        raw_ids = [(r.get("fixture") or {}).get("id") for r in raw]
        if len(raw_ids) != len(set(raw_ids)) or len(valid_ids) != len(set(valid_ids)):
            issue("CRITICAL", "DUPLICATE_HISTORY_IDS", side=side)
        if set(raw_ids) != set(valid_ids):
            issue("CRITICAL", "RAW_PERFORMANCE_ID_SET_MISMATCH", side=side,
                  raw_only=sorted(set(raw_ids)-set(valid_ids)),
                  performance_only=sorted(set(valid_ids)-set(raw_ids)))
        counts = {"raw": len(raw), "performance": len(rows),
                  "performance_valid_count": performance_team.get("valid_count"),
                  "trend_observed_n": trends_team.get("observed_n"),
                  "depth_observed_n": depth_team.get("observed_n"),
                  "player_historical_fixture_count": players_team.get("historical_fixture_count")}
        if any(n != len(rows) for n in list(counts.values())[1:]):
            issue("CRITICAL", "INCONSISTENT_HISTORICAL_COUNTS", side=side, counts=counts)
        if set(hist.get("fixture_ids") or []) != set(raw_ids) or hist.get("completed_fixtures_count") != len(raw):
            issue("WARNING", "LEGACY_HISTORY_METADATA_NOT_SYNCED", side=side,
                  detail_count=len(raw), stored_fixture_ids=len(hist.get("fixture_ids") or []),
                  stored_completed_count=hist.get("completed_fixtures_count"),
                  action="No sobrescribir sin aclarar procedencia del campo antiguo")
        for r in raw:
            f = r.get("fixture") or {}
            fid = f.get("id")
            opponents = (r.get("teams") or {})
            members = {((opponents.get(s) or {}).get("id")) for s in ("home", "away")}
            if fid == target or team_id not in members:
                issue("CRITICAL", "WRONG_HISTORICAL_FIXTURE", side=side, fixture_id=fid)
            date = parse_utc(f.get("date"))
            status = (f.get("status") or {}).get("short")
            if date is None or status != "FT" or (cutoff and date + timedelta(hours=3) >= cutoff):
                issue("CRITICAL", "HISTORY_NOT_COMPLETED_BEFORE_CUTOFF", side=side,
                      fixture_id=fid, status=status, kickoff=f.get("date"))
        for r in rows:
            fid = r.get("fixture_id")
            qual = r.get("data_quality") or {}
            present = [value(r, key) is not None for key in METRICS if key != "goals_for"]
            if not any(present) and qual.get("status") == "OK":
                issue("WARNING", "EMPTY_STATS_MARKED_OK", side=side, fixture_id=fid)
        coverage = {}
        ordered = sorted(rows, key=lambda r: (r.get("kickoff_utc") or "", r.get("fixture_id") or 0), reverse=True)
        for window in (5, 10, 20):
            subset = ordered[:window]
            metrics = {}
            for key in METRICS:
                observed = sum(value(r, key) is not None for r in subset)
                metrics[key] = {"observed_n": observed, "window_n": len(subset),
                                "requested_n": window, "missing_n": len(subset)-observed,
                                "status": "COMPLETE" if observed == window else "PARTIAL" if observed else "UNAVAILABLE"}
            coverage[f"L{window}"] = {"fixture_n": len(subset), "requested_n": window,
                                       "metrics": metrics}
            incomplete = {m: data["observed_n"] for m, data in metrics.items()
                          if data["observed_n"] < window}
            if incomplete:
                issue("WARNING", "WINDOW_METRIC_COVERAGE_INCOMPLETE", side=side,
                      window=f"L{window}", fixture_n=len(subset),
                      incomplete_metric_observed_n=incomplete)
        halves = {"with_full_h1_h2_for_team": 0, "with_full_h1_h2_for_both_teams": 0,
                  "games_with_halves_rows": 0, "compared_additive_values": 0,
                  "mismatch_n": 0, "mismatches": []}
        for r in raw:
            fid = (r.get("fixture") or {}).get("id")
            halves_rows = r.get("statistics_halves") or []
            if halves_rows:
                halves["games_with_halves_rows"] += 1
            team_halves = [rec for rec in halves_rows if (rec.get("team") or {}).get("id") == team_id]
            if len(team_halves) == 1 and all(isinstance(team_halves[0].get(k), list) for k in ("statistics_1h", "statistics_2h")):
                halves["with_full_h1_h2_for_team"] += 1
            if len(halves_rows) == 2 and all(isinstance(rec.get("statistics_1h"), list)
                                            and isinstance(rec.get("statistics_2h"), list) for rec in halves_rows):
                halves["with_full_h1_h2_for_both_teams"] += 1
            for rec in halves_rows:
                ft = stat_map(rec.get("statistics"))
                h1 = stat_map(rec.get("statistics_1h"))
                h2 = stat_map(rec.get("statistics_2h"))
                for metric in sorted(ADDITIVE_STATS & ft.keys() & h1.keys() & h2.keys()):
                    halves["compared_additive_values"] += 1
                    gap = ft[metric] - h1[metric] - h2[metric]
                    if abs(gap) > 1e-8:
                        note = {"fixture_id": fid, "stat_team_id": (rec.get("team") or {}).get("id"),
                                "metric": metric, "FT": ft[metric], "H1": h1[metric],
                                "H2": h2[metric], "difference_FT_minus_halves": gap}
                        halves["mismatches"].append(note)
                        all_halves_discrepancies.append({"side": side, **note})
                        halves["mismatch_n"] += 1
        if halves["mismatch_n"]:
            issue("WARNING", "FT_H1_H2_SUM_DISCREPANCY", side=side,
                  count=halves["mismatch_n"], note="No imputar; revisar semántica del proveedor")
        if halves["with_full_h1_h2_for_team"] < len(raw):
            issue("WARNING", "HALVES_PARTIAL_COVERAGE", side=side,
                  observed=halves["with_full_h1_h2_for_team"], total=len(raw))
        player_summary = {"profiles": 0, "denominator_errors": 0,
                          "observed_by_metric": {}}
        player_list = players_team.get("players") or []
        player_summary["profiles"] = len(player_list)
        obs_by_metric: dict[str, Counter] = {}
        allowed = set(valid_ids)
        for p in player_list:
            played = p.get("played_n", 0)
            if not isinstance(played, int) or played < 0 or played > len(rows):
                player_summary["denominator_errors"] += 1
            for key, evidence in (p.get("metrics") or {}).items():
                observed = evidence.get("observed_n")
                hits = evidence.get("hit_1plus_n")
                hist_ids = evidence.get("hist_fixture_ids") or []
                obs_by_metric.setdefault(key, Counter())["player_metric_observations"] += (observed or 0)
                obs_by_metric[key]["players_with_any_evidence"] += int(bool(observed))
                if (not isinstance(observed, int) or not isinstance(hits, int)
                        or not isinstance(played, int) or not 0 <= hits <= observed <= played
                        or evidence.get("played_n") != played or len(hist_ids) != observed
                        or len(hist_ids) != len(set(hist_ids)) or not set(hist_ids).issubset(allowed)):
                    player_summary["denominator_errors"] += 1
        if player_summary["denominator_errors"]:
            issue("CRITICAL", "PLAYER_EVIDENCE_DENOMINATOR_INVALID", side=side,
                  count=player_summary["denominator_errors"])
        player_summary["observed_by_metric"] = {key: dict(ct) for key, ct in sorted(obs_by_metric.items())}
        summary[side] = {"name": performance_team.get("team_name"), "team_id": team_id,
                         "history": counts, "coverage": coverage, "halves": halves,
                         "players": player_summary}

    original_manifest = folder / f"auditoria_adquisicion_{event_id}.json"
    if not original_manifest.is_file():
        issue("WARNING", "ACQUISITION_MANIFEST_MISSING",
              note="La ejecución anterior pudo terminar antes de escribir el manifiesto")
    # Quality is distinct from Bwin identity.  Neither establishes calibrated probability.
    blocking_quality = any(it["severity"] == "CRITICAL" for it in issues)
    result = {"schema": SCHEMA, "event_id_bwin": event_id, "target_fixture_id": target,
              "identity_status": identity_status, "quality_status":
              "BLOCKED" if blocking_quality else "REVIEW_REQUIRED" if any(it["severity"] == "WARNING" for it in issues) else "CHECKED",
              "final_with_bwin_odds_allowed": False,
              "final_with_bwin_odds_reason": "QUALITY_AUDIT_IS_NOT_BETTING_AUTHORIZATION" if identity_status == "FIXTURE_AND_BWIN_TIME_VERIFIED" else "BWIN_TIME_LINK_NOT_VERIFIED",
              "time_control": {"kickoff_utc": kickoff.isoformat() if kickoff else None,
                               "cutoff_utc": cutoff.isoformat() if cutoff else None,
                               "enriched_generated_at_utc": generated.isoformat() if generated else None,
                               "snapshot_generated_at_utc": snapshot.isoformat() if snapshot else None,
                               "declared_times_only": True},
              "teams": summary, "issues": issues,
              "issue_counts": dict(Counter(it["severity"] for it in issues)),
              "source_sha256": {key: hash_file(path) for key, path in paths.items()},
              "identity_sha256": hash_file(identity_path) if identity_path and identity_path.is_file() else None,
              "limits": ["No consulta API ni verifica de forma externa los históricos",
                         "No recalcula ni repara datos faltantes o discrepancias FT/H1/H2",
                         "El informe NO autoriza cuotas, probabilidades ni picks finales"]}
    return result


def markdown(report: dict) -> str:
    lines = [f"# Auditoría PREMATCH Fase 2.1 — Bwin {report['event_id_bwin']}", "",
             f"- Fixture API: `{report['target_fixture_id']}`",
             f"- Identidad: **{report['identity_status']}**",
             f"- Calidad: **{report['quality_status']}**",
             f"- Corte declarado: `{report['time_control']['cutoff_utc']}`",
             "- Las coberturas son observaciones válidas, no probabilidades ni confianza calibrada.", ""]
    for side in ("home", "away"):
        team = report["teams"][side]
        lines.extend([f"## {team['name']} ({side})", "",
                      f"Históricos brutos: {team['history']['raw']}; procesados: {team['history']['performance']}.", "",
                      "| Indicador | L5 | L10 | L20 |",
                      "|---|---:|---:|---:|"])
        for key in METRICS:
            nums = []
            for window in ("L5", "L10", "L20"):
                a = team["coverage"][window]["metrics"][key]
                nums.append(f"{a['observed_n']}/{a['requested_n']}")
            lines.append(f"| {key} | {' | '.join(nums)} |")
        h = team["halves"]
        lines.extend(["", f"H1/H2 para equipo: {h['with_full_h1_h2_for_team']}/{team['history']['raw']}; "
                      f"discrepancias FT vs mitades (ambos equipos): {h['mismatch_n']}.",
                      f"Perfiles históricos de jugadores: {team['players']['profiles']}; "
                      f"errores de denominador: {team['players']['denominator_errors']}.", ""])
        if h["mismatches"]:
            lines.extend(["**Discrepancias FT/H1+H2:**", "",
                          "| Fixture | Equipo ID | Métrica | FT | H1 | H2 | Δ |",
                          "|---|---:|---|---:|---:|---:|---:|"])
            for m in h["mismatches"]:
                lines.append(f"| {m['fixture_id']} | {m['stat_team_id']} | {m['metric']} | "
                             f"{m['FT']} | {m['H1']} | {m['H2']} | {m['difference_FT_minus_halves']} |")
            lines.append("")
    lines.extend(["## Incidencias", "", "| Severidad | Código | Equipo | Fixture |",
                  "|---|---|---|---:|"])
    for it in report["issues"]:
        lines.append(f"| {it['severity']} | {it['code']} | {it.get('side','—')} | "
                     f"{it.get('fixture_id','—')} |")
    lines.extend(["", "**Decisión:** informe solo descriptivo. No habilita `finish`, "
                  "probabilidades ni picks a partir de estas métricas.", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True, help="ID Bwin, solo dígitos")
    parser.add_argument("--dir", type=Path, help="Directorio deep_v1042 (por defecto outputs/matches/ID/analysis/deep_v1042)")
    parser.add_argument("--identity", type=Path, help="Ruta match_identity.json; por defecto outputs/matches/ID/inputs/match_identity.json")
    args = parser.parse_args()
    if not args.event_id.isdigit():
        parser.error("--event-id debe ser numérico")
    folder = args.dir or Path("outputs/matches") / args.event_id / "analysis" / "deep_v1042"
    identity = args.identity or Path("outputs/matches") / args.event_id / "inputs" / "match_identity.json"
    report = audit_bundle(folder, event_id=args.event_id, identity_path=identity)
    folder.mkdir(parents=True, exist_ok=True)
    jpath = folder / f"auditoria_calidad_{args.event_id}.json"
    mpath = folder / f"auditoria_calidad_{args.event_id}.md"
    jpath.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    mpath.write_text(markdown(report), encoding="utf-8")
    print(f"Fase 2.1 · Bwin {args.event_id} · {report['quality_status']} · "
          f"{report['identity_status']} · incidentes {report['issue_counts']}")
    for side in ("home", "away"):
        t = report["teams"][side]
        print(f"{t['name']}: {t['history']['performance']} históricos | "
              f"L20 remates {t['coverage']['L20']['metrics']['shots_for']['observed_n']}/20 | "
              f"xG {t['coverage']['L20']['metrics']['xg_for']['observed_n']}/20 | "
              f"H1/H2 {t['halves']['with_full_h1_h2_for_team']}/20")
    print(mpath)
    if report["quality_status"] == "BLOCKED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
