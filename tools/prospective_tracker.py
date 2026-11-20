#!/usr/bin/env python3
"""CLI de registro y evaluación prospectiva PREMATCH (Iteración 2.2B -> 2.2C.1).

Ejemplos:
  # 1) Registrar predicciones congeladas ANTES del kickoff (con Elo y SoS as-of):
  PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py record \
    --match-engine outputs/matches/<EVENT_ID>/inputs/match_engine_<EVENT_ID>.json \
    --bwin-txt outputs/matches/<EVENT_ID>/inputs/cuotas_bwin_<EVENT_ID>.txt \
    --manifest audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json \
    --corpus-root . \
    --records-dir outputs/prospective_records

  # 2) Liquidar resultado oficial DESPUÉS del partido con fuente autorizada (FT/AET/PEN):
  PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py settle \
    --record outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json \
    --hg 2 --ag 1 --status FT \
    --source-json outputs/matches/<EVENT_ID>/inputs/target_fixture_<FIXTURE_ID>.json

  # 3) Evaluar comparativa pareada sobre registros acreditados (VERIFIED_AUTHORITATIVE):
  PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py evaluate \
    --records-dir outputs/prospective_records
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from bet_engine.modeling.prospective_cand_2b3b import (
    evaluate_2b3b_prospective_records,
    record_2b3b_batch,
    record_2b3b_prospective_prediction,
    settle_2b3b_prospective_record,
)
from bet_engine.modeling.prospective_registry import (
    evaluate_prospective_records,
    extract_bwin_capture_utc,
    record_prospective_prediction,
    settle_prospective_record,
)
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt


def main() -> int:
    parser = argparse.ArgumentParser(description="Tracker prospectivo PREMATCH (2.2C.1 / Fase 2B.4)")
    parser.add_argument(
        "--track",
        choices=("legacy", "2b3b"),
        default="legacy",
        help="legacy = M0/M0+Elo/M4/M4+Elo (2.2C); 2b3b = Baseline B0 vs CoherentMatchModeler_cand_2b3b_v1",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record", help="Registra predicciones congeladas antes del kickoff")
    p_rec.add_argument("--match-engine", type=Path, required=True)
    p_rec.add_argument("--manifest", type=Path, default=None)
    p_rec.add_argument("--corpus-root", type=Path, default=Path("."), help="Raíz del corpus histórico as-of (solo modo legacy)")
    p_rec.add_argument("--records-dir", type=Path, default=None)
    p_rec.add_argument("--bwin-txt", type=Path, default=None)
    p_rec.add_argument("--shadow-json", type=Path, default=None)
    p_rec.add_argument("--qualitative-md", type=Path, default=None)

    p_batch = sub.add_parser("record-batch", help="Registra en lote expedientes futuros bajo outputs/matches (solo --track 2b3b)")
    p_batch.add_argument("--matches-root", type=Path, default=Path("outputs/matches"))
    p_batch.add_argument("--manifest", type=Path, default=Path("audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json"))
    p_batch.add_argument("--records-dir", type=Path, default=Path("outputs/prospective_2b3b"))

    p_set = sub.add_parser("settle", help="Incorpora resultado final en archivo separado sin alterar el registro original")
    p_set.add_argument("--record", type=Path, required=True)
    p_set.add_argument("--hg", type=int, required=True)
    p_set.add_argument("--ag", type=int, required=True)
    p_set.add_argument("--status", type=str, default="FT")
    p_set.add_argument("--source-json", type=Path, default=None, help="JSON autorizado del resultado oficial (API-Football)")
    p_set.add_argument("--simulate", action="store_true", help="Modo simulación solo para tests (no acreditable)")

    p_eval = sub.add_parser("evaluate", help="Evalúa comparativa pareada sobre registros VERIFIED_AUTHORITATIVE")
    p_eval.add_argument("--records-dir", type=Path, default=None)

    args = parser.parse_args()
    default_manifest = (
        Path("audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json")
        if args.track == "2b3b"
        else Path("audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json")
    )
    default_records = Path("outputs/prospective_2b3b") if args.track == "2b3b" else Path("outputs/prospective_records")

    if args.cmd == "record":
        payload = json.loads(args.match_engine.read_text(encoding="utf-8"))
        manifest_path = args.manifest or default_manifest
        records_dir = args.records_dir or default_records
        odds = None
        odds_sha = None
        odds_cap_utc = None
        if args.bwin_txt and args.bwin_txt.is_file():
            fx = payload.get("fixture") or {}
            odds = parse_bwin_odds_txt(args.bwin_txt, home_name=fx.get("home_name"), away_name=fx.get("away_name"))
            odds_sha = hashlib.sha256(args.bwin_txt.read_bytes()).hexdigest()
            odds_cap_utc = extract_bwin_capture_utc(args.bwin_txt)
        shadow_decision = None
        if args.shadow_json and args.shadow_json.is_file():
            sdoc = json.loads(args.shadow_json.read_text(encoding="utf-8"))
            shadow_decision = str(sdoc.get("decision") or ((sdoc.get("shadow_report") or {}).get("decision")) or sdoc.get("status") or "")
        qual_md = args.qualitative_md.read_text(encoding="utf-8-sig", errors="replace") if (args.qualitative_md and args.qualitative_md.is_file()) else None

        if args.track == "2b3b":
            out = record_2b3b_prospective_prediction(
                match_engine_payload=payload,
                records_dir=records_dir,
                manifest_path=manifest_path if manifest_path.is_file() else None,
                bwin_odds=odds,
                odds_captured_utc=odds_cap_utc,
                odds_sha256=odds_sha,
                shadow_decision=shadow_decision or None,
                qualitative_markdown=qual_md,
                source_paths={
                    "match_engine": str(args.match_engine),
                    "bwin_txt": str(args.bwin_txt) if args.bwin_txt else "",
                    "manifest": str(manifest_path),
                },
                verify_manifest=manifest_path.is_file(),
            )
            rec_doc = json.loads(out.read_text(encoding="utf-8"))
            pre = rec_doc["pre_kickoff"]
            summary = {
                "track": "2b3b",
                "record_path": str(out),
                "fixture_id": pre["fixture_id"],
                "event_id": pre["event_id"],
                "kickoff_utc": pre["timing"]["kickoff_utc"],
                "kickoff_madrid": pre["timing"]["kickoff_madrid"],
                "temporal_integrity_status": pre["timing"]["temporal_integrity_status"],
                "fixture_identity_verified": pre["identity_verification"]["fixture_identity_verified"],
                "history_coverage": pre["diagnostics"]["history_coverage"],
                "data_quality": pre["diagnostics"]["data_quality"],
                "odds_status": pre["bwin_odds"]["odds_status"],
                "experimental_bet_authorized": pre["governance"]["experimental_bet_authorized"],
                "pre_kickoff_sha256": rec_doc["pre_kickoff_sha256"],
            }
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0

        out = record_prospective_prediction(
            match_engine_payload=payload,
            manifest_path=manifest_path,
            records_dir=records_dir,
            corpus_root=args.corpus_root,
            bwin_odds=odds,
            odds_captured_utc=odds_cap_utc,
            odds_sha256=odds_sha,
            shadow_decision=shadow_decision,
            source_paths={
                "match_engine": str(args.match_engine),
                "bwin_txt": str(args.bwin_txt) if args.bwin_txt else "",
                "manifest": str(manifest_path),
                "corpus_root": str(args.corpus_root) if args.corpus_root else "",
            },
        )
        rec_doc = json.loads(out.read_text(encoding="utf-8"))
        pre = rec_doc["pre_kickoff"]
        summary = {
            "record_path": str(out),
            "fixture_id": pre["fixture_id"],
            "event_id": pre["event_id"],
            "kickoff_utc": pre["timing"]["kickoff_utc"],
            "kickoff_madrid": pre["timing"]["kickoff_madrid"],
            "temporal_integrity_status": pre["timing"]["temporal_integrity_status"],
            "fixture_identity_verified": pre["identity_verification"]["fixture_identity_verified"],
            "history_summary": pre["history_summary"],
            "model_comparison_integrity": pre["model_comparison_integrity"],
            "odds_status": pre["bwin_odds"]["odds_status"],
            "pre_kickoff_sha256": rec_doc["pre_kickoff_sha256"],
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "record-batch":
        rep = record_2b3b_batch(
            matches_root=args.matches_root,
            records_dir=args.records_dir,
            manifest_path=args.manifest if args.manifest.is_file() else None,
            verify_manifest=args.manifest.is_file(),
        )
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "settle":
        auth_fx = json.loads(args.source_json.read_text(encoding="utf-8")) if args.source_json else None
        fn = settle_2b3b_prospective_record if args.track == "2b3b" else settle_prospective_record
        doc = fn(
            args.record,
            hg=args.hg,
            ag=args.ag,
            match_status=args.status,
            authoritative_fixture=auth_fx,
            source_path=args.source_json,
            source_label=str(args.source_json) if args.source_json else None,
            simulation_mode=bool(args.simulate),
        )
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "evaluate":
        records_dir = args.records_dir or default_records
        rep = (
            evaluate_2b3b_prospective_records(records_dir)
            if args.track == "2b3b"
            else evaluate_prospective_records(records_dir)
        )
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
