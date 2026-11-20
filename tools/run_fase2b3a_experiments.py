#!/usr/bin/env python3
"""Experimentos A/B aislados de calibración y especificación estadística (Fase 2B.3A).

No modifica código de producción ni expedientes PREMATCH cerrados.
Evalúa variantes aisladas A (Priors), B (Localía) y C (Competiciones) frente al
baseline congelado CoherentMatchModeler_baseline_2.0_2.1A usando:
1) Validación cronológica walk-forward estrictamente interna a train_strict (3 folds expansivos);
2) Partición cronológica alternativa dentro de train_strict (robustez frente al corte);
3) Pasada única confirmatoria sobre test_strict (sin selección de hiperparámetros en test);
4) Análisis de sensibilidad (L6/L10/L20, bilateral vs unilateral, UNKNOWN, venue-split y favoritos).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    _CLUB_FRIENDLY_LEAGUE_IDS,
    same_competition_season,
    same_meaningful_stratum,
)
from bet_engine.modeling.calibration import (
    brier_score,
    expected_calibration_error,
    log_loss,
    parse_utc_timestamp,
)
from bet_engine.modeling.distributions import (
    DEFAULT_PRIORS,
    FAMILY_DISPERSION_FLOORS,
    STRATUM_PRIORS,
    _estimate_team_rate_detailed,
    _red_card_weight,
    _resolve_prior,
    dixon_coles_joint_grid,
)
from tools.reproduce_baseline_backtest import _extract_fixture_record, _load_corpus

MODEL_BASELINE_VERSION = "CoherentMatchModeler_baseline_2.0_2.1A"
BOUNDARY_STRICT_ISO = "2026-09-26T14:00:00+00:00"
BOOT_REPLICATES = 2000
BOOT_SEED = 42


@dataclass(frozen=True)
class VariantSpec:
    variant_id: str
    group: str  # BASELINE | A_PRIORS | B_VENUE | C_COMPETITIONS
    description: str
    prior_mode: str = "fixed"  # fixed | empirical_stratum | empirical_league
    shrink_k: float = 8.0
    use_kish_neff: bool = False
    venue_mode: str = "legacy_25"  # legacy_25 | blend_0 | blend_50_unshrunk | shrunk_anchor_50 | shrunk_anchor_70 | shrunk_anchor_80
    venue_blend: float = 0.25
    venue_k: float = 4.0
    comp_policy: str = "legacy"  # legacy | exclude_friendlies_fallback | exclude_friendlies_strict | restrict_cups_league_ge6 | strict_comp_identity
    strict_competition_identity: bool = False


def _is_friendly_match(m: HistoricalTeamMatch) -> bool:
    ln = str(m.league_name or "").lower()
    return m.league_id in _CLUB_FRIENDLY_LEAGUE_IDS or "friendl" in ln or "amistos" in ln


def _fit_empirical_priors_asof(
    all_fixtures: dict[int, dict[str, HistoricalTeamMatch]],
    cutoff_dt: datetime,
) -> dict[str, Any]:
    """Calcula priors empíricos jerárquicos (global -> estrato -> liga) estrictamente as-of < cutoff_dt."""
    h_vals: list[float] = []
    a_vals: list[float] = []
    by_stratum: dict[str, list[tuple[float, float]]] = defaultdict(list)
    by_league: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
    fit_fids: set[int] = set()

    for fid, sides in all_fixtures.items():
        if "home" in sides and "away" in sides:
            hm, am = sides["home"], sides["away"]
            ko_str, av_str, lid, stratum = hm.kickoff_utc, hm.available_at_utc, hm.league_id, hm.competition_stratum
            hg, ag = hm.own_metrics.get("goals"), am.own_metrics.get("goals")
        elif "home" in sides:
            hm = sides["home"]
            ko_str, av_str, lid, stratum = hm.kickoff_utc, hm.available_at_utc, hm.league_id, hm.competition_stratum
            hg, ag = hm.own_metrics.get("goals"), hm.conceded_metrics.get("goals")
        elif "away" in sides:
            am = sides["away"]
            ko_str, av_str, lid, stratum = am.kickoff_utc, am.available_at_utc, am.league_id, am.competition_stratum
            hg, ag = am.conceded_metrics.get("goals"), am.own_metrics.get("goals")
        else:
            continue

        if hg is None or ag is None:
            continue
        ko_dt = parse_utc_timestamp(ko_str)
        av_dt = parse_utc_timestamp(av_str)
        if ko_dt >= cutoff_dt or av_dt > cutoff_dt:
            continue

        fit_fids.add(fid)
        hf, af = float(hg), float(ag)
        h_vals.append(hf)
        a_vals.append(af)
        st = str(stratum or "UNKNOWN")
        by_stratum[st].append((hf, af))
        if int(lid or 0) > 0:
            by_league[int(lid)].append((hf, af, st))

    g_home = statistics.fmean(h_vals) if h_vals else DEFAULT_PRIORS["goals"][0]
    g_away = statistics.fmean(a_vals) if a_vals else DEFAULT_PRIORS["goals"][1]

    # Suavizado jerárquico estrato -> global (m_st = 25)
    m_st = 25.0
    stratum_priors: dict[str, tuple[float, float]] = {}
    for st, pairs in by_stratum.items():
        n = len(pairs)
        sh = (sum(x[0] for x in pairs) + m_st * g_home) / (n + m_st)
        sa = (sum(x[1] for x in pairs) + m_st * g_away) / (n + m_st)
        stratum_priors[st] = (sh, sa)

    # Suavizado jerárquico liga -> estrato (m_lg = 30)
    m_lg = 30.0
    league_priors: dict[int, tuple[float, float]] = {}
    for lid, rows in by_league.items():
        n = len(rows)
        dominant_st = Counter(x[2] for x in rows).most_common(1)[0][0]
        base_h, base_a = stratum_priors.get(dominant_st, (g_home, g_away))
        lh = (sum(x[0] for x in rows) + m_lg * base_h) / (n + m_lg)
        la = (sum(x[1] for x in rows) + m_lg * base_a) / (n + m_lg)
        league_priors[lid] = (lh, la)

    return {
        "fit_fids": fit_fids,
        "global": (g_home, g_away),
        "stratum": stratum_priors,
        "league": league_priors,
        "fit_n": len(fit_fids),
    }


def _resolve_variant_prior(
    spec: VariantSpec,
    stratum: str | None,
    league_id: int,
    venue: str,
    empirical_fit: dict[str, Any] | None,
) -> float:
    idx = 0 if venue.upper() == "HOME" else 1
    if spec.prior_mode == "fixed" or empirical_fit is None:
        return _resolve_prior("goals", stratum, venue)
    if spec.prior_mode == "empirical_stratum":
        st = str(stratum or "")
        if st in empirical_fit["stratum"]:
            return empirical_fit["stratum"][st][idx]
        return empirical_fit["global"][idx]
    if spec.prior_mode == "empirical_league":
        if int(league_id or 0) in empirical_fit["league"]:
            return empirical_fit["league"][int(league_id)][idx]
        st = str(stratum or "")
        if st in empirical_fit["stratum"]:
            return empirical_fit["stratum"][st][idx]
        return empirical_fit["global"][idx]
    raise ValueError(f"UNKNOWN_PRIOR_MODE:{spec.prior_mode}")


def _filter_competition_history(
    ms: tuple[HistoricalTeamMatch, ...],
    *,
    comp_policy: str,
    target_league_id: int,
    target_stratum: str | None,
) -> tuple[HistoricalTeamMatch, ...]:
    if comp_policy in ("legacy", "strict_comp_identity"):
        return ms
    if comp_policy == "exclude_friendlies_strict":
        return tuple(m for m in ms if not _is_friendly_match(m))
    if comp_policy == "exclude_friendlies_fallback":
        non_fr = tuple(m for m in ms if not _is_friendly_match(m))
        return non_fr if len(non_fr) >= 4 else ms
    if comp_policy == "restrict_cups_league_ge6":
        if target_stratum == "DOMESTIC_LEAGUE":
            league_cnt = sum(
                1
                for m in ms
                if (int(m.league_id or 0) == int(target_league_id or 0) and int(target_league_id or 0) > 0)
                or m.competition_stratum == "DOMESTIC_LEAGUE"
            )
            if league_cnt >= 6:
                no_cup = tuple(m for m in ms if m.competition_stratum != "DOMESTIC_CUP")
                if len(no_cup) >= 6:
                    return no_cup
        return ms
    raise ValueError(f"UNKNOWN_COMP_POLICY:{comp_policy}")


def _estimate_side_rate_variant(
    own_matches: tuple[HistoricalTeamMatch, ...],
    opp_matches: tuple[HistoricalTeamMatch, ...],
    *,
    target_league_id: int,
    target_season: int,
    target_stratum: str | None,
    own_venue: str,
    opp_venue: str,
    prior_mean: float,
    other_venue_prior_mean: float,
    history_limit: int,
    spec: VariantSpec,
) -> dict[str, Any]:
    """Estimador aislado para experimentos A/B replicando exactamente baseline cuando spec == BASELINE."""
    own_pool = _filter_competition_history(
        own_matches, comp_policy=spec.comp_policy, target_league_id=target_league_id, target_stratum=target_stratum
    )
    opp_pool = _filter_competition_history(
        opp_matches, comp_policy=spec.comp_policy, target_league_id=target_league_id, target_stratum=target_stratum
    )

    def filter_valid(ms: tuple[HistoricalTeamMatch, ...], field_map: str) -> list[HistoricalTeamMatch]:
        ordered = sorted(ms, key=lambda m: m.kickoff_utc, reverse=True)
        valid = [
            m
            for m in ordered
            if _red_card_weight(m, policy="exclude_any") > 0.0
            and (m.own_metrics if field_map == "own" else m.conceded_metrics).get("goals") is not None
        ]
        return valid[:history_limit]

    own_sel = filter_valid(own_pool, "own")
    opp_sel = filter_valid(opp_pool, "conceded")
    if not own_sel and not opp_sel:
        return {"lambda": prior_mean, "n_eff": 0.0, "n_own": 0, "n_opp": 0}

    shared_h2h_ids = {m.fixture_id for m in own_sel} & {m.fixture_id for m in opp_sel}

    def comp_weight(m: HistoricalTeamMatch) -> float:
        if spec.strict_competition_identity:
            if same_competition_season(m, target_league_id, target_season):
                return 1.0
            if same_meaningful_stratum(m, target_stratum):
                return 0.50
            return 0.25
        if m.league_id == target_league_id and m.season == target_season:
            return 1.0
        if target_stratum and m.competition_stratum == target_stratum:
            return 0.50
        return 0.25

    def weighted_shrunk(ms: list[HistoricalTeamMatch], getter: Any, *, apply_h2h_discount: bool) -> tuple[float, float, list[tuple[HistoricalTeamMatch, float, float]]]:
        if not ms:
            return prior_mean, 0.0, []
        pairs: list[tuple[HistoricalTeamMatch, float, float]] = []
        for m in ms:
            v = getter(m)
            if v is None:
                continue
            w = comp_weight(m)
            if apply_h2h_discount and m.fixture_id in shared_h2h_ids:
                w *= 0.50
            pairs.append((m, float(v), w))
        if not pairs:
            return prior_mean, 0.0, []
        w_sum = sum(w for _, _, w in pairs)
        l20_mean = sum(v * w for _, v, w in pairs) / w_sum
        p6 = pairs[:6]
        w6_sum = sum(w for _, _, w in p6)
        l6_mean = sum(v * w for _, v, w in p6) / w6_sum if w6_sum > 0 else l20_mean
        sample_est = (0.35 * l6_mean + 0.65 * l20_mean) if len(pairs) >= 7 else l20_mean
        if spec.use_kish_neff:
            w_sq = sum(w * w for _, _, w in pairs)
            eff = (w_sum * w_sum / w_sq) if w_sq > 0 else 0.0
        else:
            eff = w_sum
        shrunk = (eff * sample_est + spec.shrink_k * prior_mean) / (eff + spec.shrink_k)
        return shrunk, eff, pairs

    own_shrunk, own_w_eff, own_pairs = weighted_shrunk(
        own_sel, lambda m: m.own_metrics.get("goals"), apply_h2h_discount=False
    )
    opp_shrunk, opp_w_eff, opp_pairs = weighted_shrunk(
        opp_sel, lambda m: m.conceded_metrics.get("goals"), apply_h2h_discount=True
    )

    own_venue_pairs = [(float(m.own_metrics["goals"]), comp_weight(m)) for m in own_sel if m.venue == own_venue]
    opp_venue_pairs = [(float(m.conceded_metrics["goals"]), comp_weight(m)) for m in opp_sel if m.venue == opp_venue]

    prior_mid = max(0.25, 0.5 * (prior_mean + other_venue_prior_mean))
    venue_mult = prior_mean / prior_mid

    def _apply_venue(base_shrunk: float, vpairs: list[tuple[float, float]]) -> float:
        if spec.venue_mode == "blend_0":
            return base_shrunk
        if spec.venue_mode in ("legacy_25", "blend_50_unshrunk"):
            if len(vpairs) >= 3:
                vw = sum(w for _, w in vpairs)
                vm = sum(v * w for v, w in vpairs) / vw
                return (1.0 - spec.venue_blend) * base_shrunk + spec.venue_blend * vm
            return base_shrunk
        # Modos con contracción bayesiana explícita de sede (evita ruido cuando venue_n es pequeño)
        anchor = max(0.15, base_shrunk * venue_mult)
        if not vpairs:
            return (1.0 - spec.venue_blend) * base_shrunk + spec.venue_blend * anchor
        vw = sum(w for _, w in vpairs)
        vm = sum(v * w for v, w in vpairs) / vw
        v_shrunk = (vw * vm + spec.venue_k * anchor) / (vw + spec.venue_k)
        return (1.0 - spec.venue_blend) * base_shrunk + spec.venue_blend * v_shrunk

    own_final = _apply_venue(own_shrunk, own_venue_pairs)
    opp_final = _apply_venue(opp_shrunk, opp_venue_pairs)

    lam = max(0.05, 0.55 * own_final + 0.45 * opp_final)
    n_eff = round(0.55 * own_w_eff + 0.45 * opp_w_eff, 4)
    return {"lambda": lam, "n_eff": n_eff, "n_own": len(own_sel), "n_opp": len(opp_sel)}


def _predict_variant(
    rec: dict[str, Any],
    spec: VariantSpec,
    empirical_fit: dict[str, Any] | None,
    *,
    window: int = 20,
) -> dict[str, Any]:
    lid, season, stratum = rec["league_id"], rec["season"], rec["stratum"]
    h_prior = _resolve_variant_prior(spec, stratum, lid, "HOME", empirical_fit)
    a_prior = _resolve_variant_prior(spec, stratum, lid, "AWAY", empirical_fit)

    eh = _estimate_side_rate_variant(
        rec["hp_rev"],
        rec["ap_rev"],
        target_league_id=lid,
        target_season=season,
        target_stratum=stratum,
        own_venue="HOME",
        opp_venue="AWAY",
        prior_mean=h_prior,
        other_venue_prior_mean=a_prior,
        history_limit=window,
        spec=spec,
    )
    ea = _estimate_side_rate_variant(
        rec["ap_rev"],
        rec["hp_rev"],
        target_league_id=lid,
        target_season=season,
        target_stratum=stratum,
        own_venue="AWAY",
        opp_venue="HOME",
        prior_mean=a_prior,
        other_venue_prior_mean=h_prior,
        history_limit=window,
        spec=spec,
    )
    lh, la = float(eh["lambda"]), float(ea["lambda"])
    g = dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=12)
    p1 = sum(g[i][j] for i in range(13) for j in range(13) if i > j)
    px = sum(g[i][i] for i in range(13))
    p2 = sum(g[i][j] for i in range(13) for j in range(13) if i < j)
    po = sum(g[i][j] for i in range(13) for j in range(13) if i + j > 2)
    pb = sum(g[i][j] for i in range(1, 13) for j in range(1, 13))

    hg, ag = rec["hg"], rec["ag"]
    y1 = 1 if hg > ag else 0
    yx = 1 if hg == ag else 0
    y2 = 1 if hg < ag else 0
    yo = 1 if (hg + ag) > 2.5 else 0
    yb = 1 if (hg > 0 and ag > 0) else 0

    rps = ((p1 - y1) ** 2 + ((p1 + px) - (y1 + yx)) ** 2) / 2.0
    eps = 1e-6
    p_true = p1 if y1 else (px if yx else p2)
    ll_1x2 = -math.log(max(eps, p_true))
    brier_1x2 = ((p1 - y1) ** 2 + (px - yx) ** 2 + (p2 - y2) ** 2) / 3.0

    return {
        "fixture_id": rec["fixture_id"],
        "kickoff_utc": rec["kickoff_utc"],
        "stratum": stratum,
        "side_mode": rec["side_mode"],
        "min_prior": min(len(rec["hp_rev"]), len(rec["ap_rev"])),
        "high_venue_gap": rec.get("high_venue_gap", False),
        "hg": hg,
        "ag": ag,
        "lambda_home": lh,
        "lambda_away": la,
        "p_home": p1,
        "p_draw": px,
        "p_away": p2,
        "p_over_2_5": po,
        "p_btts_yes": pb,
        "y1": y1,
        "yx": yx,
        "y2": y2,
        "yo": yo,
        "yb": yb,
        "rps": rps,
        "ll_1x2": ll_1x2,
        "brier_1x2": brier_1x2,
        "brier_o25": (po - yo) ** 2,
        "ll_o25": -(yo * math.log(max(eps, po)) + (1 - yo) * math.log(max(eps, 1.0 - po))),
        "brier_btts": (pb - yb) ** 2,
        "ll_btts": -(yb * math.log(max(eps, pb)) + (1 - yb) * math.log(max(eps, 1.0 - pb))),
        "home_n_eff": eh["n_eff"],
        "away_n_eff": ea["n_eff"],
        "n_own_h": eh["n_own"],
        "n_own_a": ea["n_own"],
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    hw_p = [(r["p_home"], r["y1"]) for r in rows]
    dr_p = [(r["p_draw"], r["yx"]) for r in rows]
    aw_p = [(r["p_away"], r["y2"]) for r in rows]
    o25_p = [(r["p_over_2_5"], r["yo"]) for r in rows]
    btts_p = [(r["p_btts_yes"], r["yb"]) for r in rows]

    def _sub_bias(items: list[dict[str, Any]], pk: str, yk: str) -> dict[str, Any]:
        if not items:
            return {"n": 0}
        pm = statistics.fmean(x[pk] for x in items)
        ob = statistics.fmean(x[yk] for x in items)
        return {"n": len(items), "pred_mean": round(pm, 4), "obs_rate": round(ob, 4), "bias": round(pm - ob, 4)}

    fav_home = [r for r in rows if r["p_home"] >= 0.55]
    fav_away = [r for r in rows if r["p_away"] >= 0.55]
    fav_all = [(r["p_home"], r["y1"]) for r in fav_home] + [(r["p_away"], r["y2"]) for r in fav_away]
    lam_h_fav = [r for r in rows if (r["lambda_home"] - r["lambda_away"]) > 0.50]
    lam_a_fav = [r for r in rows if (r["lambda_home"] - r["lambda_away"]) < -0.50]

    return {
        "n": n,
        "rps": round(statistics.fmean(r["rps"] for r in rows), 5),
        "ll_1x2": round(statistics.fmean(r["ll_1x2"] for r in rows), 5),
        "brier_1x2": round(statistics.fmean(r["brier_1x2"] for r in rows), 5),
        "brier_o25": brier_score(o25_p),
        "ll_o25": log_loss(o25_p),
        "brier_btts": brier_score(btts_p),
        "ll_btts": log_loss(btts_p),
        "ece_5_hw": expected_calibration_error(hw_p, n_bins=5) if n >= 200 else None,
        "ece_5_aw": expected_calibration_error(aw_p, n_bins=5) if n >= 200 else None,
        "ece_5_o25": expected_calibration_error(o25_p, n_bins=5) if n >= 200 else None,
        "bias_home_win": round(statistics.fmean(r["p_home"] - r["y1"] for r in rows), 4),
        "bias_draw": round(statistics.fmean(r["p_draw"] - r["yx"] for r in rows), 4),
        "bias_away_win": round(statistics.fmean(r["p_away"] - r["y2"] for r in rows), 4),
        "bias_lambda_home": round(statistics.fmean(r["lambda_home"] - r["hg"] for r in rows), 4),
        "bias_lambda_away": round(statistics.fmean(r["lambda_away"] - r["ag"] for r in rows), 4),
        "mean_home_n_eff": round(statistics.fmean(r["home_n_eff"] for r in rows), 3),
        "mean_away_n_eff": round(statistics.fmean(r["away_n_eff"] for r in rows), 3),
        "favorites_p_ge_0_55": {
            "home": _sub_bias(fav_home, "p_home", "y1"),
            "away": _sub_bias(fav_away, "p_away", "y2"),
            "combined_bias": round(statistics.fmean(p - y for p, y in fav_all), 4) if fav_all else None,
            "combined_n": len(fav_all),
        },
        "strength_diff_gt_0_50_home": _sub_bias(lam_h_fav, "p_home", "y1"),
        "strength_diff_lt_neg0_50_away": _sub_bias(lam_a_fav, "p_away", "y2"),
    }


def _paired_boot_delta(
    alt_rows: list[dict[str, Any]],
    ref_rows: list[dict[str, Any]],
    key: str,
    *,
    block_mode: str = "day",
) -> dict[str, Any]:
    ref_map = {r["fixture_id"]: r for r in ref_rows}
    blocks: dict[str, list[float]] = defaultdict(list)
    deltas: list[float] = []
    for a in alt_rows:
        r = ref_map[a["fixture_id"]]
        d = float(a[key]) - float(r[key])
        deltas.append(d)
        dt = parse_utc_timestamp(a["kickoff_utc"])
        bkey = dt.strftime("%Y-%m-%d") if block_mode == "day" else f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        blocks[bkey].append(d)
    bsums = [(sum(blocks[k]), len(blocks[k])) for k in sorted(blocks.keys())]
    m = len(bsums)
    mean_d = sum(deltas) / max(1, len(deltas))
    if m < 5:
        return {"n": len(deltas), "n_blocks": m, "mean_delta": round(mean_d, 6), "ci_95": None, "prob_improves_lt_0": None}
    rng = random.Random(BOOT_SEED)
    boots: list[float] = []
    for _ in range(BOOT_REPLICATES):
        s = 0.0
        c = 0
        for _ in range(m):
            bs, bn = bsums[rng.randrange(m)]
            s += bs
            c += bn
        boots.append(s / max(1, c))
    boots.sort()
    lo = boots[int(0.025 * (BOOT_REPLICATES - 1))]
    hi = boots[int(0.975 * (BOOT_REPLICATES - 1))]
    p_imp = sum(1 for v in boots if v < 0.0) / BOOT_REPLICATES
    return {
        "n": len(deltas),
        "n_blocks": m,
        "mean_delta": round(mean_d, 6),
        "ci_95": [round(lo, 6), round(hi, 6)],
        "prob_improves_lt_0": round(p_imp, 4),
    }


def _classify_verdict(
    *,
    rps_main_wf: dict[str, Any],
    ll_main_wf: dict[str, Any],
    o25_main_wf: dict[str, Any],
    rps_alt_wf: dict[str, Any],
    fold_deltas_rps: list[float],
    n_affected: int | None = None,
) -> str:
    if n_affected is not None and n_affected < 50:
        return "EVIDENCIA_INSUFICIENTE"
    ci_r = rps_main_wf.get("ci_95")
    ci_l = ll_main_wf.get("ci_95")
    ci_o = o25_main_wf.get("ci_95")
    ci_a = rps_alt_wf.get("ci_95")
    # Mejora estadísticamente respaldada en ambas métricas principales y consistente en folds y partición alternativa sin dañar O2.5
    if (
        ci_r is not None
        and ci_l is not None
        and ci_a is not None
        and ci_r[1] < 0.0
        and ci_l[1] < 0.0
        and ci_a[1] < 0.0
        and all(d < 0.0 for d in fold_deltas_rps)
        and (ci_o is None or ci_o[0] <= 0.002)
    ):
        return "MEJORA_RESPALDADA"
    # Empeora si intervalo de confianza excluye 0 positivamente o daña consistentemente
    if (
        (ci_r is not None and ci_r[0] > 0.0)
        or (ci_l is not None and ci_l[0] > 0.0)
        or (rps_main_wf["mean_delta"] > 0.0 and ll_main_wf["mean_delta"] > 0.0 and rps_alt_wf["mean_delta"] > 0.0)
    ):
        return "EMPEORA"
    return "MEJORA_NO_CONCLUYENTE"


def _compute_venue_gap_flag(rec: dict[str, Any]) -> bool:
    for side in ("hp_rev", "ap_rev"):
        ms = [m for m in rec[side][:20] if m.own_metrics.get("goals") is not None]
        h_g = [float(m.own_metrics["goals"]) for m in ms if m.venue == "HOME"]
        a_g = [float(m.own_metrics["goals"]) for m in ms if m.venue == "AWAY"]
        if len(h_g) >= 3 and len(a_g) >= 3:
            if abs(statistics.fmean(h_g) - statistics.fmean(a_g)) >= 0.70:
                return True
    return False


def _variants() -> list[VariantSpec]:
    return [
        VariantSpec("B0_baseline_frozen", "BASELINE", "CoherentMatchModeler_baseline_2.0_2.1A congelado (1.56/1.26, k=8, vb=0.25)"),
        # Experimento A — Priors y contracción
        VariantSpec("A1_emp_stratum_priors", "A_PRIORS", "Priors empíricos por estrato ajustados as-of < T_fold (m_st=25, k=8)", prior_mode="empirical_stratum"),
        VariantSpec("A2_emp_league_priors", "A_PRIORS", "Priors jerárquicos empíricos por liga/estrato as-of < T_fold (m_lg=30, k=8)", prior_mode="empirical_league"),
        VariantSpec("A3_shrink_k_4", "A_PRIORS", "Priors baseline (1.56/1.26) con menor contracción shrink_k=4.0", shrink_k=4.0),
        VariantSpec("A4_shrink_k_2", "A_PRIORS", "Priors baseline (1.56/1.26) con contracción ligera shrink_k=2.0", shrink_k=2.0),
        VariantSpec("A5_shrink_k_12", "A_PRIORS", "Priors baseline (1.56/1.26) con mayor contracción shrink_k=12.0", shrink_k=12.0),
        VariantSpec("A6_kish_neff_k6", "A_PRIORS", "Tamaño efectivo Kish n_eff con shrink_k=6.0 sobre priors baseline", shrink_k=6.0, use_kish_neff=True),
        # Experimento B — Localía y contracción por sede
        VariantSpec("B1_venue_blend_0", "B_VENUE", "Sin mezcla empírica de sede (venue_blend=0.0)", venue_mode="blend_0", venue_blend=0.0),
        VariantSpec("B2_venue_blend_50_unshrunk", "B_VENUE", "Mezcla de sede 50% sin contracción (cuando venue_n >= 3)", venue_mode="blend_50_unshrunk", venue_blend=0.50),
        VariantSpec("B3_venue_shrunk_50_k4", "B_VENUE", "Mezcla 50% con promedio de sede contraído (k_v=4.0 hacia ancla de equipo+sede)", venue_mode="shrunk_anchor_50", venue_blend=0.50, venue_k=4.0),
        VariantSpec("B4_venue_shrunk_70_k4", "B_VENUE", "Mezcla 70% con promedio de sede contraído (k_v=4.0 hacia ancla de equipo+sede)", venue_mode="shrunk_anchor_70", venue_blend=0.70, venue_k=4.0),
        VariantSpec("B5_venue_shrunk_80_k4", "B_VENUE", "Mezcla 80% con promedio de sede contraído (k_v=4.0 hacia ancla de equipo+sede)", venue_mode="shrunk_anchor_80", venue_blend=0.80, venue_k=4.0),
        # Experimento C — Política de competiciones
        VariantSpec("C1_exclude_friendlies_fb4", "C_COMPETITIONS", "Excluye amistosos del historial si quedan >= 4 partidos no-amistosos", comp_policy="exclude_friendlies_fallback"),
        VariantSpec("C2_exclude_friendlies_strict", "C_COMPETITIONS", "Excluye todo partido amistoso del historial sin fallback (mide caída de cobertura si < 6)", comp_policy="exclude_friendlies_strict"),
        VariantSpec("C3_restrict_cups_league_ge6", "C_COMPETITIONS", "En objetivos de liga doméstica excluye copas si hay >= 6 partidos de liga", comp_policy="restrict_cups_league_ge6"),
        VariantSpec("C4_strict_comp_identity", "C_COMPETITIONS", "Identidad competitiva estricta (UNKNOWN != UNKNOWN, league_id/season > 0)", comp_policy="strict_comp_identity", strict_competition_identity=True),
    ]


def run_experiments(root: Path, out_json: Path) -> dict[str, Any]:
    # 1. Verificación previa de referencia congelada y no regresión
    source_meta, ts_clean, af_clean, meta_clean = _load_corpus(root, order_invariant=True, use_league_catalog=True)
    _, ts_base, af_base, meta_base = _load_corpus(root, order_invariant=False, use_league_catalog=False)

    def _build_records(ts_map, af_map) -> list[dict[str, Any]]:
        to = {t: sorted(f.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id)) for t, f in ts_map.items()}
        recs: list[dict[str, Any]] = []
        for fid in sorted(af_map.keys()):
            r, reason = _extract_fixture_record(fid, af_map[fid], to)
            if r is None:
                continue
            r["high_venue_gap"] = _compute_venue_gap_flag(r)
            recs.append(r)
        recs.sort(key=lambda r: (r["kickoff_dt"], r["fixture_id"]))
        return recs

    recs_clean = _build_records(ts_clean, af_clean)
    recs_base = _build_records(ts_base, af_base)
    assert len(recs_clean) == 4496, f"Expected 4496, got {len(recs_clean)}"
    train_clean = [r for r in recs_clean if r["kickoff_utc"] < BOUNDARY_STRICT_ISO]
    test_clean = [r for r in recs_clean if r["kickoff_utc"] >= BOUNDARY_STRICT_ISO]
    assert len(train_clean) == 2173 and len(test_clean) == 2323

    # Comprobar paridad exacta de B0 frente a _estimate_team_rate_detailed de producción
    b0_spec = _variants()[0]
    max_parity_diff = 0.0
    for r in train_clean[:200]:
        eh_prod = _estimate_team_rate_detailed(
            r["hp_rev"], r["ap_rev"], metric="goals", target_league_id=r["league_id"], target_season=r["season"],
            target_stratum=r["stratum"], own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", r["stratum"], "HOME"), history_limit=20,
        )
        ea_prod = _estimate_team_rate_detailed(
            r["ap_rev"], r["hp_rev"], metric="goals", target_league_id=r["league_id"], target_season=r["season"],
            target_stratum=r["stratum"], own_venue="AWAY", opp_venue="HOME",
            prior_mean=_resolve_prior("goals", r["stratum"], "AWAY"), history_limit=20,
        )
        pr_var = _predict_variant(r, b0_spec, None, window=20)
        max_parity_diff = max(
            max_parity_diff,
            abs(float(eh_prod["lambda"]) - pr_var["lambda_home"]),
            abs(float(ea_prod["lambda"]) - pr_var["lambda_away"]),
        )
    assert max_parity_diff < 1e-12, f"Baseline parity mismatch: {max_parity_diff}"

    # 2. Definir esquemas walk-forward internos a train_strict (sin tocar test_strict para selección)
    main_cuts = [
        ("fold1", "2026-08-26T00:30:00+00:00", "2026-09-05T22:00:00+00:00"),
        ("fold2", "2026-09-05T22:00:00+00:00", "2026-09-15T22:30:00+00:00"),
        ("fold3", "2026-09-15T22:30:00+00:00", BOUNDARY_STRICT_ISO),
    ]
    alt_cuts = [
        ("alt1", "2026-08-22T14:15:00+00:00", "2026-09-05T13:30:00+00:00"),
        ("alt2", "2026-09-05T13:30:00+00:00", "2026-09-13T01:10:00+00:00"),
        ("alt3", "2026-09-13T01:10:00+00:00", BOUNDARY_STRICT_ISO),
    ]

    def _run_wf_schedule(cuts: list[tuple[str, str, str]], recs_pool: list[dict[str, Any]], spec: VariantSpec, *, window: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pooled: list[dict[str, Any]] = []
        fold_meta: dict[str, Any] = {}
        for fname, start_iso, end_iso in cuts:
            start_dt = parse_utc_timestamp(start_iso)
            emp_fit = _fit_empirical_priors_asof(af_clean, start_dt) if spec.prior_mode != "fixed" else None
            val_recs = [r for r in recs_pool if start_iso <= r["kickoff_utc"] < end_iso]
            if emp_fit is not None:
                val_fids = {r["fixture_id"] for r in val_recs}
                assert not (emp_fit["fit_fids"] & val_fids), "Fixture overlap between empirical fit and val!"
            preds = [_predict_variant(r, spec, emp_fit, window=window) for r in val_recs]
            pooled.extend(preds)
            fold_meta[fname] = {
                "val_n": len(preds),
                "fit_n": emp_fit["fit_n"] if emp_fit else None,
                "rps": round(statistics.fmean(x["rps"] for x in preds), 5) if preds else None,
                "ll_1x2": round(statistics.fmean(x["ll_1x2"] for x in preds), 5) if preds else None,
                "bias_hw": round(statistics.fmean(x["p_home"] - x["y1"] for x in preds), 4) if preds else None,
            }
        return pooled, fold_meta

    specs = _variants()
    wf_main_preds: dict[str, list[dict[str, Any]]] = {}
    wf_main_folds: dict[str, dict[str, Any]] = {}
    wf_alt_preds: dict[str, list[dict[str, Any]]] = {}
    wf_alt_folds: dict[str, dict[str, Any]] = {}

    for sp in specs:
        wf_main_preds[sp.variant_id], wf_main_folds[sp.variant_id] = _run_wf_schedule(main_cuts, train_clean, sp, window=20)
        wf_alt_preds[sp.variant_id], wf_alt_folds[sp.variant_id] = _run_wf_schedule(alt_cuts, train_clean, sp, window=20)

    # Pasada única confirmatoria sobre test_strict (empirical priors ajustados estrictamente < BOUNDARY_STRICT_ISO)
    test_emp_fit = _fit_empirical_priors_asof(af_clean, parse_utc_timestamp(BOUNDARY_STRICT_ISO))
    assert not (test_emp_fit["fit_fids"] & {r["fixture_id"] for r in test_clean})
    test_preds: dict[str, list[dict[str, Any]]] = {}
    for sp in specs:
        fit_obj = test_emp_fit if sp.prior_mode != "fixed" else None
        test_preds[sp.variant_id] = [_predict_variant(r, sp, fit_obj, window=20) for r in test_clean]

    # Detectar subconjunto realmente afectado por C2_exclude_friendlies_strict (< 6 previos tras quitar amistosos)
    c2_clean_fids_wf = {
        r["fixture_id"]
        for r in [x for x in train_clean if x["kickoff_utc"] >= main_cuts[0][1]]
        if sum(1 for m in r["hp_rev"][:20] if not _is_friendly_match(m) and not m.red_card_distortion and m.own_metrics.get("goals") is not None) >= 6
        and sum(1 for m in r["ap_rev"][:20] if not _is_friendly_match(m) and not m.red_card_distortion and m.own_metrics.get("goals") is not None) >= 6
    }

    ref_main = wf_main_preds["B0_baseline_frozen"]
    ref_alt = wf_alt_preds["B0_baseline_frozen"]
    ref_test = test_preds["B0_baseline_frozen"]

    variants_report: dict[str, Any] = {}
    for sp in specs:
        vid = sp.variant_id
        rows_m = wf_main_preds[vid]
        rows_a = wf_alt_preds[vid]
        rows_t = test_preds[vid]

        d_rps_m = _paired_boot_delta(rows_m, ref_main, "rps", block_mode="day")
        d_ll_m = _paired_boot_delta(rows_m, ref_main, "ll_1x2", block_mode="day")
        d_b1_m = _paired_boot_delta(rows_m, ref_main, "brier_1x2", block_mode="day")
        d_o25_m = _paired_boot_delta(rows_m, ref_main, "brier_o25", block_mode="day")
        d_btts_m = _paired_boot_delta(rows_m, ref_main, "brier_btts", block_mode="day")
        d_rps_a = _paired_boot_delta(rows_a, ref_alt, "rps", block_mode="day")
        d_rps_w = _paired_boot_delta(rows_m, ref_main, "rps", block_mode="week")

        fold_deltas = [
            round((wf_main_folds[vid][fk]["rps"] or 0.0) - (wf_main_folds["B0_baseline_frozen"][fk]["rps"] or 0.0), 6)
            for fk in ("fold1", "fold2", "fold3")
        ]
        verdict = (
            "BASELINE"
            if vid == "B0_baseline_frozen"
            else _classify_verdict(
                rps_main_wf=d_rps_m,
                ll_main_wf=d_ll_m,
                o25_main_wf=d_o25_m,
                rps_alt_wf=d_rps_a,
                fold_deltas_rps=fold_deltas,
            )
        )

        # Segmentos requeridos en train_wf
        seg_wf = {
            "ALL": _summarize_rows(rows_m),
            "DOMESTIC_LEAGUE": _summarize_rows([x for x in rows_m if x["stratum"] == "DOMESTIC_LEAGUE"]),
            "UNKNOWN": _summarize_rows([x for x in rows_m if x["stratum"] == "UNKNOWN"]),
            "DOMESTIC_CUP": _summarize_rows([x for x in rows_m if x["stratum"] == "DOMESTIC_CUP"]),
            "NATIONAL_TEAM": _summarize_rows([x for x in rows_m if x["stratum"] == "NATIONAL_TEAM"]),
            "HIGH_VENUE_GAP": _summarize_rows([x for x in rows_m if x["high_venue_gap"]]),
            "LOW_DEPTH_MIN_6_9": _summarize_rows([x for x in rows_m if x["min_prior"] < 10]),
            "DEPTH_10_19": _summarize_rows([x for x in rows_m if x["min_prior"] >= 10]),
            "BOTH_SIDES": _summarize_rows([x for x in rows_m if x["side_mode"] == "BOTH_SIDES"]),
        }

        d_rps_t = _paired_boot_delta(rows_t, ref_test, "rps", block_mode="day")
        d_ll_t = _paired_boot_delta(rows_t, ref_test, "ll_1x2", block_mode="day")
        d_o25_t = _paired_boot_delta(rows_t, ref_test, "brier_o25", block_mode="day")

        extra_info: dict[str, Any] = {}
        if vid != "B0_baseline_frozen":
            chg_idx = [
                i for i, (x, y) in enumerate(zip(rows_m, ref_main))
                if abs(x["lambda_home"] - y["lambda_home"]) > 1e-12 or abs(x["lambda_away"] - y["lambda_away"]) > 1e-12
            ]
            chg_idx_t = [
                i for i, (x, y) in enumerate(zip(rows_t, ref_test))
                if abs(x["lambda_home"] - y["lambda_home"]) > 1e-12 or abs(x["lambda_away"] - y["lambda_away"]) > 1e-12
            ]
            if chg_idx:
                extra_info["changed_subset_wf"] = {
                    "changed_n": len(chg_idx),
                    "delta_rps": _paired_boot_delta([rows_m[i] for i in chg_idx], [ref_main[i] for i in chg_idx], "rps", block_mode="day"),
                    "delta_ll_1x2": _paired_boot_delta([rows_m[i] for i in chg_idx], [ref_main[i] for i in chg_idx], "ll_1x2", block_mode="day"),
                    "variant_summary": _summarize_rows([rows_m[i] for i in chg_idx]),
                    "b0_summary": _summarize_rows([ref_main[i] for i in chg_idx]),
                }
            if chg_idx_t:
                extra_info["changed_subset_test"] = {
                    "changed_n": len(chg_idx_t),
                    "delta_rps": _paired_boot_delta([rows_t[i] for i in chg_idx_t], [ref_test[i] for i in chg_idx_t], "rps", block_mode="day"),
                    "delta_ll_1x2": _paired_boot_delta([rows_t[i] for i in chg_idx_t], [ref_test[i] for i in chg_idx_t], "ll_1x2", block_mode="day"),
                }
        if vid == "C2_exclude_friendlies_strict":
            sub_c2 = [x for x in rows_m if x["fixture_id"] in c2_clean_fids_wf]
            sub_b0 = [x for x in ref_main if x["fixture_id"] in c2_clean_fids_wf]
            extra_info["strict_non_friendly_subset"] = {
                "kept_n": len(sub_c2),
                "dropped_below_6_n": len(rows_m) - len(sub_c2),
                "delta_rps_kept_only": _paired_boot_delta(sub_c2, sub_b0, "rps", block_mode="day"),
            }

        variants_report[vid] = {
            "group": sp.group,
            "description": sp.description,
            "verdict_from_train_wf": verdict,
            "train_wf_main": {
                "summary": seg_wf,
                "folds": wf_main_folds[vid],
                "fold_rps_deltas_vs_b0": fold_deltas,
                "paired_delta_vs_b0_day": {
                    "rps": d_rps_m,
                    "log_loss_1x2": d_ll_m,
                    "brier_1x2": d_b1_m,
                    "brier_o25": d_o25_m,
                    "brier_btts": d_btts_m,
                },
                "paired_delta_vs_b0_week": {"rps": d_rps_w},
            },
            "train_wf_alt_partition": {
                "summary": _summarize_rows(rows_a),
                "folds": wf_alt_folds[vid],
                "paired_delta_rps_vs_b0_day": d_rps_a,
            },
            "confirmatory_test_strict_locked": {
                "ALL": _summarize_rows(rows_t),
                "DOMESTIC_LEAGUE": _summarize_rows([x for x in rows_t if x["stratum"] == "DOMESTIC_LEAGUE"]),
                "UNKNOWN": _summarize_rows([x for x in rows_t if x["stratum"] == "UNKNOWN"]),
                "HIGH_VENUE_GAP": _summarize_rows([x for x in rows_t if x["high_venue_gap"]]),
                "RECONSTRUCTED": _summarize_rows([x for x in rows_t if x["side_mode"] != "BOTH_SIDES"]),
                "BOTH_SIDES": _summarize_rows([x for x in rows_t if x["side_mode"] == "BOTH_SIDES"]),
                "paired_delta_vs_b0_day": {
                    "rps": d_rps_t,
                    "log_loss_1x2": d_ll_t,
                    "brier_o25": d_o25_t,
                },
            },
            **extra_info,
        }

    # 3. Sensibilidad L6 / L10 / L20 sobre top candidatos de train WF + B0
    window_sensitivity: dict[str, Any] = {}
    key_vids = ("B0_baseline_frozen", "A1_emp_stratum_priors", "A3_shrink_k_4", "B3_venue_shrunk_50_k4", "B4_venue_shrunk_70_k4", "C1_exclude_friendlies_fb4", "C4_strict_comp_identity")
    for vid in key_vids:
        sp = next(x for x in specs if x.variant_id == vid)
        window_sensitivity[vid] = {}
        for w in (6, 10, 20):
            rows_w, _ = _run_wf_schedule(main_cuts, train_clean, sp, window=w)
            rows_w_test = [_predict_variant(r, sp, test_emp_fit if sp.prior_mode != "fixed" else None, window=w) for r in test_clean]
            window_sensitivity[vid][f"L{w}"] = {
                "train_wf": _summarize_rows(rows_w),
                "test_confirmatory": _summarize_rows(rows_w_test),
            }

    manifest_v2_path = root / "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_version": MODEL_BASELINE_VERSION,
        "manifest_v2_sha256": hashlib.sha256(manifest_v2_path.read_bytes()).hexdigest() if manifest_v2_path.is_file() else None,
        "reference_verification": {
            "max_parity_diff_custom_b0_vs_prod_estimator": max_parity_diff,
            "corpus_total_prematch_eligible": len(recs_clean),
            "train_strict_n": len(train_clean),
            "test_strict_n": len(test_clean),
            "train_wf_main_val_n": len(ref_main),
            "train_wf_alt_val_n": len(ref_alt),
            "league_catalog_size": meta_clean["league_catalog_size"],
            "test_strict_baseline_rps_with_catalog": _summarize_rows(ref_test)["rps"],
            "legacy_base_secondary_4489_test_n": sum(1 for r in recs_base if (not r["rc_target"]) and r["kickoff_utc"] >= BOUNDARY_STRICT_ISO),
        },
        "empirical_priors_fit_snapshot_before_test": {
            "fit_n": test_emp_fit["fit_n"],
            "global": [round(x, 4) for x in test_emp_fit["global"]],
            "stratum": {k: [round(v[0], 4), round(v[1], 4)] for k, v in sorted(test_emp_fit["stratum"].items())},
        },
        "variants": variants_report,
        "window_depth_sensitivity": window_sensitivity,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    out = Path("audit/fase2b_experimentos/resultados_ab.json")
    rep = run_experiments(Path(".").resolve(), out)
    summary = {
        vid: {
            "verdict": v["verdict_from_train_wf"],
            "wf_rps": v["train_wf_main"]["summary"]["ALL"]["rps"],
            "wf_d_rps": v["train_wf_main"]["paired_delta_vs_b0_day"]["rps"]["mean_delta"],
            "wf_ci": v["train_wf_main"]["paired_delta_vs_b0_day"]["rps"]["ci_95"],
            "wf_bias1": v["train_wf_main"]["summary"]["ALL"]["bias_home_win"],
            "test_rps": v["confirmatory_test_strict_locked"]["ALL"]["rps"],
            "test_bias1": v["confirmatory_test_strict_locked"]["ALL"]["bias_home_win"],
        }
        for vid, v in rep["variants"].items()
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
