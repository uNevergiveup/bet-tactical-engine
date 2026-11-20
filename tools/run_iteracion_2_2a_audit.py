#!/usr/bin/env python3
"""Auditoría reproducible Iteración 2.2A:
- Comparación congelada de candidatos M0, M0+Elo, M3, M4, M4+Elo en test_strict (n=2,319).
- Sensibilidad ante RED_CARD_UNKNOWN (w_unknown in {1.0, 0.85, 0.70}).
- Comprobación de muestra prospectiva posterior a 2026-11-14T19:45:00+00:00.
- Emisión de COMPARACION_CANDIDATOS.json y MANIFEST_MODELOS_CONGELADOS.json.

Uso:
  PYTHONPATH=. .venv/bin/python tools/run_iteracion_2_2a_audit.py --out-dir audit/iteracion_2_2a
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.modeling.calibration import parse_utc_timestamp
from bet_engine.modeling.experimental_v2_1b import AsOfEloTracker, ExperimentalConfig
from tools.reproduce_baseline_backtest import _block_bootstrap_paired, _extract_fixture_record, _load_corpus, _summarize_preds
from tools.run_iteracion_2_1b_experiments import _build_all_chronological_fixtures, _eval_dataset

SCRIPT_SEED = 42
BOOTSTRAP_REPS = 2000
LAST_CORPUS_FIXTURE_UTC = "2026-11-14T19:45:00+00:00"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Auditoría congelada Iteración 2.2A")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--out-dir", type=Path, default=Path("audit/iteracion_2_2a"))
    args = parser.parse_args()

    root = args.root.resolve()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    source_meta, team_series, all_fixtures, _ = _load_corpus(root)
    team_ordered = {
        tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
        for tid, fmap in team_series.items()
    }
    all_chron = _build_all_chronological_fixtures(all_fixtures)
    elo_tracker = AsOfEloTracker.build_from_fixtures(all_chron, k_factor=20.0, hfa=55.0)

    records_clean: list[dict[str, Any]] = []
    for fid in sorted(all_fixtures.keys()):
        rec, reason = _extract_fixture_record(fid, all_fixtures[fid], team_ordered)
        if reason is None and rec is not None and not rec["rc_target"]:
            records_clean.append(rec)
    records_clean.sort(key=lambda r: (r["kickoff_dt"], r["fixture_id"]))

    boundary_iso = "2026-09-26T14:00:00+00:00"
    train_strict = [r for r in records_clean if r["kickoff_utc"] < boundary_iso]
    test_strict = [r for r in records_clean if r["kickoff_utc"] >= boundary_iso]
    mid_ko = train_strict[len(train_strict) // 2]["kickoff_utc"]
    train_fit = [r for r in train_strict if r["kickoff_utc"] < mid_ko]

    base_rates_fit: dict[str, tuple[float, float]] = {}
    for sk, sub in [
        ("CLUB", [r for r in train_fit if r["stratum"] != "NATIONAL_TEAM"]),
        ("NATIONAL_TEAM", [r for r in train_fit if r["stratum"] == "NATIONAL_TEAM"]),
    ]:
        base_rates_fit[sk] = (
            round(statistics.fmean(r["hg"] for r in sub), 4),
            round(statistics.fmean(r["ag"] for r in sub), 4),
        )

    # Configuraciones estrictamente congeladas (sin reoptimización sobre test)
    candidates: dict[str, ExperimentalConfig] = {
        "M0": ExperimentalConfig(name="M0_baseline_L20"),
        "M0_plus_Elo": ExperimentalConfig(
            name="M0_plus_Elo_0.25",
            multiplicative=False,
            use_elo=True,
            elo_weight=0.25,
            base_rates_by_stratum=base_rates_fit,
        ),
        "M3": ExperimentalConfig(
            name="M3_mult_sos0.4_novb",
            multiplicative=True,
            use_sos_opp_rates=True,
            sos_alpha=0.4,
            use_elo=False,
            half_life_days=None,
            venue_blend=0.0,
            shrink_k=8.0,
            rho=-0.04,
            base_rates_by_stratum=base_rates_fit,
        ),
        "M4": ExperimentalConfig(
            name="M4_mult_sos0.4_novb_k12",
            multiplicative=True,
            use_sos_opp_rates=True,
            sos_alpha=0.4,
            use_elo=False,
            half_life_days=None,
            venue_blend=0.0,
            shrink_k=12.0,
            rho=-0.04,
            base_rates_by_stratum=base_rates_fit,
        ),
        "M4_plus_Elo": ExperimentalConfig(
            name="M4_plus_Elo_0.25",
            multiplicative=True,
            use_sos_opp_rates=True,
            sos_alpha=0.4,
            use_elo=True,
            elo_weight=0.25,
            half_life_days=None,
            venue_blend=0.0,
            shrink_k=12.0,
            rho=-0.04,
            base_rates_by_stratum=base_rates_fit,
        ),
    }

    # Sensibilidad ante RED_CARD_UNKNOWN
    red_sens_cfgs: dict[str, ExperimentalConfig] = {
        "M0_plus_Elo_w_unknown_1.0": candidates["M0_plus_Elo"],
        "M0_plus_Elo_w_unknown_0.85": ExperimentalConfig(
            name="M0_plus_Elo_w_unknown_0.85",
            multiplicative=False,
            use_elo=True,
            elo_weight=0.25,
            base_rates_by_stratum=base_rates_fit,
            unknown_red_weight=0.85,
        ),
        "M0_plus_Elo_w_unknown_0.70": ExperimentalConfig(
            name="M0_plus_Elo_w_unknown_0.70",
            multiplicative=False,
            use_elo=True,
            elo_weight=0.25,
            base_rates_by_stratum=base_rates_fit,
            unknown_red_weight=0.70,
        ),
        "M4_w_unknown_1.0": candidates["M4"],
        "M4_w_unknown_0.85": ExperimentalConfig(
            name="M4_w_unknown_0.85",
            multiplicative=True,
            use_sos_opp_rates=True,
            sos_alpha=0.4,
            use_elo=False,
            half_life_days=None,
            venue_blend=0.0,
            shrink_k=12.0,
            rho=-0.04,
            base_rates_by_stratum=base_rates_fit,
            unknown_red_weight=0.85,
        ),
    }

    eval_rows: dict[str, list[dict[str, Any]]] = {}
    cand_report: dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": SCRIPT_SEED,
        "bootstrap_replicates": BOOTSTRAP_REPS,
        "test_strict_n": len(test_strict),
        "note": "test_strict ya observado en 2.1A/2.1B; comparación pareada congelada para verificar M4+Elo vs M0+Elo antes de validación prospectiva.",
        "candidates": {},
        "paired_vs_m0": {},
        "paired_m4_plus_elo_vs_m0_plus_elo": {},
        "paired_m4_plus_elo_vs_m4": {},
        "red_card_unknown_sensitivity": {},
    }

    for name, cfg in candidates.items():
        rows, _, extra = _eval_dataset(test_strict, cfg, team_ordered=team_ordered, elo_tracker=elo_tracker)
        eval_rows[name] = rows
        cand_report["candidates"][name] = {
            "operational": extra,
            "ALL": _summarize_preds(rows),
            "CLUB": _summarize_preds([r for r in rows if r["stratum"] != "NATIONAL_TEAM"]),
            "NATIONAL_TEAM": _summarize_preds([r for r in rows if r["stratum"] == "NATIONAL_TEAM"]),
        }

    for name in ("M0_plus_Elo", "M3", "M4", "M4_plus_Elo"):
        cand_report["paired_vs_m0"][name] = {
            "day_blocks": {
                k: _block_bootstrap_paired(eval_rows[name], eval_rows["M0"], k, block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED)
                for k in ("rps", "ll_1x2", "brier_1x2_per_out", "brier_o25", "brier_btts")
            },
            "week_blocks": {
                k: _block_bootstrap_paired(eval_rows[name], eval_rows["M0"], k, block_mode="week", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED)
                for k in ("rps", "ll_1x2")
            },
        }

    cand_report["paired_m4_plus_elo_vs_m0_plus_elo"] = {
        "day_blocks": {
            k: _block_bootstrap_paired(eval_rows["M4_plus_Elo"], eval_rows["M0_plus_Elo"], k, block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED)
            for k in ("rps", "ll_1x2", "brier_o25", "brier_btts")
        }
    }
    cand_report["paired_m4_plus_elo_vs_m4"] = {
        "day_blocks": {
            k: _block_bootstrap_paired(eval_rows["M4_plus_Elo"], eval_rows["M4"], k, block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED)
            for k in ("rps", "ll_1x2", "brier_o25", "brier_btts")
        }
    }

    for sname, scfg in red_sens_cfgs.items():
        srows, _, sextra = _eval_dataset(test_strict, scfg, team_ordered=team_ordered, elo_tracker=elo_tracker)
        sm = _summarize_preds(srows)
        cand_report["red_card_unknown_sensitivity"][sname] = {
            "unknown_red_weight": scfg.unknown_red_weight,
            "rps": sm["1x2_multiclass"]["rps"],
            "log_loss_1x2": sm["1x2_multiclass"]["log_loss"],
            "brier_over_2_5": sm["binary_markets"]["over_2_5"]["brier"],
            "brier_btts": sm["binary_markets"]["btts_yes"]["brier"],
            "mean_home_n_eff": sextra["mean_home_n_eff"],
            "fav_ge_0_55_bias": sm["favorite_underdog_bias_two_sides_2n"]["favorite_ge_0_55"].get("bias"),
        }

    (out_dir / "COMPARACION_CANDIDATOS.json").write_text(
        json.dumps(cand_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Buscar partidos FT posteriores a LAST_CORPUS_FIXTURE_UTC en outputs/matches/
    cut_dt = parse_utc_timestamp(LAST_CORPUS_FIXTURE_UTC)
    prospective_ft_after_cut = []
    for p in sorted((root / "outputs/matches").glob("*/inputs/match_engine_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        fx = d.get("fixture") or {}
        ko = fx.get("date")
        st = ((d.get("context") or {}).get("target_fixture_full") or {}).get("response") or []
        st_short = ((st[0].get("fixture") or {}).get("status") or {}).get("short") if st else None
        if ko and parse_utc_timestamp(ko) > cut_dt and st_short in {"FT", "AET", "PEN"}:
            prospective_ft_after_cut.append({"event_id": p.parent.parent.name, "kickoff_utc": ko, "status": st_short})

    manifest = {
        "schema_version": "2.2A-frozen-models",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "corpus_last_fixture_utc": LAST_CORPUS_FIXTURE_UTC,
        "prospective_sample_status": "PENDIENTE_MUESTRA_PROSPECTIVA" if len(prospective_ft_after_cut) < 50 else "READY",
        "prospective_ft_found_after_cutoff_n": len(prospective_ft_after_cut),
        "prospective_min_sample_target": {"CLUB": 200, "NATIONAL_TEAM": 50},
        "code_hashes": {
            p: _sha256_file(root / p)
            for p in (
                "bet_engine/modeling/experimental_v2_1b.py",
                "bet_engine/modeling/distributions.py",
                "bet_engine/context/observation_store.py",
                "config/constants.py",
            )
        },
        "train_fit_base_rates": base_rates_fit,
        "elo_parameters": {"initial_rating": 1500.0, "k_factor": 20.0, "hfa": 55.0, "scale": 400.0, "min_matches_for_use": 3},
        "frozen_candidates": {
            k: {
                "name": v.name,
                "multiplicative": v.multiplicative,
                "use_sos_opp_rates": v.use_sos_opp_rates,
                "sos_alpha": v.sos_alpha,
                "use_elo": v.use_elo,
                "elo_weight": v.elo_weight,
                "half_life_days": v.half_life_days,
                "venue_blend": v.venue_blend,
                "shrink_k": v.shrink_k,
                "rho": v.rho,
            }
            for k, v in candidates.items()
        },
        "sources": source_meta,
    }
    (out_dir / "MANIFEST_MODELOS_CONGELADOS.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    for cname in ("M0", "M0_plus_Elo", "M3", "M4", "M4_plus_Elo"):
        s = cand_report["candidates"][cname]["ALL"]
        print(
            cname,
            "RPS=", s["1x2_multiclass"]["rps"],
            "LL=", s["1x2_multiclass"]["log_loss"],
            "O25=", s["binary_markets"]["over_2_5"]["brier"],
            "BTTS=", s["binary_markets"]["btts_yes"]["brier"],
            "FavBias=", s["favorite_underdog_bias_two_sides_2n"]["favorite_ge_0_55"].get("bias"),
        )
    print("prospective_status:", manifest["prospective_sample_status"], "found:", len(prospective_ft_after_cut))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
