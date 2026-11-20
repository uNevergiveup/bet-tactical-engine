"""Evaluación fuera de muestra (Walk-Forward Backtesting): Brier Score, Log-Loss,
ECE (Expected Calibration Error), MAE y comparación contra el motor anterior y baselines (Etapa 4/5).

Regla 7A / Condición 1:
- Nunca declarar calibrado un modelo con muestras pequeñas (< 25 observaciones efectivas).
- Mantener separados resultados de pruebas matemáticas, integración y validación predictiva.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from bet_engine.context.history_quality import build_point_in_time_opponent_lookup
from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    ObservationStore,
    _same_coach_person,
    same_competition_season,
    same_meaningful_stratum,
)
from bet_engine.contracts import TemporalLeakageError
from bet_engine.contracts.epistemic import parse_utc_timestamp

from .distributions import (
    DEFAULT_PRIORS,
    FAMILY_DISPERSION_FLOORS,
    _gamma_rate_bounds,
    _pmf_with_disp,
    _red_card_weight,
    _resolve_prior,
    build_count_pmf,
    dixon_coles_joint_grid,
    poisson_pmf,
)


def brier_score(predictions: list[tuple[float, int]]) -> float | None:
    if not predictions:
        return None
    return round(statistics.fmean((p - y) ** 2 for p, y in predictions), 6)


def log_loss(predictions: list[tuple[float, int]], eps: float = 1e-4) -> float | None:
    if not predictions:
        return None
    vals: list[float] = []
    for p, y in predictions:
        pc = max(eps, min(1.0 - eps, float(p)))
        vals.append(-(y * math.log(pc) + (1 - y) * math.log(1.0 - pc)))
    return round(statistics.fmean(vals), 6)


def expected_calibration_error(predictions: list[tuple[float, int]], n_bins: int = 5) -> float | None:
    if not predictions:
        return None
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in predictions:
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[idx].append((p, y))
    total = len(predictions)
    ece = 0.0
    for b in bins:
        if not b:
            continue
        avg_p = statistics.fmean(p for p, _ in b)
        avg_y = statistics.fmean(y for _, y in b)
        ece += (len(b) / total) * abs(avg_p - avg_y)
    return round(ece, 6)


def brier_decomposition(
    predictions: list[tuple[float, int]],
    n_bins: int = 5,
) -> dict[str, float | None]:
    """Descompone el Brier Score en Reliability, Resolution y Uncertainty (Murphy 1973)."""
    if not predictions:
        return {"brier": None, "reliability": None, "resolution": None, "uncertainty": None}
    n = len(predictions)
    y_bar = statistics.fmean(y for _, y in predictions)
    unc = y_bar * (1.0 - y_bar)
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in predictions:
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[idx].append((p, y))
    rel = 0.0
    res = 0.0
    for b in bins:
        if not b:
            continue
        w = len(b) / n
        pk = statistics.fmean(p for p, _ in b)
        yk = statistics.fmean(y for _, y in b)
        rel += w * ((pk - yk) ** 2)
        res += w * ((yk - y_bar) ** 2)
    return {
        "brier": round(statistics.fmean((p - y) ** 2 for p, y in predictions), 6),
        "reliability": round(rel, 6),
        "resolution": round(res, 6),
        "uncertainty": round(unc, 6),
    }


def multiclass_1x2_log_loss(
    predictions: list[tuple[tuple[float, float, float], int]],
    eps: float = 1e-4,
) -> float | None:
    """Log-loss multiclase estricto para 1X2 (outcome_idx: 0=Home, 1=Draw, 2=Away)."""
    if not predictions:
        return None
    losses: list[float] = []
    for probs, outcome_idx in predictions:
        s = max(1e-9, sum(max(eps, float(x)) for x in probs))
        p_target = max(eps, float(probs[outcome_idx])) / s
        losses.append(-math.log(max(eps, min(1.0 - eps, p_target))))
    return round(statistics.fmean(losses), 6)


def ranked_probability_score_1x2(
    predictions: list[tuple[tuple[float, float, float], int]],
) -> float | None:
    """Ranked Probability Score ordinal sobre 1X2 (0=Home, 1=Draw, 2=Away)."""
    if not predictions:
        return None
    vals: list[float] = []
    for (ph, pd, pa), outcome_idx in predictions:
        s = max(1e-9, float(ph) + float(pd) + float(pa))
        qh, qd = float(ph) / s, float(pd) / s
        yh = 1.0 if outcome_idx == 0 else 0.0
        yd = 1.0 if outcome_idx == 1 else 0.0
        rps = 0.5 * (((qh - yh) ** 2) + ((qh + qd - yh - yd) ** 2))
        vals.append(rps)
    return round(statistics.fmean(vals), 6)


def paired_bootstrap_delta_ci(
    base_losses: list[float],
    alt_losses: list[float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
) -> dict[str, float | int | None]:
    """Bootstrap pareado de diferencia de pérdida (alt - base); < 0 favorece a alt."""
    n = min(len(base_losses), len(alt_losses))
    if n == 0:
        return {
            "n": 0,
            "mean_delta": None,
            "ci_95_low": None,
            "ci_95_high": None,
            "prob_alt_improves": None,
        }
    diffs = [float(alt_losses[i]) - float(base_losses[i]) for i in range(n)]
    mean_delta = statistics.fmean(diffs)
    if n < 2 or n_boot <= 0:
        return {
            "n": n,
            "mean_delta": round(mean_delta, 6),
            "ci_95_low": round(mean_delta, 6),
            "ci_95_high": round(mean_delta, 6),
            "prob_alt_improves": 1.0 if mean_delta < 0 else (0.0 if mean_delta > 0 else 0.5),
        }
    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_boot):
        acc = 0.0
        for _j in range(n):
            acc += diffs[rng.randrange(n)]
        boot_means.append(acc / n)
    boot_means.sort()
    lo_idx = max(0, min(n_boot - 1, int(math.floor(0.025 * (n_boot - 1)))))
    hi_idx = max(0, min(n_boot - 1, int(math.ceil(0.975 * (n_boot - 1)))))
    improved = sum(1 for x in boot_means if x < 0.0) / len(boot_means)
    return {
        "n": n,
        "mean_delta": round(mean_delta, 6),
        "ci_95_low": round(boot_means[lo_idx], 6),
        "ci_95_high": round(boot_means[hi_idx], 6),
        "prob_alt_improves": round(improved, 4),
    }


def summarize_probability_calibration(
    predictions: list[tuple[float, int]],
    *,
    n_bins: int = 10,
) -> dict[str, Any]:
    """Resumen completo Brier/Murphy/LogLoss/ECE y calibración por bandas (débiles vs favoritos)."""
    if not predictions:
        return {
            "n": 0,
            "base_rate": None,
            "brier": None,
            "reliability": None,
            "resolution": None,
            "uncertainty": None,
            "log_loss": None,
            "ece": None,
            "probability_bands": [],
        }
    eff_bins = min(n_bins, 10) if len(predictions) >= 150 else min(n_bins, 5)
    decomp = brier_decomposition(predictions, n_bins=eff_bins)
    bands_def = (
        ("very_low_[0.00,0.20)", 0.0, 0.20),
        ("underdog_[0.20,0.35)", 0.20, 0.35),
        ("mid_low_[0.35,0.50)", 0.35, 0.50),
        ("mid_high_[0.50,0.65)", 0.50, 0.65),
        ("favorite_[0.65,1.00]", 0.65, 1.0001),
    )
    band_rows: list[dict[str, Any]] = []
    for label, lo, hi in bands_def:
        sub = [(p, y) for p, y in predictions if lo <= p < hi]
        if not sub:
            band_rows.append({"band": label, "n": 0})
            continue
        mp = statistics.fmean(p for p, _ in sub)
        emp = statistics.fmean(y for _, y in sub)
        band_rows.append(
            {
                "band": label,
                "n": len(sub),
                "mean_p": round(mp, 4),
                "empirical_rate": round(emp, 4),
                "bias_p_minus_emp": round(mp - emp, 4),
                "brier": brier_score(sub),
                "log_loss": log_loss(sub),
            }
        )
    return {
        "n": len(predictions),
        "base_rate": round(statistics.fmean(y for _, y in predictions), 4),
        "brier": decomp["brier"],
        "reliability": decomp["reliability"],
        "resolution": decomp["resolution"],
        "uncertainty": decomp["uncertainty"],
        "log_loss": log_loss(predictions),
        "ece": expected_calibration_error(predictions, n_bins=eff_bins),
        "probability_bands": band_rows,
    }


def evaluate_interval_calibration(
    predictions: list[tuple[float, float, float, int]],
    n_bins: int = 5,
    *,
    count_realizations: list[tuple[int, int, float]] | None = None,
) -> dict[str, Any]:
    """Evalúa intervalos de probabilidad sobre tasas empíricas estratificadas por cuantiles
    y, cuando se aportan recuentos reales, cobertura predictiva del 80% [q10, q90] sobre enteros Y.

    NOTA METODOLÓGICA: la cobertura 5/5 de estratos solo verifica compatibilidad grupal entre tasa
    empírica observada y banda media de incertidumbre; no demuestra por sí sola calibración fina.
    """
    if not predictions:
        return {
            "n": 0,
            "n_bins": 0,
            "mean_width": None,
            "raw_bin_coverage": None,
            "se_adjusted_bin_coverage": None,
            "max_abs_bin_bias": None,
            "count_interval_coverage_80": None,
            "bins": [],
        }
    ordered = sorted(predictions, key=lambda x: x[0])
    n = len(ordered)
    eff_bins = min(n_bins, max(1, n // 10))
    step = max(1, n // eff_bins)
    raw_ok = 0
    se_ok = 0
    bin_rows: list[dict[str, Any]] = []
    max_abs_bias = 0.0
    for b in range(eff_bins):
        chunk = ordered[b * step : (n if b == eff_bins - 1 else (b + 1) * step)]
        m_mid = statistics.fmean(r[0] for r in chunk)
        m_lo = statistics.fmean(r[1] for r in chunk)
        m_hi = statistics.fmean(r[2] for r in chunk)
        emp = statistics.fmean(r[3] for r in chunk)
        se = 1.96 * math.sqrt(max(1e-7, emp * (1.0 - emp) / len(chunk)))
        r_cov = m_lo <= emp <= m_hi
        s_cov = (m_lo - se) <= emp <= (m_hi + se)
        raw_ok += int(r_cov)
        se_ok += int(s_cov)
        abs_bias = abs(emp - m_mid)
        if abs_bias > max_abs_bias:
            max_abs_bias = abs_bias
        bin_rows.append(
            {
                "bin": b + 1,
                "n": len(chunk),
                "mean_p": round(m_mid, 4),
                "mean_p_low": round(m_lo, 4),
                "mean_p_high": round(m_hi, 4),
                "mean_width": round(m_hi - m_lo, 4),
                "empirical_rate": round(emp, 4),
                "abs_rate_bias": round(abs_bias, 4),
                "binomial_95_se": round(se, 4),
                "raw_covered": r_cov,
                "se_adjusted_covered": s_cov,
            }
        )
    mean_width = statistics.fmean(max(0.0, r[2] - r[1]) for r in ordered)
    count_cov_80 = None
    if count_realizations:
        count_cov_80 = round(
            statistics.fmean(1.0 if (lo <= y <= hi) else 0.0 for lo, hi, y in count_realizations),
            4,
        )
    return {
        "n": n,
        "n_bins": eff_bins,
        "mean_width": round(mean_width, 4),
        "raw_bin_coverage": round(raw_ok / eff_bins, 4),
        "se_adjusted_bin_coverage": round(se_ok / eff_bins, 4),
        "raw_bins_covered": raw_ok,
        "se_adjusted_bins_covered": se_ok,
        "max_abs_bin_bias": round(max_abs_bias, 4),
        "count_interval_coverage_80": count_cov_80,
        "coverage_scope_note": "RATE_BAND_BIN_CONTAINMENT_NOT_EQUATED_TO_FULL_CALIBRATION",
        "bins": bin_rows,
    }


@dataclass(frozen=True)
class WalkForwardComparisonResult:
    """Comparación fuera de muestra entre el motor nuevo (EB L6/L20 + localía + NegBin/Poisson),
    el motor anterior (P-1..P-6 crudo), Poisson sin contracción y baseline climatológico.
    """

    metric: str
    line: float
    out_of_sample_n: int
    effective_teams_n: int
    effective_competitions_n: int
    temporal_leakage_violations: int
    calibration_status: str  # CALIBRATED_OUT_OF_SAMPLE | OUT_OF_SAMPLE_EVALUATED_LIMITED_SAMPLE_NOT_DECLARED_CALIBRATED | INSUFFICIENT_SAMPLE
    new_model_brier: float | None
    legacy_p6_brier: float | None
    unshrunk_poisson_brier: float | None
    naive_base_rate_brier: float | None
    new_model_log_loss: float | None
    legacy_p6_log_loss: float | None
    unshrunk_poisson_log_loss: float | None
    naive_base_rate_log_loss: float | None
    new_model_ece: float | None
    legacy_p6_ece: float | None
    naive_base_rate_ece: float | None
    new_model_mae: float | None
    legacy_p6_mae: float | None
    brier_improvement_vs_legacy_p6: float | None
    log_loss_improvement_vs_legacy_p6: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _collect_deduplicated_observations(
    bundles: list[MatchObservationBundle] | tuple[MatchObservationBundle, ...],
) -> tuple[dict[int, dict[int, HistoricalTeamMatch]], dict[int, dict[str, HistoricalTeamMatch]]]:
    """Consolida historial de bundles conservando la observación más informativa de forma invariante al orden."""
    team_series: dict[int, dict[int, HistoricalTeamMatch]] = {}
    fixtures_by_side: dict[int, dict[str, HistoricalTeamMatch]] = {}
    for b in bundles:
        for m in b.home_matches + b.away_matches:
            tmap = team_series.setdefault(m.team_id, {})
            existing = tmap.get(m.fixture_id)
            merged = (
                ObservationStore.prefer_informative_historical_match(existing, m)
                if existing is not None
                else m
            )
            tmap[m.fixture_id] = merged
            fixtures_by_side.setdefault(m.fixture_id, {})[merged.venue.lower()] = merged
    return team_series, fixtures_by_side


def _target_red_card_status_pair(hm: HistoricalTeamMatch, am: HistoricalTeamMatch) -> str:
    statuses = {hm.red_card_status, am.red_card_status}
    if "RED_CARD_CONFIRMED" in statuses or hm.red_card_distortion or am.red_card_distortion:
        return "RED_CARD_CONFIRMED"
    if "NO_RED_CARD_VERIFIED" in statuses:
        return "NO_RED_CARD_VERIFIED"
    return "RED_CARD_UNKNOWN"


class WalkForwardEvaluator:
    """Backtester cronológico estricto fuera de muestra (as-of available_at_utc <= kickoff_utc)."""

    MIN_PRIOR_MATCHES = 4
    MIN_CALIBRATED_N = 50

    @classmethod
    def _step_predictions_with_bands(
        cls,
        matches: list[HistoricalTeamMatch] | tuple[HistoricalTeamMatch, ...],
        *,
        metric: str,
        line: float,
        shrink_k: float = 8.0,
        model_version: str = "stage2",
    ) -> dict[str, Any]:
        """Ejecuta pasos Walk-Forward sin fuga temporal devolviendo predicciones puntuales e intervalos."""
        chronological = sorted(
            [m for m in matches if m.own_metrics.get(metric) is not None],
            key=lambda m: parse_utc_timestamp(m.kickoff_utc),
        )
        new_preds: list[tuple[int, int, float, int]] = []
        interval_preds: list[tuple[float, float, float, int]] = []
        count_intervals: list[tuple[int, int, float]] = []
        leg_preds: list[tuple[float, int]] = []
        unshrunk_preds: list[tuple[float, int]] = []
        naive_preds: list[tuple[float, int]] = []
        new_abs_err: list[float] = []
        leg_abs_err: list[float] = []
        teams_seen: set[int] = set()
        leagues_seen: set[int] = set()
        leakage_violations = 0

        threshold = math.floor(line + 1e-9)
        max_k = max(35, int(line * 4 + 12))
        phi_floor = FAMILY_DISPERSION_FLOORS.get(metric, 1.2)

        for idx, target in enumerate(chronological):
            t_ko = parse_utc_timestamp(target.kickoff_utc)
            prior = [
                p
                for p in chronological[:idx]
                if parse_utc_timestamp(p.available_at_utc) <= t_ko
                and not p.red_card_distortion
            ]
            if any(parse_utc_timestamp(p.available_at_utc) > t_ko for p in prior):
                leakage_violations += 1
                raise TemporalLeakageError("WALK_FORWARD_TEMPORAL_LEAKAGE")

            if len(prior) < cls.MIN_PRIOR_MATCHES:
                continue

            prior_20 = prior[-20:]
            prior_6 = prior[-6:]
            vals_20 = [float(p.own_metrics[metric]) for p in prior_20 if p.own_metrics.get(metric) is not None]
            vals_6 = [float(p.own_metrics[metric]) for p in prior_6 if p.own_metrics.get(metric) is not None]
            if len(vals_20) < cls.MIN_PRIOR_MATCHES or not vals_6:
                continue

            y_val = float(target.own_metrics[metric])
            y_hit = 1 if y_val > threshold else 0

            if model_version == "stage1":
                home_prior_s1, away_prior_s1 = DEFAULT_PRIORS.get(metric, (1.30, 1.10))
                venue_prior = home_prior_s1 if target.venue == "HOME" else away_prior_s1
                l20_mean = statistics.fmean(vals_20)
                l6_mean = statistics.fmean(vals_6)
                adaptive_prior = 0.50 * venue_prior + 0.50 * l20_mean
                sample_est = (0.60 * l6_mean + 0.40 * l20_mean) if len(vals_20) >= 7 else l20_mean
                venue_vals = [
                    float(p.own_metrics[metric])
                    for p in prior_20
                    if p.venue == target.venue and p.own_metrics.get(metric) is not None
                ]
                if len(venue_vals) >= 3:
                    sample_est = 0.75 * sample_est + 0.25 * statistics.fmean(venue_vals)
                n20 = len(vals_20)
                lam_new = (n20 * sample_est + shrink_k * adaptive_prior) / (n20 + shrink_k)
                var_20 = statistics.pvariance(vals_20) if n20 >= 5 else None
                pmf_new, _, _ = build_count_pmf(lam_new, var_20, n20, max_k=max_k)
                p_new = sum(p for k, p in enumerate(pmf_new) if k > threshold)
                half_band = min(0.22, (1.0 / math.sqrt(max(1.0, n20 + 2.0))) * 0.45)
                p_lo = max(0.001, p_new - half_band)
                p_hi = min(0.999, p_new + half_band)
            else:
                # Stage 2: prior por estrato, decaimiento entre competiciones, mezcla 0.35 L6 + 0.65 L20 y phi contraído
                venue_prior = _resolve_prior(metric, getattr(target, "competition_stratum", None), target.venue)
                # Más reciente primero en prior_20
                rev = list(reversed(prior_20))
                ws = [
                    1.0
                    if (p.league_id == target.league_id and p.season == target.season)
                    else (0.50 if p.competition_stratum == target.competition_stratum else 0.25)
                    for p in rev
                ]
                vs = [float(p.own_metrics[metric]) for p in rev]
                sw = sum(ws)
                l20_mean = sum(v * w for v, w in zip(vs, ws)) / sw if sw > 0 else statistics.fmean(vs)
                sw6 = sum(ws[:6])
                l6_mean = sum(v * w for v, w in zip(vs[:6], ws[:6])) / sw6 if sw6 > 0 else l20_mean
                sample_est = (0.35 * l6_mean + 0.65 * l20_mean) if len(vs) >= 7 else l20_mean
                k_eff = shrink_k if shrink_k != 6.0 else 8.0
                lam_new = (sw * sample_est + k_eff * venue_prior) / (sw + k_eff)
                venue_vals = [float(p.own_metrics[metric]) for p in rev if p.venue == target.venue]
                if len(venue_vals) >= 3:
                    lam_new = 0.75 * lam_new + 0.25 * statistics.fmean(venue_vals)
                lam_new = max(0.05, lam_new)

                n20 = len(vs)
                sample_mean = statistics.fmean(vs) if vs else lam_new
                raw_var = statistics.pvariance(vs) if n20 >= 4 else (lam_new * phi_floor)
                raw_phi = raw_var / max(sample_mean, 0.1)
                shrunk_phi = (n20 * raw_phi + 12.0 * phi_floor) / (n20 + 12.0)
                shrunk_var = (lam_new * shrunk_phi) if (metric != "yellow" or shrunk_phi > 1.20) else None
                pmf_new, _, disp_r = build_count_pmf(lam_new, shrunk_var, n20, max_k=max_k)
                p_new = max(0.001, min(0.999, sum(p for k, p in enumerate(pmf_new) if k > threshold)))

                l_lo, l_hi = _gamma_rate_bounds(lam_new, sw + k_eff, max(0.85, shrunk_phi))
                p_lo = max(0.001, min(p_new, sum(p for k, p in enumerate(_pmf_with_disp(l_lo, disp_r, max_k)) if k > threshold)))
                p_hi = min(0.999, max(p_new, sum(p for k, p in enumerate(_pmf_with_disp(l_hi, disp_r, max_k)) if k > threshold)))

            # Cuantiles predictivos discretos [q10, q90] sobre recuentos Y
            cdf = 0.0
            q10 = q90 = None
            for k_idx, pk in enumerate(pmf_new):
                cdf += pk
                if q10 is None and cdf >= 0.10:
                    q10 = k_idx
                if q90 is None and cdf >= 0.90:
                    q90 = k_idx
            count_intervals.append((0 if q10 is None else q10, max_k if q90 is None else q90, y_val))

            hits_6 = sum(1 for v in vals_6 if v > threshold)
            p_leg = hits_6 / len(vals_6)
            lam_un = max(0.05, statistics.fmean(vals_6))
            p_un = sum(poisson_pmf(k, lam_un) for k in range(threshold + 1, max_k + 1))
            hits_20 = sum(1 for v in vals_20 if v > threshold)
            p_naive = (hits_20 + 0.5) / (len(vals_20) + 1.0)

            new_preds.append((target.fixture_id, target.team_id, p_new, y_hit))
            interval_preds.append((p_new, p_lo, p_hi, y_hit))
            leg_preds.append((p_leg, y_hit))
            unshrunk_preds.append((p_un, y_hit))
            naive_preds.append((p_naive, y_hit))
            new_abs_err.append(abs(lam_new - y_val))
            leg_abs_err.append(abs(statistics.fmean(vals_6) - y_val))
            teams_seen.add(target.team_id)
            leagues_seen.add(target.league_id)

        return {
            "new_preds": new_preds,
            "interval_preds": interval_preds,
            "count_intervals": count_intervals,
            "leg_preds": leg_preds,
            "unshrunk_preds": unshrunk_preds,
            "naive_preds": naive_preds,
            "new_abs_err": new_abs_err,
            "leg_abs_err": leg_abs_err,
            "teams_seen": teams_seen,
            "leagues_seen": leagues_seen,
            "leakage_violations": leakage_violations,
        }

    @classmethod
    def _step_predictions(
        cls,
        matches: list[HistoricalTeamMatch] | tuple[HistoricalTeamMatch, ...],
        *,
        metric: str,
        line: float,
        shrink_k: float,
    ) -> tuple[
        list[tuple[int, int, float, int]],  # (fixture_id, team_id, p_new, y)
        list[tuple[float, int]],  # legacy_p6
        list[tuple[float, int]],  # unshrunk_poisson
        list[tuple[float, int]],  # naive_base_rate
        list[float],  # new_abs_err
        list[float],  # leg_abs_err
        set[int],  # teams
        set[int],  # leagues
        int,  # leakage_violations
    ]:
        res = cls._step_predictions_with_bands(
            matches, metric=metric, line=line, shrink_k=shrink_k, model_version="stage2"
        )
        return (
            res["new_preds"],
            res["leg_preds"],
            res["unshrunk_preds"],
            res["naive_preds"],
            res["new_abs_err"],
            res["leg_abs_err"],
            res["teams_seen"],
            res["leagues_seen"],
            res["leakage_violations"],
        )

    @classmethod
    def _summarize(
        cls,
        *,
        metric: str,
        line: float,
        new_pairs: list[tuple[float, int]],
        leg_pairs: list[tuple[float, int]],
        un_pairs: list[tuple[float, int]],
        naive_pairs: list[tuple[float, int]],
        new_abs_err: list[float],
        leg_abs_err: list[float],
        n_teams: int,
        n_leagues: int,
        leakage_violations: int,
    ) -> WalkForwardComparisonResult:
        oos_n = len(new_pairs)
        if oos_n == 0:
            return WalkForwardComparisonResult(
                metric=metric,
                line=line,
                out_of_sample_n=0,
                effective_teams_n=0,
                effective_competitions_n=0,
                temporal_leakage_violations=leakage_violations,
                calibration_status="INSUFFICIENT_SAMPLE",
                new_model_brier=None,
                legacy_p6_brier=None,
                unshrunk_poisson_brier=None,
                naive_base_rate_brier=None,
                new_model_log_loss=None,
                legacy_p6_log_loss=None,
                unshrunk_poisson_log_loss=None,
                naive_base_rate_log_loss=None,
                new_model_ece=None,
                legacy_p6_ece=None,
                naive_base_rate_ece=None,
                new_model_mae=None,
                legacy_p6_mae=None,
                brier_improvement_vs_legacy_p6=None,
                log_loss_improvement_vs_legacy_p6=None,
            )

        nb = brier_score(new_pairs)
        lb = brier_score(leg_pairs)
        ub = brier_score(un_pairs)
        naib = brier_score(naive_pairs)
        nll = log_loss(new_pairs)
        lll = log_loss(leg_pairs)
        ull = log_loss(un_pairs)
        naill = log_loss(naive_pairs)
        nece = expected_calibration_error(new_pairs)
        lece = expected_calibration_error(leg_pairs)
        naiece = expected_calibration_error(naive_pairs)
        nmae = round(statistics.fmean(new_abs_err), 4)
        lmae = round(statistics.fmean(leg_abs_err), 4)

        b_imp = round(lb - nb, 6) if (lb is not None and nb is not None) else None
        ll_imp = round(lll - nll, 6) if (lll is not None and nll is not None) else None

        if (
            oos_n >= cls.MIN_CALIBRATED_N
            and n_teams >= 10
            and n_leagues >= 3
            and leakage_violations == 0
            and metric in ("goals", "goals_h1")
            and b_imp is not None
            and b_imp > 0
            and ll_imp is not None
            and ll_imp > 0
            and nece is not None
            and nece < 0.10
        ):
            status = "OUT_OF_SAMPLE_BASELINE_SUPERIOR_PENDING_RESAMPLING_AUDIT"
        elif metric not in ("goals", "goals_h1"):
            status = "LIMITED_PREDICTIVE_EVIDENCE_FAMILY_NOT_CALIBRATED"
        else:
            status = "OUT_OF_SAMPLE_EVALUATED_LIMITED_SAMPLE_NOT_DECLARED_CALIBRATED"

        return WalkForwardComparisonResult(
            metric=metric,
            line=line,
            out_of_sample_n=oos_n,
            effective_teams_n=n_teams,
            effective_competitions_n=n_leagues,
            temporal_leakage_violations=leakage_violations,
            calibration_status=status,
            new_model_brier=nb,
            legacy_p6_brier=lb,
            unshrunk_poisson_brier=ub,
            naive_base_rate_brier=naib,
            new_model_log_loss=nll,
            legacy_p6_log_loss=lll,
            unshrunk_poisson_log_loss=ull,
            naive_base_rate_log_loss=naill,
            new_model_ece=nece,
            legacy_p6_ece=lece,
            naive_base_rate_ece=naiece,
            new_model_mae=nmae,
            legacy_p6_mae=lmae,
            brier_improvement_vs_legacy_p6=b_imp,
            log_loss_improvement_vs_legacy_p6=ll_imp,
        )

    @classmethod
    def evaluate_series(
        cls,
        matches: list[HistoricalTeamMatch] | tuple[HistoricalTeamMatch, ...],
        *,
        metric: str = "goals",
        line: float = 1.5,
        shrink_k: float = 4.0,
    ) -> WalkForwardComparisonResult:
        (
            new_preds,
            leg_preds,
            un_preds,
            naive_preds,
            new_err,
            leg_err,
            teams,
            leagues,
            leaks,
        ) = cls._step_predictions(matches, metric=metric, line=line, shrink_k=shrink_k)
        return cls._summarize(
            metric=metric,
            line=line,
            new_pairs=[(p, y) for _, _, p, y in new_preds],
            leg_pairs=leg_preds,
            un_pairs=un_preds,
            naive_pairs=naive_preds,
            new_abs_err=new_err,
            leg_abs_err=leg_err,
            n_teams=len(teams),
            n_leagues=len(leagues),
            leakage_violations=leaks,
        )

    @classmethod
    def evaluate_multi_series_corpus(
        cls,
        bundles: list[MatchObservationBundle],
        *,
        metric: str = "goals",
        line: float = 1.5,
        shrink_k: float = 4.0,
    ) -> WalkForwardComparisonResult:
        """Evalúa Walk-Forward agregado sobre múltiples equipos y competiciones deduplicando pasos."""
        seen_step_keys: set[tuple[int, int]] = set()
        all_new: list[tuple[float, int]] = []
        all_leg: list[tuple[float, int]] = []
        all_un: list[tuple[float, int]] = []
        all_naive: list[tuple[float, int]] = []
        all_new_err: list[float] = []
        all_leg_err: list[float] = []
        all_teams: set[int] = set()
        all_leagues: set[int] = set()
        total_leaks = 0

        for b in bundles:
            for side_matches in (b.home_matches, b.away_matches):
                (
                    new_preds,
                    leg_preds,
                    un_preds,
                    naive_preds,
                    new_err,
                    leg_err,
                    teams,
                    leagues,
                    leaks,
                ) = cls._step_predictions(side_matches, metric=metric, line=line, shrink_k=shrink_k)
                total_leaks += leaks
                all_teams |= teams
                all_leagues |= leagues
                for idx, (fid, tid, p_new, y) in enumerate(new_preds):
                    if (fid, tid) in seen_step_keys:
                        continue
                    seen_step_keys.add((fid, tid))
                    all_new.append((p_new, y))
                    all_leg.append(leg_preds[idx])
                    all_un.append(un_preds[idx])
                    all_naive.append(naive_preds[idx])
                    all_new_err.append(new_err[idx])
                    all_leg_err.append(leg_err[idx])

        return cls._summarize(
            metric=metric,
            line=line,
            new_pairs=all_new,
            leg_pairs=all_leg,
            un_pairs=all_un,
            naive_pairs=all_naive,
            new_abs_err=all_new_err,
            leg_abs_err=all_leg_err,
            n_teams=len(all_teams),
            n_leagues=len(all_leagues),
            leakage_violations=total_leaks,
        )

    @classmethod
    def audit_corpus_dependencies_and_segments(
        cls,
        bundles: list[MatchObservationBundle],
        *,
        metric: str = "goals",
        line: float = 1.5,
        shrink_k: float = 4.0,
    ) -> dict[str, Any]:
        """Audita dependencias entre equipos/ventanas, separa predicciones individuales
        de totales conjuntos de partido y reporta dispersión por estrato competitivo.
        """
        seen_step_keys: set[tuple[int, int]] = set()
        duplicate_steps_excluded = 0
        steps_per_team: dict[int, int] = {}
        by_stratum: dict[str, dict[str, list[Any]]] = {}
        joint_new: list[tuple[float, int]] = []
        joint_leg: list[tuple[float, int]] = []
        joint_seen_fixtures: set[int] = set()

        home_prior, away_prior = DEFAULT_PRIORS.get(metric, (1.30, 1.10))
        threshold = math.floor(line + 1e-9)

        for b in bundles:
            # 1) Auditoría individual por equipo y por estrato competitivo
            for side_matches in (b.home_matches, b.away_matches):
                (
                    new_preds,
                    leg_preds,
                    _,
                    _,
                    new_err,
                    leg_err,
                    _,
                    _,
                    _,
                ) = cls._step_predictions(side_matches, metric=metric, line=line, shrink_k=shrink_k)
                by_fid = {m.fixture_id: m for m in side_matches}
                for idx, (fid, tid, p_new, y) in enumerate(new_preds):
                    if (fid, tid) in seen_step_keys:
                        duplicate_steps_excluded += 1
                        continue
                    seen_step_keys.add((fid, tid))
                    steps_per_team[tid] = steps_per_team.get(tid, 0) + 1
                    stratum = by_fid[fid].competition_stratum if fid in by_fid else "UNKNOWN"
                    bucket = by_stratum.setdefault(stratum, {"new": [], "leg": []})
                    bucket["new"].append((p_new, y))
                    bucket["leg"].append(leg_preds[idx])

            # 2) Evaluación conjunta del partido objetivo de bundle (si hay historial ambos lados y fixture no repetido)
            if b.fixture_id not in joint_seen_fixtures and metric == "goals":
                # Solo evaluamos totales conjuntos cuando existan goles propios+concedidos en historia previa de ambos
                h_own = [float(m.own_metrics["goals"]) for m in b.home_matches if m.own_metrics.get("goals") is not None]
                a_own = [float(m.own_metrics["goals"]) for m in b.away_matches if m.own_metrics.get("goals") is not None]
                if len(h_own) >= 4 and len(a_own) >= 4:
                    joint_seen_fixtures.add(b.fixture_id)
                    lam_tot = ((sum(h_own) + 4 * home_prior) / (len(h_own) + 4)) + (
                        (sum(a_own) + 4 * away_prior) / (len(a_own) + 4)
                    )
                    p_tot_o25 = sum(poisson_pmf(k, lam_tot) for k in range(3, 25))
                    # Guardamos únicamente la predicción prepartido conjunta (sin target post-kickoff inventado)
                    joint_new.append((p_tot_o25, -1))
                    joint_leg.append((sum(1 for v in (h_own[:6] + a_own[:6]) if v > 1.5) / min(12, len(h_own[:6] + a_own[:6])), -1))

        strata_report: dict[str, Any] = {}
        for st, vals in sorted(by_stratum.items()):
            briers = [(p - y) ** 2 for p, y in vals["new"]]
            strata_report[st] = {
                "n": len(vals["new"]),
                "new_brier": brier_score(vals["new"]),
                "legacy_p6_brier": brier_score(vals["leg"]),
                "brier_std_dispersion": round(statistics.pstdev(briers), 6) if len(briers) > 1 else None,
                "new_log_loss": log_loss(vals["new"]),
                "legacy_p6_log_loss": log_loss(vals["leg"]),
                "new_ece": expected_calibration_error(vals["new"]),
            }

        counts_list = list(steps_per_team.values())
        return {
            "scope": "INDIVIDUAL_TEAM_METRICS_SEPARATED_FROM_JOINT_MATCH",
            "metric": metric,
            "line": line,
            "unique_team_fixture_steps": len(seen_step_keys),
            "duplicate_steps_excluded": duplicate_steps_excluded,
            "distinct_teams": len(steps_per_team),
            "max_steps_per_team": max(counts_list) if counts_list else 0,
            "median_steps_per_team": statistics.median(counts_list) if counts_list else 0,
            "strata_breakdown": strata_report,
            "joint_match_targets_tracked_prematch": len(joint_new),
            "joint_note": "Las métricas individuales (team_goals) se auditan por separado de los totales conjuntos del partido (H+A); sin resultados FT post-kickoff para el target futuro no se fabrica Brier conjunto.",
        }

    @classmethod
    def evaluate_stage1_vs_stage2(
        cls,
        bundles: list[MatchObservationBundle] | tuple[MatchObservationBundle, ...],
        *,
        metric: str,
        line: float,
    ) -> dict[str, Any]:
        """Compara fuera de muestra Stage 1 vs Stage 2 vs Legacy P6 con calibración de intervalos."""
        series_by_team, _ = _collect_deduplicated_observations(bundles)

        stage1_preds: list[tuple[float, int]] = []
        stage2_preds: list[tuple[float, int]] = []
        legacy_preds: list[tuple[float, int]] = []
        stage1_intervals: list[tuple[float, float, float, int]] = []
        stage2_intervals: list[tuple[float, float, float, int]] = []
        stage1_counts: list[tuple[int, int, float]] = []
        stage2_counts: list[tuple[int, int, float]] = []
        s1_mae: list[float] = []
        s2_mae: list[float] = []
        seen_keys: set[tuple[int, int]] = set()
        teams_seen: set[int] = set()
        leagues_seen: set[int] = set()
        leakage = 0

        for tid, fmap in series_by_team.items():
            ordered = sorted(fmap.values(), key=lambda x: parse_utc_timestamp(x.kickoff_utc))
            r1 = cls._step_predictions_with_bands(ordered, metric=metric, line=line, shrink_k=6.0, model_version="stage1")
            r2 = cls._step_predictions_with_bands(ordered, metric=metric, line=line, shrink_k=8.0, model_version="stage2")
            leakage += max(r1["leakage_violations"], r2["leakage_violations"])

            map1 = {(fid, t_id): (p, y, iv, ci, ae) for (fid, t_id, p, y), iv, ci, ae in zip(r1["new_preds"], r1["interval_preds"], r1["count_intervals"], r1["new_abs_err"])}
            for idx, (fid, t_id, p2, y2) in enumerate(r2["new_preds"]):
                key = (fid, t_id)
                if key in seen_keys or key not in map1:
                    continue
                seen_keys.add(key)
                p1, y1, iv1, ci1, ae1 = map1[key]
                stage1_preds.append((p1, y1))
                stage2_preds.append((p2, y2))
                legacy_preds.append(r2["leg_preds"][idx])
                stage1_intervals.append(iv1)
                stage2_intervals.append(r2["interval_preds"][idx])
                stage1_counts.append(ci1)
                stage2_counts.append(r2["count_intervals"][idx])
                s1_mae.append(ae1)
                s2_mae.append(r2["new_abs_err"][idx])
                teams_seen.add(t_id)
            leagues_seen.update(r2["leagues_seen"])

        return {
            "metric": metric,
            "line": line,
            "out_of_sample_n": len(stage2_preds),
            "teams_n": len(teams_seen),
            "competitions_n": len(leagues_seen),
            "temporal_leakage_violations": leakage,
            "legacy_p6": {
                "brier": brier_score(legacy_preds),
                "log_loss": log_loss(legacy_preds),
                "ece": expected_calibration_error(legacy_preds),
            },
            "stage1": {
                "brier_decomp": brier_decomposition(stage1_preds),
                "log_loss": log_loss(stage1_preds),
                "ece": expected_calibration_error(stage1_preds),
                "mae": round(statistics.fmean(s1_mae), 4) if s1_mae else None,
                "interval_calibration": evaluate_interval_calibration(stage1_intervals, n_bins=10, count_realizations=stage1_counts),
            },
            "stage2": {
                "brier_decomp": brier_decomposition(stage2_preds),
                "log_loss": log_loss(stage2_preds),
                "ece": expected_calibration_error(stage2_preds),
                "mae": round(statistics.fmean(s2_mae), 4) if s2_mae else None,
                "interval_calibration": evaluate_interval_calibration(stage2_intervals, n_bins=10, count_realizations=stage2_counts),
            },
        }

    @classmethod
    def evaluate_joint_match_walk_forward(
        cls,
        bundles: list[MatchObservationBundle] | tuple[MatchObservationBundle, ...],
        *,
        min_prior_per_team: int = 6,
        exclude_target_red_cards: bool = False,
        max_goals: int = 12,
        strict_competition_identity: bool = False,
    ) -> dict[str, Any]:
        """Valida mercados conjuntos reales (Over 2.5 FT, BTTS Sí, Victoria Local) con historial as-of.

        Por defecto (`exclude_target_red_cards=False`) evalúa el universo PREMATCH sin filtrar por
        expulsiones ocurridas tras el kickoff del encuentro objetivo.
        """
        team_series, fixtures_both = _collect_deduplicated_observations(bundles)

        team_ordered = {
            tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
            for tid, fmap in team_series.items()
        }

        from .distributions import _estimate_team_rate_detailed

        markets = ("over_2_5", "btts_yes", "home_win")
        stage1_scores: dict[str, list[tuple[float, int]]] = {k: [] for k in markets}
        stage2_scores: dict[str, list[tuple[float, int]]] = {k: [] for k in markets}
        stage2_ivs: dict[str, list[tuple[float, float, float, int]]] = {k: [] for k in markets}
        target_rc_counts: dict[str, int] = {
            "RED_CARD_CONFIRMED": 0,
            "NO_RED_CARD_VERIFIED": 0,
            "RED_CARD_UNKNOWN": 0,
        }
        target_rc_excluded_n = 0

        ordered_pairs = sorted(
            fixtures_both.items(),
            key=lambda kv: (
                parse_utc_timestamp((kv[1].get("home") or kv[1].get("away")).kickoff_utc),
                kv[0],
            ),
        )

        for fid, sides in ordered_pairs:
            if "home" not in sides or "away" not in sides:
                continue
            hm, am = sides["home"], sides["away"]
            hg, ag = hm.own_metrics.get("goals"), am.own_metrics.get("goals")
            if hg is None or ag is None:
                continue
            t_ko = parse_utc_timestamp(hm.kickoff_utc)
            hp = [
                m
                for m in team_ordered.get(hm.team_id, ())
                if m.fixture_id != fid
                and parse_utc_timestamp(m.available_at_utc) <= t_ko
                and parse_utc_timestamp(m.kickoff_utc) < t_ko
                and not m.red_card_distortion
                and m.own_metrics.get("goals") is not None
            ]
            ap = [
                m
                for m in team_ordered.get(am.team_id, ())
                if m.fixture_id != fid
                and parse_utc_timestamp(m.available_at_utc) <= t_ko
                and parse_utc_timestamp(m.kickoff_utc) < t_ko
                and not m.red_card_distortion
                and m.own_metrics.get("goals") is not None
            ]
            if len(hp) < min_prior_per_team or len(ap) < min_prior_per_team:
                continue

            rc_status = _target_red_card_status_pair(hm, am)
            target_rc_counts[rc_status] = target_rc_counts.get(rc_status, 0) + 1
            if exclude_target_red_cards and (hm.red_card_distortion or am.red_card_distortion):
                target_rc_excluded_n += 1
                continue

            hp_rev = tuple(reversed(hp))
            ap_rev = tuple(reversed(ap))

            # Stage 1 reference rate (prior (1.42, 1.15), 0.55 L6 + 0.45 L20, shrink_k=6)
            def _s1_rate(own_ms: tuple[HistoricalTeamMatch, ...], opp_ms: tuple[HistoricalTeamMatch, ...], ov: str, cv: str, pr: float) -> float:
                osel = [m for m in own_ms if m.league_id == hm.league_id and m.season == hm.season][:20]
                if len(osel) < 4:
                    osel = list(own_ms[:20])
                csel = [m for m in opp_ms if m.league_id == hm.league_id and m.season == hm.season][:20]
                if len(csel) < 4:
                    csel = list(opp_ms[:20])
                ovals = [float(m.own_metrics["goals"]) for m in osel if m.own_metrics.get("goals") is not None]
                cvals = [float(m.conceded_metrics["goals"]) for m in csel if m.conceded_metrics.get("goals") is not None]
                o_est = (0.55 * statistics.fmean(ovals[:6]) + 0.45 * statistics.fmean(ovals)) if len(ovals) >= 7 else statistics.fmean(ovals)
                c_est = (0.55 * statistics.fmean(cvals[:6]) + 0.45 * statistics.fmean(cvals)) if len(cvals) >= 7 else (statistics.fmean(cvals) if cvals else pr)
                oshr = (len(ovals) * o_est + 6.0 * pr) / (len(ovals) + 6.0)
                cshr = (len(cvals) * c_est + 6.0 * pr) / (len(cvals) + 6.0)
                ov_v = [float(m.own_metrics["goals"]) for m in osel if m.venue == ov and m.own_metrics.get("goals") is not None]
                cv_v = [float(m.conceded_metrics["goals"]) for m in csel if m.venue == cv and m.conceded_metrics.get("goals") is not None]
                if len(ov_v) >= 3:
                    oshr = 0.75 * oshr + 0.25 * statistics.fmean(ov_v)
                if len(cv_v) >= 3:
                    cshr = 0.75 * cshr + 0.25 * statistics.fmean(cv_v)
                return max(0.05, 0.55 * oshr + 0.45 * cshr)

            lh1 = _s1_rate(hp_rev, ap_rev, "HOME", "AWAY", 1.42)
            la1 = _s1_rate(ap_rev, hp_rev, "AWAY", "HOME", 1.15)
            g1 = dixon_coles_joint_grid(lh1, la1, rho=-0.04, max_goals=max_goals)

            # Stage 2 detailed estimate + propagated bounds
            eh = _estimate_team_rate_detailed(
                hp_rev,
                ap_rev,
                metric="goals",
                target_league_id=hm.league_id,
                target_season=hm.season,
                target_stratum=hm.competition_stratum,
                own_venue="HOME",
                opp_venue="AWAY",
                prior_mean=_resolve_prior("goals", hm.competition_stratum, "HOME"),
                strict_competition_identity=strict_competition_identity,
            )
            ea = _estimate_team_rate_detailed(
                ap_rev,
                hp_rev,
                metric="goals",
                target_league_id=hm.league_id,
                target_season=hm.season,
                target_stratum=hm.competition_stratum,
                own_venue="AWAY",
                opp_venue="HOME",
                prior_mean=_resolve_prior("goals", hm.competition_stratum, "AWAY"),
                strict_competition_identity=strict_competition_identity,
            )
            lh2, la2 = eh["lambda"], ea["lambda"]
            lh_lo, lh_hi = _gamma_rate_bounds(lh2, eh["n_eff"] + 8.0, eh["phi"])
            la_lo, la_hi = _gamma_rate_bounds(la2, ea["n_eff"] + 8.0, ea["phi"])

            def _dc_m(grid: list[list[float]]) -> dict[str, float]:
                mg = len(grid) - 1
                o25 = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i + j > 2)
                btts = sum(grid[i][j] for i in range(1, mg + 1) for j in range(1, mg + 1))
                hw = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i > j)
                return {"over_2_5": o25, "btts_yes": btts, "home_win": hw}

            s1_m = _dc_m(g1)
            s2_m = _dc_m(dixon_coles_joint_grid(lh2, la2, rho=-0.04, max_goals=max_goals))
            corners = [_dc_m(dixon_coles_joint_grid(a, b, rho=-0.04, max_goals=max_goals)) for a in (lh_lo, lh_hi) for b in (la_lo, la_hi)] + [s2_m]

            y_map = {
                "over_2_5": 1 if (float(hg) + float(ag)) > 2.5 else 0,
                "btts_yes": 1 if (float(hg) > 0 and float(ag) > 0) else 0,
                "home_win": 1 if float(hg) > float(ag) else 0,
            }
            for mk in markets:
                stage1_scores[mk].append((s1_m[mk], y_map[mk]))
                stage2_scores[mk].append((s2_m[mk], y_map[mk]))
                stage2_ivs[mk].append((s2_m[mk], min(x[mk] for x in corners), max(x[mk] for x in corners), y_map[mk]))

        report: dict[str, Any] = {
            "protocol_audit": {
                "max_goals": max_goals,
                "target_red_card_policy": (
                    "EXCLUDE_CONFIRMED_TARGET_RC_RETROSPECTIVE"
                    if exclude_target_red_cards
                    else "INCLUDE_ALL_PREMATCH"
                ),
                "target_red_card_excluded_n": target_rc_excluded_n,
                "target_red_card_status_counts": target_rc_counts,
                "strict_competition_identity": strict_competition_identity,
            }
        }
        for mk in markets:
            report[mk] = {
                "n": len(stage2_scores[mk]),
                "stage1": {
                    "brier_decomp": brier_decomposition(stage1_scores[mk]),
                    "log_loss": log_loss(stage1_scores[mk]),
                    "ece": expected_calibration_error(stage1_scores[mk]),
                },
                "stage2": {
                    "brier_decomp": brier_decomposition(stage2_scores[mk]),
                    "log_loss": log_loss(stage2_scores[mk]),
                    "ece": expected_calibration_error(stage2_scores[mk]),
                    "interval_calibration": evaluate_interval_calibration(stage2_ivs[mk]),
                },
            }
        return report

    @staticmethod
    def _estimate_experimental_goal_rate(
        own_matches: tuple[HistoricalTeamMatch, ...],
        opp_matches: tuple[HistoricalTeamMatch, ...],
        *,
        target_ko: datetime,
        target_league_id: int,
        target_season: int,
        target_stratum: str | None,
        own_venue: str,
        opp_venue: str,
        prior_mean: float,
        opp_baseline: float,
        history_limit: int,
        shrink_k: float = 8.0,
        half_life_days: float | None = None,
        coach_decay_factor: float | None = None,
        red_card_policy: str = "exclude_any",
        opponent_lookup: Any = None,
        sos_alpha: float = 0.0,
        multiplicative: bool = False,
    ) -> dict[str, Any]:
        """Variante experimental offline para P1-B1 (no modifica producción)."""
        def filter_side(ms: tuple[HistoricalTeamMatch, ...], field_map: str) -> list[HistoricalTeamMatch]:
            ordered = sorted(ms, key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
            valid = [
                m
                for m in ordered
                if _red_card_weight(m, policy=red_card_policy) > 0.0
                and (m.own_metrics if field_map == "own" else m.conceded_metrics).get("goals") is not None
            ]
            return valid[:history_limit]

        own_sel = filter_side(own_matches, "own")
        opp_sel = filter_side(opp_matches, "conceded")
        shared_ids = {m.fixture_id for m in own_sel} & {m.fixture_id for m in opp_sel}
        if not own_sel and not opp_sel:
            return {"lambda": prior_mean, "n_eff": 0.0, "phi": FAMILY_DISPERSION_FLOORS["goals"]}

        def infer_latest_coach(ms: list[HistoricalTeamMatch]) -> tuple[int | None, str | None]:
            for m in ms:
                if m.coach_id is not None or m.coach_name:
                    return m.coach_id, m.coach_name
            return None, None

        own_cur_cid, own_cur_cname = infer_latest_coach(own_sel)
        opp_cur_cid, opp_cur_cname = infer_latest_coach(opp_sel)

        def comp_weight(m: HistoricalTeamMatch) -> float:
            if m.league_id == target_league_id and m.season == target_season:
                return 1.0
            if target_stratum and m.competition_stratum == target_stratum:
                return 0.50
            return 0.25

        def obs_weight(m: HistoricalTeamMatch, cur_cid: int | None, cur_cname: str | None) -> float:
            w = comp_weight(m) * _red_card_weight(m, policy=red_card_policy)
            if w <= 0.0:
                return 0.0
            if half_life_days is not None and half_life_days > 0.0:
                age_d = max(0.0, (target_ko - parse_utc_timestamp(m.kickoff_utc)).total_seconds() / 86400.0)
                w *= 0.5 ** (age_d / half_life_days)
            if coach_decay_factor is not None and (cur_cid is not None or cur_cname):
                if m.coach_id is not None or m.coach_name:
                    if not _same_coach_person(m.coach_id, m.coach_name, cur_cid, cur_cname):
                        w *= coach_decay_factor
            return w

        def adjust_goal_val(m: HistoricalTeamMatch, val: float, side_kind: str) -> float:
            if sos_alpha <= 0.0 or opponent_lookup is None:
                return val
            opp_st = opponent_lookup(m.opponent_id, parse_utc_timestamp(m.kickoff_utc))
            if not opp_st:
                return val
            if side_kind == "own":
                opp_rate = max(0.30, float(opp_st["goals_against_pg"]))
                softness = max(0.55, min(1.80, opp_rate / max(0.40, prior_mean)))
            else:
                opp_rate = max(0.30, float(opp_st["goals_for_pg"]))
                softness = max(0.55, min(1.80, opp_rate / max(0.40, opp_baseline)))
            factor = (1.0 - sos_alpha) + sos_alpha * softness
            return max(0.0, val / max(0.55, min(1.75, factor)))

        def shrunk_side(
            ms: list[HistoricalTeamMatch],
            side_kind: str,
            cur_cid: int | None,
            cur_cname: str | None,
            target_venue: str,
            prior: float,
            apply_h2h_discount: bool,
        ) -> tuple[float, float]:
            pairs: list[tuple[HistoricalTeamMatch, float, float]] = []
            for m in ms:
                raw_v = m.own_metrics.get("goals") if side_kind == "own" else m.conceded_metrics.get("goals")
                if raw_v is None:
                    continue
                w = obs_weight(m, cur_cid, cur_cname)
                if apply_h2h_discount and m.fixture_id in shared_ids:
                    w *= 0.50
                if w <= 0.0:
                    continue
                adj_v = adjust_goal_val(m, float(raw_v), side_kind)
                pairs.append((m, adj_v, w))
            if not pairs:
                return prior, 0.0
            w_sum = sum(w for _, _, w in pairs)
            full_mean = sum(v * w for _, v, w in pairs) / w_sum if w_sum > 0 else prior
            if half_life_days is None and len(pairs) >= 7:
                p6 = pairs[:6]
                w6 = sum(w for _, _, w in p6)
                l6_mean = sum(v * w for _, v, w in p6) / w6 if w6 > 0 else full_mean
                est = 0.35 * l6_mean + 0.65 * full_mean
            else:
                est = full_mean
            shr = (w_sum * est + shrink_k * prior) / (w_sum + shrink_k)
            ven_pairs = [(v, w) for m, v, w in pairs if m.venue == target_venue]
            if len(ven_pairs) >= 3:
                vw = sum(w for _, w in ven_pairs)
                if vw > 0:
                    v_mean = sum(v * w for v, w in ven_pairs) / vw
                    shr = 0.80 * shr + 0.20 * v_mean if multiplicative else 0.75 * shr + 0.25 * v_mean
            return max(0.05, shr), w_sum

        own_shr, own_w = shrunk_side(own_sel, "own", own_cur_cid, own_cur_cname, own_venue, prior_mean, False)
        opp_shr, opp_w = shrunk_side(opp_sel, "conceded", opp_cur_cid, opp_cur_cname, opp_venue, prior_mean, True)
        if multiplicative:
            att_ratio = max(0.35, min(2.40, own_shr / max(0.25, prior_mean)))
            def_ratio = max(0.35, min(2.40, opp_shr / max(0.25, prior_mean)))
            lam = max(0.05, prior_mean * (att_ratio ** 0.70) * (def_ratio ** 0.60))
        else:
            lam = max(0.05, 0.55 * own_shr + 0.45 * opp_shr)

        uvals: dict[int, float] = {}
        for m in own_sel:
            if m.own_metrics.get("goals") is not None:
                uvals.setdefault(m.fixture_id, float(m.own_metrics["goals"]))
        for m in opp_sel:
            if m.conceded_metrics.get("goals") is not None and m.fixture_id not in uvals:
                uvals[m.fixture_id] = float(m.conceded_metrics["goals"])
        vals = list(uvals.values())
        phi_prior = FAMILY_DISPERSION_FLOORS["goals"]
        if len(vals) >= 4:
            sm = statistics.fmean(vals)
            raw_phi = statistics.pvariance(vals) / max(sm, 0.1)
            phi = (len(vals) * raw_phi + 12.0 * phi_prior) / (len(vals) + 12.0)
        else:
            phi = phi_prior
        return {"lambda": lam, "n_eff": round(0.55 * own_w + 0.45 * opp_w, 4), "phi": max(0.85, phi)}

    @classmethod
    def evaluate_window_depth_and_weighting(
        cls,
        bundles: list[MatchObservationBundle] | tuple[MatchObservationBundle, ...],
        *,
        windows: tuple[int, ...] = (6, 10, 20),
        min_prior_per_team: int = 10,
        include_weighting_experiments: bool = True,
        include_legacy_p6: bool = True,
        bootstrap_samples: int = 1000,
        bootstrap_seed: int = 42,
        exclude_target_red_cards: bool = False,
        max_goals: int = 12,
        clip_probs: bool = False,
        strict_competition_identity: bool = False,
    ) -> dict[str, Any]:
        """Evaluación cronológica Walk-Forward pareada de profundidad histórica (6 vs 10 vs 20) y ponderaciones.

        Por defecto (`exclude_target_red_cards=False`, `max_goals=12`, `clip_probs=False`) evalúa el
        universo PREMATCH completo alineado con CoherentMatchModeler sin filtrar expulsiones objetivo.
        """
        if not windows:
            raise ValueError("WINDOWS_REQUIRED")
        ordered_windows = tuple(sorted(set(int(w) for w in windows)))
        max_w = ordered_windows[-1]
        if any(w <= 0 for w in ordered_windows):
            raise ValueError(f"INVALID_WINDOW_IN:{ordered_windows}")

        team_series, fixtures_both = _collect_deduplicated_observations(bundles)

        team_ordered = {
            tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
            for tid, fmap in team_series.items()
        }
        opp_lookup = build_point_in_time_opponent_lookup(team_ordered, min_matches=4)

        from .distributions import _estimate_team_rate_detailed

        variant_specs: list[tuple[str, dict[str, Any]]] = [
            (f"prod_w{w}", {"kind": "prod", "window": w}) for w in ordered_windows
        ]
        if include_legacy_p6:
            variant_specs.append(("legacy_p6_empirical", {"kind": "legacy_p6"}))
        if include_weighting_experiments:
            variant_specs.extend(
                [
                    (
                        f"w{max_w}_exp_decay_180d",
                        {"kind": "exp", "window": max_w, "half_life_days": 180.0, "coach_decay_factor": None, "red_card_policy": "exclude_any", "sos_alpha": 0.0, "multiplicative": False},
                    ),
                    (
                        f"w{max_w}_diff_red_card",
                        {"kind": "exp", "window": max_w, "half_life_days": None, "coach_decay_factor": None, "red_card_policy": "differentiated_weight", "sos_alpha": 0.0, "multiplicative": False},
                    ),
                    (
                        f"w{max_w}_coach_decay_50",
                        {"kind": "exp", "window": max_w, "half_life_days": None, "coach_decay_factor": 0.50, "red_card_policy": "exclude_any", "sos_alpha": 0.0, "multiplicative": False},
                    ),
                    (
                        f"w{max_w}_p1b2_mult_sos",
                        {"kind": "exp", "window": max_w, "half_life_days": 180.0, "coach_decay_factor": 0.50, "red_card_policy": "differentiated_weight", "sos_alpha": 0.25, "multiplicative": True},
                    ),
                ]
            )

        markets = ("over_2_5", "btts_yes", "home_win", "draw", "away_win")
        scores: dict[str, dict[str, list[tuple[float, int]]]] = {
            name: {mk: [] for mk in markets} for name, _ in variant_specs
        }
        brier_losses: dict[str, dict[str, list[float]]] = {
            name: {mk: [] for mk in markets} for name, _ in variant_specs
        }
        log_losses: dict[str, dict[str, list[float]]] = {
            name: {mk: [] for mk in markets} for name, _ in variant_specs
        }
        ivs: dict[str, dict[str, list[tuple[float, float, float, int]]]] = {
            name: {mk: [] for mk in markets} for name, _ in variant_specs
        }
        probs_1x2: dict[str, list[tuple[tuple[float, float, float], int]]] = {
            name: [] for name, _ in variant_specs
        }
        rps_losses_1x2: dict[str, list[float]] = {name: [] for name, _ in variant_specs}
        strata_scores: dict[str, dict[str, dict[str, list[tuple[float, int]]]]] = {
            name: {} for name, _ in variant_specs
        }

        leakage_violations = 0
        evaluated_targets = 0
        target_strata_counts: dict[str, int] = {}
        target_rc_counts: dict[str, int] = {
            "RED_CARD_CONFIRMED": 0,
            "NO_RED_CARD_VERIFIED": 0,
            "RED_CARD_UNKNOWN": 0,
        }
        target_rc_excluded_n = 0
        teams_evaluated: set[int] = set()
        actual_depth_h: list[int] = []
        actual_depth_a: list[int] = []

        ordered_targets = sorted(
            fixtures_both.items(),
            key=lambda kv: (
                parse_utc_timestamp((kv[1].get("home") or kv[1].get("away")).kickoff_utc),
                kv[0],
            ),
        )

        def dc_summary(lh: float, la: float) -> dict[str, float]:
            grid = dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=max_goals)
            mg = len(grid) - 1
            o25 = bt = hw = dr = aw = 0.0
            for i in range(mg + 1):
                for j in range(mg + 1):
                    p = grid[i][j]
                    if i + j > 2:
                        o25 += p
                    if i >= 1 and j >= 1:
                        bt += p
                    if i > j:
                        hw += p
                    elif i == j:
                        dr += p
                    else:
                        aw += p
            if clip_probs:
                return {
                    "over_2_5": max(0.001, min(0.999, o25)),
                    "btts_yes": max(0.001, min(0.999, bt)),
                    "home_win": max(0.001, min(0.999, hw)),
                    "draw": max(0.001, min(0.999, dr)),
                    "away_win": max(0.001, min(0.999, aw)),
                }
            return {
                "over_2_5": o25,
                "btts_yes": bt,
                "home_win": hw,
                "draw": dr,
                "away_win": aw,
            }

        for fid, sides in ordered_targets:
            if "home" not in sides or "away" not in sides:
                continue
            hm, am = sides["home"], sides["away"]
            hg, ag = hm.own_metrics.get("goals"), am.own_metrics.get("goals")
            if hg is None or ag is None:
                continue
            t_ko = parse_utc_timestamp(hm.kickoff_utc)

            hp_raw = [
                m
                for m in team_ordered.get(hm.team_id, ())
                if m.fixture_id != fid
                and parse_utc_timestamp(m.available_at_utc) <= t_ko
                and parse_utc_timestamp(m.kickoff_utc) < t_ko
                and m.own_metrics.get("goals") is not None
            ]
            ap_raw = [
                m
                for m in team_ordered.get(am.team_id, ())
                if m.fixture_id != fid
                and parse_utc_timestamp(m.available_at_utc) <= t_ko
                and parse_utc_timestamp(m.kickoff_utc) < t_ko
                and m.own_metrics.get("goals") is not None
            ]
            if any(parse_utc_timestamp(m.available_at_utc) > t_ko for m in hp_raw + ap_raw):
                leakage_violations += 1
                raise TemporalLeakageError("WALK_FORWARD_WINDOW_TEMPORAL_LEAKAGE")

            hp_clean = [m for m in hp_raw if not m.red_card_distortion]
            ap_clean = [m for m in ap_raw if not m.red_card_distortion]
            if len(hp_clean) < min_prior_per_team or len(ap_clean) < min_prior_per_team:
                continue

            rc_status = _target_red_card_status_pair(hm, am)
            target_rc_counts[rc_status] = target_rc_counts.get(rc_status, 0) + 1
            if exclude_target_red_cards and (hm.red_card_distortion or am.red_card_distortion):
                target_rc_excluded_n += 1
                continue

            evaluated_targets += 1
            teams_evaluated.update((hm.team_id, am.team_id))
            stratum = hm.competition_stratum or "UNKNOWN"
            target_strata_counts[stratum] = target_strata_counts.get(stratum, 0) + 1
            actual_depth_h.append(min(max_w, len(hp_clean)))
            actual_depth_a.append(min(max_w, len(ap_clean)))

            hp_rev = tuple(reversed(hp_raw))
            ap_rev = tuple(reversed(ap_raw))
            h_prior = _resolve_prior("goals", stratum, "HOME")
            a_prior = _resolve_prior("goals", stratum, "AWAY")

            hg_f, ag_f = float(hg), float(ag)
            y_map = {
                "over_2_5": 1 if (hg_f + ag_f) > 2.5 else 0,
                "btts_yes": 1 if (hg_f > 0 and ag_f > 0) else 0,
                "home_win": 1 if hg_f > ag_f else 0,
                "draw": 1 if hg_f == ag_f else 0,
                "away_win": 1 if hg_f < ag_f else 0,
            }
            outcome_1x2 = 0 if hg_f > ag_f else (1 if hg_f == ag_f else 2)

            for name, spec in variant_specs:
                kind = spec["kind"]
                if kind == "legacy_p6":
                    p6_h = hp_clean[-6:]
                    p6_a = ap_clean[-6:]
                    total_goals_h = [
                        float(m.own_metrics["goals"]) + float(m.conceded_metrics["goals"])
                        for m in p6_h
                        if m.conceded_metrics.get("goals") is not None
                    ]
                    total_goals_a = [
                        float(m.own_metrics["goals"]) + float(m.conceded_metrics["goals"])
                        for m in p6_a
                        if m.conceded_metrics.get("goals") is not None
                    ]
                    p_o25 = (
                        0.5
                        * (
                            statistics.fmean(1.0 if g > 2.5 else 0.0 for g in total_goals_h)
                            + statistics.fmean(1.0 if g > 2.5 else 0.0 for g in total_goals_a)
                        )
                        if total_goals_h and total_goals_a
                        else 0.50
                    )
                    btts_h = [
                        1.0 if (float(m.own_metrics["goals"]) > 0 and float(m.conceded_metrics["goals"]) > 0) else 0.0
                        for m in p6_h
                        if m.conceded_metrics.get("goals") is not None
                    ]
                    btts_a = [
                        1.0 if (float(m.own_metrics["goals"]) > 0 and float(m.conceded_metrics["goals"]) > 0) else 0.0
                        for m in p6_a
                        if m.conceded_metrics.get("goals") is not None
                    ]
                    p_bt = 0.5 * (statistics.fmean(btts_h) + statistics.fmean(btts_a)) if btts_h and btts_a else 0.50
                    h_wins = statistics.fmean(
                        1.0 if float(m.own_metrics["goals"]) > float(m.conceded_metrics["goals"]) else 0.0
                        for m in p6_h
                        if m.conceded_metrics.get("goals") is not None
                    )
                    a_wins = statistics.fmean(
                        1.0 if float(m.own_metrics["goals"]) > float(m.conceded_metrics["goals"]) else 0.0
                        for m in p6_a
                        if m.conceded_metrics.get("goals") is not None
                    )
                    d_rate = statistics.fmean(
                        1.0 if float(m.own_metrics["goals"]) == float(m.conceded_metrics["goals"]) else 0.0
                        for m in (p6_h + p6_a)
                        if m.conceded_metrics.get("goals") is not None
                    )
                    raw_hw = max(0.05, 0.5 * (h_wins + (1.0 - a_wins - d_rate * 0.5)))
                    raw_dr = max(0.08, d_rate)
                    raw_aw = max(0.05, 1.0 - min(0.90, raw_hw + raw_dr))
                    norm_s = raw_hw + raw_dr + raw_aw
                    mid = {
                        "over_2_5": max(0.01, min(0.99, p_o25)),
                        "btts_yes": max(0.01, min(0.99, p_bt)),
                        "home_win": max(0.01, min(0.99, raw_hw / norm_s)),
                        "draw": max(0.01, min(0.99, raw_dr / norm_s)),
                        "away_win": max(0.01, min(0.99, raw_aw / norm_s)),
                    }
                    corners_m = [mid]
                else:
                    if kind == "prod":
                        w_lim = int(spec["window"])
                        eh = _estimate_team_rate_detailed(
                            hp_rev,
                            ap_rev,
                            metric="goals",
                            target_league_id=hm.league_id,
                            target_season=hm.season,
                            target_stratum=stratum,
                            own_venue="HOME",
                            opp_venue="AWAY",
                            prior_mean=h_prior,
                            history_limit=w_lim,
                            strict_competition_identity=strict_competition_identity,
                        )
                        ea = _estimate_team_rate_detailed(
                            ap_rev,
                            hp_rev,
                            metric="goals",
                            target_league_id=hm.league_id,
                            target_season=hm.season,
                            target_stratum=stratum,
                            own_venue="AWAY",
                            opp_venue="HOME",
                            prior_mean=a_prior,
                            history_limit=w_lim,
                            strict_competition_identity=strict_competition_identity,
                        )
                    else:
                        eh = cls._estimate_experimental_goal_rate(
                            hp_rev,
                            ap_rev,
                            target_ko=t_ko,
                            target_league_id=hm.league_id,
                            target_season=hm.season,
                            target_stratum=stratum,
                            own_venue="HOME",
                            opp_venue="AWAY",
                            prior_mean=h_prior,
                            opp_baseline=a_prior,
                            history_limit=int(spec["window"]),
                            half_life_days=spec["half_life_days"],
                            coach_decay_factor=spec["coach_decay_factor"],
                            red_card_policy=spec["red_card_policy"],
                            opponent_lookup=opp_lookup,
                            sos_alpha=float(spec["sos_alpha"]),
                            multiplicative=bool(spec["multiplicative"]),
                        )
                        ea = cls._estimate_experimental_goal_rate(
                            ap_rev,
                            hp_rev,
                            target_ko=t_ko,
                            target_league_id=hm.league_id,
                            target_season=hm.season,
                            target_stratum=stratum,
                            own_venue="AWAY",
                            opp_venue="HOME",
                            prior_mean=a_prior,
                            opp_baseline=h_prior,
                            history_limit=int(spec["window"]),
                            half_life_days=spec["half_life_days"],
                            coach_decay_factor=spec["coach_decay_factor"],
                            red_card_policy=spec["red_card_policy"],
                            opponent_lookup=opp_lookup,
                            sos_alpha=float(spec["sos_alpha"]),
                            multiplicative=bool(spec["multiplicative"]),
                        )
                    lh, la = eh["lambda"], ea["lambda"]
                    lh_lo, lh_hi = _gamma_rate_bounds(lh, eh["n_eff"] + 8.0, eh["phi"])
                    la_lo, la_hi = _gamma_rate_bounds(la, ea["n_eff"] + 8.0, ea["phi"])
                    mid = dc_summary(lh, la)
                    corners_m = [
                        mid,
                        dc_summary(lh_lo, la_lo),
                        dc_summary(lh_lo, la_hi),
                        dc_summary(lh_hi, la_lo),
                        dc_summary(lh_hi, la_hi),
                    ]

                for mk in markets:
                    p = mid[mk]
                    y = y_map[mk]
                    scores[name][mk].append((p, y))
                    brier_losses[name][mk].append((p - y) ** 2)
                    pc = max(1e-4, min(1.0 - 1e-4, p))
                    log_losses[name][mk].append(-(y * math.log(pc) + (1 - y) * math.log(1.0 - pc)))
                    ivs[name][mk].append(
                        (p, min(c[mk] for c in corners_m), max(c[mk] for c in corners_m), y)
                    )
                    strata_scores[name].setdefault(stratum, {}).setdefault(mk, []).append((p, y))

                trip = (mid["home_win"], mid["draw"], mid["away_win"])
                probs_1x2[name].append((trip, outcome_1x2))
                s_1x2 = max(1e-9, sum(trip))
                qh, qd = trip[0] / s_1x2, trip[1] / s_1x2
                yh_f = 1.0 if outcome_1x2 == 0 else 0.0
                yd_f = 1.0 if outcome_1x2 == 1 else 0.0
                rps_losses_1x2[name].append(0.5 * (((qh - yh_f) ** 2) + ((qh + qd - yh_f - yd_f) ** 2)))

        ref_name = f"prod_w{ordered_windows[0]}"
        variants_report: dict[str, Any] = {}
        for name, _ in variant_specs:
            mk_rep: dict[str, Any] = {}
            for mk in markets:
                cal = summarize_probability_calibration(scores[name][mk], n_bins=10)
                iv_cal = evaluate_interval_calibration(ivs[name][mk], n_bins=5)
                delta_brier = (
                    paired_bootstrap_delta_ci(
                        brier_losses[ref_name][mk],
                        brier_losses[name][mk],
                        n_boot=bootstrap_samples,
                        seed=bootstrap_seed,
                    )
                    if name != ref_name
                    else None
                )
                delta_ll = (
                    paired_bootstrap_delta_ci(
                        log_losses[ref_name][mk],
                        log_losses[name][mk],
                        n_boot=bootstrap_samples,
                        seed=bootstrap_seed,
                    )
                    if name != ref_name
                    else None
                )
                mk_rep[mk] = {
                    **cal,
                    "interval_calibration": iv_cal,
                    "delta_brier_vs_ref": delta_brier,
                    "delta_log_loss_vs_ref": delta_ll,
                }
            stratum_rep: dict[str, Any] = {}
            for st_name, st_mks in strata_scores[name].items():
                st_n = len(st_mks.get("home_win", ()))
                stratum_rep[st_name] = {
                    "n": st_n,
                    "sufficient_sample": st_n >= 25,
                    "home_win": {
                        "brier": brier_score(st_mks["home_win"]),
                        "log_loss": log_loss(st_mks["home_win"]),
                        "ece": expected_calibration_error(st_mks["home_win"], n_bins=5),
                    },
                    "over_2_5": {
                        "brier": brier_score(st_mks["over_2_5"]),
                        "log_loss": log_loss(st_mks["over_2_5"]),
                        "ece": expected_calibration_error(st_mks["over_2_5"], n_bins=5),
                    },
                }
            variants_report[name] = {
                "markets": mk_rep,
                "multiclass_1x2": {
                    "n": len(probs_1x2[name]),
                    "log_loss": multiclass_1x2_log_loss(probs_1x2[name]),
                    "rps": ranked_probability_score_1x2(probs_1x2[name]),
                    "delta_rps_vs_ref": (
                        paired_bootstrap_delta_ci(
                            rps_losses_1x2[ref_name],
                            rps_losses_1x2[name],
                            n_boot=bootstrap_samples,
                            seed=bootstrap_seed,
                        )
                        if name != ref_name
                        else None
                    ),
                },
                "by_stratum": stratum_rep,
            }

        return {
            "reference_variant": ref_name,
            "windows_compared": list(ordered_windows),
            "min_prior_per_team": min_prior_per_team,
            "evaluated_targets_n": evaluated_targets,
            "teams_evaluated_n": len(teams_evaluated),
            "temporal_leakage_violations": leakage_violations,
            "sufficient_sample_overall": evaluated_targets >= cls.MIN_CALIBRATED_N,
            "target_strata_counts": target_strata_counts,
            "mean_effective_depth_home": round(statistics.fmean(actual_depth_h), 2) if actual_depth_h else 0.0,
            "mean_effective_depth_away": round(statistics.fmean(actual_depth_a), 2) if actual_depth_a else 0.0,
            "protocol_audit": {
                "max_goals": max_goals,
                "clip_probs": clip_probs,
                "target_red_card_policy": (
                    "EXCLUDE_CONFIRMED_TARGET_RC_RETROSPECTIVE"
                    if exclude_target_red_cards
                    else "INCLUDE_ALL_PREMATCH"
                ),
                "target_red_card_excluded_n": target_rc_excluded_n,
                "target_red_card_status_counts": target_rc_counts,
                "strict_competition_identity": strict_competition_identity,
            },
            "variants": variants_report,
        }

    @classmethod
    def evaluate_single_team_metric_windows(
        cls,
        bundles: list[MatchObservationBundle] | tuple[MatchObservationBundle, ...],
        *,
        metric: str = "corners",
        line: float = 4.5,
        windows: tuple[int, ...] = (6, 10),
        min_prior_per_team: int = 10,
        bootstrap_samples: int = 1000,
        bootstrap_seed: int = 42,
        exclude_target_red_cards: bool = False,
    ) -> dict[str, Any]:
        """Comparación Walk-Forward pareada por equipo individual (p.ej. corners/yellow en W=6 vs W=10)."""
        ordered_windows = tuple(sorted(set(int(w) for w in windows)))
        threshold = math.floor(line + 1e-9)
        max_k = max(35, int(line * 4 + 12))
        phi_floor = FAMILY_DISPERSION_FLOORS.get(metric, 1.2)

        series_by_team, _ = _collect_deduplicated_observations(bundles)

        preds: dict[int, list[tuple[float, int]]] = {w: [] for w in ordered_windows}
        ivs: dict[int, list[tuple[float, float, float, int]]] = {w: [] for w in ordered_windows}
        b_losses: dict[int, list[float]] = {w: [] for w in ordered_windows}
        ll_losses: dict[int, list[float]] = {w: [] for w in ordered_windows}
        abs_errs: dict[int, list[float]] = {w: [] for w in ordered_windows}
        teams_seen: set[int] = set()
        leakage = 0
        target_rc_counts: dict[str, int] = {
            "RED_CARD_CONFIRMED": 0,
            "NO_RED_CARD_VERIFIED": 0,
            "RED_CARD_UNKNOWN": 0,
        }
        target_rc_excluded_n = 0

        for tid in sorted(series_by_team.keys()):
            fmap = series_by_team[tid]
            chron = sorted(
                [m for m in fmap.values() if m.own_metrics.get(metric) is not None],
                key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id),
            )
            for idx, target in enumerate(chron):
                t_ko = parse_utc_timestamp(target.kickoff_utc)
                prior = [
                    p
                    for p in chron[:idx]
                    if p.fixture_id != target.fixture_id
                    and parse_utc_timestamp(p.available_at_utc) <= t_ko
                    and parse_utc_timestamp(p.kickoff_utc) < t_ko
                    and not p.red_card_distortion
                ]
                if any(parse_utc_timestamp(p.available_at_utc) > t_ko for p in prior):
                    leakage += 1
                    raise TemporalLeakageError("WALK_FORWARD_SINGLE_METRIC_LEAKAGE")
                if len(prior) < min_prior_per_team:
                    continue
                rc_st = target.red_card_status
                target_rc_counts[rc_st] = target_rc_counts.get(rc_st, 0) + 1
                if exclude_target_red_cards and target.red_card_distortion:
                    target_rc_excluded_n += 1
                    continue
                teams_seen.add(tid)
                y_val = float(target.own_metrics[metric])
                y_hit = 1 if y_val > threshold else 0
                venue_prior = _resolve_prior(metric, getattr(target, "competition_stratum", None), target.venue)

                for w in ordered_windows:
                    rev = list(reversed(prior[-w:]))
                    ws = [
                        1.0
                        if (p.league_id == target.league_id and p.season == target.season)
                        else (0.50 if p.competition_stratum == target.competition_stratum else 0.25)
                        for p in rev
                    ]
                    vs = [float(p.own_metrics[metric]) for p in rev]
                    sw = sum(ws)
                    full_mean = sum(v * wt for v, wt in zip(vs, ws)) / sw if sw > 0 else statistics.fmean(vs)
                    sw6 = sum(ws[:6])
                    l6_mean = sum(v * wt for v, wt in zip(vs[:6], ws[:6])) / sw6 if sw6 > 0 else full_mean
                    sample_est = (0.35 * l6_mean + 0.65 * full_mean) if len(vs) >= 7 else full_mean
                    lam = (sw * sample_est + 8.0 * venue_prior) / (sw + 8.0)
                    v_vals = [float(p.own_metrics[metric]) for p in rev if p.venue == target.venue]
                    if len(v_vals) >= 3:
                        lam = 0.75 * lam + 0.25 * statistics.fmean(v_vals)
                    lam = max(0.05, lam)

                    n_obs = len(vs)
                    raw_var = statistics.pvariance(vs) if n_obs >= 4 else (lam * phi_floor)
                    raw_phi = raw_var / max(statistics.fmean(vs), 0.1)
                    shr_phi = (n_obs * raw_phi + 12.0 * phi_floor) / (n_obs + 12.0)
                    shr_var = (lam * shr_phi) if (metric != "yellow" or shr_phi > 1.20) else None
                    pmf, _, disp_r = build_count_pmf(lam, shr_var, n_obs, max_k=max_k)
                    p_hit = max(0.001, min(0.999, sum(pk for k, pk in enumerate(pmf) if k > threshold)))
                    l_lo, l_hi = _gamma_rate_bounds(lam, sw + 8.0, max(0.85, shr_phi))
                    p_lo = max(0.001, min(p_hit, sum(pk for k, pk in enumerate(_pmf_with_disp(l_lo, disp_r, max_k)) if k > threshold)))
                    p_hi = min(0.999, max(p_hit, sum(pk for k, pk in enumerate(_pmf_with_disp(l_hi, disp_r, max_k)) if k > threshold)))

                    preds[w].append((p_hit, y_hit))
                    ivs[w].append((p_hit, p_lo, p_hi, y_hit))
                    b_losses[w].append((p_hit - y_hit) ** 2)
                    pc = max(1e-4, min(1.0 - 1e-4, p_hit))
                    ll_losses[w].append(-(y_hit * math.log(pc) + (1 - y_hit) * math.log(1.0 - pc)))
                    abs_errs[w].append(abs(lam - y_val))

        ref_w = ordered_windows[0]
        n_total = len(preds[ref_w])
        if n_total == 0:
            status = "NO_OBSERVATIONS"
        elif n_total < 25:
            status = "INSUFFICIENT_SAMPLE_LT_25"
        elif n_total < cls.MIN_CALIBRATED_N:
            status = "LIMITED_SAMPLE_NOT_DECLARED_CALIBRATED"
        else:
            status = "OUT_OF_SAMPLE_PAIRED_EVALUATED"

        win_rep: dict[str, Any] = {}
        for w in ordered_windows:
            cal = summarize_probability_calibration(preds[w], n_bins=5)
            win_rep[f"w{w}"] = {
                **cal,
                "mae": round(statistics.fmean(abs_errs[w]), 4) if abs_errs[w] else None,
                "interval_calibration": evaluate_interval_calibration(ivs[w], n_bins=5),
                "delta_brier_vs_ref": (
                    paired_bootstrap_delta_ci(
                        b_losses[ref_w],
                        b_losses[w],
                        n_boot=bootstrap_samples,
                        seed=bootstrap_seed,
                    )
                    if w != ref_w
                    else None
                ),
                "delta_log_loss_vs_ref": (
                    paired_bootstrap_delta_ci(
                        ll_losses[ref_w],
                        ll_losses[w],
                        n_boot=bootstrap_samples,
                        seed=bootstrap_seed,
                    )
                    if w != ref_w
                    else None
                ),
            }

        return {
            "metric": metric,
            "line": line,
            "reference_window": f"w{ref_w}",
            "windows_compared": list(ordered_windows),
            "min_prior_per_team": min_prior_per_team,
            "out_of_sample_n": n_total,
            "teams_n": len(teams_seen),
            "temporal_leakage_violations": leakage,
            "sample_status": status,
            "protocol_audit": {
                "target_red_card_policy": (
                    "EXCLUDE_CONFIRMED_TARGET_RC_RETROSPECTIVE"
                    if exclude_target_red_cards
                    else "INCLUDE_ALL_PREMATCH"
                ),
                "target_red_card_excluded_n": target_rc_excluded_n,
                "target_red_card_status_counts": target_rc_counts,
            },
            "windows": win_rep,
        }
