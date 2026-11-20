"""Modelos probabilísticos coherentes por familia de mercado (Etapa 4).

Garantías matemáticas y de diseño:
1. Compatibilidad exacta entre probabilidades por equipo y totales del partido:
   la distribución del total (H + A) se obtiene por convolución de la grilla conjunta
   (H, A), cumpliendo E[H + A] = E[H] + E[A] y P(Over L) + P(Under L) = 1.0.
2. Modelos especializados por familia:
   - Goles (FT, H1, BTTS, 1X2, Doble Oportunidad, Goles Equipo): Empirical Bayes + Poisson Bivariante / Dixon-Coles.
   - Conteos sobredispersos (Córners, Remates, Tiros a Puerta, Faltas, Tarjetas): Empirical Bayes + Binomial Negativa (o Poisson si Var <= 1.15 * Mean).
3. Prohibición estricta de que patrones exploratorios o hipótesis alteren p_model:
   solo variables registradas como AcceptedModelFeature pueden aplicar ajustes acotados.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    same_competition_season,
    same_meaningful_stratum,
)
from bet_engine.contracts import (
    AcceptedModelFeature,
    EpistemicContractError,
    StatisticalPattern,
    TacticalHypothesis,
    ValidatedPattern,
)

MODEL_VERSION = "CoherentMatchModeler_baseline_2.0_2.1A"


def poisson_pmf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def negbin_pmf(k: int, mean: float, dispersion_r: float) -> float:
    """PMF Binomial Negativa parametrizada por media mu y dispersión r (Var = mu + mu^2 / r)."""
    if k < 0:
        return 0.0
    if mean <= 0.0:
        return 1.0 if k == 0 else 0.0
    r = max(0.5, float(dispersion_r))
    p = r / (r + mean)
    log_prob = (
        math.lgamma(k + r)
        - math.lgamma(r)
        - math.lgamma(k + 1)
        + r * math.log(p)
        + k * math.log(max(1e-15, 1.0 - p))
    )
    return math.exp(log_prob)


def build_count_pmf(mean: float, variance: float | None, n_obs: int, max_k: int = 35) -> tuple[list[float], str, float | None]:
    """Selecciona Binomial Negativa cuando hay sobredispersión verificada (n >= 4) o Poisson."""
    lam = max(0.05, float(mean))
    if variance is not None and n_obs >= 4 and variance > 1.15 * lam:
        r = (lam * lam) / max(0.05, variance - lam)
        r_clamped = max(1.5, min(80.0, r))
        pmf = [negbin_pmf(k, lam, r_clamped) for k in range(max_k + 1)]
        model_type = "NEGATIVE_BINOMIAL"
        disp = round(r_clamped, 4)
    else:
        pmf = [poisson_pmf(k, lam) for k in range(max_k + 1)]
        model_type = "POISSON"
        disp = None
    total = sum(pmf)
    if total > 0:
        pmf = [p / total for p in pmf]
    return pmf, model_type, disp


def convolve_pmfs(pmf_a: list[float], pmf_b: list[float]) -> list[float]:
    """Convolución discreta exacta P(A + B = k) para garantizar coherencia Equipo <-> Partido."""
    out = [0.0] * (len(pmf_a) + len(pmf_b) - 1)
    for i, pa in enumerate(pmf_a):
        if pa == 0.0:
            continue
        for j, pb in enumerate(pmf_b):
            out[i + j] += pa * pb
    total = sum(out)
    return [p / total for p in out] if total > 0 else out


def dixon_coles_joint_grid(
    lam_home: float,
    lam_away: float,
    rho: float = -0.05,
    max_goals: int = 12,
) -> list[list[float]]:
    """Construye grilla conjunta P(H=i, A=j) con ajuste Dixon-Coles acotado en marcadores bajos."""
    lh = max(0.05, float(lam_home))
    la = max(0.05, float(lam_away))
    r = max(-0.12, min(0.12, float(rho)))

    pmf_h = [poisson_pmf(i, lh) for i in range(max_goals + 1)]
    pmf_a = [poisson_pmf(j, la) for j in range(max_goals + 1)]

    grid = [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]
    total = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            tau = 1.0
            if i == 0 and j == 0:
                tau = max(0.2, 1.0 - lh * la * r)
            elif i == 0 and j == 1:
                tau = max(0.2, 1.0 + lh * r)
            elif i == 1 and j == 0:
                tau = max(0.2, 1.0 + la * r)
            elif i == 1 and j == 1:
                tau = max(0.2, 1.0 - r)
            val = pmf_h[i] * pmf_a[j] * tau
            grid[i][j] = val
            total += val

    if total > 0:
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                grid[i][j] /= total
    return grid


def _gamma_rate_bounds(lam: float, n_post: float, phi: float, *, z: float = 1.2816) -> tuple[float, float]:
    """Cuantiles Gamma (aproximación Wilson-Hilferty) para tasa media con sobredispersión phi."""
    l_safe = max(0.05, float(lam))
    phi_safe = max(0.85, float(phi))
    n_safe = max(1.0, float(n_post))
    alpha = max(1.5, (n_safe * l_safe) / phi_safe)
    t = 1.0 / (9.0 * alpha)
    sq = math.sqrt(t)
    f_low = max(0.25, (1.0 - t - z * sq) ** 3)
    f_high = max(1.0, (1.0 - t + z * sq) ** 3)
    return round(max(0.05, l_safe * f_low), 4), round(max(0.05, l_safe * f_high), 4)


def _pmf_with_disp(mean: float, dispersion_r: float | None, max_k: int) -> list[float]:
    """Construye PMF manteniendo Poisson o Binomial Negativa con parámetro r fijo."""
    lam = max(0.05, float(mean))
    if dispersion_r is not None and dispersion_r > 0:
        pmf = [negbin_pmf(k, lam, dispersion_r) for k in range(max_k + 1)]
    else:
        pmf = [poisson_pmf(k, lam) for k in range(max_k + 1)]
    tot = sum(pmf)
    return [p / tot for p in pmf] if tot > 0 else pmf


@dataclass(frozen=True)
class FamilyDistributionEstimate:
    """Distribución coherente para una familia (Equipo Local, Equipo Visitante y Total Partido)."""

    family: str  # GOALS_FT | GOALS_H1 | CORNERS | SHOTS | ON_TARGET | FOULS | YELLOW_CARDS
    model_type: str
    lambda_home: float
    lambda_away: float
    lambda_total: float
    home_sample_n: int
    away_sample_n: int
    dispersion_home: float | None
    dispersion_away: float | None
    pmf_home: tuple[float, ...]
    pmf_away: tuple[float, ...]
    pmf_total: tuple[float, ...]
    uncertainty_rel_width: float
    warnings: tuple[str, ...] = ()
    accepted_features_applied: tuple[str, ...] = ()
    lambda_home_low: float | None = None
    lambda_home_high: float | None = None
    lambda_away_low: float | None = None
    lambda_away_high: float | None = None
    dispersion_phi_home: float | None = None
    dispersion_phi_away: float | None = None
    unique_fixture_n: int | None = None
    shared_h2h_n: int = 0
    uncertainty_method: str = "GAMMA_QUASI_POISSON_RATE_POSTERIOR"
    home_n_eff: float | None = None
    away_n_eff: float | None = None

    def prob_over(self, scope: str, line: float) -> dict[str, float]:
        """Calcula P(X > line), P(X < line) y bandas de incertidumbre coherentes por familia."""
        pmf = (
            self.pmf_home
            if scope == "home"
            else self.pmf_away
            if scope == "away"
            else self.pmf_total
        )
        threshold = math.floor(line + 1e-9)
        p_over = sum(p for k, p in enumerate(pmf) if k > threshold)
        p_over = max(0.001, min(0.999, p_over))
        p_under = max(0.001, min(0.999, 1.0 - p_over))

        if (
            self.lambda_home_low is not None
            and self.lambda_home_high is not None
            and self.lambda_away_low is not None
            and self.lambda_away_high is not None
        ):
            if scope == "home":
                max_k = max(12, len(self.pmf_home) - 1)
                pmf_low = _pmf_with_disp(self.lambda_home_low, self.dispersion_home, max_k)
                pmf_high = _pmf_with_disp(self.lambda_home_high, self.dispersion_home, max_k)
            elif scope == "away":
                max_k = max(12, len(self.pmf_away) - 1)
                pmf_low = _pmf_with_disp(self.lambda_away_low, self.dispersion_away, max_k)
                pmf_high = _pmf_with_disp(self.lambda_away_high, self.dispersion_away, max_k)
            else:
                if self.family == "GOALS_FT":
                    def _dc_tot(l_h: float, l_a: float) -> list[float]:
                        g = dixon_coles_joint_grid(l_h, l_a, rho=-0.04, max_goals=12)
                        tot_p = [0.0] * (2 * len(g) - 1)
                        for i in range(len(g)):
                            for j in range(len(g)):
                                tot_p[i + j] += g[i][j]
                        return tot_p
                    pmf_low = _dc_tot(self.lambda_home_low, self.lambda_away_low)
                    pmf_high = _dc_tot(self.lambda_home_high, self.lambda_away_high)
                else:
                    max_kh = max(12, len(self.pmf_home) - 1)
                    max_ka = max(12, len(self.pmf_away) - 1)
                    pmf_low = convolve_pmfs(
                        _pmf_with_disp(self.lambda_home_low, self.dispersion_home, max_kh),
                        _pmf_with_disp(self.lambda_away_low, self.dispersion_away, max_ka),
                    )
                    pmf_high = convolve_pmfs(
                        _pmf_with_disp(self.lambda_home_high, self.dispersion_home, max_kh),
                        _pmf_with_disp(self.lambda_away_high, self.dispersion_away, max_ka),
                    )
            p_over_low = min(p_over, sum(p for k, p in enumerate(pmf_low) if k > threshold))
            p_over_high = max(p_over, sum(p for k, p in enumerate(pmf_high) if k > threshold))
            p_over_low = max(0.001, min(p_over, p_over_low))
            p_over_high = min(0.999, max(p_over, p_over_high))
            p_under_low = max(0.001, min(p_under, 1.0 - p_over_high))
            p_under_high = min(0.999, max(p_under, 1.0 - p_over_low))
        else:
            half_band = min(0.22, self.uncertainty_rel_width * 0.45)
            p_over_low = max(0.001, p_over - half_band)
            p_over_high = min(0.999, p_over + half_band)
            p_under_low = max(0.001, p_under - half_band)
            p_under_high = min(0.999, p_under + half_band)

        return {
            "line": line,
            "p_over": round(p_over, 4),
            "p_under": round(p_under, 4),
            "p_over_low": round(p_over_low, 4),
            "p_over_high": round(p_over_high, 4),
            "p_under_low": round(p_under_low, 4),
            "p_under_high": round(p_under_high, 4),
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pmf_home"] = [round(x, 6) for x in self.pmf_home[:16]]
        d["pmf_away"] = [round(x, 6) for x in self.pmf_away[:16]]
        d["pmf_total"] = [round(x, 6) for x in self.pmf_total[:25]]
        return d


@dataclass(frozen=True)
class MatchProbabilisticModel:
    """Modelo probabilístico consolidado de un partido PREMATCH (Etapa 4)."""

    event_id: str
    fixture_id: int
    cutoff_utc: str
    families: dict[str, FamilyDistributionEstimate]
    outcomes_1x2: dict[str, float]
    double_chance: dict[str, float]
    btts: dict[str, float]
    coherence_checks: dict[str, Any]
    ignored_exploratory_count: int = 0
    outcomes_1x2_bounds: dict[str, tuple[float, float]] = field(default_factory=dict)
    double_chance_bounds: dict[str, tuple[float, float]] = field(default_factory=dict)
    btts_bounds: dict[str, tuple[float, float]] = field(default_factory=dict)
    model_version: str = MODEL_VERSION

    def joint_grid_with_corners(self, period: str = "FT") -> tuple[list[list[float]], list[list[list[float]]]]:
        """Devuelve la grilla bivariada central y los extremos epistémicos coherentes con la familia (FT o H1)."""
        fam_key = "GOALS_H1" if period == "H1" else "GOALS_FT"
        fam = self.families.get(fam_key)
        if fam is None:
            return [], []
        lh_lo = fam.lambda_home_low if fam.lambda_home_low is not None else fam.lambda_home
        lh_hi = fam.lambda_home_high if fam.lambda_home_high is not None else fam.lambda_home
        la_lo = fam.lambda_away_low if fam.lambda_away_low is not None else fam.lambda_away
        la_hi = fam.lambda_away_high if fam.lambda_away_high is not None else fam.lambda_away

        def _make_grid(lh_v: float, la_v: float) -> list[list[float]]:
            if period == "H1":
                max_g = max(8, len(fam.pmf_home) - 1)
                ph = _pmf_with_disp(lh_v, fam.dispersion_home, max_g)
                pa = _pmf_with_disp(la_v, fam.dispersion_away, max_g)
                return [[ph[i] * pa[j] for j in range(max_g + 1)] for i in range(max_g + 1)]
            return dixon_coles_joint_grid(lh_v, la_v, rho=-0.04, max_goals=12)

        center = _make_grid(fam.lambda_home, fam.lambda_away)
        corners = [center]
        if lh_lo != lh_hi or la_lo != la_hi:
            sample_pts = (
                (lh_lo, la_lo),
                (lh_lo, la_hi),
                (lh_hi, la_lo),
                (lh_hi, la_hi),
                (fam.lambda_home, la_lo),
                (fam.lambda_home, la_hi),
                (lh_lo, fam.lambda_away),
                (lh_hi, fam.lambda_away),
            )
            for a, b in sample_pts:
                corners.append(_make_grid(a, b))
        return center, corners

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "fixture_id": self.fixture_id,
            "cutoff_utc": self.cutoff_utc,
            "families": {k: v.to_dict() for k, v in self.families.items()},
            "outcomes_1x2": self.outcomes_1x2,
            "outcomes_1x2_bounds": {k: list(v) for k, v in self.outcomes_1x2_bounds.items()},
            "double_chance": self.double_chance,
            "double_chance_bounds": {k: list(v) for k, v in self.double_chance_bounds.items()},
            "btts": self.btts,
            "btts_bounds": {k: list(v) for k, v in self.btts_bounds.items()},
            "coherence_checks": self.coherence_checks,
            "ignored_exploratory_count": self.ignored_exploratory_count,
            "model_version": self.model_version,
        }


DEFAULT_PRIORS: dict[str, tuple[float, float]] = {
    # metric: (home_prior, away_prior) calibrados out-of-sample (Etapa 2)
    "goals": (1.56, 1.26),
    "goals_h1": (0.70, 0.51),
    "corners": (5.40, 4.45),
    "shots": (14.80, 12.10),
    "on_target": (5.05, 4.10),
    "fouls": (12.25, 12.40),
    "yellow": (2.00, 2.20),
}

STRATUM_PRIORS: dict[str, dict[str, tuple[float, float]]] = {
    "NATIONAL_TEAM": {
        "goals": (1.50, 1.40),
        "goals_h1": (0.74, 0.62),
        "corners": (4.95, 4.30),
        "shots": (14.00, 12.80),
        "on_target": (4.60, 4.25),
        "fouls": (12.10, 12.80),
        "yellow": (1.90, 2.15),
    },
}

FAMILY_DISPERSION_FLOORS: dict[str, float] = {
    "goals": 1.18,
    "goals_h1": 1.08,
    "corners": 1.62,
    "shots": 2.40,
    "on_target": 1.44,
    "fouls": 1.38,
    "yellow": 0.95,
}


def _resolve_prior(metric: str, stratum: str | None, venue: str) -> float:
    base_map = STRATUM_PRIORS.get(stratum or "", {})
    pair = base_map.get(metric) or DEFAULT_PRIORS.get(metric, (1.40, 1.20))
    return pair[0] if venue.upper() == "HOME" else pair[1]


def _red_card_weight(m: HistoricalTeamMatch, *, policy: str = "exclude_any") -> float:
    """Ponderación aislada por política de tarjeta roja ('exclude_any' por defecto en P1-A)."""
    if policy == "exclude_any":
        return 0.0 if m.red_card_distortion else 1.0
    if policy == "differentiated_weight":
        detail = getattr(m, "red_card_detail", None) or {}
        side = detail.get("distortion_side")
        if side in (None, "NONE", "LATE_ONLY"):
            return 1.0
        own_share = float(detail.get("own_affected_share") or 0.0)
        opp_share = float(detail.get("opp_affected_share") or 0.0)
        if side == "OWN_EARLY":
            return max(0.20, 1.0 - 0.70 * own_share)
        if side == "OPPONENT_EARLY":
            return max(0.35, 1.0 - 0.50 * opp_share)
        if side == "BOTH_EARLY":
            return max(0.25, 1.0 - 0.60 * max(own_share, opp_share))
        return 0.50 if m.red_card_distortion else 1.0
    raise ValueError(f"UNKNOWN_RED_CARD_POLICY:{policy}")


def _estimate_team_rate_detailed(
    own_matches: tuple[HistoricalTeamMatch, ...],
    opp_matches: tuple[HistoricalTeamMatch, ...],
    *,
    metric: str,
    target_league_id: int,
    target_season: int,
    target_stratum: str | None = None,
    own_venue: str,
    opp_venue: str,
    prior_mean: float,
    shrink_k: float = 8.0,
    history_limit: int = 20,
    red_card_policy: str = "exclude_any",
    strict_competition_identity: bool = False,
) -> dict[str, Any]:
    """Estima lambda con contracción Empirical Bayes, decaimiento entre competiciones y deduplicación H2H."""
    if history_limit <= 0:
        raise ValueError(f"INVALID_HISTORY_LIMIT:{history_limit}")
    warnings: list[str] = []

    def filter_valid(ms: tuple[HistoricalTeamMatch, ...], field_map: str) -> list[HistoricalTeamMatch]:
        ordered = sorted(ms, key=lambda m: m.kickoff_utc, reverse=True)
        valid = [
            m
            for m in ordered
            if _red_card_weight(m, policy=red_card_policy) > 0.0
            and (m.own_metrics if field_map == "own" else m.conceded_metrics).get(metric) is not None
        ]
        if len(valid) > history_limit:
            valid = valid[:history_limit]
        return valid

    if red_card_policy != "exclude_any":
        warnings.append(f"RED_CARD_POLICY:{red_card_policy}")

    own_sel = filter_valid(own_matches, "own")
    opp_sel = filter_valid(opp_matches, "conceded")

    own_fids = {m.fixture_id for m in own_sel}
    opp_fids = {m.fixture_id for m in opp_sel}
    shared_h2h_ids = own_fids & opp_fids
    shared_h2h_n = len(shared_h2h_ids)
    unique_fixture_ids = own_fids | opp_fids

    if strict_competition_identity:
        if any(not same_competition_season(m, target_league_id, target_season) for m in own_sel + opp_sel):
            warnings.append("COMPETITION_MIX_FALLBACK")
    elif any(m.league_id != target_league_id or m.season != target_season for m in own_sel + opp_sel):
        warnings.append("COMPETITION_MIX_FALLBACK")
    if shared_h2h_n > 0:
        warnings.append(f"SHARED_H2H_DEDUPLICATED_{shared_h2h_n}")

    if not own_sel and not opp_sel:
        warnings.append("NO_OBSERVATIONS_USING_PRIOR")
        phi_p = FAMILY_DISPERSION_FLOORS.get(metric, 1.2)
        return {
            "lambda": prior_mean,
            "variance": None,
            "phi": phi_p,
            "n_own": 0,
            "n_opp": 0,
            "n_eff": 0.0,
            "unique_fixture_n": 0,
            "shared_h2h_n": 0,
            "warnings": warnings,
        }

    def comp_weight(m: HistoricalTeamMatch) -> float:
        if strict_competition_identity:
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

    def match_weight(m: HistoricalTeamMatch) -> float:
        return comp_weight(m) * _red_card_weight(m, policy=red_card_policy)

    def weighted_shrunk(ms: list[HistoricalTeamMatch], getter: Any, *, apply_h2h_discount: bool) -> tuple[float, float]:
        if not ms:
            return prior_mean, 0.0
        pairs: list[tuple[HistoricalTeamMatch, float, float]] = []
        for m in ms:
            v = getter(m)
            if v is None:
                continue
            w = match_weight(m)
            if w <= 0.0:
                continue
            if apply_h2h_discount and m.fixture_id in shared_h2h_ids:
                # Un H2H compartido es la misma variable aleatoria en own_home y conceded_away; evitamos doble peso.
                w *= 0.50
            pairs.append((m, float(v), w))
        if not pairs:
            return prior_mean, 0.0
        w_sum = sum(w for _, _, w in pairs)
        l20_mean = sum(v * w for _, v, w in pairs) / w_sum if w_sum > 0 else statistics.fmean(v for _, v, _ in pairs)
        p6 = pairs[:6]
        w6_sum = sum(w for _, _, w in p6)
        l6_mean = sum(v * w for _, v, w in p6) / w6_sum if w6_sum > 0 else l20_mean
        sample_est = (0.35 * l6_mean + 0.65 * l20_mean) if len(pairs) >= 7 else l20_mean
        shrunk = (w_sum * sample_est + shrink_k * prior_mean) / (w_sum + shrink_k)
        return shrunk, w_sum

    own_shrunk, own_w_eff = weighted_shrunk(
        own_sel,
        lambda m: m.own_metrics.get(metric),
        apply_h2h_discount=False,
    )
    opp_shrunk, opp_w_eff = weighted_shrunk(
        opp_sel,
        lambda m: m.conceded_metrics.get(metric),
        apply_h2h_discount=True,
    )

    own_venue_pairs = [
        (float(m.own_metrics[metric]), match_weight(m))
        for m in own_sel
        if m.venue == own_venue and m.own_metrics.get(metric) is not None and match_weight(m) > 0.0
    ]
    opp_venue_pairs = [
        (float(m.conceded_metrics[metric]), match_weight(m))
        for m in opp_sel
        if m.venue == opp_venue and m.conceded_metrics.get(metric) is not None and match_weight(m) > 0.0
    ]

    if len(own_venue_pairs) >= 3:
        ow = sum(w for _, w in own_venue_pairs)
        ov_mean = sum(v * w for v, w in own_venue_pairs) / ow if ow > 0 else statistics.fmean(v for v, _ in own_venue_pairs)
        own_shrunk = 0.75 * own_shrunk + 0.25 * ov_mean
    if len(opp_venue_pairs) >= 3:
        cw = sum(w for _, w in opp_venue_pairs)
        cv_mean = sum(v * w for v, w in opp_venue_pairs) / cw if cw > 0 else statistics.fmean(v for v, _ in opp_venue_pairs)
        opp_shrunk = 0.75 * opp_shrunk + 0.25 * cv_mean

    lam = max(0.05, 0.55 * own_shrunk + 0.45 * opp_shrunk)

    # Varianza muestral sobre fixtures ÚNICOS (sin doble conteo del H2H compartido)
    unique_obs: dict[int, float] = {}
    for m in own_sel:
        val = m.own_metrics.get(metric)
        if val is not None:
            unique_obs.setdefault(m.fixture_id, float(val))
    for m in opp_sel:
        val = m.conceded_metrics.get(metric)
        if val is not None and m.fixture_id not in unique_obs:
            unique_obs[m.fixture_id] = float(val)

    unique_vals = list(unique_obs.values())
    phi_prior = FAMILY_DISPERSION_FLOORS.get(metric, 1.2)
    sample_mean = statistics.fmean(unique_vals) if unique_vals else lam
    raw_var = statistics.pvariance(unique_vals) if len(unique_vals) >= 4 else None
    if raw_var is not None:
        # La sobredispersión empírica se mide respecto a la media muestral real, no respecto a lam inflado por prior.
        raw_phi = raw_var / max(sample_mean, 0.1)
        shrunk_phi = (len(unique_vals) * raw_phi + 12.0 * phi_prior) / (len(unique_vals) + 12.0)
    else:
        shrunk_phi = phi_prior
    # Para amarillas (sub/equidispersas) no forzamos NegBin salvo evidencia fuerte; preservamos dispersión para incertidumbre
    shrunk_var = (lam * shrunk_phi) if (metric != "yellow" or shrunk_phi > 1.20) else None
    n_eff = round(0.55 * own_w_eff + 0.45 * opp_w_eff, 4)
    if unique_vals and shrink_k > n_eff and abs(lam - sample_mean) / max(0.5, sample_mean) > 0.35:
        warnings.append("PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE")

    return {
        "lambda": lam,
        "variance": shrunk_var,
        "phi": round(max(0.85, shrunk_phi), 4),
        "n_own": len(own_sel),
        "n_opp": len(opp_sel),
        "n_eff": n_eff,
        "unique_fixture_n": len(unique_fixture_ids),
        "shared_h2h_n": shared_h2h_n,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _estimate_team_rate(
    own_matches: tuple[HistoricalTeamMatch, ...],
    opp_matches: tuple[HistoricalTeamMatch, ...],
    *,
    metric: str,
    target_league_id: int,
    target_season: int,
    own_venue: str,
    opp_venue: str,
    prior_mean: float,
    shrink_k: float = 8.0,
    history_limit: int = 20,
) -> tuple[float, float | None, int, list[str]]:
    """Compatibilidad de firma: delega a _estimate_team_rate_detailed."""
    est = _estimate_team_rate_detailed(
        own_matches,
        opp_matches,
        metric=metric,
        target_league_id=target_league_id,
        target_season=target_season,
        own_venue=own_venue,
        opp_venue=opp_venue,
        prior_mean=prior_mean,
        shrink_k=shrink_k,
        history_limit=history_limit,
    )
    return est["lambda"], est["variance"], est["n_own"], est["warnings"]


class CoherentMatchModeler:
    """Constructor de MatchProbabilisticModel coherente y blindado frente a patrones exploratorios."""

    FAMILY_MAP: dict[str, str] = {
        "GOALS_FT": "goals",
        "GOALS_H1": "goals_h1",
        "CORNERS": "corners",
        "SHOTS": "shots",
        "ON_TARGET": "on_target",
        "FOULS": "fouls",
        "YELLOW_CARDS": "yellow",
    }

    @classmethod
    def build_model(
        cls,
        bundle: MatchObservationBundle,
        *,
        accepted_features: tuple[AcceptedModelFeature, ...] = (),
        exploratory_artifacts: tuple[StatisticalPattern | TacticalHypothesis | ValidatedPattern, ...] = (),
        history_limit: int = 20,
        strict_competition_identity: bool = False,
    ) -> MatchProbabilisticModel:
        """Construye distribuciones para todas las familias garantizando que ningún artefacto
        exploratorio o no promovido a AcceptedModelFeature altere p_model.
        """
        if history_limit <= 0:
            raise ValueError(f"INVALID_HISTORY_LIMIT:{history_limit}")
        for item in accepted_features:
            if not isinstance(item, AcceptedModelFeature):
                raise EpistemicContractError(
                    f"UNAUTHORIZED_OBJECT_IN_ACCEPTED_FEATURES: {type(item).__name__} no puede modificar p_model"
                )

        # Cualquier patrón exploratorio/validado pasado en exploratory_artifacts se contabiliza
        # pero jamás altera las tasas lambda ni p_model.
        ignored_exploratory = len(exploratory_artifacts)

        families: dict[str, FamilyDistributionEstimate] = {}
        outcomes_1x2 = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
        double_chance = {"1X": 0.0, "X2": 0.0, "12": 0.0}
        btts = {"yes": 0.0, "no": 0.0}
        outcomes_1x2_bounds: dict[str, tuple[float, float]] = {}
        double_chance_bounds: dict[str, tuple[float, float]] = {}
        btts_bounds: dict[str, tuple[float, float]] = {}

        target_stratum = next(
            (
                m.competition_stratum
                for m in (bundle.home_matches + bundle.away_matches)
                if m.league_id == bundle.league_id
                and m.competition_stratum
                and m.competition_stratum != "UNKNOWN"
            ),
            None,
        )

        for fam_name, metric_key in cls.FAMILY_MAP.items():
            h_prior = _resolve_prior(metric_key, target_stratum, "HOME")
            a_prior = _resolve_prior(metric_key, target_stratum, "AWAY")
            est_h = _estimate_team_rate_detailed(
                bundle.home_matches,
                bundle.away_matches,
                metric=metric_key,
                target_league_id=bundle.league_id,
                target_season=bundle.season,
                target_stratum=target_stratum,
                own_venue="HOME",
                opp_venue="AWAY",
                prior_mean=h_prior,
                history_limit=history_limit,
                strict_competition_identity=strict_competition_identity,
            )
            est_a = _estimate_team_rate_detailed(
                bundle.away_matches,
                bundle.home_matches,
                metric=metric_key,
                target_league_id=bundle.league_id,
                target_season=bundle.season,
                target_stratum=target_stratum,
                own_venue="AWAY",
                opp_venue="HOME",
                prior_mean=a_prior,
                history_limit=history_limit,
                strict_competition_identity=strict_competition_identity,
            )
            lh, vh, nh, phih, n_eff_h = est_h["lambda"], est_h["variance"], est_h["n_own"], est_h["phi"], est_h["n_eff"]
            la, va, na, phia, n_eff_a = est_a["lambda"], est_a["variance"], est_a["n_own"], est_a["phi"], est_a["n_eff"]
            wh, wa = est_h["warnings"], est_a["warnings"]

            eff_n = min(n_eff_h, n_eff_a) if min(nh, na) > 0 else float(min(nh, na))
            unc_width = round(1.0 / math.sqrt(max(1.0, eff_n + 2.0)), 4)
            warnings = tuple(dict.fromkeys(wh + wa))

            # Penalización epistémica moderada cuando hay mezcla entre competiciones distintas
            n_post_h = max(1.0, (n_eff_h + 8.0) * (0.85 if "COMPETITION_MIX_FALLBACK" in wh else 1.0))
            n_post_a = max(1.0, (n_eff_a + 8.0) * (0.85 if "COMPETITION_MIX_FALLBACK" in wa else 1.0))
            lh_low, lh_high = _gamma_rate_bounds(lh, n_post_h, phih)
            la_low, la_high = _gamma_rate_bounds(la, n_post_a, phia)
            shared_h2h_n = max(est_h["shared_h2h_n"], est_a["shared_h2h_n"])
            unique_fixture_n = len(
                {m.fixture_id for m in (bundle.home_matches + bundle.away_matches) if not m.red_card_distortion}
            )

            if fam_name == "GOALS_FT":
                def _dc_summary(l_h: float, l_a: float) -> dict[str, Any]:
                    g = dixon_coles_joint_grid(l_h, l_a, rho=-0.04, max_goals=12)
                    mg = len(g) - 1
                    hw = d = aw = bt = 0.0
                    p_h_m = [sum(g[i][j] for j in range(mg + 1)) for i in range(mg + 1)]
                    p_a_m = [sum(g[i][j] for i in range(mg + 1)) for j in range(mg + 1)]
                    p_t_m = [0.0] * (2 * mg + 1)
                    for i in range(mg + 1):
                        for j in range(mg + 1):
                            p = g[i][j]
                            p_t_m[i + j] += p
                            if i > j:
                                hw += p
                            elif i == j:
                                d += p
                            else:
                                aw += p
                            if i >= 1 and j >= 1:
                                bt += p
                    return {
                        "pmf_h": p_h_m,
                        "pmf_a": p_a_m,
                        "pmf_tot": p_t_m,
                        "home_win": hw,
                        "draw": d,
                        "away_win": aw,
                        "1X": hw + d,
                        "X2": d + aw,
                        "12": hw + aw,
                        "yes": bt,
                        "no": 1.0 - bt,
                    }

                c = _dc_summary(lh, la)
                corners = [
                    c,
                    _dc_summary(lh_low, la_low),
                    _dc_summary(lh_low, la_high),
                    _dc_summary(lh_high, la_low),
                    _dc_summary(lh_high, la_high),
                ]
                outcomes_1x2 = {k: round(c[k], 4) for k in ("home_win", "draw", "away_win")}
                double_chance = {k: round(c[k], 4) for k in ("1X", "X2", "12")}
                btts = {k: round(c[k], 4) for k in ("yes", "no")}
                outcomes_1x2_bounds = {
                    k: (
                        round(max(0.001, min(x[k] for x in corners)), 4),
                        round(min(0.999, max(x[k] for x in corners)), 4),
                    )
                    for k in ("home_win", "draw", "away_win")
                }
                double_chance_bounds = {
                    k: (
                        round(max(0.001, min(x[k] for x in corners)), 4),
                        round(min(0.999, max(x[k] for x in corners)), 4),
                    )
                    for k in ("1X", "X2", "12")
                }
                btts_bounds = {
                    k: (
                        round(max(0.001, min(x[k] for x in corners)), 4),
                        round(min(0.999, max(x[k] for x in corners)), 4),
                    )
                    for k in ("yes", "no")
                }

                pmf_h = c["pmf_h"]
                pmf_a = c["pmf_a"]
                pmf_tot = c["pmf_tot"]
                exp_h = sum(i * p for i, p in enumerate(pmf_h))
                exp_a = sum(j * p for j, p in enumerate(pmf_a))
                exp_t = sum(k * p for k, p in enumerate(pmf_tot))
                families[fam_name] = FamilyDistributionEstimate(
                    family=fam_name,
                    model_type="BIVARIATE_DIXON_COLES_POISSON",
                    lambda_home=round(exp_h, 4),
                    lambda_away=round(exp_a, 4),
                    lambda_total=round(exp_t, 4),
                    home_sample_n=nh,
                    away_sample_n=na,
                    dispersion_home=None,
                    dispersion_away=None,
                    pmf_home=tuple(pmf_h),
                    pmf_away=tuple(pmf_a),
                    pmf_total=tuple(pmf_tot),
                    uncertainty_rel_width=unc_width,
                    warnings=warnings,
                    lambda_home_low=lh_low,
                    lambda_home_high=lh_high,
                    lambda_away_low=la_low,
                    lambda_away_high=la_high,
                    dispersion_phi_home=phih,
                    dispersion_phi_away=phia,
                    unique_fixture_n=unique_fixture_n,
                    shared_h2h_n=shared_h2h_n,
                    home_n_eff=n_eff_h,
                    away_n_eff=n_eff_a,
                )
            else:
                max_k = 12 if fam_name == "GOALS_H1" else (45 if fam_name in ("SHOTS", "FOULS") else 30)
                pmf_h, m_h, disp_h = build_count_pmf(lh, vh, nh, max_k=max_k)
                pmf_a, m_a, disp_a = build_count_pmf(la, va, na, max_k=max_k)
                pmf_tot = convolve_pmfs(pmf_h, pmf_a)
                model_label = "NEGATIVE_BINOMIAL_CONVOLUTION" if "NEGATIVE_BINOMIAL" in (m_h, m_a) else "POISSON_CONVOLUTION"
                exp_h = sum(i * p for i, p in enumerate(pmf_h))
                exp_a = sum(j * p for j, p in enumerate(pmf_a))
                exp_t = sum(k * p for k, p in enumerate(pmf_tot))
                families[fam_name] = FamilyDistributionEstimate(
                    family=fam_name,
                    model_type=model_label,
                    lambda_home=round(exp_h, 4),
                    lambda_away=round(exp_a, 4),
                    lambda_total=round(exp_t, 4),
                    home_sample_n=nh,
                    away_sample_n=na,
                    dispersion_home=disp_h,
                    dispersion_away=disp_a,
                    pmf_home=tuple(pmf_h),
                    pmf_away=tuple(pmf_a),
                    pmf_total=tuple(pmf_tot),
                    uncertainty_rel_width=unc_width,
                    warnings=warnings,
                    lambda_home_low=lh_low,
                    lambda_home_high=lh_high,
                    lambda_away_low=la_low,
                    lambda_away_high=la_high,
                    dispersion_phi_home=phih,
                    dispersion_phi_away=phia,
                    unique_fixture_n=unique_fixture_n,
                    shared_h2h_n=shared_h2h_n,
                    home_n_eff=n_eff_h,
                    away_n_eff=n_eff_a,
                )

        # Verificación matemática de coherencia Equipo <-> Partido
        coherence: dict[str, Any] = {"all_coherent": True, "families": {}}
        for fk, fdist in families.items():
            diff = abs((fdist.lambda_home + fdist.lambda_away) - fdist.lambda_total)
            sum_pmf = sum(fdist.pmf_total)
            ok = diff < 1e-3 and abs(sum_pmf - 1.0) < 1e-4
            if not ok:
                coherence["all_coherent"] = False
            coherence["families"][fk] = {
                "mean_additivity_error": round(diff, 6),
                "total_pmf_sum": round(sum_pmf, 6),
                "coherent": ok,
            }

        return MatchProbabilisticModel(
            event_id=bundle.event_id,
            fixture_id=bundle.fixture_id,
            cutoff_utc=bundle.cutoff_utc,
            families=families,
            outcomes_1x2=outcomes_1x2,
            double_chance=double_chance,
            btts=btts,
            coherence_checks=coherence,
            ignored_exploratory_count=ignored_exploratory,
            outcomes_1x2_bounds=outcomes_1x2_bounds,
            double_chance_bounds=double_chance_bounds,
            btts_bounds=btts_bounds,
        )
