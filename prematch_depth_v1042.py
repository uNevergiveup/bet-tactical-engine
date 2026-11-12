#!/usr/bin/env python3
"""PREMATCH V1.0.4 Fase 2; standalone; no edits to existing pipeline.

Offline example:
 python prematch_depth_v1042.py --input outputs/matches/7896590/inputs/match_engine_7896590.json

For a FUTURE fixture ONLY, incremental API fetch (the original JSON still needs
pre-kickoff timestamp), with cache and API budget:
 python prematch_depth_v1042.py --input outputs/matches/EID/inputs/match_engine_EID.json \\
  --online --history 20 --max-api-calls 110
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

import match_performance_engine as performance
import team_form_engine as form
import prematch_depth_collector as collection
import prematch_depth_analysis as depth
import prematch_player_evidence as players
import prematch_quality_audit_v1043 as quality


def run(args: argparse.Namespace) -> dict:
    raw = args.input.read_bytes()
    original_sha = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw.decode("utf-8-sig"))
    fixture = data.get("fixture") or {}
    event = str((data.get("match") or {}).get("event_id") or "")
    if not event.isdigit():
        raise ValueError("INVALID_EVENT_ID")
    out_dir = args.output_dir or Path("outputs/matches") / event / "analysis" / "deep_v1042"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"mode": "OFFLINE_HISTORICAL_SNAPSHOT_ONLY", "base_sha256": original_sha,
              "event_id_bwin": event, "fixture_id": fixture.get("fixture_id"),
              "data_collection": {}, "warnings": []}
    if args.online:
        from datetime import datetime, timezone
        kickoff = performance.aware(fixture.get("date"), "target.kickoff")
        if datetime.now(timezone.utc) >= kickoff:
            raise ValueError("ONLINE_PREMATCH_ONLY: partido iniciado o finalizado, no se permite backfill")
        client = collection.ApiClient(args.cache_dir, max_calls=args.max_api_calls)
        data, acquisition = collection.enrich(data, client, history=args.history,
                                              include_players=args.include_players,
                                              include_halves=args.include_halves)
        report["mode"] = "ONLINE_PREMATCH_ENRICHMENT"
        report["data_collection"] = acquisition
        # Only separate output; never mutate original /inputs/ or manifests.
        source = out_dir / f"contexto_ampliado_{event}.json"
        enriched_sha = collection.dump_json(source, data)
        report["enriched_sha256"] = enriched_sha
    else:
        if args.include_players or args.include_halves:
            report["warnings"].append("--include-players/--include-halves only act with --online")
    data_bytes = (json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n").encode()
    perf = performance.build(data, input_sha256=hashlib.sha256(data_bytes).hexdigest(), as_of=args.as_of)
    ppath = out_dir / f"rendimiento_profundo_{event}.json"
    p_hash = collection.dump_json(ppath, perf)
    f = form.build(perf, source_sha256=p_hash)
    fpath = out_dir / f"tendencias_profundas_{event}.json"
    collection.dump_json(fpath, f)
    pl = players.build(data, perf)
    plpath = out_dir / f"jugadores_historicos_{event}.json"
    collection.dump_json(plpath, pl)
    d = depth.build(perf, f)
    dpath = out_dir / f"profundidad_historica_{event}.json"
    collection.dump_json(dpath, d)
    mdpath = out_dir / f"comparativa_matchup_{event}.md"
    mdpath.write_text(depth.markdown(d, original_source=report["mode"],
                                     calls=report["data_collection"].get("requests")), encoding="utf-8")
    report["artifacts"] = [str(p) for p in (ppath, fpath, plpath, dpath, mdpath)]
    collection.dump_json(out_dir / f"auditoria_adquisicion_{event}.json", report)
    if args.online:
        # Auditoría local tras guardar la adquisición; no red, no cambio de inputs.
        quality_report = quality.audit_bundle(
            out_dir, event_id=event, identity_path=args.input.with_name("match_identity.json"))
        qjson = out_dir / f"auditoria_calidad_{event}.json"
        qmd = out_dir / f"auditoria_calidad_{event}.md"
        collection.dump_json(qjson, quality_report)
        qmd.write_text(quality.markdown(quality_report), encoding="utf-8")
        report["quality_status"] = quality_report["quality_status"]
        report["artifacts"].extend((str(qjson), str(qmd)))
        collection.dump_json(out_dir / f"auditoria_adquisicion_{event}.json", report)
        print(f"AUDITORÍA Fase 2.1: {quality_report['quality_status']} · "
              f"{quality_report['issue_counts']}")
        if quality_report["quality_status"] == "BLOCKED":
            raise RuntimeError("QUALITY_AUDIT_BLOCKED: revisar auditoria_calidad antes del motor")
    else:
        report["quality_status"] = "NOT_RUN_OFFLINE"
        report["warnings"].append("QUALITY_AUDIT_NOT_RUN_OFFLINE")
        collection.dump_json(out_dir / f"auditoria_adquisicion_{event}.json", report)
    print(f"OK Fase 2 · Bwin {event} · fixture {d['target_fixture_id']} · {report['mode']}")
    for side in ("home", "away"):
        s = d["teams"][side]
        print(f"{s['name']}: {s['observed_n']} históricos | "
              f"L10={'OK' if s['coverage']['L10'] else 'PARCIAL'} "
              f"L20={'OK' if s['coverage']['L20'] else 'PARCIAL'}")
    print(mdpath)
    return d


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--as-of", help="ISO8601 explicit offset; no posterior a snapshot")
    parser.add_argument("--online", action="store_true", help="Sólo para eventos futuros antes del kickoff")
    parser.add_argument("--history", type=int, default=20, choices=range(6, 31), metavar="6..30")
    parser.add_argument("--max-api-calls", type=int, default=110)
    parser.add_argument("--cache-dir", type=Path, default=Path("outputs/.api_football_history_cache"))
    parser.add_argument("--include-players", action="store_true", help="Añade snapshots crudos de jugadores")
    parser.add_argument("--include-halves", action="store_true", help="Añade estadísticas crudas de mitades")
    args = parser.parse_args()
    if args.max_api_calls < 1 or args.max_api_calls > 300:
        parser.error("--max-api-calls entre 1 y 300")
    run(args)

if __name__ == "__main__":
    main()
