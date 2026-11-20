#!/usr/bin/env python3
"""Reproducción versionada y determinista del Baseline Walk-Forward (Iteración 2.1A — Fase A).

Uso:
  PYTHONPATH=. .venv/bin/python tools/reproduce_baseline_backtest.py \
      --out-json audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json \
      --out-csv audit/iteracion_2_1a/PREDICCIONES_OOS.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.context.observation_store import HistoricalTeamMatch, ObservationStore
from bet_engine.modeling.calibration import (
    brier_score,
    evaluate_interval_calibration,
    expected_calibration_error,
    log_loss,
    multiclass_1x2_log_loss,
    parse_utc_timestamp,
    ranked_probability_score_1x2,
)
from bet_engine.modeling.distributions import (
    DEFAULT_PRIORS,
    FAMILY_DISPERSION_FLOORS,
    STRATUM_PRIORS,
    _estimate_team_rate_detailed,
    _gamma_rate_bounds,
    _resolve_prior,
    dixon_coles_joint_grid,
)

MODEL_BASELINE_VERSION = "CoherentMatchModeler_baseline_2.0_2.1A"
SCRIPT_SEED = 42
BLOCK_BOOTSTRAP_REPLICATES = 2000


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _discover_source_files(root: Path) -> list[Path]:
    recovered = root / "outputs/reconciliation_real_20261111/coverage_models_final/preselection_cache_recovered.json"
    enriched = sorted((root / "outputs/discovery").glob("*/prospective_trial/enriched/preselection.json"))
    files = [p for p in [recovered, *enriched] if p.is_file()]
    return files


def _load_corpus(
    root: Path,
    *,
    order_invariant: bool = True,
    use_league_catalog: bool = False,
) -> tuple[list[dict[str, Any]], dict[int, dict[int, HistoricalTeamMatch]], dict[int, dict[str, HistoricalTeamMatch]], dict[str, int]]:
    source_files = _discover_source_files(root)
    source_meta: list[dict[str, Any]] = []
    seen_target_fids: set[int] = set()
    raw_targets: list[tuple[dict[str, Any], Any]] = []

    for p in source_files:
        rel = str(p.relative_to(root))
        source_meta.append({"path": rel, "sha256": _sha256_file(p), "bytes": p.stat().st_size})
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("matches", raw) if isinstance(raw, dict) else raw
        for m in items:
            fid = int(m.get("fixture_id") or 0)
            if not fid or fid in seen_target_fids:
                continue
            cut = m.get("kickoff_utc") or m.get("kickoff_madrid")
            if not cut:
                continue
            seen_target_fids.add(fid)
            raw_targets.append((m, cut))

    league_catalog = (
        ObservationStore.build_league_catalog([m for m, _ in raw_targets])
        if use_league_catalog
        else None
    )
    bundles = [
        ObservationStore.from_preselection_match(m, cutoff_utc=cut, league_catalog=league_catalog)
        for m, cut in raw_targets
    ]

    team_series: dict[int, dict[int, HistoricalTeamMatch]] = {}
    all_fixtures: dict[int, dict[str, HistoricalTeamMatch]] = {}
    stratum_overwrite_prevented = 0
    for b in bundles:
        for hm in b.home_matches + b.away_matches:
            tmap = team_series.setdefault(hm.team_id, {})
            existing = tmap.get(hm.fixture_id)
            if order_invariant and existing is not None:
                merged = ObservationStore.prefer_informative_historical_match(existing, hm)
                if (existing.league_name, existing.competition_stratum) != (hm.league_name, hm.competition_stratum):
                    stratum_overwrite_prevented += 1
            else:
                merged = hm
            tmap[hm.fixture_id] = merged
            all_fixtures.setdefault(hm.fixture_id, {})[merged.venue.lower()] = merged

    meta_counts = {
        "source_files_n": len(source_meta),
        "unique_preselection_bundles_n": len(bundles),
        "total_unique_teams": len(team_series),
        "total_unique_finished_fixtures": len(all_fixtures),
        "two_sided_observed_fixtures": sum(1 for s in all_fixtures.values() if "home" in s and "away" in s),
        "stratum_overwrites_prevented_n": stratum_overwrite_prevented,
        "league_catalog_size": len(league_catalog or {}),
    }
    return source_meta, team_series, all_fixtures, meta_counts


def _extract_fixture_record(
    fid: int,
    sides: dict[str, HistoricalTeamMatch],
    team_ordered: dict[int, list[HistoricalTeamMatch]],
) -> tuple[dict[str, Any] | None, str | None]:
    if "home" in sides and "away" in sides:
        hm, am = sides["home"], sides["away"]
        hid, aid = hm.team_id, am.team_id
        ko, lid, season, stratum = hm.kickoff_utc, hm.league_id, hm.season, hm.competition_stratum
        hg, ag = hm.own_metrics.get("goals"), am.own_metrics.get("goals")
        rc_target = bool(hm.red_card_distortion or am.red_card_distortion)
        rc_statuses = {hm.red_card_status, am.red_card_status}
        red_card_status = (
            "RED_CARD_CONFIRMED"
            if ("RED_CARD_CONFIRMED" in rc_statuses or rc_target)
            else ("NO_RED_CARD_VERIFIED" if "NO_RED_CARD_VERIFIED" in rc_statuses else "RED_CARD_UNKNOWN")
        )
        side_mode = "BOTH_SIDES"
    elif "home" in sides:
        hm = sides["home"]
        hid, aid = hm.team_id, hm.opponent_id
        ko, lid, season, stratum = hm.kickoff_utc, hm.league_id, hm.season, hm.competition_stratum
        hg, ag = hm.own_metrics.get("goals"), hm.conceded_metrics.get("goals")
        rc_target = bool(hm.red_card_distortion)
        red_card_status = hm.red_card_status
        side_mode = "RECONSTRUCTED_FROM_HOME"
    elif "away" in sides:
        am = sides["away"]
        hid, aid = am.opponent_id, am.team_id
        ko, lid, season, stratum = am.kickoff_utc, am.league_id, am.season, am.competition_stratum
        hg, ag = am.conceded_metrics.get("goals"), am.own_metrics.get("goals")
        rc_target = bool(am.red_card_distortion)
        red_card_status = am.red_card_status
        side_mode = "RECONSTRUCTED_FROM_AWAY"
    else:
        return None, "NO_VALID_SIDE"

    if hg is None or ag is None:
        return None, "MISSING_TARGET_GOALS"

    t_ko = parse_utc_timestamp(ko)
    hp = [
        x
        for x in team_ordered.get(hid, ())
        if parse_utc_timestamp(x.available_at_utc) <= t_ko
        and parse_utc_timestamp(x.kickoff_utc) < t_ko
        and not x.red_card_distortion
        and x.own_metrics.get("goals") is not None
    ]
    ap = [
        x
        for x in team_ordered.get(aid, ())
        if parse_utc_timestamp(x.available_at_utc) <= t_ko
        and parse_utc_timestamp(x.kickoff_utc) < t_ko
        and not x.red_card_distortion
        and x.own_metrics.get("goals") is not None
    ]
    if len(hp) < 6 or len(ap) < 6:
        return None, "PRIOR_HISTORY_LT_6"

    return {
        "fixture_id": fid,
        "kickoff_dt": t_ko,
        "kickoff_utc": t_ko.isoformat(),
        "cutoff_utc": t_ko.isoformat(),
        "home_id": hid,
        "away_id": aid,
        "league_id": lid,
        "season": season,
        "stratum": stratum,
        "hg": float(hg),
        "ag": float(ag),
        "rc_target": rc_target,
        "red_card_status": red_card_status,
        "red_card_verified": red_card_status in {"RED_CARD_CONFIRMED", "NO_RED_CARD_VERIFIED"},
        "side_mode": side_mode,
        "hp_rev": tuple(reversed(hp)),
        "ap_rev": tuple(reversed(ap)),
    }, None


def _quantile_range_from_pmf(pmf: tuple[float, ...], q_lo: float = 0.10, q_hi: float = 0.90) -> tuple[int, int]:
    cdf = 0.0
    lo_k, hi_k = 0, len(pmf) - 1
    found_lo = False
    for k, p in enumerate(pmf):
        cdf += p
        if not found_lo and cdf >= q_lo:
            lo_k = k
            found_lo = True
        if cdf >= q_hi:
            hi_k = k
            break
    return lo_k, hi_k


def _predict_fixture(
    rec: dict[str, Any],
    window: int,
    *,
    max_goals: int = 12,
    strict_competition_identity: bool = False,
) -> dict[str, Any]:
    lid, season, stratum = rec["league_id"], rec["season"], rec["stratum"]
    hp_rev, ap_rev = rec["hp_rev"], rec["ap_rev"]

    eh = _estimate_team_rate_detailed(
        hp_rev,
        ap_rev,
        metric="goals",
        target_league_id=lid,
        target_season=season,
        target_stratum=stratum,
        own_venue="HOME",
        opp_venue="AWAY",
        prior_mean=_resolve_prior("goals", stratum, "HOME"),
        history_limit=window,
        strict_competition_identity=strict_competition_identity,
    )
    ea = _estimate_team_rate_detailed(
        ap_rev,
        hp_rev,
        metric="goals",
        target_league_id=lid,
        target_season=season,
        target_stratum=stratum,
        own_venue="AWAY",
        opp_venue="HOME",
        prior_mean=_resolve_prior("goals", stratum, "AWAY"),
        history_limit=window,
        strict_competition_identity=strict_competition_identity,
    )
    lh, la = float(eh["lambda"]), float(ea["lambda"])
    grid = dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=max_goals)

    n_post_h = (eh["n_eff"] + 8.0) * (0.85 if eh.get("competition_mix_fallback") else 1.0)
    n_post_a = (ea["n_eff"] + 8.0) * (0.85 if ea.get("competition_mix_fallback") else 1.0)
    lh_lo, lh_hi = _gamma_rate_bounds(lh, n_post_h, eh["phi"])
    la_lo, la_hi = _gamma_rate_bounds(la, n_post_a, ea["phi"])

    def _from_grid(g: list[list[float]]) -> dict[str, float]:
        mg = len(g) - 1
        p1 = sum(g[i][j] for i in range(mg + 1) for j in range(mg + 1) if i > j)
        px = sum(g[i][i] for i in range(mg + 1))
        p2 = sum(g[i][j] for i in range(mg + 1) for j in range(mg + 1) if i < j)
        po25 = sum(g[i][j] for i in range(mg + 1) for j in range(mg + 1) if i + j > 2)
        pbtts = sum(g[i][j] for i in range(1, mg + 1) for j in range(1, mg + 1))
        return {"home_win": p1, "draw": px, "away_win": p2, "over_2_5": po25, "btts_yes": pbtts}

    pt = _from_grid(grid)
    corners = [_from_grid(dixon_coles_joint_grid(a, b, rho=-0.04, max_goals=max_goals)) for a in (lh_lo, lh_hi) for b in (la_lo, la_hi)] + [pt]

    # Distribución marginal de goles totales desde la matriz conjunta
    tot_pmf_list = [0.0] * (2 * max_goals + 1)
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            tot_pmf_list[i + j] += grid[i][j]
    q10_tot, q90_tot = _quantile_range_from_pmf(tuple(tot_pmf_list), 0.10, 0.90)

    hg, ag = rec["hg"], rec["ag"]
    y_1x2 = "HOME" if hg > ag else ("DRAW" if hg == ag else "AWAY")
    y_hw = 1 if hg > ag else 0
    y_dr = 1 if hg == ag else 0
    y_aw = 1 if hg < ag else 0
    y_o25 = 1 if (hg + ag) > 2.5 else 0
    y_btts = 1 if (hg > 0 and ag > 0) else 0

    rps = ((pt["home_win"] - y_hw) ** 2 + ((pt["home_win"] + pt["draw"]) - (y_hw + y_dr)) ** 2) / 2.0
    eps = 1e-6
    ll_1x2 = -math.log(max(eps, pt["home_win"] if y_1x2 == "HOME" else (pt["draw"] if y_1x2 == "DRAW" else pt["away_win"])))
    brier_1x2_per_out = ((pt["home_win"] - y_hw) ** 2 + (pt["draw"] - y_dr) ** 2 + (pt["away_win"] - y_aw) ** 2) / 3.0

    return {
        "fixture_id": rec["fixture_id"],
        "kickoff_utc": rec["kickoff_utc"],
        "stratum": stratum,
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
        "p_home": pt["home_win"],
        "p_draw": pt["draw"],
        "p_away": pt["away_win"],
        "p_over_2_5": pt["over_2_5"],
        "p_btts_yes": pt["btts_yes"],
        "hw_lo": min(c["home_win"] for c in corners),
        "hw_hi": max(c["home_win"] for c in corners),
        "o25_lo": min(c["over_2_5"] for c in corners),
        "o25_hi": max(c["over_2_5"] for c in corners),
        "btts_lo": min(c["btts_yes"] for c in corners),
        "btts_hi": max(c["btts_yes"] for c in corners),
        "q10_total": q10_tot,
        "q90_total": q90_tot,
        "rps": rps,
        "ll_1x2": ll_1x2,
        "brier_1x2_per_out": brier_1x2_per_out,
        "brier_o25": (pt["over_2_5"] - y_o25) ** 2,
        "ll_o25": -math.log(max(eps, pt["over_2_5"] if y_o25 == 1 else (1.0 - pt["over_2_5"]))),
        "brier_btts": (pt["btts_yes"] - y_btts) ** 2,
        "ll_btts": -math.log(max(eps, pt["btts_yes"] if y_btts == 1 else (1.0 - pt["btts_yes"]))),
        "used_home_n": min(window, len(hp_rev)),
        "used_away_n": min(window, len(ap_rev)),
        "last_home_avail_utc": hp_rev[0].available_at_utc,
        "last_away_avail_utc": ap_rev[0].available_at_utc,
        "home_n_eff": eh["n_eff"],
        "away_n_eff": ea["n_eff"],
    }


def _summarize_preds(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    n = len(rows)
    hw_pairs = [(r["p_home"], r["y_hw"]) for r in rows]
    dr_pairs = [(r["p_draw"], r["y_dr"]) for r in rows]
    aw_pairs = [(r["p_away"], r["y_aw"]) for r in rows]
    o25_pairs = [(r["p_over_2_5"], r["y_o25"]) for r in rows]
    btts_pairs = [(r["p_btts_yes"], r["y_btts"]) for r in rows]

    def _mkt(pairs: list[tuple[float, int]]) -> dict[str, Any]:
        return {
            "brier": brier_score(pairs),
            "log_loss": log_loss(pairs),
            "ece_5bins": expected_calibration_error(pairs, n_bins=5),
            "pred_mean": round(statistics.fmean(p for p, _ in pairs), 4),
            "obs_rate": round(statistics.fmean(y for _, y in pairs), 4),
        }

    # Evaluación por separado de lado local/visitante y agregación 2*n claramente etiquetada
    side_obs = [(r["p_home"], r["y_hw"]) for r in rows] + [(r["p_away"], r["y_aw"]) for r in rows]

    def _band_stats(items: list[tuple[float, int]]) -> dict[str, Any]:
        if not items:
            return {"n": 0}
        mp = statistics.fmean(p for p, _ in items)
        obs = statistics.fmean(y for _, y in items)
        return {"n": len(items), "pred_mean": round(mp, 4), "obs_rate": round(obs, 4), "bias": round(mp - obs, 4)}

    hw_iv = evaluate_interval_calibration([(r["p_home"], r["hw_lo"], r["hw_hi"], r["y_hw"]) for r in rows])
    o25_iv = evaluate_interval_calibration(
        [(r["p_over_2_5"], r["o25_lo"], r["o25_hi"], r["y_o25"]) for r in rows],
        count_realizations=[(r["q10_total"], r["q90_total"], r["hg"] + r["ag"]) for r in rows],
    )
    btts_iv = evaluate_interval_calibration([(r["p_btts_yes"], r["btts_lo"], r["btts_hi"], r["y_btts"]) for r in rows])

    return {
        "n_fixtures": n,
        "1x2_multiclass": {
            "brier_per_outcome": round(statistics.fmean(r["brier_1x2_per_out"] for r in rows), 5),
            "log_loss": round(statistics.fmean(r["ll_1x2"] for r in rows), 5),
            "rps": round(statistics.fmean(r["rps"] for r in rows), 5),
        },
        "binary_markets": {
            "home_win": _mkt(hw_pairs),
            "draw": _mkt(dr_pairs),
            "away_win": _mkt(aw_pairs),
            "over_2_5": _mkt(o25_pairs),
            "btts_yes": _mkt(btts_pairs),
        },
        "favorite_underdog_bias_two_sides_2n": {
            "note": "Cada fixture aporta 2 observaciones binarias (P(home_win) y P(away_win)), sumando 2 * n_fixtures.",
            "n_side_observations": len(side_obs),
            "favorite_ge_0_55": _band_stats([(p, y) for p, y in side_obs if p >= 0.55]),
            "balanced_0_35_to_0_55": _band_stats([(p, y) for p, y in side_obs if 0.35 <= p < 0.55]),
            "underdog_lt_0_35": _band_stats([(p, y) for p, y in side_obs if p < 0.35]),
            "home_only_ge_0_55": _band_stats([(p, y) for p, y in hw_pairs if p >= 0.55]),
            "away_only_ge_0_55": _band_stats([(p, y) for p, y in aw_pairs if p >= 0.55]),
        },
        "goals_error": {
            "mae_home": round(statistics.fmean(abs(r["lambda_home"] - r["hg"]) for r in rows), 4),
            "mae_away": round(statistics.fmean(abs(r["lambda_away"] - r["ag"]) for r in rows), 4),
            "mae_total": round(statistics.fmean(abs((r["lambda_home"] + r["lambda_away"]) - (r["hg"] + r["ag"])) for r in rows), 4),
            "rmse_total": round(math.sqrt(statistics.fmean(((r["lambda_home"] + r["lambda_away"]) - (r["hg"] + r["ag"])) ** 2 for r in rows)), 4),
            "bias_home_pred_minus_obs": round(statistics.fmean(r["lambda_home"] - r["hg"] for r in rows), 4),
            "bias_away_pred_minus_obs": round(statistics.fmean(r["lambda_away"] - r["ag"] for r in rows), 4),
        },
        "interval_diagnostics": {
            "home_win_rate_bounds": {k: v for k, v in hw_iv.items() if k != "bins"},
            "over_2_5_rate_bounds_and_total_q80": {k: v for k, v in o25_iv.items() if k != "bins"},
            "btts_yes_rate_bounds": {k: v for k, v in btts_iv.items() if k != "bins"},
        },
    }


def _block_bootstrap_paired(
    alt_rows: list[dict[str, Any]],
    ref_rows: list[dict[str, Any]],
    metric_key: str,
    *,
    block_mode: str = "day",
    n_boot: int = BLOCK_BOOTSTRAP_REPLICATES,
    seed: int = SCRIPT_SEED,
) -> dict[str, Any]:
    """Bootstrap pareado por bloques temporales (por día UTC o semana ISO)."""
    assert len(alt_rows) == len(ref_rows)
    blocks: dict[str, list[float]] = {}
    all_deltas: list[float] = []
    for a, r in zip(alt_rows, ref_rows):
        assert a["fixture_id"] == r["fixture_id"]
        d = float(a[metric_key]) - float(r[metric_key])
        all_deltas.append(d)
        dt = parse_utc_timestamp(a["kickoff_utc"])
        bkey = dt.strftime("%Y-%m-%d") if block_mode == "day" else f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        blocks.setdefault(bkey, []).append(d)

    block_keys = sorted(blocks.keys())
    block_sums = [(sum(blocks[k]), len(blocks[k])) for k in block_keys]
    rng = random.Random(seed)
    m_blocks = len(block_sums)
    boot_means: list[float] = []
    for _ in range(n_boot):
        s_val = 0.0
        n_val = 0
        for _ in range(m_blocks):
            bs, bn = block_sums[rng.randrange(m_blocks)]
            s_val += bs
            n_val += bn
        boot_means.append(s_val / max(1, n_val))
    boot_means.sort()
    lo = boot_means[int(0.025 * (n_boot - 1))]
    hi = boot_means[int(0.975 * (n_boot - 1))]
    p_imp = sum(1 for v in boot_means if v < 0.0) / n_boot
    mean_d = sum(all_deltas) / len(all_deltas)
    return {
        "n_fixtures": len(all_deltas),
        "n_blocks": m_blocks,
        "block_unit": block_mode,
        "mean_delta_alt_minus_ref": round(mean_d, 6),
        "ci_95_low": round(lo, 6),
        "ci_95_high": round(hi, 6),
        "prob_alt_improves_lt_0": round(p_imp, 4),
    }


def run_reproducible_baseline(
    root: Path,
    out_json: Path,
    out_csv: Path,
    *,
    order_invariant: bool = False,
    use_league_catalog: bool = False,
    strict_competition_identity: bool = False,
) -> dict[str, Any]:
    source_meta, team_series, all_fixtures, meta_counts = _load_corpus(
        root,
        order_invariant=order_invariant,
        use_league_catalog=use_league_catalog,
    )
    team_ordered = {
        tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
        for tid, fmap in team_series.items()
    }

    exclusions: dict[str, int] = {}
    records_all_valid: list[dict[str, Any]] = []
    for fid in sorted(all_fixtures.keys()):
        rec, reason = _extract_fixture_record(fid, all_fixtures[fid], team_ordered)
        if reason is not None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        assert rec is not None
        records_all_valid.append(rec)

    records_all_valid.sort(key=lambda r: (r["kickoff_dt"], r["fixture_id"]))
    records_no_rc = [r for r in records_all_valid if not r["rc_target"]]
    rc_target_count = len(records_all_valid) - len(records_no_rc)

    # Split 1: Reproducción exacta del corte n=2248 / n=2248 (sobre 4496 con desempate determinista (kickoff, fid))
    half = len(records_all_valid) // 2
    train_2248 = records_all_valid[:half]
    test_2248 = records_all_valid[half:]
    boundary_iso = test_2248[0]["kickoff_utc"]
    tied_at_boundary = [r["fixture_id"] for r in records_all_valid if r["kickoff_utc"] == boundary_iso]

    # Split 2: Corte temporal ESTRICTO sin empates atravesando frontera y excluyendo los 7 target con roja
    # Todos los fixtures con kickoff_utc >= boundary_iso pasan a test (max(train.ko) < min(test.ko))
    train_strict = [r for r in records_no_rc if r["kickoff_utc"] < boundary_iso]
    test_strict = [r for r in records_no_rc if r["kickoff_utc"] >= boundary_iso]
    assert not ({r["fixture_id"] for r in train_strict} & {r["fixture_id"] for r in test_strict})
    assert train_strict[-1]["kickoff_dt"] < test_strict[0]["kickoff_dt"]

    def _base_rates(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for label, sub in [
            ("ALL", rows),
            ("CLUB", [r for r in rows if r["stratum"] != "NATIONAL_TEAM"]),
            ("NATIONAL_TEAM", [r for r in rows if r["stratum"] == "NATIONAL_TEAM"]),
        ]:
            out[label] = [round(statistics.fmean(r["hg"] for r in sub), 4), round(statistics.fmean(r["ag"] for r in sub), 4)] if sub else []
        return out

    preds_2248: dict[int, list[dict[str, Any]]] = {
        w: [_predict_fixture(r, w, strict_competition_identity=strict_competition_identity) for r in test_2248]
        for w in (6, 10, 20)
    }
    preds_strict: dict[int, list[dict[str, Any]]] = {
        w: [_predict_fixture(r, w, strict_competition_identity=strict_competition_identity) for r in test_strict]
        for w in (6, 10, 20)
    }

    def _build_split_block(rows_dict: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
        blk: dict[str, Any] = {}
        for w in (6, 10, 20):
            rows = rows_dict[w]
            blk[f"L{w}"] = {
                "ALL": _summarize_preds(rows),
                "CLUB": _summarize_preds([r for r in rows if r["stratum"] != "NATIONAL_TEAM"]),
                "NATIONAL_TEAM": _summarize_preds([r for r in rows if r["stratum"] == "NATIONAL_TEAM"]),
            }
        blk["paired_block_bootstrap"] = {}
        for label, alt_w, ref_w in [("L10_vs_L6", 10, 6), ("L20_vs_L10", 20, 10), ("L20_vs_L6", 20, 6)]:
            blk["paired_block_bootstrap"][label] = {
                "day_blocks": {
                    "rps": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "rps", block_mode="day"),
                    "log_loss_1x2": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "ll_1x2", block_mode="day"),
                    "brier_1x2_per_out": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "brier_1x2_per_out", block_mode="day"),
                    "brier_over_2_5": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "brier_o25", block_mode="day"),
                    "brier_btts": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "brier_btts", block_mode="day"),
                },
                "week_blocks": {
                    "rps": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "rps", block_mode="week"),
                    "log_loss_1x2": _block_bootstrap_paired(rows_dict[alt_w], rows_dict[ref_w], "ll_1x2", block_mode="week"),
                },
            }
        return blk

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_BASELINE_VERSION,
        "reproducibility": {
            "command": "PYTHONPATH=. .venv/bin/python tools/reproduce_baseline_backtest.py --out-json audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json --out-csv audit/iteracion_2_1a/PREDICCIONES_OOS.csv",
            "seed": SCRIPT_SEED,
            "bootstrap_replicates": BLOCK_BOOTSTRAP_REPLICATES,
            "model_parameters": {
                "default_priors": DEFAULT_PRIORS,
                "stratum_priors": STRATUM_PRIORS,
                "dispersion_floors": FAMILY_DISPERSION_FLOORS,
                "shrink_k": 8.0,
                "phi_k": 12.0,
                "own_opp_weights": [0.55, 0.45],
                "dixon_coles_rho": -0.04,
                "max_goals": 12,
                "red_card_policy": "exclude_any",
            },
            "sources": source_meta,
        },
        "corpus_summary": {
            **meta_counts,
            "eligible_min6_including_rc_target": len(records_all_valid),
            "target_red_card_excluded_for_strict": rc_target_count,
            "eligible_min6_clean_strict": len(records_no_rc),
            "exclusions": exclusions,
            "side_reconstruction_counts_in_4496": {
                k: sum(1 for r in records_all_valid if r["side_mode"] == k)
                for k in ("BOTH_SIDES", "RECONSTRUCTED_FROM_HOME", "RECONSTRUCTED_FROM_AWAY")
            },
        },
        "boundary_audit": {
            "naive_half_boundary_kickoff_utc": boundary_iso,
            "fixtures_sharing_exact_boundary_kickoff": len(tied_at_boundary),
            "split_2248_disjoint_fids": len({r["fixture_id"] for r in train_2248} & {r["fixture_id"] for r in test_2248}) == 0,
            "split_2248_crosses_same_kickoff": train_2248[-1]["kickoff_dt"] == test_2248[0]["kickoff_dt"],
            "split_strict_disjoint_fids": len({r["fixture_id"] for r in train_strict} & {r["fixture_id"] for r in test_strict}) == 0,
            "split_strict_max_train_lt_min_test": train_strict[-1]["kickoff_dt"] < test_strict[0]["kickoff_dt"],
            "split_strict_train_n": len(train_strict),
            "split_strict_test_n": len(test_strict),
            "split_strict_train_range": [train_strict[0]["kickoff_utc"], train_strict[-1]["kickoff_utc"]],
            "split_strict_test_range": [test_strict[0]["kickoff_utc"], test_strict[-1]["kickoff_utc"]],
        },
        "train_base_rates_2248": _base_rates(train_2248),
        "train_base_rates_strict": _base_rates(train_strict),
        "split_2248_deterministic": _build_split_block(preds_2248),
        "split_strict_no_boundary_tie_no_rc": _build_split_block(preds_strict),
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # Exportar CSV por fixture+ventana sobre los 2,248 + flag in_strict_test
    strict_fids = {r["fixture_id"] for r in test_strict}
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "fixture_id", "kickoff_utc", "cutoff_utc", "stratum", "in_split_2248_test", "in_split_strict_test",
        "rc_target", "side_mode", "home_id", "away_id", "hg", "ag", "actual_1x2", "actual_o25", "actual_btts",
        "window", "used_home_n", "used_away_n", "last_home_avail_utc", "last_away_avail_utc",
        "home_n_eff", "away_n_eff", "lambda_home", "lambda_away",
        "p_home", "p_draw", "p_away", "p_over_2_5", "p_btts_yes",
        "rps", "ll_1x2", "brier_1x2_per_out", "brier_o25", "brier_btts",
    ]
    rec_by_fid = {r["fixture_id"]: r for r in records_all_valid}
    all_test_fids = sorted({r["fixture_id"] for r in test_2248} | strict_fids, key=lambda f: (rec_by_fid[f]["kickoff_dt"], f))
    test_2248_fids = {r["fixture_id"] for r in test_2248}
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for win in (6, 10, 20):
            # Precalcular para cualquier fixture de union
            pred_map = {r["fixture_id"]: r for r in preds_2248[win]}
            for fid in all_test_fids:
                r = pred_map.get(fid)
                if r is None:
                    r = _predict_fixture(rec_by_fid[fid], win)
                base_rec = rec_by_fid[fid]
                w.writerow({
                    "fixture_id": fid,
                    "kickoff_utc": base_rec["kickoff_utc"],
                    "cutoff_utc": base_rec["cutoff_utc"],
                    "stratum": base_rec["stratum"],
                    "in_split_2248_test": fid in test_2248_fids,
                    "in_split_strict_test": fid in strict_fids,
                    "rc_target": base_rec["rc_target"],
                    "side_mode": base_rec["side_mode"],
                    "home_id": base_rec["home_id"],
                    "away_id": base_rec["away_id"],
                    "hg": r["hg"],
                    "ag": r["ag"],
                    "actual_1x2": r["y_1x2"],
                    "actual_o25": r["y_o25"],
                    "actual_btts": r["y_btts"],
                    "window": f"L{win}",
                    "used_home_n": r["used_home_n"],
                    "used_away_n": r["used_away_n"],
                    "last_home_avail_utc": r["last_home_avail_utc"],
                    "last_away_avail_utc": r["last_away_avail_utc"],
                    "home_n_eff": r["home_n_eff"],
                    "away_n_eff": r["away_n_eff"],
                    "lambda_home": round(r["lambda_home"], 5),
                    "lambda_away": round(r["lambda_away"], 5),
                    "p_home": round(r["p_home"], 6),
                    "p_draw": round(r["p_draw"], 6),
                    "p_away": round(r["p_away"], 6),
                    "p_over_2_5": round(r["p_over_2_5"], 6),
                    "p_btts_yes": round(r["p_btts_yes"], 6),
                    "rps": round(r["rps"], 6),
                    "ll_1x2": round(r["ll_1x2"], 6),
                    "brier_1x2_per_out": round(r["brier_1x2_per_out"], 6),
                    "brier_o25": round(r["brier_o25"], 6),
                    "brier_btts": round(r["brier_btts"], 6),
                })

    return report


def run_protocol_2b2a_audit(root: Path, out_json: Path) -> dict[str, Any]:
    """Ejecuta la comparación completa 2B.2A sin alterar el baseline congelado."""

    def _extract_all(order_inv: bool, use_cat: bool) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
        source_meta, team_series, all_fixtures, meta_counts = _load_corpus(
            root, order_invariant=order_inv, use_league_catalog=use_cat
        )
        team_ordered = {
            tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
            for tid, fmap in team_series.items()
        }
        exclusions: dict[str, int] = {}
        recs: list[dict[str, Any]] = []
        for fid in sorted(all_fixtures.keys()):
            rec, reason = _extract_fixture_record(fid, all_fixtures[fid], team_ordered)
            if reason is not None:
                exclusions[reason] = exclusions.get(reason, 0) + 1
                continue
            assert rec is not None
            recs.append(rec)
        recs.sort(key=lambda r: (r["kickoff_dt"], r["fixture_id"]))
        return recs, meta_counts, {"exclusions": exclusions, "sources": source_meta}

    boundary_iso = "2026-09-26T14:00:00+00:00"

    def _eval_slice(
        recs: list[dict[str, Any]],
        *,
        strict_comp: bool,
        max_goals: int = 12,
        exclude_target_rc: bool = False,
    ) -> dict[str, Any]:
        used = [r for r in recs if not (exclude_target_rc and r["rc_target"])]
        tr = [r for r in used if r["kickoff_utc"] < boundary_iso]
        te = [r for r in used if r["kickoff_utc"] >= boundary_iso]
        preds_te = [_predict_fixture(r, 20, max_goals=max_goals, strict_competition_identity=strict_comp) for r in te]
        preds_tr = [_predict_fixture(r, 20, max_goals=max_goals, strict_competition_identity=strict_comp) for r in tr]
        rc_counts = {
            st: sum(1 for r in used if r["red_card_status"] == st)
            for st in ("RED_CARD_CONFIRMED", "NO_RED_CARD_VERIFIED", "RED_CARD_UNKNOWN")
        }
        stratum_counts: dict[str, int] = {}
        for r in used:
            stratum_counts[r["stratum"]] = stratum_counts.get(r["stratum"], 0) + 1
        return {
            "total_n": len(used),
            "train_n": len(tr),
            "test_n": len(te),
            "target_red_card_excluded_n": len(recs) - len(used),
            "red_card_status_counts": rc_counts,
            "stratum_counts": stratum_counts,
            "train_L20_ALL": _summarize_preds(preds_tr),
            "test_L20_ALL": _summarize_preds(preds_te),
            "test_L20_NATIONAL_TEAM": _summarize_preds([p for p in preds_te if p["stratum"] == "NATIONAL_TEAM"]),
            "_test_rows": preds_te,
        }

    recs_legacy, meta_legacy, extra_legacy = _extract_all(False, False)
    recs_clean, meta_clean, extra_clean = _extract_all(True, False)
    recs_cat, meta_cat, extra_cat = _extract_all(True, True)

    frozen_sec = _eval_slice(recs_legacy, strict_comp=False, max_goals=12, exclude_target_rc=True)
    frozen_pre = _eval_slice(recs_legacy, strict_comp=False, max_goals=12, exclude_target_rc=False)
    clean_pre = _eval_slice(recs_clean, strict_comp=False, max_goals=12, exclude_target_rc=False)
    clean_sec = _eval_slice(recs_clean, strict_comp=False, max_goals=12, exclude_target_rc=True)
    variant_comp = _eval_slice(recs_cat, strict_comp=True, max_goals=12, exclude_target_rc=False)
    clean_mg10 = _eval_slice(recs_clean, strict_comp=False, max_goals=10, exclude_target_rc=False)

    # Paridad numérica max_goals=10 vs max_goals=12 sobre las mismas 2,323 filas de test
    r12 = clean_pre.pop("_test_rows")
    r10 = clean_mg10.pop("_test_rows")
    v_rows = variant_comp.pop("_test_rows")
    frozen_sec.pop("_test_rows")
    frozen_pre.pop("_test_rows")
    clean_sec.pop("_test_rows")

    max_diff_hw = max(abs(a["p_home"] - b["p_home"]) for a, b in zip(r10, r12))
    max_diff_o25 = max(abs(a["p_over_2_5"] - b["p_over_2_5"]) for a, b in zip(r10, r12))
    max_diff_btts = max(abs(a["p_btts_yes"] - b["p_btts_yes"]) for a, b in zip(r10, r12))

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_version": MODEL_BASELINE_VERSION,
        "isolated_variant_version": "CoherentMatchModeler_comp_identity_2b2a_v1",
        "promoted_to_production": False,
        "accredited_family": False,
        "boundary_utc": boundary_iso,
        "corpus_integrity": {
            "legacy_last_wins": {**meta_legacy, **extra_legacy["exclusions"]},
            "clean_order_invariant": {**meta_clean, **extra_clean["exclusions"]},
            "clean_with_league_catalog": {**meta_cat, **extra_cat["exclusions"]},
        },
        "frozen_baseline_2_1a_secondary_4489": frozen_sec,
        "frozen_baseline_prematch_4496": frozen_pre,
        "protocol_2b2a_principal_prematch_4496": clean_pre,
        "protocol_2b2a_secondary_no_rc_4489": clean_sec,
        "isolated_variant_strict_comp_identity_4496": {
            **variant_comp,
            "paired_delta_vs_clean_principal_day_blocks": {
                "rps": _block_bootstrap_paired(v_rows, r12, "rps", block_mode="day"),
                "log_loss_1x2": _block_bootstrap_paired(v_rows, r12, "ll_1x2", block_mode="day"),
                "brier_1x2_per_out": _block_bootstrap_paired(v_rows, r12, "brier_1x2_per_out", block_mode="day"),
            },
        },
        "evaluator_consistency_max_goals_10_vs_12": {
            "test_n": len(r12),
            "mg10_rps": clean_mg10["test_L20_ALL"]["1x2_multiclass"]["rps"],
            "mg12_rps": clean_pre["test_L20_ALL"]["1x2_multiclass"]["rps"],
            "mg10_ll_1x2": clean_mg10["test_L20_ALL"]["1x2_multiclass"]["log_loss"],
            "mg12_ll_1x2": clean_pre["test_L20_ALL"]["1x2_multiclass"]["log_loss"],
            "max_abs_diff_home_win": round(max_diff_hw, 8),
            "max_abs_diff_over_2_5": round(max_diff_o25, 8),
            "max_abs_diff_btts_yes": round(max_diff_btts, 8),
        },
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenera el baseline reproducible Walk-Forward y protocolo 2B.2A")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--protocol", choices=("legacy_2_1a", "2b2a"), default="legacy_2_1a")
    parser.add_argument("--out-json", type=Path, default=Path("audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json"))
    parser.add_argument("--out-csv", type=Path, default=Path("audit/iteracion_2_1a/PREDICCIONES_OOS.csv"))
    args = parser.parse_args()
    if args.protocol == "2b2a":
        out_p = args.out_json
        if str(out_p) == "audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json":
            out_p = Path("audit/fase2b_backtesting/PROTOCOL_2B2A_COMPARISON.json")
        rep = run_protocol_2b2a_audit(args.root.resolve(), out_p)
        print(json.dumps({
            "protocol": "2b2a",
            "frozen_4489_rps": rep["frozen_baseline_2_1a_secondary_4489"]["test_L20_ALL"]["1x2_multiclass"]["rps"],
            "principal_4496_rps": rep["protocol_2b2a_principal_prematch_4496"]["test_L20_ALL"]["1x2_multiclass"]["rps"],
            "variant_comp_rps": rep["isolated_variant_strict_comp_identity_4496"]["test_L20_ALL"]["1x2_multiclass"]["rps"],
            "overwrites_prevented": rep["corpus_integrity"]["clean_order_invariant"]["stratum_overwrites_prevented_n"],
            "out": str(out_p),
        }))
        return 0
    rep = run_reproducible_baseline(args.root.resolve(), args.out_json, args.out_csv)
    print(json.dumps({
        "model_version": rep["model_version"],
        "eligible_4496": rep["corpus_summary"]["eligible_min6_including_rc_target"],
        "eligible_strict": rep["corpus_summary"]["eligible_min6_clean_strict"],
        "split_2248_L20_rps": rep["split_2248_deterministic"]["L20"]["ALL"]["1x2_multiclass"]["rps"],
        "split_strict_L20_rps": rep["split_strict_no_boundary_tie_no_rc"]["L20"]["ALL"]["1x2_multiclass"]["rps"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
