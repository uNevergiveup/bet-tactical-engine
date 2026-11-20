#!/usr/bin/env python3
"""Runner reproducible de Iteración 2.1B: ablaciones M0..M4, validación interna en val_select,
evaluación OOS en test_strict (n=2,319) y sensibilidad 7909713.

Uso:
  PYTHONPATH=. .venv/bin/python tools/run_iteracion_2_1b_experiments.py --out-dir audit/iteracion_2_1b
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.context.observation_store import ObservationStore
from bet_engine.modeling.calibration import parse_utc_timestamp
from bet_engine.modeling.distributions import _gamma_rate_bounds, dixon_coles_joint_grid
from bet_engine.modeling.experimental_v2_1b import (
    AsOfEloTracker,
    ExperimentalConfig,
    predict_experimental_match,
)
from tools.reproduce_baseline_backtest import (
    _block_bootstrap_paired,
    _extract_fixture_record,
    _load_corpus,
    _summarize_preds,
)

SCRIPT_SEED = 42
BOOTSTRAP_REPS = 2000


def _build_all_chronological_fixtures(all_fixtures: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fid, sides in all_fixtures.items():
        if "home" in sides and "away" in sides:
            hm, am = sides["home"], sides["away"]
            hid, aid, ko, avail, stratum = hm.team_id, am.team_id, hm.kickoff_utc, hm.available_at_utc, hm.competition_stratum
            hg, ag = hm.own_metrics.get("goals"), am.own_metrics.get("goals")
        elif "home" in sides:
            hm = sides["home"]
            hid, aid, ko, avail, stratum = hm.team_id, hm.opponent_id, hm.kickoff_utc, hm.available_at_utc, hm.competition_stratum
            hg, ag = hm.own_metrics.get("goals"), hm.conceded_metrics.get("goals")
        elif "away" in sides:
            am = sides["away"]
            hid, aid, ko, avail, stratum = am.opponent_id, am.team_id, am.kickoff_utc, am.available_at_utc, am.competition_stratum
            hg, ag = am.conceded_metrics.get("goals"), am.own_metrics.get("goals")
        else:
            continue
        if hg is None or ag is None or not hid or not aid:
            continue
        rows.append({
            "fixture_id": fid,
            "home_id": hid,
            "away_id": aid,
            "hg": float(hg),
            "ag": float(ag),
            "stratum": stratum,
            "ko_dt": parse_utc_timestamp(ko),
            "avail_dt": parse_utc_timestamp(avail),
        })
    rows.sort(key=lambda r: (r["avail_dt"], r["ko_dt"], r["fixture_id"]))
    return rows


def _eval_dataset(
    records: list[dict[str, Any]],
    cfg: ExperimentalConfig,
    *,
    team_ordered: dict[int, list[Any]],
    elo_tracker: AsOfEloTracker,
) -> tuple[list[dict[str, Any]], float, dict[str, Any]]:
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    sos_covs: list[float] = []
    elo_covs: list[int] = []
    n_eff_h_list: list[float] = []
    n_eff_a_list: list[float] = []

    for rec in records:
        pred = predict_experimental_match(
            rec["hp_rev"],
            rec["ap_rev"],
            home_id=rec["home_id"],
            away_id=rec["away_id"],
            target_ko_dt=rec["kickoff_dt"],
            target_league_id=rec["league_id"],
            target_season=rec["season"],
            target_stratum=rec["stratum"],
            cfg=cfg,
            team_ordered_by_id=team_ordered,
            elo_tracker=elo_tracker,
            history_limit=20,
        )
        lh, la = pred["lambda_home"], pred["lambda_away"]
        p1, px, p2 = pred["p_home"], pred["p_draw"], pred["p_away"]
        po25, pbtts = pred["p_over_2_5"], pred["p_btts_yes"]
        hg, ag = rec["hg"], rec["ag"]
        y_1x2 = "HOME" if hg > ag else ("DRAW" if hg == ag else "AWAY")
        y_hw = 1 if hg > ag else 0
        y_dr = 1 if hg == ag else 0
        y_aw = 1 if hg < ag else 0
        y_o25 = 1 if (hg + ag) > 2.5 else 0
        y_btts = 1 if (hg > 0 and ag > 0) else 0

        rps = ((p1 - y_hw) ** 2 + ((p1 + px) - (y_hw + y_dr)) ** 2) / 2.0
        eps = 1e-6
        ll_1x2 = -math.log(max(eps, p1 if y_1x2 == "HOME" else (px if y_1x2 == "DRAW" else p2)))
        brier_1x2_per_out = ((p1 - y_hw) ** 2 + (px - y_dr) ** 2 + (p2 - y_aw) ** 2) / 3.0

        comp = pred["components"]
        sos_covs.append(float(comp.get("sos_coverage_rate") or 0.0))
        elo_covs.append(1 if comp.get("elo_coverage_ok") else 0)
        n_eff_h_list.append(float(comp.get("home_n_eff") or 0.0))
        n_eff_a_list.append(float(comp.get("away_n_eff") or 0.0))

        # Bandas de referencia (misma lógica para compatibilidad con _summarize_preds)
        rows.append({
            "fixture_id": rec["fixture_id"],
            "kickoff_utc": rec["kickoff_utc"],
            "stratum": rec["stratum"],
            "hg": hg,
            "ag": ag,
            "y_1x2": y_1x2,
            "y_hw": y_hw,
            "y_dr": y_dr,
            "y_aw": y_aw,
            "y_o25": y_o25,
            "y_btts": y_btts,
            "lambda_home": lh,
            "lambda_away": la,
            "p_home": p1,
            "p_draw": px,
            "p_away": p2,
            "p_over_2_5": po25,
            "p_btts_yes": pbtts,
            "hw_lo": p1,
            "hw_hi": p1,
            "o25_lo": po25,
            "o25_hi": po25,
            "btts_lo": pbtts,
            "btts_hi": pbtts,
            "q10_total": 0,
            "q90_total": 10,
            "rps": rps,
            "ll_1x2": ll_1x2,
            "brier_1x2_per_out": brier_1x2_per_out,
            "brier_o25": (po25 - y_o25) ** 2,
            "ll_o25": -math.log(max(eps, po25 if y_o25 == 1 else (1.0 - po25))),
            "brier_btts": (pbtts - y_btts) ** 2,
            "ll_btts": -math.log(max(eps, pbtts if y_btts == 1 else (1.0 - pbtts))),
            "home_n_eff": comp.get("home_n_eff"),
            "away_n_eff": comp.get("away_n_eff"),
            "sos_coverage_rate": comp.get("sos_coverage_rate"),
            "elo_coverage_ok": bool(comp.get("elo_coverage_ok")),
        })

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    extra = {
        "elapsed_ms_total": round(elapsed_ms, 2),
        "ms_per_match": round(elapsed_ms / max(1, len(records)), 4),
        "mean_home_n_eff": round(statistics.fmean(n_eff_h_list), 3) if n_eff_h_list else 0.0,
        "mean_away_n_eff": round(statistics.fmean(n_eff_a_list), 3) if n_eff_a_list else 0.0,
        "mean_sos_opp_coverage_rate": round(statistics.fmean(sos_covs), 4) if sos_covs else 0.0,
        "elo_both_sides_ge3_coverage": round(statistics.fmean(elo_covs), 4) if elo_covs else 0.0,
    }
    return rows, elapsed_ms, extra


def _evaluate_7909713(
    configs: dict[str, ExperimentalConfig],
    *,
    team_ordered: dict[int, list[Any]],
    elo_tracker: AsOfEloTracker,
) -> dict[str, Any]:
    me_path = Path("outputs/matches/7909713/inputs/match_engine_7909713.json")
    payload = json.loads(me_path.read_text(encoding="utf-8"))
    bundle = ObservationStore.from_match_engine_json(payload, case_dir=me_path.parent.parent)
    target_ko = parse_utc_timestamp(bundle.kickoff_utc)
    stratum = bundle.home_matches[0].competition_stratum if bundle.home_matches else "NATIONAL_TEAM"

    out: dict[str, Any] = {
        "event_id": "7909713",
        "match": "Argentina vs Costa Rica",
        "snapshot_history_depth": {"home": len(bundle.home_matches), "away": len(bundle.away_matches), "note": "Snapshot congelado L6; no se introducen datos futuros ni L20 externos."},
        "bwin_reference_not_for_training": {"home_odd": 1.055, "draw_odd": 15.5, "away_odd": 29.0, "raw_implied_home": 0.9479},
        "models": {},
    }
    base_p1 = None
    for name, cfg in configs.items():
        res = predict_experimental_match(
            bundle.home_matches,
            bundle.away_matches,
            home_id=bundle.home_id,
            away_id=bundle.away_id,
            target_ko_dt=target_ko,
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=stratum,
            cfg=cfg,
            team_ordered_by_id=team_ordered,
            elo_tracker=elo_tracker,
            history_limit=20,
        )
        lh, la = res["lambda_home"], res["lambda_away"]
        comp = res["components"]
        # Incertidumbre referencial con gamma bounds actuales usando n_eff del modelo
        nh_post = (float(comp["home_n_eff"]) + 8.0) * 0.85
        na_post = (float(comp["away_n_eff"]) + 8.0) * 0.85
        lh_lo, lh_hi = _gamma_rate_bounds(lh, nh_post, 1.18)
        la_lo, la_hi = _gamma_rate_bounds(la, na_post, 1.18)
        corners = [
            sum(g[i][j] for i in range(13) for j in range(13) if i > j)
            for g in [dixon_coles_joint_grid(a, b, rho=cfg.rho, max_goals=12) for a in (lh_lo, lh_hi) for b in (la_lo, la_hi)]
        ]
        if base_p1 is None:
            base_p1 = res["p_home"]
        out["models"][name] = {
            "lambda_home": lh,
            "lambda_away": la,
            "outcomes_1x2": {
                "home_win": round(res["p_home"], 4),
                "draw": round(res["p_draw"], 4),
                "away_win": round(res["p_away"], 4),
            },
            "p_over_2_5": round(res["p_over_2_5"], 4),
            "p_btts_yes": round(res["p_btts_yes"], 4),
            "delta_p_home_vs_m0": round(res["p_home"] - base_p1, 4),
            "uncertainty_p_home_bounds": [round(min(corners), 4), round(max(corners), 4)],
            "prior_weight_share_home_att": round(cfg.shrink_k / (float(comp["home_n_eff"]) + cfg.shrink_k), 4),
            "components": comp,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Experimentos M0..M4 Iteración 2.1B")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--out-dir", type=Path, default=Path("audit/iteracion_2_1b"))
    args = parser.parse_args()

    root = args.root.resolve()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    source_meta, team_series, all_fixtures, meta_counts = _load_corpus(root)
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

    # Dividir train_strict (n=2,170) sin empates de kickoff en train_fit + val_select
    mid_ko = train_strict[len(train_strict) // 2]["kickoff_utc"]
    train_fit = [r for r in train_strict if r["kickoff_utc"] < mid_ko]
    val_select = [r for r in train_strict if r["kickoff_utc"] >= mid_ko]
    assert train_fit[-1]["kickoff_dt"] < val_select[0]["kickoff_dt"]

    # Estimar tasas base exclusivamente en train_fit
    base_rates_fit: dict[str, tuple[float, float]] = {}
    for sk, sub in [
        ("CLUB", [r for r in train_fit if r["stratum"] != "NATIONAL_TEAM"]),
        ("NATIONAL_TEAM", [r for r in train_fit if r["stratum"] == "NATIONAL_TEAM"]),
    ]:
        if len(sub) >= 10:
            base_rates_fit[sk] = (
                round(statistics.fmean(r["hg"] for r in sub), 4),
                round(statistics.fmean(r["ag"] for r in sub), 4),
            )
        else:
            # Si selecciones en train_fit es muy pequeño (<10), usar prior stratum fallback (1.50, 1.40) o CLUB?
            # Medimos explícitamente cuántos hay en train_fit:
            base_rates_fit[sk] = (1.65, 1.30)

    # Selección de hiperparámetros EXCLUSIVAMENTE sobre val_select (sin mirar test_strict)
    # Paso 1: M2 sobre val_select (sos_alpha y elo_weight)
    best_m2 = None
    best_m2_score = 999.0
    m2_grid_results = []
    for sos_a in (0.0, 0.25, 0.40):
        for elo_w in (0.0, 0.20, 0.35, 0.50):
            if sos_a == 0.0 and elo_w == 0.0:
                continue
            c = ExperimentalConfig(
                name=f"M2_sos{sos_a}_elo{elo_w}",
                multiplicative=True,
                use_sos_opp_rates=(sos_a > 0),
                sos_alpha=sos_a,
                use_elo=(elo_w > 0),
                elo_weight=elo_w,
                base_rates_by_stratum=base_rates_fit,
            )
            rows_v, _, _ = _eval_dataset(val_select, c, team_ordered=team_ordered, elo_tracker=elo_tracker)
            ll_v = statistics.fmean(r["ll_1x2"] for r in rows_v)
            rps_v = statistics.fmean(r["rps"] for r in rows_v)
            m2_grid_results.append({"sos_alpha": sos_a, "elo_weight": elo_w, "val_ll": round(ll_v, 5), "val_rps": round(rps_v, 5)})
            if ll_v < best_m2_score:
                best_m2_score = ll_v
                best_m2 = c
    assert best_m2 is not None

    # Paso 2: M3 sobre val_select (half_life_days y venue_blend)
    best_m3 = None
    best_m3_score = 999.0
    m3_grid_results = []
    for hl in (None, 90.0, 180.0, 365.0):
        for vb in (0.0, 0.15, 0.25):
            c = ExperimentalConfig(
                name=f"M3_hl{hl}_vb{vb}",
                multiplicative=True,
                use_sos_opp_rates=best_m2.use_sos_opp_rates,
                sos_alpha=best_m2.sos_alpha,
                use_elo=best_m2.use_elo,
                elo_weight=best_m2.elo_weight,
                half_life_days=hl,
                venue_blend=vb,
                base_rates_by_stratum=base_rates_fit,
            )
            rows_v, _, _ = _eval_dataset(val_select, c, team_ordered=team_ordered, elo_tracker=elo_tracker)
            ll_v = statistics.fmean(r["ll_1x2"] for r in rows_v)
            rps_v = statistics.fmean(r["rps"] for r in rows_v)
            m3_grid_results.append({"half_life_days": hl, "venue_blend": vb, "val_ll": round(ll_v, 5), "val_rps": round(rps_v, 5)})
            if ll_v < best_m3_score:
                best_m3_score = ll_v
                best_m3 = c
    assert best_m3 is not None

    # Paso 3: M4 sobre val_select (shrink_k y rho)
    best_m4 = None
    best_m4_score = 999.0
    m4_grid_results = []
    for sk in (4.0, 6.0, 8.0, 12.0, 16.0):
        for rho_v in (-0.04, -0.06, -0.08, -0.10):
            c = ExperimentalConfig(
                name=f"M4_k{sk}_rho{rho_v}",
                multiplicative=True,
                use_sos_opp_rates=best_m3.use_sos_opp_rates,
                sos_alpha=best_m3.sos_alpha,
                use_elo=best_m3.use_elo,
                elo_weight=best_m3.elo_weight,
                half_life_days=best_m3.half_life_days,
                venue_blend=best_m3.venue_blend,
                shrink_k=sk,
                rho=rho_v,
                base_rates_by_stratum=base_rates_fit,
            )
            rows_v, _, _ = _eval_dataset(val_select, c, team_ordered=team_ordered, elo_tracker=elo_tracker)
            ll_v = statistics.fmean(r["ll_1x2"] for r in rows_v)
            rps_v = statistics.fmean(r["rps"] for r in rows_v)
            m4_grid_results.append({"shrink_k": sk, "rho": rho_v, "val_ll": round(ll_v, 5), "val_rps": round(rps_v, 5)})
            if ll_v < best_m4_score:
                best_m4_score = ll_v
                best_m4 = c
    assert best_m4 is not None

    # Configuraciones principales (M0..M4) + ablaciones individuales desde M0
    cfg_m0 = ExperimentalConfig(name="M0_baseline_L20")
    cfg_m1 = ExperimentalConfig(name="M1_multiplicative", multiplicative=True, base_rates_by_stratum=base_rates_fit)
    cfg_m2 = ExperimentalConfig(
        name="M2_multiplicative_sos_elo",
        multiplicative=True,
        use_sos_opp_rates=best_m2.use_sos_opp_rates,
        sos_alpha=best_m2.sos_alpha,
        use_elo=best_m2.use_elo,
        elo_weight=best_m2.elo_weight,
        base_rates_by_stratum=base_rates_fit,
    )
    cfg_m3 = ExperimentalConfig(
        name="M3_recency_venue",
        multiplicative=True,
        use_sos_opp_rates=best_m3.use_sos_opp_rates,
        sos_alpha=best_m3.sos_alpha,
        use_elo=best_m3.use_elo,
        elo_weight=best_m3.elo_weight,
        half_life_days=best_m3.half_life_days,
        venue_blend=best_m3.venue_blend,
        base_rates_by_stratum=base_rates_fit,
    )
    cfg_m4 = ExperimentalConfig(
        name="M4_priors_shrink_rho",
        multiplicative=True,
        use_sos_opp_rates=best_m4.use_sos_opp_rates,
        sos_alpha=best_m4.sos_alpha,
        use_elo=best_m4.use_elo,
        elo_weight=best_m4.elo_weight,
        half_life_days=best_m4.half_life_days,
        venue_blend=best_m4.venue_blend,
        shrink_k=best_m4.shrink_k,
        rho=best_m4.rho,
        base_rates_by_stratum=base_rates_fit,
    )

    # Ablaciones individuales desde M0 para aislar cada efecto
    all_configs: dict[str, ExperimentalConfig] = {
        "M0": cfg_m0,
        "M1": cfg_m1,
        "M2": cfg_m2,
        "M3": cfg_m3,
        "M4": cfg_m4,
        "ABL_only_sos_additive": ExperimentalConfig(
            name="ABL_only_sos_additive", multiplicative=False, use_sos_opp_rates=True, sos_alpha=best_m2.sos_alpha or 0.35, base_rates_by_stratum=base_rates_fit
        ),
        "ABL_only_elo_additive": ExperimentalConfig(
            name="ABL_only_elo_additive", multiplicative=False, use_elo=True, elo_weight=best_m2.elo_weight or 0.25, base_rates_by_stratum=base_rates_fit
        ),
        "ABL_only_recency_additive": ExperimentalConfig(
            name="ABL_only_recency_additive", multiplicative=False, half_life_days=best_m3.half_life_days or 180.0, base_rates_by_stratum=base_rates_fit
        ),
        "ABL_only_shrink_additive": ExperimentalConfig(
            name="ABL_only_shrink_additive", multiplicative=False, shrink_k=best_m4.shrink_k, rho=best_m4.rho, base_rates_by_stratum=base_rates_fit
        ),
    }

    # Evaluar una sola vez en test_strict (n=2,319)
    eval_rows: dict[str, list[dict[str, Any]]] = {}
    metrics_oos: dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": SCRIPT_SEED,
        "bootstrap_replicates": BOOTSTRAP_REPS,
        "splits": {
            "train_fit_n": len(train_fit),
            "train_fit_range": [train_fit[0]["kickoff_utc"], train_fit[-1]["kickoff_utc"]],
            "train_fit_nat_n": sum(1 for r in train_fit if r["stratum"] == "NATIONAL_TEAM"),
            "val_select_n": len(val_select),
            "val_select_range": [val_select[0]["kickoff_utc"], val_select[-1]["kickoff_utc"]],
            "test_strict_n": len(test_strict),
            "test_strict_range": [test_strict[0]["kickoff_utc"], test_strict[-1]["kickoff_utc"]],
            "test_strict_note": "Conjunto de 2,319 partidos ya observado en 2.1A; no usado en selección de hiperparámetros, pero no es un holdout virgen.",
        },
        "train_fit_base_rates": base_rates_fit,
        "val_selection_summary": {
            "m2_selected": {"sos_alpha": cfg_m2.sos_alpha, "elo_weight": cfg_m2.elo_weight},
            "m3_selected": {"half_life_days": cfg_m3.half_life_days, "venue_blend": cfg_m3.venue_blend},
            "m4_selected": {"shrink_k": cfg_m4.shrink_k, "rho": cfg_m4.rho},
            "m2_grid": m2_grid_results,
            "m3_grid": m3_grid_results,
            "m4_grid": m4_grid_results,
        },
        "sources": source_meta,
        "models": {},
        "paired_bootstrap_vs_m0": {},
    }

    for name, cfg in all_configs.items():
        rows, elapsed_ms, extra = _eval_dataset(test_strict, cfg, team_ordered=team_ordered, elo_tracker=elo_tracker)
        eval_rows[name] = rows
        metrics_oos["models"][name] = {
            "config": {
                "multiplicative": cfg.multiplicative,
                "use_sos_opp_rates": cfg.use_sos_opp_rates,
                "sos_alpha": cfg.sos_alpha,
                "use_elo": cfg.use_elo,
                "elo_weight": cfg.elo_weight,
                "half_life_days": cfg.half_life_days,
                "venue_blend": cfg.venue_blend,
                "shrink_k": cfg.shrink_k,
                "rho": cfg.rho,
            },
            "operational": extra,
            "ALL": _summarize_preds(rows),
            "CLUB": _summarize_preds([r for r in rows if r["stratum"] != "NATIONAL_TEAM"]),
            "NATIONAL_TEAM": _summarize_preds([r for r in rows if r["stratum"] == "NATIONAL_TEAM"]),
        }

    m0_rows = eval_rows["M0"]
    for name in all_configs:
        if name == "M0":
            continue
        alt = eval_rows[name]
        metrics_oos["paired_bootstrap_vs_m0"][name] = {
            "day_blocks": {
                "rps": _block_bootstrap_paired(alt, m0_rows, "rps", block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
                "log_loss_1x2": _block_bootstrap_paired(alt, m0_rows, "ll_1x2", block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
                "brier_1x2_per_out": _block_bootstrap_paired(alt, m0_rows, "brier_1x2_per_out", block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
                "brier_over_2_5": _block_bootstrap_paired(alt, m0_rows, "brier_o25", block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
                "brier_btts": _block_bootstrap_paired(alt, m0_rows, "brier_btts", block_mode="day", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
            },
            "week_blocks": {
                "rps": _block_bootstrap_paired(alt, m0_rows, "rps", block_mode="week", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
                "log_loss_1x2": _block_bootstrap_paired(alt, m0_rows, "ll_1x2", block_mode="week", n_boot=BOOTSTRAP_REPS, seed=SCRIPT_SEED),
            },
        }

    (out_dir / "METRICAS_OOS.json").write_text(json.dumps(metrics_oos, indent=2, ensure_ascii=False), encoding="utf-8")

    # ABLACIONES.json compacto y legible
    abl_summary = {}
    for name, md in metrics_oos["models"].items():
        all_m = md["ALL"]
        boot = metrics_oos["paired_bootstrap_vs_m0"].get(name, {}).get("day_blocks", {})
        abl_summary[name] = {
            "rps": all_m["1x2_multiclass"]["rps"],
            "log_loss_1x2": all_m["1x2_multiclass"]["log_loss"],
            "brier_1x2": all_m["1x2_multiclass"]["brier_per_outcome"],
            "home_win_ece": all_m["binary_markets"]["home_win"]["ece_5bins"],
            "over_2_5_brier": all_m["binary_markets"]["over_2_5"]["brier"],
            "btts_brier": all_m["binary_markets"]["btts_yes"]["brier"],
            "fav_ge_0_55_bias": all_m["favorite_underdog_bias_two_sides_2n"]["favorite_ge_0_55"].get("bias"),
            "bias_home_goals": all_m["goals_error"]["bias_home_pred_minus_obs"],
            "bias_away_goals": all_m["goals_error"]["bias_away_pred_minus_obs"],
            "ms_per_match": md["operational"]["ms_per_match"],
            "delta_rps_vs_m0": boot.get("rps", {}).get("mean_delta_alt_minus_ref"),
            "delta_rps_ci95": [boot.get("rps", {}).get("ci_95_low"), boot.get("rps", {}).get("ci_95_high")] if "rps" in boot else None,
            "delta_ll_vs_m0": boot.get("log_loss_1x2", {}).get("mean_delta_alt_minus_ref"),
            "delta_ll_ci95": [boot.get("log_loss_1x2", {}).get("ci_95_low"), boot.get("log_loss_1x2", {}).get("ci_95_high")] if "log_loss_1x2" in boot else None,
            "prob_improves_ll": boot.get("log_loss_1x2", {}).get("prob_alt_improves_lt_0"),
        }
    (out_dir / "ABLACIONES.json").write_text(json.dumps(abl_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # PREDICCIONES_COMPARATIVAS.csv (M0..M4 sobre los 2,319 partidos)
    csv_path = out_dir / "PREDICCIONES_COMPARATIVAS.csv"
    fnames = [
        "model", "fixture_id", "kickoff_utc", "stratum", "hg", "ag", "actual_1x2",
        "lambda_home", "lambda_away", "p_home", "p_draw", "p_away", "p_over_2_5", "p_btts_yes",
        "rps", "ll_1x2", "brier_o25", "brier_btts", "home_n_eff", "away_n_eff", "sos_coverage_rate", "elo_coverage_ok",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fnames)
        w.writeheader()
        for mname in ("M0", "M1", "M2", "M3", "M4"):
            for r in eval_rows[mname]:
                w.writerow({
                    "model": mname,
                    "fixture_id": r["fixture_id"],
                    "kickoff_utc": r["kickoff_utc"],
                    "stratum": r["stratum"],
                    "hg": r["hg"],
                    "ag": r["ag"],
                    "actual_1x2": r["y_1x2"],
                    "lambda_home": r["lambda_home"],
                    "lambda_away": r["lambda_away"],
                    "p_home": round(r["p_home"], 6),
                    "p_draw": round(r["p_draw"], 6),
                    "p_away": round(r["p_away"], 6),
                    "p_over_2_5": round(r["p_over_2_5"], 6),
                    "p_btts_yes": round(r["p_btts_yes"], 6),
                    "rps": round(r["rps"], 6),
                    "ll_1x2": round(r["ll_1x2"], 6),
                    "brier_o25": round(r["brier_o25"], 6),
                    "brier_btts": round(r["brier_btts"], 6),
                    "home_n_eff": r["home_n_eff"],
                    "away_n_eff": r["away_n_eff"],
                    "sos_coverage_rate": r["sos_coverage_rate"],
                    "elo_coverage_ok": r["elo_coverage_ok"],
                })

    # SENSIBILIDAD_7909713_MODELOS.json
    sens_7909713 = _evaluate_7909713(
        {"M0": cfg_m0, "M1": cfg_m1, "M2": cfg_m2, "M3": cfg_m3, "M4": cfg_m4},
        team_ordered=team_ordered,
        elo_tracker=elo_tracker,
    )
    (out_dir / "SENSIBILIDAD_7909713_MODELOS.json").write_text(json.dumps(sens_7909713, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(abl_summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
