#!/usr/bin/env python3
"""Aislamiento de mecanismos y evaluación controlada (Fase 2B.3B).

No modifica producción ni utiliza test_strict para seleccionar parámetros.
Ejecuta dentro de train_strict (walk-forward principal n=1,447 y partición
alternativa n=1,550):
1) Auditoría completa de Fuerza del Rival (SoS/Elo) y disponibilidad temporal;
2) Descomposición 2x2 de A6 (B0, Kish+k=8, sum(w)+k=6, Kish+k=6);
3) Análisis granular de localía (B0, B3 50% k_v=4, B4 70% k_v=4) separando
   calibración (ECE/Murphy Reliability) vs puntuación predictiva (RPS/LL/Brier/Resolution);
4) Integración controlada opcional (solo si lo justifica el aislamiento previo),
   separando explícitamente C4 (corrección de integridad) de las ganancias predictivas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bet_engine.modeling.calibration import (
    brier_score,
    expected_calibration_error,
    log_loss,
    parse_utc_timestamp,
)
from bet_engine.modeling.distributions import (
    MODEL_VERSION,
    _estimate_team_rate_detailed,
    _resolve_prior,
)
from bet_engine.modeling.experimental_v2_1b import AsOfEloTracker
from tools.reproduce_baseline_backtest import _extract_fixture_record, _load_corpus
from tools.run_fase2b3a_experiments import (
    BOOT_REPLICATES,
    BOUNDARY_STRICT_ISO,
    VariantSpec,
    _compute_venue_gap_flag,
    _paired_boot_delta,
    _predict_variant,
)
from tools.run_iteracion_2_1b_experiments import _build_all_chronological_fixtures

MAIN_CUTS = [
    ("fold1", "2026-08-26T00:30:00+00:00", "2026-09-05T22:00:00+00:00"),
    ("fold2", "2026-09-05T22:00:00+00:00", "2026-09-15T22:30:00+00:00"),
    ("fold3", "2026-09-15T22:30:00+00:00", BOUNDARY_STRICT_ISO),
]
ALT_CUTS = [
    ("alt1", "2026-08-22T14:15:00+00:00", "2026-09-05T13:30:00+00:00"),
    ("alt2", "2026-09-05T13:30:00+00:00", "2026-09-13T01:10:00+00:00"),
    ("alt3", "2026-09-13T01:10:00+00:00", BOUNDARY_STRICT_ISO),
]


def _compute_venue_history_counts(rec: dict[str, Any]) -> dict[str, Any]:
    def _cnt(ms: list[Any], venue: str, metric_key: str) -> int:
        c = 0
        for m in ms:
            if m.red_card_distortion:
                continue
            m_dict = m.own_metrics if metric_key == "own" else m.conceded_metrics
            if m_dict.get("goals") is None:
                continue
            if m.venue == venue:
                c += 1
        return min(c, 20)

    def _valid_total(ms: list[Any]) -> int:
        return sum(1 for m in ms[:20] if not m.red_card_distortion and m.own_metrics.get("goals") is not None)

    hp = [m for m in rec["hp_rev"] if not m.red_card_distortion and m.own_metrics.get("goals") is not None][:20]
    ap = [m for m in rec["ap_rev"] if not m.red_card_distortion and m.own_metrics.get("goals") is not None][:20]
    hh = sum(1 for m in hp if m.venue == "HOME")
    ah_con = sum(1 for m in ap if m.venue == "AWAY")
    aa = sum(1 for m in ap if m.venue == "AWAY")
    hh_con = sum(1 for m in hp if m.venue == "HOME")
    return {
        "n_valid_home_l20": len(hp),
        "n_valid_away_l20": len(ap),
        "min_depth": min(len(hp), len(ap)),
        "own_home_n": hh,
        "opp_away_conceded_n": ah_con,
        "own_away_n": aa,
        "opp_home_conceded_n": hh_con,
        "min_venue_n": min(hh, ah_con, aa, hh_con),
        "min_own_venue_n": min(hh, aa),
    }


def _murphy_brier_decomp(pairs: list[tuple[float, int]], n_bins: int = 5) -> dict[str, Any]:
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    pbar = sum(y for _, y in pairs) / n
    unc = pbar * (1.0 - pbar)
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in pairs:
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[idx].append((p, y))
    rel = 0.0
    res = 0.0
    for b in bins:
        if not b:
            continue
        nb = len(b)
        pm = sum(x[0] for x in b) / nb
        om = sum(x[1] for x in b) / nb
        rel += (nb / n) * ((pm - om) ** 2)
        res += (nb / n) * ((om - pbar) ** 2)
    brier_val = sum((p - y) ** 2 for p, y in pairs) / n
    return {
        "n": n,
        "brier": round(brier_val, 6),
        "reliability": round(rel, 6),
        "resolution": round(res, 6),
        "uncertainty": round(unc, 6),
    }


def _predict_extended(rec: dict[str, Any], spec: VariantSpec, *, window: int = 20) -> dict[str, Any]:
    row = _predict_variant(rec, spec, None, window=window)
    lh, la = row["lambda_home"], row["lambda_away"]
    hg, ag = row["hg"], row["ag"]
    eps = 1e-9
    pnll_h = -(hg * math.log(max(eps, lh)) - lh - math.lgamma(hg + 1.0))
    pnll_a = -(ag * math.log(max(eps, la)) - la - math.lgamma(ag + 1.0))
    p1, p2 = row["p_home"], row["p_away"]
    pmax = max(p1, p2)
    vc = rec["venue_counts"]
    row.update(
        {
            "lambda_total": round(lh + la, 6),
            "total_goals": hg + ag,
            "goals_err_total": (lh + la) - (hg + ag),
            "goals_sq_err_total": ((lh + la) - (hg + ag)) ** 2,
            "poisson_nll_total": pnll_h + pnll_a,
            "p_max_ha": pmax,
            "fav_side": "HOME" if p1 >= p2 else "AWAY",
            "min_depth": vc["min_depth"],
            "min_venue_n": vc["min_venue_n"],
            "min_own_venue_n": vc["min_own_venue_n"],
            "own_home_n": vc["own_home_n"],
        }
    )
    return row


def _sub_bias(items: list[dict[str, Any]], pk: str, yk: str) -> dict[str, Any]:
    if not items:
        return {"n": 0}
    pm = statistics.fmean(x[pk] for x in items)
    ob = statistics.fmean(x[yk] for x in items)
    return {"n": len(items), "pred_mean": round(pm, 4), "obs_rate": round(ob, 4), "bias": round(pm - ob, 4)}


def _summarize_detailed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    hw_p = [(r["p_home"], r["y1"]) for r in rows]
    dr_p = [(r["p_draw"], r["yx"]) for r in rows]
    aw_p = [(r["p_away"], r["y2"]) for r in rows]
    o25_p = [(r["p_over_2_5"], r["yo"]) for r in rows]
    btts_p = [(r["p_btts_yes"], r["yb"]) for r in rows]

    m_hw = _murphy_brier_decomp(hw_p, n_bins=5)
    m_dr = _murphy_brier_decomp(dr_p, n_bins=5)
    m_aw = _murphy_brier_decomp(aw_p, n_bins=5)
    m_o25 = _murphy_brier_decomp(o25_p, n_bins=5)
    m_btts = _murphy_brier_decomp(btts_p, n_bins=5)

    fav_h_strong = [r for r in rows if r["p_home"] >= 0.55]
    fav_a_strong = [r for r in rows if r["p_away"] >= 0.55]
    fav_strong_all = [(r["p_home"], r["y1"]) for r in fav_h_strong] + [(r["p_away"], r["y2"]) for r in fav_a_strong]

    fav_h_weak = [r for r in rows if 0.45 <= r["p_home"] < 0.55 and r["p_home"] >= r["p_away"]]
    fav_a_weak = [r for r in rows if 0.45 <= r["p_away"] < 0.55 and r["p_away"] > r["p_home"]]
    fav_weak_all = [(r["p_home"], r["y1"]) for r in fav_h_weak] + [(r["p_away"], r["y2"]) for r in fav_a_weak]
    balanced = [r for r in rows if max(r["p_home"], r["p_away"]) < 0.45]

    lam_h_fav = [r for r in rows if (r["lambda_home"] - r["lambda_away"]) > 0.50]
    lam_a_fav = [r for r in rows if (r["lambda_home"] - r["lambda_away"]) < -0.50]

    return {
        "n": n,
        "1x2": {
            "rps": round(statistics.fmean(r["rps"] for r in rows), 5),
            "log_loss": round(statistics.fmean(r["ll_1x2"] for r in rows), 5),
            "brier_per_outcome": round(statistics.fmean(r["brier_1x2"] for r in rows), 5),
            "murphy_1x2_mean_reliability": round((m_hw["reliability"] + m_dr["reliability"] + m_aw["reliability"]) / 3.0, 6),
            "murphy_1x2_mean_resolution": round((m_hw["resolution"] + m_dr["resolution"] + m_aw["resolution"]) / 3.0, 6),
        },
        "binary": {
            "home_win": {
                "brier": brier_score(hw_p),
                "log_loss": log_loss(hw_p),
                "ece_5": expected_calibration_error(hw_p, n_bins=5),
                "bias": round(statistics.fmean(r["p_home"] - r["y1"] for r in rows), 4),
                "murphy": m_hw,
            },
            "draw": {
                "brier": brier_score(dr_p),
                "bias": round(statistics.fmean(r["p_draw"] - r["yx"] for r in rows), 4),
            },
            "away_win": {
                "brier": brier_score(aw_p),
                "log_loss": log_loss(aw_p),
                "ece_5": expected_calibration_error(aw_p, n_bins=5),
                "bias": round(statistics.fmean(r["p_away"] - r["y2"] for r in rows), 4),
                "murphy": m_aw,
            },
            "over_2_5": {
                "brier": brier_score(o25_p),
                "log_loss": log_loss(o25_p),
                "ece_5": expected_calibration_error(o25_p, n_bins=5),
                "bias": round(statistics.fmean(r["p_over_2_5"] - r["yo"] for r in rows), 4),
                "murphy": m_o25,
            },
            "btts_yes": {
                "brier": brier_score(btts_p),
                "log_loss": log_loss(btts_p),
                "ece_5": expected_calibration_error(btts_p, n_bins=5),
                "bias": round(statistics.fmean(r["p_btts_yes"] - r["yb"] for r in rows), 4),
                "murphy": m_btts,
            },
        },
        "goals_ft": {
            "bias_lambda_home": round(statistics.fmean(r["lambda_home"] - r["hg"] for r in rows), 4),
            "bias_lambda_away": round(statistics.fmean(r["lambda_away"] - r["ag"] for r in rows), 4),
            "bias_lambda_total": round(statistics.fmean(r["goals_err_total"] for r in rows), 4),
            "rmse_total_goals": round(math.sqrt(statistics.fmean(r["goals_sq_err_total"] for r in rows)), 5),
            "poisson_nll_total": round(statistics.fmean(r["poisson_nll_total"] for r in rows), 5),
            "mean_home_n_eff": round(statistics.fmean(r["home_n_eff"] for r in rows), 3),
            "mean_away_n_eff": round(statistics.fmean(r["away_n_eff"] for r in rows), 3),
        },
        "favorites": {
            "strong_p_ge_0_55": {
                "home": _sub_bias(fav_h_strong, "p_home", "y1"),
                "away": _sub_bias(fav_a_strong, "p_away", "y2"),
                "combined_bias": round(statistics.fmean(p - y for p, y in fav_strong_all), 4) if fav_strong_all else None,
                "combined_n": len(fav_strong_all),
            },
            "weak_p_0_45_to_0_55": {
                "home": _sub_bias(fav_h_weak, "p_home", "y1"),
                "away": _sub_bias(fav_a_weak, "p_away", "y2"),
                "combined_bias": round(statistics.fmean(p - y for p, y in fav_weak_all), 4) if fav_weak_all else None,
                "combined_n": len(fav_weak_all),
            },
            "balanced_pmax_lt_0_45": {
                "n": len(balanced),
                "home_bias": round(statistics.fmean(r["p_home"] - r["y1"] for r in balanced), 4) if balanced else None,
                "draw_bias": round(statistics.fmean(r["p_draw"] - r["yx"] for r in balanced), 4) if balanced else None,
                "away_bias": round(statistics.fmean(r["p_away"] - r["y2"] for r in balanced), 4) if balanced else None,
                "rps": round(statistics.fmean(r["rps"] for r in balanced), 5) if balanced else None,
            },
            "strength_diff_gt_0_50_home": _sub_bias(lam_h_fav, "p_home", "y1"),
            "strength_diff_lt_neg0_50_away": _sub_bias(lam_a_fav, "p_away", "y2"),
        },
    }


def _audit_rival_strength_feasibility(
    recs_train: list[dict[str, Any]],
    wf_main: list[dict[str, Any]],
    all_fixtures: dict[int, dict[str, Any]],
    team_ordered: dict[int, list[Any]],
) -> dict[str, Any]:
    """Audita los 5 criterios obligatorios de Fuerza del Rival (SoS/Elo) antes de usar cualquier ajuste."""
    chron = _build_all_chronological_fixtures(all_fixtures)
    elo_125 = AsOfEloTracker.build_from_fixtures(chron, k_factor=20.0, hfa=55.0)
    chron_240 = [dict(r, avail_dt=r["ko_dt"] + timedelta(minutes=240)) for r in chron]
    elo_240 = AsOfEloTracker.build_from_fixtures(chron_240, k_factor=20.0, hfa=55.0)

    fid_info: dict[int, tuple[int, str, str]] = {}
    raw_offsets: Counter[float] = Counter()
    mixed_teams = 0
    gaps_le_180m = 0
    for tid, ms in team_ordered.items():
        pools = set()
        for i, m in enumerate(ms):
            fid_info[m.fixture_id] = (int(m.league_id or 0), str(m.league_name or ""), str(m.competition_stratum or "UNKNOWN"))
            pools.add("NATIONAL_TEAM" if m.competition_stratum == "NATIONAL_TEAM" else "CLUB")
            diff_m = round((parse_utc_timestamp(m.available_at_utc) - parse_utc_timestamp(m.kickoff_utc)).total_seconds() / 60.0, 1)
            raw_offsets[diff_m] += 1
            if i > 0:
                gap_m = (parse_utc_timestamp(m.kickoff_utc) - parse_utc_timestamp(ms[i - 1].kickoff_utc)).total_seconds() / 60.0
                if gap_m <= 180.0:
                    gaps_le_180m += 1
        if len(pools) > 1:
            mixed_teams += 1

    # Conectividad y desglose cronológico
    chron_breakdown: Counter[str] = Counter()
    adj: dict[int, set[int]] = defaultdict(set)
    adj_no_fr: dict[int, set[int]] = defaultdict(set)
    for fx in chron:
        lid, lname, st = fid_info.get(fx["fixture_id"], (0, "", fx["stratum"]))
        is_fr = (lid == 667 or "friendl" in lname.lower() or "amistos" in lname.lower())
        chron_breakdown["friendly" if is_fr else ("unknown_stratum" if st == "UNKNOWN" else "official_known")] += 1
        if fx["stratum"] != "NATIONAL_TEAM":
            adj[fx["home_id"]].add(fx["away_id"])
            adj[fx["away_id"]].add(fx["home_id"])
            if not is_fr:
                adj_no_fr[fx["home_id"]].add(fx["away_id"])
                adj_no_fr[fx["away_id"]].add(fx["home_id"])

    def _comp_stats(g: dict[int, set[int]]) -> tuple[int, list[int]]:
        seen: set[int] = set()
        sizes: list[int] = []
        for n in g:
            if n in seen:
                continue
            q = deque([n])
            seen.add(n)
            sz = 0
            while q:
                u = q.popleft()
                sz += 1
                for v in g[u]:
                    if v not in seen:
                        seen.add(v)
                        q.append(v)
            sizes.append(sz)
        sizes.sort(reverse=True)
        return len(sizes), sizes[:5]

    comps_with_fr, top_with_fr = _comp_stats(adj)
    comps_no_fr, top_no_fr = _comp_stats(adj_no_fr)

    # Cobertura SoS y sensibilidad Elo en train_wf_main
    wf_diff_125_240 = 0
    wf_elo_ge3 = 0
    wf_elo_h_depths: list[int] = []
    wf_elo_a_depths: list[int] = []
    sos_checked = 0
    sos_covered_ge3 = 0
    for r in wf_main:
        ko = r["kickoff_dt"]
        r1h, n1h = elo_125.get_rating_asof(r["stratum"], r["home_id"], ko)
        r2h, n2h = elo_240.get_rating_asof(r["stratum"], r["home_id"], ko)
        r1a, n1a = elo_125.get_rating_asof(r["stratum"], r["away_id"], ko)
        r2a, n2a = elo_240.get_rating_asof(r["stratum"], r["away_id"], ko)
        wf_elo_h_depths.append(n1h)
        wf_elo_a_depths.append(n1a)
        if n1h >= 3 and n1a >= 3:
            wf_elo_ge3 += 1
        if abs(r1h - r2h) > 1e-9 or abs(r1a - r2a) > 1e-9 or n1h != n2h or n1a != n2a:
            wf_diff_125_240 += 1

        for m in r["hp_rev"][:20] + r["ap_rev"][:20]:
            if m.red_card_distortion or m.own_metrics.get("goals") is None:
                continue
            sos_checked += 1
            m_ko = parse_utc_timestamp(m.kickoff_utc)
            opp_hist = [
                x for x in team_ordered.get(m.opponent_id, ())
                if parse_utc_timestamp(x.available_at_utc) <= m_ko
                and parse_utc_timestamp(x.kickoff_utc) < m_ko
                and not x.red_card_distortion
                and x.own_metrics.get("goals") is not None
                and x.conceded_metrics.get("goals") is not None
            ]
            if len(opp_hist) >= 3:
                sos_covered_ge3 += 1

    train_elo_ge3 = sum(
        1 for r in recs_train
        if elo_125.get_rating_asof(r["stratum"], r["home_id"], r["kickoff_dt"])[1] >= 3
        and elo_125.get_rating_asof(r["stratum"], r["away_id"], r["kickoff_dt"])[1] >= 3
    )

    return {
        "audit_status_sos": "PENDIENTE_COBERTURA_INSUFICIENTE",
        "audit_status_elo": "PENDIENTE_POR_IDENTIDAD_TIMESTAMPS_Y_DESCONEXION",
        "temporal_availability": {
            "chron_fixtures_n": len(chron),
            "elo_provenance_counts": dict(elo_125.provenance_counts),
            "verified_timestamp_count": int(elo_125.provenance_counts.get("VERIFIED_TIMESTAMP", 0)),
            "raw_match_avail_offset_minutes": {str(k): v for k, v in raw_offsets.items()},
            "team_sequential_gaps_le_180m": gaps_le_180m,
            "wf_main_rating_diffs_125m_vs_240m": wf_diff_125_240,
        },
        "identity_and_graph_stability": {
            "teams_mixing_club_and_national_team_pools": mixed_teams,
            "chron_breakdown_23063": dict(chron_breakdown),
            "club_components_with_friendlies_n": comps_with_fr,
            "club_top_component_sizes_with_friendlies": top_with_fr,
            "club_components_without_friendlies_n": comps_no_fr,
            "club_top_component_sizes_without_friendlies": top_no_fr,
        },
        "coverage": {
            "train_strict_elo_ge3": f"{train_elo_ge3}/{len(recs_train)}",
            "wf_main_elo_ge3": f"{wf_elo_ge3}/{len(wf_main)}",
            "wf_main_elo_min_depth": min(min(wf_elo_h_depths), min(wf_elo_a_depths)),
            "wf_main_elo_mean_depth": round(0.5 * (statistics.fmean(wf_elo_h_depths) + statistics.fmean(wf_elo_a_depths)), 2),
            "wf_main_sos_opp_hist_ge3": sos_covered_ge3,
            "wf_main_sos_opp_pairs_total": sos_checked,
            "wf_main_sos_coverage_rate": round(sos_covered_ge3 / max(1, sos_checked), 4),
        },
        "missing_history_treatment": "Si elo_hn < 3 o elo_an < 3 vuelve al rating neutro 1500.0 sin multiplicador; en SoS, rivales sin >=3 partidos previos limpios en team_ordered_by_id quedan sin ajuste (>57% del total).",
    }


def _bootstrap_multiplicity(rows_v: list[dict[str, Any]], rows_ref: list[dict[str, Any]], *, m_family: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric in ("rps", "ll_1x2", "brier_1x2", "brier_o25", "brier_btts", "goals_sq_err_total", "poisson_nll_total"):
        day_b = _paired_boot_delta(rows_v, rows_ref, metric, block_mode="day")
        p_imp = day_b["prob_improves_lt_0"]
        if p_imp is not None:
            # p unilateral conservador (B=2000) ajustado por multiplicidad Bonferroni dentro del bloque
            k_bad = round((1.0 - p_imp) * BOOT_REPLICATES)
            p_one_sided = (k_bad + 1.0) / (BOOT_REPLICATES + 1.0)
            p_two_sided = min(1.0, 2.0 * min((round(p_imp * BOOT_REPLICATES) + 1.0) / (BOOT_REPLICATES + 1.0), p_one_sided))
            day_b["p_one_sided_improves"] = round(p_one_sided, 5)
            day_b["p_two_sided"] = round(p_two_sided, 5)
            day_b["bonferroni_m"] = m_family
            day_b["p_two_sided_bonferroni"] = round(min(1.0, p_two_sided * m_family), 5)
        out[metric] = day_b
    out["rps_week_block"] = _paired_boot_delta(rows_v, rows_ref, "rps", block_mode="week")
    return out


def _evaluate_specs(
    specs: list[VariantSpec],
    *,
    train_clean: list[dict[str, Any]],
    m_family_by_group: dict[str, int],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    wf_main: dict[str, list[dict[str, Any]]] = {}
    wf_alt: dict[str, list[dict[str, Any]]] = {}
    fold_rows: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)

    for sp in specs:
        pooled_m: list[dict[str, Any]] = []
        for fname, s_iso, e_iso in MAIN_CUTS:
            sub = [r for r in train_clean if s_iso <= r["kickoff_utc"] < e_iso]
            preds = [_predict_extended(r, sp, window=20) for r in sub]
            fold_rows[sp.variant_id][fname] = preds
            pooled_m.extend(preds)
        wf_main[sp.variant_id] = pooled_m

        pooled_a: list[dict[str, Any]] = []
        for fname, s_iso, e_iso in ALT_CUTS:
            sub = [r for r in train_clean if s_iso <= r["kickoff_utc"] < e_iso]
            preds = [_predict_extended(r, sp, window=20) for r in sub]
            fold_rows[sp.variant_id][fname] = preds
            pooled_a.extend(preds)
        wf_alt[sp.variant_id] = pooled_a

    ref_m = wf_main["B0_baseline_frozen"]
    ref_a = wf_alt["B0_baseline_frozen"]

    report: dict[str, Any] = {}
    for sp in specs:
        vid = sp.variant_id
        rows_m = wf_main[vid]
        rows_a = wf_alt[vid]
        m_fam = m_family_by_group.get(sp.group, 1)

        seg_defs = {
            "ALL": lambda x: True,
            "DOMESTIC_LEAGUE": lambda x: x["stratum"] == "DOMESTIC_LEAGUE",
            "UNKNOWN": lambda x: x["stratum"] == "UNKNOWN",
            "DOMESTIC_CUP": lambda x: x["stratum"] == "DOMESTIC_CUP",
            "NATIONAL_TEAM": lambda x: x["stratum"] == "NATIONAL_TEAM",
            "CONTINENTAL_CLUB": lambda x: x["stratum"] == "CONTINENTAL_CLUB",
            "LOW_DEPTH_MIN_6_9": lambda x: 6 <= x["min_depth"] <= 9,
            "MID_DEPTH_10_14": lambda x: 10 <= x["min_depth"] <= 14,
            "HIGH_DEPTH_GE_15": lambda x: x["min_depth"] >= 15,
            "INSUFFICIENT_VENUE_ANY_LT3": lambda x: x["min_venue_n"] < 3,
            "SUFFICIENT_VENUE_ALL_GE3": lambda x: x["min_venue_n"] >= 3,
            "INSUFFICIENT_OWN_HOME_LT3": lambda x: x["own_home_n"] < 3,
            "HIGH_VENUE_GAP": lambda x: bool(x["high_venue_gap"]),
        }
        segs = {sk: _summarize_detailed([r for r in rows_m if pred(r)]) for sk, pred in seg_defs.items()}

        folds_summary = {
            fk: _summarize_detailed(fold_rows[vid][fk])
            for fk in ("fold1", "fold2", "fold3", "alt1", "alt2", "alt3")
        }

        deltas_vs_b0 = {} if vid == "B0_baseline_frozen" else _bootstrap_multiplicity(rows_m, ref_m, m_family=m_fam)
        alt_rps_delta = {} if vid == "B0_baseline_frozen" else _paired_boot_delta(rows_a, ref_a, "rps", block_mode="day")

        report[vid] = {
            "group": sp.group,
            "description": sp.description,
            "spec": {
                "shrink_k": sp.shrink_k,
                "use_kish_neff": sp.use_kish_neff,
                "venue_mode": sp.venue_mode,
                "venue_blend": sp.venue_blend,
                "venue_k": sp.venue_k,
                "strict_competition_identity": sp.strict_competition_identity,
            },
            "train_wf_main": {
                "segments": segs,
                "folds": {k: folds_summary[k] for k in ("fold1", "fold2", "fold3")},
                "paired_delta_vs_b0": deltas_vs_b0,
            },
            "train_wf_alt": {
                "summary": _summarize_detailed(rows_a),
                "folds": {k: folds_summary[k] for k in ("alt1", "alt2", "alt3")},
                "paired_delta_rps_vs_b0": alt_rps_delta,
            },
        }
    return wf_main, wf_alt, report


def _variant_specs(*, include_integration: bool) -> list[VariantSpec]:
    specs = [
        VariantSpec("B0_baseline_frozen", "BASELINE", "CoherentMatchModeler_baseline_2.0_2.1A congelado (sum(w), k=8, vb=0.25 unshrunk)"),
        # Bloque 1: Descomposición A6 (2x2)
        VariantSpec("A6a_kish_k8", "A6_DECOMP", "Solo Kish n_eff manteniendo shrink_k=8.0", shrink_k=8.0, use_kish_neff=True),
        VariantSpec("A6b_sumw_k6", "A6_DECOMP", "Solo shrink_k=6.0 manteniendo sum(w) sin Kish", shrink_k=6.0, use_kish_neff=False),
        VariantSpec("A6_kish_k6", "A6_DECOMP", "Kish n_eff + shrink_k=6.0", shrink_k=6.0, use_kish_neff=True),
        # Bloque 2: Localía B3 / B4
        VariantSpec("B3_venue_shrunk_50_k4", "VENUE_SHRUNK", "Localía 50% con contracción bayesiana (venue_k=4.0)", venue_mode="shrunk_anchor_50", venue_blend=0.50, venue_k=4.0),
        VariantSpec("B4_venue_shrunk_70_k4", "VENUE_SHRUNK", "Localía 70% con contracción bayesiana (venue_k=4.0)", venue_mode="shrunk_anchor_70", venue_blend=0.70, venue_k=4.0),
        # Integridad C4 aislada para aislar su efecto respecto a B0
        VariantSpec("C4_strict_comp_identity", "INTEGRITY", "Solo identidad competitiva estricta C4 (UNKNOWN != UNKNOWN)", strict_competition_identity=True),
    ]
    if include_integration:
        specs.extend(
            [
                VariantSpec(
                    "INT_B3_A6b_k6",
                    "INTEGRATION",
                    "B3 (localía 50%, venue_k=4) + shrink_k=6 sin Kish (sin C4)",
                    shrink_k=6.0,
                    use_kish_neff=False,
                    venue_mode="shrunk_anchor_50",
                    venue_blend=0.50,
                    venue_k=4.0,
                ),
                VariantSpec(
                    "INT_B3_A6b_k6_C4",
                    "INTEGRATION",
                    "B3 (localía 50%, venue_k=4) + shrink_k=6 + identidad estricta C4",
                    shrink_k=6.0,
                    use_kish_neff=False,
                    venue_mode="shrunk_anchor_50",
                    venue_blend=0.50,
                    venue_k=4.0,
                    strict_competition_identity=True,
                ),
                VariantSpec(
                    "INT_B3_A6a_kish_k8",
                    "INTEGRATION",
                    "B3 (localía 50%, venue_k=4) + Kish n_eff con shrink_k=8 (sin C4)",
                    shrink_k=8.0,
                    use_kish_neff=True,
                    venue_mode="shrunk_anchor_50",
                    venue_blend=0.50,
                    venue_k=4.0,
                ),
                VariantSpec(
                    "INT_B3_A6a_kish_k8_C4",
                    "INTEGRATION",
                    "B3 (localía 50%, venue_k=4) + Kish n_eff k=8 + identidad estricta C4",
                    shrink_k=8.0,
                    use_kish_neff=True,
                    venue_mode="shrunk_anchor_50",
                    venue_blend=0.50,
                    venue_k=4.0,
                    strict_competition_identity=True,
                ),
                VariantSpec(
                    "INT_B3_A6_kish_k6_C4",
                    "INTEGRATION",
                    "B3 (localía 50%, venue_k=4) + Kish n_eff k=6 + identidad estricta C4",
                    shrink_k=6.0,
                    use_kish_neff=True,
                    venue_mode="shrunk_anchor_50",
                    venue_blend=0.50,
                    venue_k=4.0,
                    strict_competition_identity=True,
                ),
                VariantSpec(
                    "INT_B3_C4_only",
                    "INTEGRATION",
                    "B3 (localía 50%, venue_k=4, k=8 sum(w)) + identidad estricta C4",
                    shrink_k=8.0,
                    use_kish_neff=False,
                    venue_mode="shrunk_anchor_50",
                    venue_blend=0.50,
                    venue_k=4.0,
                    strict_competition_identity=True,
                ),
            ]
        )
    return specs


def run_fase2b3b(root: Path, out_json: Path, *, include_integration: bool = False) -> dict[str, Any]:
    _, ts_clean, af_clean, meta_clean = _load_corpus(root, order_invariant=True, use_league_catalog=True)
    to = {t: sorted(f.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id)) for t, f in ts_clean.items()}
    recs_clean: list[dict[str, Any]] = []
    for fid in sorted(af_clean.keys()):
        r, _ = _extract_fixture_record(fid, af_clean[fid], to)
        if r is None:
            continue
        r["high_venue_gap"] = _compute_venue_gap_flag(r)
        r["venue_counts"] = _compute_venue_history_counts(r)
        recs_clean.append(r)
    recs_clean.sort(key=lambda r: (r["kickoff_dt"], r["fixture_id"]))

    train_clean = [r for r in recs_clean if r["kickoff_utc"] < BOUNDARY_STRICT_ISO]
    wf_main_recs = [r for r in train_clean if MAIN_CUTS[0][1] <= r["kickoff_utc"] < BOUNDARY_STRICT_ISO]
    assert len(train_clean) == 2173 and len(wf_main_recs) == 1447

    # Verificación paridad B0 con _estimate_team_rate_detailed de producción
    b0 = VariantSpec("B0_baseline_frozen", "BASELINE", "ref")
    max_parity = 0.0
    for r in train_clean[:200]:
        eh_p = _estimate_team_rate_detailed(
            r["hp_rev"], r["ap_rev"], metric="goals", target_league_id=r["league_id"], target_season=r["season"],
            target_stratum=r["stratum"], own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", r["stratum"], "HOME"), history_limit=20,
        )
        ea_p = _estimate_team_rate_detailed(
            r["ap_rev"], r["hp_rev"], metric="goals", target_league_id=r["league_id"], target_season=r["season"],
            target_stratum=r["stratum"], own_venue="AWAY", opp_venue="HOME",
            prior_mean=_resolve_prior("goals", r["stratum"], "AWAY"), history_limit=20,
        )
        pr = _predict_extended(r, b0, window=20)
        max_parity = max(max_parity, abs(float(eh_p["lambda"]) - pr["lambda_home"]), abs(float(ea_p["lambda"]) - pr["lambda_away"]))
    assert max_parity < 1e-12, f"B0 parity mismatch: {max_parity}"

    specs = _variant_specs(include_integration=include_integration)
    m_counts = Counter(sp.group for sp in specs)
    wf_main_preds, wf_alt_preds, variants_rep = _evaluate_specs(
        specs, train_clean=train_clean, m_family_by_group=dict(m_counts)
    )

    pairwise_component_checks: dict[str, Any] = {}
    if include_integration:
        comparisons = [
            ("A6_kish_k6_vs_A6a_kish_k8", "A6_kish_k6", "A6a_kish_k8"),
            ("A6_kish_k6_vs_A6b_sumw_k6", "A6_kish_k6", "A6b_sumw_k6"),
            ("B4_vs_B3", "B4_venue_shrunk_70_k4", "B3_venue_shrunk_50_k4"),
            ("INT_B3_C4_only_vs_B3", "INT_B3_C4_only", "B3_venue_shrunk_50_k4"),
            ("INT_B3_A6a_kish_k8_C4_vs_B3", "INT_B3_A6a_kish_k8_C4", "B3_venue_shrunk_50_k4"),
            ("INT_B3_A6a_kish_k8_C4_vs_A6a_kish_k8", "INT_B3_A6a_kish_k8_C4", "A6a_kish_k8"),
            ("INT_B3_A6a_kish_k8_C4_vs_INT_B3_A6a_kish_k8", "INT_B3_A6a_kish_k8_C4", "INT_B3_A6a_kish_k8"),
            ("INT_B3_A6b_k6_C4_vs_B3", "INT_B3_A6b_k6_C4", "B3_venue_shrunk_50_k4"),
            ("INT_B3_A6b_k6_C4_vs_A6b_sumw_k6", "INT_B3_A6b_k6_C4", "A6b_sumw_k6"),
            ("INT_B3_A6_kish_k6_C4_vs_B3", "INT_B3_A6_kish_k6_C4", "B3_venue_shrunk_50_k4"),
            ("INT_B3_A6_kish_k6_C4_vs_A6_kish_k6", "INT_B3_A6_kish_k6_C4", "A6_kish_k6"),
            ("INT_B3_A6_kish_k6_C4_vs_INT_B3_A6a_kish_k8_C4", "INT_B3_A6_kish_k6_C4", "INT_B3_A6a_kish_k8_C4"),
        ]
    else:
        comparisons = [
            ("A6_kish_k6_vs_A6a_kish_k8", "A6_kish_k6", "A6a_kish_k8"),
            ("A6_kish_k6_vs_A6b_sumw_k6", "A6_kish_k6", "A6b_sumw_k6"),
            ("B4_vs_B3", "B4_venue_shrunk_70_k4", "B3_venue_shrunk_50_k4"),
        ]

    for cname, va, vb in comparisons:
        pairwise_component_checks[cname] = {
            "wf_main_day": {
                m: _paired_boot_delta(wf_main_preds[va], wf_main_preds[vb], m, block_mode="day")
                for m in ("rps", "ll_1x2", "brier_1x2", "brier_o25", "brier_btts", "poisson_nll_total")
            },
            "wf_alt_rps_day": _paired_boot_delta(wf_alt_preds[va], wf_alt_preds[vb], "rps", block_mode="day"),
        }

    rival_audit = _audit_rival_strength_feasibility(train_clean, wf_main_recs, af_clean, to)

    def _sha(rel: str) -> str:
        p = root / rel
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "phase": "2B.3B",
        "mode": "full_with_controlled_integration" if include_integration else "isolate_only",
        "baseline_version": MODEL_VERSION,
        "test_strict_policy": "DIAGNOSTIC_INSPECTED_EXCLUDED_FROM_SELECTION",
        "integrity_verification": {
            "max_parity_diff_b0_vs_prod": max_parity,
            "corpus_total_prematch_eligible": len(recs_clean),
            "train_strict_n": len(train_clean),
            "train_wf_main_val_n": len(wf_main_recs),
            "train_wf_alt_val_n": len(wf_alt_preds["B0_baseline_frozen"]),
            "league_catalog_size": meta_clean["league_catalog_size"],
            "code_hashes": {
                rel: _sha(rel)
                for rel in (
                    "bet_engine/modeling/distributions.py",
                    "bet_engine/modeling/experimental_v2_1b.py",
                    "bet_engine/context/observation_store.py",
                    "tools/run_fase2b3a_experiments.py",
                    "tools/run_fase2b3b_experiments.py",
                    "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json",
                    "audit/fase2b_experimentos/resultados_ab.json",
                )
            },
        },
        "rival_strength_sos_elo_audit": rival_audit,
        "variants": variants_rep,
        "pairwise_component_comparisons": pairwise_component_checks,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fase 2B.3B — Aislamiento e integración controlada")
    parser.add_argument("--stage", choices=("isolate", "full"), default="full")
    parser.add_argument("--out-json", type=Path, default=Path("audit/fase2b_experimentos/resultados_2b3b.json"))
    args = parser.parse_args()
    rep = run_fase2b3b(Path(".").resolve(), args.out_json, include_integration=(args.stage == "full"))
    summary = {
        vid: {
            "rps": v["train_wf_main"]["segments"]["ALL"]["1x2"]["rps"],
            "ll": v["train_wf_main"]["segments"]["ALL"]["1x2"]["log_loss"],
            "rel_1x2": v["train_wf_main"]["segments"]["ALL"]["1x2"]["murphy_1x2_mean_reliability"],
            "res_1x2": v["train_wf_main"]["segments"]["ALL"]["1x2"]["murphy_1x2_mean_resolution"],
            "o25_b": v["train_wf_main"]["segments"]["ALL"]["binary"]["over_2_5"]["brier"],
            "btts_b": v["train_wf_main"]["segments"]["ALL"]["binary"]["btts_yes"]["brier"],
            "bias_hw": v["train_wf_main"]["segments"]["ALL"]["binary"]["home_win"]["bias"],
            "ece_hw": v["train_wf_main"]["segments"]["ALL"]["binary"]["home_win"]["ece_5"],
            "d_rps": v["train_wf_main"]["paired_delta_vs_b0"].get("rps", {}).get("mean_delta"),
            "ci_rps": v["train_wf_main"]["paired_delta_vs_b0"].get("rps", {}).get("ci_95"),
        }
        for vid, v in rep["variants"].items()
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
