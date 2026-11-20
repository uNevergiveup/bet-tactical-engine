#!/usr/bin/env python3
"""Auditoría metodológica aislada y diagnóstica de sesgos (Fase 2B.2).

No modifica modelos de producción ni expedientes PREMATCH cerrados.
Reproduce predicciones de CoherentMatchModeler_baseline_2.0_2.1A sobre train y test,
auditando tarjetas rojas, sesgos por segmento, incertidumbre por block bootstrap,
representatividad de muestra (incluyendo min>=20) y consistencia del ObservationStore.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bet_engine.context.observation_store import HistoricalTeamMatch, ObservationStore
from bet_engine.modeling.calibration import (
    brier_score,
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
    _resolve_prior,
    dixon_coles_joint_grid,
)
from tools.reproduce_baseline_backtest import _discover_source_files, _extract_fixture_record

MODEL_VERSION = "CoherentMatchModeler_baseline_2.0_2.1A"
BOUNDARY_ISO = "2026-09-26T14:00:00+00:00"
SEED = 42
BOOT_REPLICATES = 2000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_and_audit_corpus(root: Path) -> dict[str, Any]:
    files = _discover_source_files(root)
    seen_targets: set[int] = set()
    bundles = []
    league_catalog: dict[int, dict[str, Any]] = {}
    obs_seen_meta: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)

    # Inspección cruda de targets y observaciones
    raw_target_rows = 0
    raw_target_status = Counter()
    raw_obs_rows = 0
    raw_obs_red_present = 0
    raw_obs_yellow_present = 0
    raw_obs_league_name_present = 0
    raw_obs_country_present = 0

    for p in files:
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("matches", raw) if isinstance(raw, dict) else raw
        for m in items:
            raw_target_rows += 1
            lid = int(m.get("league_id") or 0)
            if lid and lid not in league_catalog:
                league_catalog[lid] = {
                    "league_name": m.get("league"),
                    "country": m.get("country"),
                    "league_type": m.get("league_type"),
                }
            for side in ("home", "away"):
                for r in (m.get("statistical_observations") or {}).get(side) or []:
                    raw_obs_rows += 1
                    own = r.get("own") or {}
                    if own.get("red") is not None:
                        raw_obs_red_present += 1
                    if own.get("yellow") is not None:
                        raw_obs_yellow_present += 1
                    if r.get("league_name"):
                        raw_obs_league_name_present += 1
                    if r.get("country"):
                        raw_obs_country_present += 1

            fid = int(m.get("fixture_id") or 0)
            if not fid or fid in seen_targets:
                continue
            cut = m.get("kickoff_utc") or m.get("kickoff_madrid")
            if not cut:
                continue
            seen_targets.add(fid)
            raw_target_status[str(m.get("status") or "MISSING")] += 1
            b = ObservationStore.from_preselection_match(m, cutoff_utc=cut)
            bundles.append((b, p.name, int(m.get("league_id") or 0), m.get("league"), m.get("country")))

    team_series: dict[int, dict[int, HistoricalTeamMatch]] = {}
    all_fixtures: dict[int, dict[str, HistoricalTeamMatch]] = {}

    for b, fname, t_lid, t_lname, t_country in bundles:
        for hm in b.home_matches + b.away_matches:
            key = (hm.team_id, hm.fixture_id)
            obs_seen_meta[key].append(
                {
                    "target_fid": b.fixture_id,
                    "target_lid": t_lid,
                    "stratum": hm.competition_stratum,
                    "league_name": hm.league_name,
                    "red_distortion": hm.red_card_distortion,
                    "red_status": (hm.red_card_detail or {}).get("red_card_status"),
                    "goals_own": hm.own_metrics.get("goals"),
                    "goals_conc": hm.conceded_metrics.get("goals"),
                }
            )
            team_series.setdefault(hm.team_id, {})[hm.fixture_id] = hm
            all_fixtures.setdefault(hm.fixture_id, {})[hm.venue.lower()] = hm

    stratum_overwrite_keys = 0
    red_status_overwrite_keys = 0
    score_conflicts = 0
    for entries in obs_seen_meta.values():
        if len({(e["stratum"], e["league_name"]) for e in entries}) > 1:
            stratum_overwrite_keys += 1
        if len({(e["red_distortion"], e["red_status"]) for e in entries}) > 1:
            red_status_overwrite_keys += 1
        if len({(e["goals_own"], e["goals_conc"]) for e in entries}) > 1:
            score_conflicts += 1

    # Orden determinista idéntico a reproduce_baseline_backtest.py
    team_ordered = {
        tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
        for tid, fmap in team_series.items()
    }

    records_all_valid: list[dict[str, Any]] = []
    exclusions = Counter()
    for fid in sorted(all_fixtures.keys()):
        rec, reason = _extract_fixture_record(fid, all_fixtures[fid], team_ordered)
        if reason is not None:
            exclusions[reason] += 1
            continue
        assert rec is not None
        sides = all_fixtures[fid]
        red_verified = any((s.red_card_detail or {}).get("red_card_verified") for s in sides.values())
        red_statuses = sorted({str((s.red_card_detail or {}).get("red_card_status")) for s in sides.values()})
        rec["red_verified"] = red_verified
        rec["red_statuses"] = red_statuses
        rec["hp_len"] = len(rec["hp_rev"])
        rec["ap_len"] = len(rec["ap_rev"])
        rec["min_prior"] = min(rec["hp_len"], rec["ap_len"])
        rec["max_prior"] = max(rec["hp_len"], rec["ap_len"])
        cat = league_catalog.get(rec["league_id"], {})
        rec["catalog_league_name"] = cat.get("league_name")
        rec["catalog_country"] = cat.get("country")
        records_all_valid.append(rec)

    records_all_valid.sort(key=lambda r: (r["kickoff_dt"], r["fixture_id"]))

    return {
        "raw_audit": {
            "source_files": len(files),
            "raw_target_rows": raw_target_rows,
            "unique_targets_with_cutoff": len(bundles),
            "raw_target_status": dict(raw_target_status),
            "raw_obs_rows": raw_obs_rows,
            "raw_obs_red_present": raw_obs_red_present,
            "raw_obs_yellow_present": raw_obs_yellow_present,
            "raw_obs_league_name_present": raw_obs_league_name_present,
            "raw_obs_country_present": raw_obs_country_present,
            "team_fixture_keys": len(obs_seen_meta),
            "multi_seen_team_fixture_keys": sum(1 for v in obs_seen_meta.values() if len(v) > 1),
            "stratum_or_lname_overwrites_by_order": stratum_overwrite_keys,
            "red_status_overwrites_by_order": red_status_overwrite_keys,
            "score_conflicts_across_bundles": score_conflicts,
            "unique_finished_fixtures": len(all_fixtures),
            "exclusions": dict(exclusions),
            "eligible_with_rc_target": len(records_all_valid),
        },
        "records": records_all_valid,
        "team_ordered": team_ordered,
        "all_fixtures": all_fixtures,
    }


def _predict_row(rec: dict[str, Any], window: int) -> dict[str, Any]:
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
    )
    lh, la = float(eh["lambda"]), float(ea["lambda"])
    grid = dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=12)
    mg = len(grid) - 1
    p1 = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i > j)
    px = sum(grid[i][i] for i in range(mg + 1))
    p2 = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i < j)
    po25 = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i + j > 2)
    pbtts = sum(grid[i][j] for i in range(1, mg + 1) for j in range(1, mg + 1))

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
    brier_1x2 = ((p1 - y_hw) ** 2 + (px - y_dr) ** 2 + (p2 - y_aw) ** 2) / 3.0

    # Conteo de flags estructurales de entrada al estimador
    own_h_used = [m for m in hp_rev if m.own_metrics.get("goals") is not None][:window]
    opp_a_used = [m for m in ap_rev if m.conceded_metrics.get("goals") is not None][:window]
    own_a_used = [m for m in ap_rev if m.own_metrics.get("goals") is not None][:window]
    opp_h_used = [m for m in hp_rev if m.conceded_metrics.get("goals") is not None][:window]

    h_venue_n = sum(1 for m in own_h_used if m.venue == "HOME")
    a_venue_n = sum(1 for m in own_a_used if m.venue == "AWAY")
    h_same_ls = sum(1 for m in own_h_used if m.league_id == lid and m.season == season)
    a_same_ls = sum(1 for m in own_a_used if m.league_id == lid and m.season == season)
    mix_any = ("COMPETITION_MIX_FALLBACK" in eh["warnings"]) or ("COMPETITION_MIX_FALLBACK" in ea["warnings"])

    return {
        "fixture_id": rec["fixture_id"],
        "kickoff_utc": rec["kickoff_utc"],
        "month": rec["kickoff_utc"][:7],
        "split": "TEST" if rec["kickoff_utc"] >= BOUNDARY_ISO else "TRAIN",
        "league_id": lid,
        "season": rec["season"],
        "stratum": stratum,
        "catalog_league_name": rec["catalog_league_name"],
        "catalog_country": rec["catalog_country"],
        "rc_target": rec["rc_target"],
        "red_verified": rec["red_verified"],
        "side_mode": rec["side_mode"],
        "home_id": rec["home_id"],
        "away_id": rec["away_id"],
        "min_prior": rec["min_prior"],
        "hp_len": rec["hp_len"],
        "ap_len": rec["ap_len"],
        "used_h": len(own_h_used),
        "used_a": len(own_a_used),
        "h_venue_n": h_venue_n,
        "a_venue_n": a_venue_n,
        "h_same_ls": h_same_ls,
        "a_same_ls": a_same_ls,
        "mix_any": mix_any,
        "shared_h2h_max": max(eh["shared_h2h_n"], ea["shared_h2h_n"]),
        "home_n_eff": eh["n_eff"],
        "away_n_eff": ea["n_eff"],
        "hg": hg,
        "ag": ag,
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
        "rps": rps,
        "ll_1x2": ll_1x2,
        "brier_1x2": brier_1x2,
    }


def _block_boot_ci(
    rows: list[dict[str, Any]],
    val_fn,
    *,
    block_mode: str = "day",
    n_boot: int = BOOT_REPLICATES,
    seed: int = SEED,
) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    blocks: dict[str, list[float]] = defaultdict(list)
    vals: list[float] = []
    for r in rows:
        v = float(val_fn(r))
        vals.append(v)
        dt = parse_utc_timestamp(r["kickoff_utc"])
        bkey = dt.strftime("%Y-%m-%d") if block_mode == "day" else f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        blocks[bkey].append(v)
    bkeys = sorted(blocks.keys())
    bsums = [(sum(blocks[k]), len(blocks[k])) for k in bkeys]
    m = len(bsums)
    mean_v = sum(vals) / len(vals)
    if m < 5 or len(vals) < 20:
        return {"n": len(vals), "n_blocks": m, "mean": round(mean_v, 5), "ci_95": None}
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        s = 0.0
        c = 0
        for _ in range(m):
            bs, bn = bsums[rng.randrange(m)]
            s += bs
            c += bn
        boots.append(s / max(1, c))
    boots.sort()
    lo = boots[int(0.025 * (n_boot - 1))]
    hi = boots[int(0.975 * (n_boot - 1))]
    p_gt_0 = sum(1 for b in boots if b > 0) / n_boot
    p_lt_0 = sum(1 for b in boots if b < 0) / n_boot
    return {
        "n": len(vals),
        "n_blocks": m,
        "mean": round(mean_v, 5),
        "ci_95": [round(lo, 5), round(hi, 5)],
        "excludes_zero": (lo > 0) or (hi < 0),
        "prob_lt_0": round(p_lt_0, 4),
        "prob_gt_0": round(p_gt_0, 4),
    }


def _paired_boot_delta(
    alt_rows: list[dict[str, Any]],
    ref_rows: list[dict[str, Any]],
    val_fn,
    *,
    block_mode: str = "day",
    n_boot: int = BOOT_REPLICATES,
    seed: int = SEED,
) -> dict[str, Any]:
    assert len(alt_rows) == len(ref_rows)
    blocks: dict[str, list[float]] = defaultdict(list)
    deltas: list[float] = []
    for a, r in zip(alt_rows, ref_rows):
        assert a["fixture_id"] == r["fixture_id"]
        d = float(val_fn(a)) - float(val_fn(r))
        deltas.append(d)
        dt = parse_utc_timestamp(a["kickoff_utc"])
        bkey = dt.strftime("%Y-%m-%d") if block_mode == "day" else f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        blocks[bkey].append(d)
    bkeys = sorted(blocks.keys())
    bsums = [(sum(blocks[k]), len(blocks[k])) for k in bkeys]
    m = len(bsums)
    mean_d = sum(deltas) / len(deltas)
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        s = 0.0
        c = 0
        for _ in range(m):
            bs, bn = bsums[rng.randrange(m)]
            s += bs
            c += bn
        boots.append(s / max(1, c))
    boots.sort()
    lo = boots[int(0.025 * (n_boot - 1))]
    hi = boots[int(0.975 * (n_boot - 1))]
    return {
        "n": len(deltas),
        "n_blocks": m,
        "mean_delta": round(mean_d, 6),
        "ci_95": [round(lo, 6), round(hi, 6)],
        "prob_lt_0": round(sum(1 for b in boots if b < 0) / n_boot, 4),
    }


def _segment_stats(rows: list[dict[str, Any]], *, with_ci: bool = True) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    hw_p = [(r["p_home"], r["y_hw"]) for r in rows]
    dr_p = [(r["p_draw"], r["y_dr"]) for r in rows]
    aw_p = [(r["p_away"], r["y_aw"]) for r in rows]
    o25_p = [(r["p_over_2_5"], r["y_o25"]) for r in rows]
    btts_p = [(r["p_btts_yes"], r["y_btts"]) for r in rows]
    triples = [((r["p_home"], r["p_draw"], r["p_away"]), 0 if r["y_hw"] else (1 if r["y_dr"] else 2)) for r in rows]

    out: dict[str, Any] = {
        "n": n,
        "rps": round(statistics.fmean(r["rps"] for r in rows), 5),
        "ll_1x2": round(statistics.fmean(r["ll_1x2"] for r in rows), 5),
        "brier_1x2": round(statistics.fmean(r["brier_1x2"] for r in rows), 5),
        "home_win": {
            "pred_mean": round(statistics.fmean(r["p_home"] for r in rows), 4),
            "obs_rate": round(statistics.fmean(r["y_hw"] for r in rows), 4),
            "bias": round(statistics.fmean(r["p_home"] - r["y_hw"] for r in rows), 4),
            "brier": brier_score(hw_p),
            "log_loss": log_loss(hw_p),
            "ece_5": expected_calibration_error(hw_p, n_bins=5) if n >= 50 else None,
        },
        "draw": {
            "pred_mean": round(statistics.fmean(r["p_draw"] for r in rows), 4),
            "obs_rate": round(statistics.fmean(r["y_dr"] for r in rows), 4),
            "bias": round(statistics.fmean(r["p_draw"] - r["y_dr"] for r in rows), 4),
            "brier": brier_score(dr_p),
            "log_loss": log_loss(dr_p),
            "ece_5": expected_calibration_error(dr_p, n_bins=5) if n >= 50 else None,
        },
        "away_win": {
            "pred_mean": round(statistics.fmean(r["p_away"] for r in rows), 4),
            "obs_rate": round(statistics.fmean(r["y_aw"] for r in rows), 4),
            "bias": round(statistics.fmean(r["p_away"] - r["y_aw"] for r in rows), 4),
            "brier": brier_score(aw_p),
            "log_loss": log_loss(aw_p),
            "ece_5": expected_calibration_error(aw_p, n_bins=5) if n >= 50 else None,
        },
        "over_2_5": {
            "pred_mean": round(statistics.fmean(r["p_over_2_5"] for r in rows), 4),
            "obs_rate": round(statistics.fmean(r["y_o25"] for r in rows), 4),
            "bias": round(statistics.fmean(r["p_over_2_5"] - r["y_o25"] for r in rows), 4),
            "brier": brier_score(o25_p),
            "log_loss": log_loss(o25_p),
            "ece_5": expected_calibration_error(o25_p, n_bins=5) if n >= 50 else None,
        },
        "btts_yes": {
            "pred_mean": round(statistics.fmean(r["p_btts_yes"] for r in rows), 4),
            "obs_rate": round(statistics.fmean(r["y_btts"] for r in rows), 4),
            "bias": round(statistics.fmean(r["p_btts_yes"] - r["y_btts"] for r in rows), 4),
            "brier": brier_score(btts_p),
            "log_loss": log_loss(btts_p),
            "ece_5": expected_calibration_error(btts_p, n_bins=5) if n >= 50 else None,
        },
        "goals": {
            "pred_home": round(statistics.fmean(r["lambda_home"] for r in rows), 4),
            "obs_home": round(statistics.fmean(r["hg"] for r in rows), 4),
            "bias_home": round(statistics.fmean(r["lambda_home"] - r["hg"] for r in rows), 4),
            "pred_away": round(statistics.fmean(r["lambda_away"] for r in rows), 4),
            "obs_away": round(statistics.fmean(r["ag"] for r in rows), 4),
            "bias_away": round(statistics.fmean(r["lambda_away"] - r["ag"] for r in rows), 4),
            "pred_total": round(statistics.fmean(r["lambda_home"] + r["lambda_away"] for r in rows), 4),
            "obs_total": round(statistics.fmean(r["hg"] + r["ag"] for r in rows), 4),
            "bias_total": round(statistics.fmean((r["lambda_home"] + r["lambda_away"]) - (r["hg"] + r["ag"]) for r in rows), 4),
        },
    }
    if with_ci and n >= 25:
        out["ci_day_block"] = {
            "bias_home_win": _block_boot_ci(rows, lambda r: r["p_home"] - r["y_hw"]),
            "bias_draw": _block_boot_ci(rows, lambda r: r["p_draw"] - r["y_dr"]),
            "bias_away_win": _block_boot_ci(rows, lambda r: r["p_away"] - r["y_aw"]),
            "bias_lambda_home": _block_boot_ci(rows, lambda r: r["lambda_home"] - r["hg"]),
            "bias_lambda_away": _block_boot_ci(rows, lambda r: r["lambda_away"] - r["ag"]),
            "bias_over_2_5": _block_boot_ci(rows, lambda r: r["p_over_2_5"] - r["y_o25"]),
            "bias_btts_yes": _block_boot_ci(rows, lambda r: r["p_btts_yes"] - r["y_btts"]),
        }
    return out


def run_audit(root: Path, out_json: Path) -> dict[str, Any]:
    corpus = _load_and_audit_corpus(root)
    recs = corpus["records"]

    # Predicciones L6, L10, L20 sobre TODOS los 4,496 (sin excluir rc_target!)
    preds = {w: [_predict_row(r, w) for r in recs] for w in (6, 10, 20)}

    # Separar conjuntos: PREMATCH representativo (incluyendo rc_target) vs secundario sin rc_target
    p20 = preds[20]
    p20_train_all = [r for r in p20 if r["split"] == "TRAIN"]
    p20_test_all = [r for r in p20 if r["split"] == "TEST"]
    p20_train_norc = [r for r in p20_train_all if not r["rc_target"]]
    p20_test_norc = [r for r in p20_test_all if not r["rc_target"]]

    # Red card audit
    red_audit = {
        "eligible_prematch_total": len(p20),
        "train_prematch_total": len(p20_train_all),
        "test_prematch_total": len(p20_test_all),
        "train_rc_target_n": sum(1 for r in p20_train_all if r["rc_target"]),
        "test_rc_target_n": sum(1 for r in p20_test_all if r["rc_target"]),
        "train_no_rc_n": len(p20_train_norc),
        "test_no_rc_n": len(p20_test_norc),
        "test_red_verified_n": sum(1 for r in p20_test_all if r["red_verified"]),
        "test_red_verified_rc_n": sum(1 for r in p20_test_all if r["red_verified"] and r["rc_target"]),
        "national_team_test_with_rc": _segment_stats([r for r in p20_test_all if r["stratum"] == "NATIONAL_TEAM"], with_ci=False),
        "national_team_test_no_rc": _segment_stats([r for r in p20_test_norc if r["stratum"] == "NATIONAL_TEAM"], with_ci=False),
        "all_test_with_rc": _segment_stats(p20_test_all),
        "all_test_no_rc": _segment_stats(p20_test_norc),
    }

    # Sesgos por dimensión sobre conjunto principal PREMATCH (L20) y train vs test
    def _group_stats(rows: list[dict[str, Any]], key_fn, min_n: int = 1) -> dict[str, Any]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            buckets[str(key_fn(r))].append(r)
        return {
            k: _segment_stats(v, with_ci=(len(v) >= 40))
            for k, v in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            if len(v) >= min_n
        }

    # Probabilidad propia del modelo (evitando doble conteo: home y away separados)
    def _prob_bin_home(r: dict[str, Any]) -> str:
        p = r["p_home"]
        if p < 0.25: return "01_lt_0.25"
        if p < 0.35: return "02_0.25_0.35"
        if p < 0.45: return "03_0.35_0.45"
        if p < 0.55: return "04_0.45_0.55"
        return "05_ge_0.55"

    def _prob_bin_away(r: dict[str, Any]) -> str:
        p = r["p_away"]
        if p < 0.25: return "01_lt_0.25"
        if p < 0.35: return "02_0.25_0.35"
        if p < 0.45: return "03_0.35_0.45"
        if p < 0.55: return "04_0.45_0.55"
        return "05_ge_0.55"

    def _strength_diff_bin(r: dict[str, Any]) -> str:
        d = r["lambda_home"] - r["lambda_away"]
        if d < -0.50: return "01_away_strong_lt_-0.50"
        if d < -0.15: return "02_slight_away_-0.50_-0.15"
        if d <= 0.15: return "03_even_-0.15_0.15"
        if d <= 0.50: return "04_slight_home_0.15_0.50"
        return "05_home_strong_gt_0.50"

    def _depth_bin(r: dict[str, Any]) -> str:
        mp = r["min_prior"]
        if mp >= 20: return "03_min_ge_20"
        if mp >= 10: return "02_min_10_19"
        return "01_min_6_9"

    def _venue_blend_bin(r: dict[str, Any]) -> str:
        hb = "H_VB" if r["h_venue_n"] >= 3 else "H_NO_VB"
        ab = "A_VB" if r["a_venue_n"] >= 3 else "A_NO_VB"
        return f"{hb}+{ab}"

    bias_diagnosis = {
        "train_all_l20": _segment_stats(p20_train_all),
        "test_all_l20": _segment_stats(p20_test_all),
        "full_4496_l20": _segment_stats(p20),
        "train_by_stratum": _group_stats(p20_train_all, lambda r: r["stratum"]),
        "test_by_stratum": _group_stats(p20_test_all, lambda r: r["stratum"]),
        "test_by_season": _group_stats(p20_test_all, lambda r: r["season"]),
        "full_by_season": _group_stats(p20, lambda r: r["season"]),
        "test_by_home_prob_bin": _group_stats(p20_test_all, _prob_bin_home),
        "test_by_away_prob_bin": _group_stats(p20_test_all, _prob_bin_away),
        "test_by_strength_diff_lambda": _group_stats(p20_test_all, _strength_diff_bin),
        "full_by_strength_diff_lambda": _group_stats(p20, _strength_diff_bin),
        "test_by_depth_bin": _group_stats(p20_test_all, _depth_bin),
        "full_by_depth_bin": _group_stats(p20, _depth_bin),
        "test_by_comp_mix": _group_stats(p20_test_all, lambda r: "MIXED_COMP" if r["mix_any"] else "PURE_SAME_LEAGUE"),
        "test_by_venue_blend": _group_stats(p20_test_all, _venue_blend_bin),
        "test_by_side_mode": _group_stats(p20_test_all, lambda r: r["side_mode"]),
        "full_by_side_mode": _group_stats(p20, lambda r: r["side_mode"]),
        "test_top_leagues_ge_40": _group_stats(p20_test_all, lambda r: f"{r['league_id']}:{r['catalog_league_name'] or 'NO_TARGET_META'}", min_n=40),
        "full_top_countries_ge_50": _group_stats(p20, lambda r: r["catalog_country"] or "NO_TARGET_COUNTRY", min_n=50),
        "test_by_month": _group_stats(p20_test_all, lambda r: r["month"]),
    }

    # L6 vs L10 vs L20 sobre test_all (2,323) y sobre test_no_rc (2,319)
    p6_test = [r for r in preds[6] if r["split"] == "TEST"]
    p10_test = [r for r in preds[10] if r["split"] == "TEST"]
    window_comparison = {
        "test_all_n": len(p20_test_all),
        "L6": _segment_stats(p6_test),
        "L10": _segment_stats(p10_test),
        "L20": _segment_stats(p20_test_all),
        "L20_minus_L6_day": {
            "rps": _paired_boot_delta(p20_test_all, p6_test, lambda r: r["rps"]),
            "ll_1x2": _paired_boot_delta(p20_test_all, p6_test, lambda r: r["ll_1x2"]),
            "brier_home": _paired_boot_delta(p20_test_all, p6_test, lambda r: (r["p_home"] - r["y_hw"]) ** 2),
        },
        "L20_minus_L10_day": {
            "rps": _paired_boot_delta(p20_test_all, p10_test, lambda r: r["rps"]),
            "ll_1x2": _paired_boot_delta(p20_test_all, p10_test, lambda r: r["ll_1x2"]),
        },
    }

    # Representatividad y muestra min >= 20
    ge20_rows = [r for r in p20 if r["min_prior"] >= 20]
    team_counts_full = Counter()
    league_counts_full = Counter()
    country_counts_full = Counter()
    date_counts_full = Counter()
    for r in p20:
        team_counts_full[r["home_id"]] += 1
        team_counts_full[r["away_id"]] += 1
        league_counts_full[f"{r['league_id']}:{r['catalog_league_name'] or 'NO_CAT'}"] += 1
        country_counts_full[r["catalog_country"] or "NO_CAT"] += 1
        date_counts_full[r["month"]] += 1

    ge20_teams = Counter()
    ge20_leagues = Counter()
    for r in ge20_rows:
        ge20_teams[r["home_id"]] += 1
        ge20_teams[r["away_id"]] += 1
        ge20_leagues[f"{r['league_id']}:{r['catalog_league_name'] or 'NO_CAT'}"] += 1

    # Validación cronológica anidada (para Protocolo E sin tocar test_strict)
    # 3 folds expansivos dentro de TRAIN (2,173) o 4 bloques sobre 4,496 (como referencia de estabilidad)
    sorted_all = sorted(p20, key=lambda r: (r["kickoff_utc"], r["fixture_id"]))
    train_sorted = sorted(p20_train_all, key=lambda r: (r["kickoff_utc"], r["fixture_id"]))
    # Folds anidados dentro de train_all: corte estricto por fecha sin empates
    t_dates = sorted({r["kickoff_utc"] for r in train_sorted})
    nested_folds = {}
    for label, q_frac in [("fold_early_to_mid", 0.50), ("fold_mid_to_late_train", 0.75)]:
        cut_iso = train_sorted[int(len(train_sorted) * q_frac)]["kickoff_utc"]
        sub_tr = [r for r in train_sorted if r["kickoff_utc"] < cut_iso]
        sub_va = [r for r in train_sorted if r["kickoff_utc"] >= cut_iso]
        nested_folds[label] = {
            "cutoff_utc": cut_iso,
            "train_n": len(sub_tr),
            "val_n": len(sub_va),
            "val_stats": _segment_stats(sub_va, with_ci=True),
        }

    representativity = {
        "total_4496_teams_distinct": len(team_counts_full),
        "total_4496_leagues_distinct": len(league_counts_full),
        "top_10_leagues_share": round(sum(c for _, c in league_counts_full.most_common(10)) / len(p20), 4),
        "top_10_leagues": league_counts_full.most_common(10),
        "top_10_countries": country_counts_full.most_common(10),
        "months_distribution_4496": dict(sorted(date_counts_full.items())),
        "top_10_teams_appearances": team_counts_full.most_common(10),
        "ge_20_audit": {
            "n": len(ge20_rows),
            "in_train": sum(1 for r in ge20_rows if r["split"] == "TRAIN"),
            "in_test": sum(1 for r in ge20_rows if r["split"] == "TEST"),
            "date_min": min(r["kickoff_utc"] for r in ge20_rows) if ge20_rows else None,
            "date_max": max(r["kickoff_utc"] for r in ge20_rows) if ge20_rows else None,
            "distinct_teams": len(ge20_teams),
            "distinct_leagues": len(ge20_leagues),
            "leagues": ge20_leagues.most_common(),
            "stats": _segment_stats(ge20_rows, with_ci=False),
        },
        "ge_10_count_in_4496": sum(1 for r in p20 if r["min_prior"] >= 10),
        "ge_6_count_in_4496": len(p20),
    }

    report = {
        "model_version": MODEL_VERSION,
        "code_hashes": {
            "bet_engine/modeling/distributions.py": _sha256(root / "bet_engine/modeling/distributions.py"),
            "bet_engine/modeling/calibration.py": _sha256(root / "bet_engine/modeling/calibration.py"),
            "bet_engine/context/observation_store.py": _sha256(root / "bet_engine/context/observation_store.py"),
            "tools/reproduce_baseline_backtest.py": _sha256(root / "tools/reproduce_baseline_backtest.py"),
        },
        "raw_audit": corpus["raw_audit"],
        "red_card_audit": red_audit,
        "bias_diagnosis": bias_diagnosis,
        "window_comparison_test_prematch": window_comparison,
        "representativity": representativity,
        "nested_train_folds": nested_folds,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    out = Path("audit/fase2b_backtesting/audit_metrics.json")
    rep = run_audit(Path(".").resolve(), out)
    print(json.dumps({
        "eligible_prematch": rep["red_card_audit"]["eligible_prematch_total"],
        "test_prematch": rep["red_card_audit"]["test_prematch_total"],
        "test_no_rc": rep["red_card_audit"]["test_no_rc_n"],
        "stratum_overwrites": rep["raw_audit"]["stratum_or_lname_overwrites_by_order"],
        "ge20_n": rep["representativity"]["ge_20_audit"]["n"],
        "out": str(out),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
