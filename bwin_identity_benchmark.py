#!/usr/bin/env python3
"""Reproducible offline benchmark of the interrupted Bwin identity run.

This command reads frozen artifacts only.  It measures the old acquisition
storm exactly and combines it with the measured local matching time from a new
identity audit.  It deliberately does not estimate an unexecuted browser
duration for the new architecture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def instant(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def acquisition(path: Path) -> dict:
    document = read(path)
    pages = [row for row in document.get("pages", []) if row.get("catalog_page")]
    starts = [instant(row.get("started_at_utc")) for row in pages]
    ends = [instant(row.get("completed_at_utc")) for row in pages]
    starts = [value for value in starts if value]
    ends = [value for value in ends if value]
    elapsed = (max(ends) - min(starts)).total_seconds() if starts and ends else None
    return {"path": str(path.resolve()), "sha256": sha(path),
            "page_navigations": len(pages), "events_rows": len(document.get("catalog_observed", [])),
            "started_at_utc": min(starts).isoformat() if starts else None,
            "completed_at_utc": max(ends).isoformat() if ends else document.get("generated_at_utc"),
            "elapsed_seconds": round(elapsed, 6) if elapsed is not None else None}


def build(evaluation: Path, audit_path: Path) -> dict:
    old_path = evaluation / "prospective_trial/bwin/bwin_discovery.json"
    old = read(old_path)
    batches = [Path(item["path"] if isinstance(item, dict) else item) for item in old.get("source_batches", [])]
    retries = sorted((evaluation / "prospective_trial/bwin").glob("retry_*/bwin_discovery.json"))
    batch_runs = [acquisition(path) for path in batches if path.is_file()]
    retry_runs = [acquisition(path) for path in retries if path.is_file()]
    all_runs = batch_runs + retry_runs
    unique_events = {str(row.get("bwin_event_id")) for row in old.get("catalog_observed", [])
                     if str(row.get("bwin_event_id") or "").isdigit()}
    measured_seconds = [row["elapsed_seconds"] for row in all_runs if row["elapsed_seconds"] is not None]
    audit = read(audit_path)
    old_invocations = len(all_runs)
    return {
        "schema_version": "1.0-bwin-identity-benchmark",
        "network_used": False,
        "sources": {"evaluation": str(evaluation.resolve()),
                    "old_consolidated": {"path": str(old_path.resolve()), "sha256": sha(old_path)},
                    "new_identity_audit": {"path": str(audit_path.resolve()), "sha256": sha(audit_path)}},
        "before_frozen_run": {
            "initial_batch_catalog_acquisitions": len(batch_runs),
            "per_fixture_retry_catalog_acquisitions": len(retry_runs),
            "total_catalog_acquisitions": old_invocations,
            "recorded_page_navigations": sum(row["page_navigations"] for row in all_runs),
            "recorded_acquisition_elapsed_seconds_sum": round(sum(measured_seconds), 6),
            "consolidated_catalog_rows": len(old.get("catalog_observed", [])),
            "unique_event_ids": len(unique_events),
            "duplicate_rows": len(old.get("catalog_observed", [])) - len(unique_events),
            "row_duplication_factor": round(len(old.get("catalog_observed", [])) / len(unique_events), 6) if unique_events else None,
        },
        "after_architecture": {
            "full_catalog_acquisitions_per_evaluation": 1,
            "per_fixture_full_catalog_retries": 0,
            "measured_local_matching_seconds": audit.get("metrics", {}).get("matching_local_seconds"),
            "fixtures_matched_locally": audit.get("metrics", {}).get("fixtures_total"),
            "events_in_shared_index": audit.get("metrics", {}).get("bwin_events_observed"),
            "browser_capture_seconds": None,
            "browser_capture_seconds_reason": "NOT_MEASURED_NO_EXTERNAL_RUN_AUTHORIZED",
        },
        "comparison": {
            "full_catalog_acquisitions_avoided_per_evaluation": max(0, old_invocations - 1),
            "full_catalog_acquisition_reduction_percent": round((1 - 1 / old_invocations) * 100, 6) if old_invocations else None,
            "initial_batch_only_reduction_percent": round((1 - 1 / len(batch_runs)) * 100, 6) if batch_runs else None,
            "claim_scope": "ARCHITECTURE_COUNT_AND_FROZEN_BEFORE_MEASURED; NEW_BROWSER_DURATION_PENDING_CONTROLLED_ONLINE_RUN",
        },
        "old_acquisitions": all_runs,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--identity-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = build(args.evaluation, args.identity_audit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = args.output.with_suffix(".md")
    before = document["before_frozen_run"]; after = document["after_architecture"]; comparison = document["comparison"]
    markdown.write_text("\n".join([
        "# Benchmark de identidad Bwin", "", "Medición offline sobre artefactos congelados; no se usó red.", "",
        "| Métrica | Antes | Después |", "|---|---:|---:|",
        f"| Adquisiciones completas | {before['total_catalog_acquisitions']} | {after['full_catalog_acquisitions_per_evaluation']} |",
        f"| Reintentos completos por fixture | {before['per_fixture_retry_catalog_acquisitions']} | {after['per_fixture_full_catalog_retries']} |",
        f"| Filas / Event IDs únicos | {before['consolidated_catalog_rows']} / {before['unique_event_ids']} | índice deduplicado {after['events_in_shared_index']} |",
        f"| Matching local | integrado/repetido | {after['measured_local_matching_seconds']} s para {after['fixtures_matched_locally']} fixtures |",
        "", f"Reducción estructural de adquisiciones completas: {comparison['full_catalog_acquisition_reduction_percent']}%.",
        "La duración de la nueva captura browser queda pendiente de una prueba online autorizada; no se ha estimado.", ""
    ]), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), **document["comparison"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
