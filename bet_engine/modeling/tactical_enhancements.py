"""Mejoras táctico-cuantitativas post-baseline sin alterar los artefactos congelados (2B.2A).

1. Ajuste bivariado Dixon-Coles en 1ª Parte (`GOALS_H1`, rho=-0.05) para capturar la
   compactación táctica inicial (0-0 y 1-1 al descanso) en lugar de independencia pura.
2. Mezcla conservadora Goles + xG (`0.45 * lambda_goals + 0.55 * lambda_xg`) en `GOALS_FT`
   únicamente cuando ambos equipos cuentan con muestra verificada de `xg` (n >= 4), reduciendo
   el ruido de finalización en muestras cortas sin inventar datos cuando falta xG.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from bet_engine.context.observation_store import MatchObservationBundle
from bet_engine.modeling.distributions import (
    FamilyDistributionEstimate,
    MatchProbabilisticModel,
    _estimate_team_rate_detailed,
    _gamma_rate_bounds,
    _resolve_prior,
    dixon_coles_joint_grid,
)

H1_DIXON_COLES_RHO = -0.05
FT_DIXON_COLES_RHO = -0.04
XG_BLEND_WEIGHT = 0.55
MIN_XG_SAMPLE_PER_TEAM = 4


def _summarize_dc_grid(lam_h: float, lam_a: float, *, rho: float, max_goals: int) -> dict[str, Any]:
    g = dixon_coles_joint_grid(lam_h, lam_a, rho=rho, max_goals=max_goals)
    mg = len(g) - 1
    hw = d = aw = bt = 0.0
    pmf_h = [sum(g[i][j] for j in range(mg + 1)) for i in range(mg + 1)]
    pmf_a = [sum(g[i][j] for i in range(mg + 1)) for j in range(mg + 1)]
    pmf_tot = [0.0] * (2 * mg + 1)
    for i in range(mg + 1):
        for j in range(mg + 1):
            p = g[i][j]
            pmf_tot[i + j] += p
            if i > j:
                hw += p
            elif i == j:
                d += p
            else:
                aw += p
            if i >= 1 and j >= 1:
                bt += p
    return {
        "grid": g,
        "pmf_h": pmf_h,
        "pmf_a": pmf_a,
        "pmf_tot": pmf_tot,
        "home_win": hw,
        "draw": d,
        "away_win": aw,
        "1X": hw + d,
        "X2": d + aw,
        "12": hw + aw,
        "yes": bt,
        "no": 1.0 - bt,
    }


def build_h1_dixon_coles_grids(
    h1_fam: FamilyDistributionEstimate | None,
    *,
    rho: float = H1_DIXON_COLES_RHO,
    max_goals: int = 8,
) -> tuple[list[list[float]], list[list[list[float]]]]:
    """Construye grilla conjunta Dixon-Coles y esquinas epistémicas para 1ª Parte (`GOALS_H1`)."""
    if h1_fam is None:
        return [], []
    lh = h1_fam.lambda_home
    la = h1_fam.lambda_away
    lh_lo = h1_fam.lambda_home_low if h1_fam.lambda_home_low is not None else lh
    lh_hi = h1_fam.lambda_home_high if h1_fam.lambda_home_high is not None else lh
    la_lo = h1_fam.lambda_away_low if h1_fam.lambda_away_low is not None else la
    la_hi = h1_fam.lambda_away_high if h1_fam.lambda_away_high is not None else la

    center = dixon_coles_joint_grid(lh, la, rho=rho, max_goals=max_goals)
    corners = [center]
    if lh_lo != lh_hi or la_lo != la_hi:
        for a, b in (
            (lh_lo, la_lo),
            (lh_lo, la_hi),
            (lh_hi, la_lo),
            (lh_hi, la_hi),
            (lh, la_lo),
            (lh, la_hi),
            (lh_lo, la),
            (lh_hi, la),
        ):
            corners.append(dixon_coles_joint_grid(a, b, rho=rho, max_goals=max_goals))
    return center, corners


def enhance_match_model(
    bundle: MatchObservationBundle,
    model: MatchProbabilisticModel,
    *,
    history_limit: int = 20,
    enable_xg_blend: bool = True,
    enable_h1_dixon_coles: bool = True,
) -> MatchProbabilisticModel:
    """Aplica ajuste Dixon-Coles en `GOALS_H1` y blend conservador con `xG` en `GOALS_FT` cuando hay cobertura."""
    families = dict(model.families)
    outcomes_1x2 = dict(model.outcomes_1x2)
    double_chance = dict(model.double_chance)
    btts = dict(model.btts)
    outcomes_1x2_bounds = dict(model.outcomes_1x2_bounds)
    double_chance_bounds = dict(model.double_chance_bounds)
    btts_bounds = dict(model.btts_bounds)

    # 1) Ajuste Dixon-Coles en 1ª Parte (`GOALS_H1`)
    h1_fam = families.get("GOALS_H1")
    if enable_h1_dixon_coles and h1_fam is not None:
        c_h1 = _summarize_dc_grid(h1_fam.lambda_home, h1_fam.lambda_away, rho=H1_DIXON_COLES_RHO, max_goals=8)
        exp_h = sum(i * p for i, p in enumerate(c_h1["pmf_h"]))
        exp_a = sum(j * p for j, p in enumerate(c_h1["pmf_a"]))
        exp_t = sum(k * p for k, p in enumerate(c_h1["pmf_tot"]))
        families["GOALS_H1"] = replace(
            h1_fam,
            model_type="BIVARIATE_DIXON_COLES_FIRST_HALF",
            lambda_home=round(exp_h, 4),
            lambda_away=round(exp_a, 4),
            lambda_total=round(exp_t, 4),
            dispersion_home=None,
            dispersion_away=None,
            pmf_home=tuple(c_h1["pmf_h"]),
            pmf_away=tuple(c_h1["pmf_a"]),
            pmf_total=tuple(c_h1["pmf_tot"]),
            accepted_features_applied=tuple(dict.fromkeys((*h1_fam.accepted_features_applied, "H1_DIXON_COLES_RHO"))),
        )

    # 2) Blend conservador Goles + xG en `GOALS_FT` solo si ambos lados tienen >= 4 partidos con xG válido
    ft_fam = families.get("GOALS_FT")
    if enable_xg_blend and ft_fam is not None:
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
        h_prior = _resolve_prior("goals", target_stratum, "HOME")
        a_prior = _resolve_prior("goals", target_stratum, "AWAY")
        xg_h = _estimate_team_rate_detailed(
            bundle.home_matches,
            bundle.away_matches,
            metric="xg",
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=target_stratum,
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=h_prior,
            history_limit=history_limit,
        )
        xg_a = _estimate_team_rate_detailed(
            bundle.away_matches,
            bundle.home_matches,
            metric="xg",
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=target_stratum,
            own_venue="AWAY",
            opp_venue="HOME",
            prior_mean=a_prior,
            history_limit=history_limit,
        )
        if xg_h["n_own"] >= MIN_XG_SAMPLE_PER_TEAM and xg_a["n_own"] >= MIN_XG_SAMPLE_PER_TEAM:
            w_xg = XG_BLEND_WEIGHT
            lh_blend = max(0.05, (1.0 - w_xg) * ft_fam.lambda_home + w_xg * xg_h["lambda"])
            la_blend = max(0.05, (1.0 - w_xg) * ft_fam.lambda_away + w_xg * xg_a["lambda"])
            n_eff_h = ft_fam.home_n_eff if ft_fam.home_n_eff is not None else float(ft_fam.home_sample_n)
            n_eff_a = ft_fam.away_n_eff if ft_fam.away_n_eff is not None else float(ft_fam.away_sample_n)
            phih = ft_fam.dispersion_phi_home if ft_fam.dispersion_phi_home is not None else 1.18
            phia = ft_fam.dispersion_phi_away if ft_fam.dispersion_phi_away is not None else 1.18
            mix_pen_h = 0.85 if "COMPETITION_MIX_FALLBACK" in ft_fam.warnings else 1.0
            mix_pen_a = 0.85 if "COMPETITION_MIX_FALLBACK" in ft_fam.warnings else 1.0
            lh_low, lh_high = _gamma_rate_bounds(lh_blend, max(1.0, (n_eff_h + 8.0) * mix_pen_h), phih)
            la_low, la_high = _gamma_rate_bounds(la_blend, max(1.0, (n_eff_a + 8.0) * mix_pen_a), phia)

            c_ft = _summarize_dc_grid(lh_blend, la_blend, rho=FT_DIXON_COLES_RHO, max_goals=12)
            corner_summaries = [
                c_ft,
                _summarize_dc_grid(lh_low, la_low, rho=FT_DIXON_COLES_RHO, max_goals=12),
                _summarize_dc_grid(lh_low, la_high, rho=FT_DIXON_COLES_RHO, max_goals=12),
                _summarize_dc_grid(lh_high, la_low, rho=FT_DIXON_COLES_RHO, max_goals=12),
                _summarize_dc_grid(lh_high, la_high, rho=FT_DIXON_COLES_RHO, max_goals=12),
            ]
            outcomes_1x2 = {k: round(c_ft[k], 4) for k in ("home_win", "draw", "away_win")}
            double_chance = {k: round(c_ft[k], 4) for k in ("1X", "X2", "12")}
            btts = {k: round(c_ft[k], 4) for k in ("yes", "no")}
            outcomes_1x2_bounds = {
                k: (
                    round(max(0.001, min(x[k] for x in corner_summaries)), 4),
                    round(min(0.999, max(x[k] for x in corner_summaries)), 4),
                )
                for k in ("home_win", "draw", "away_win")
            }
            double_chance_bounds = {
                k: (
                    round(max(0.001, min(x[k] for x in corner_summaries)), 4),
                    round(min(0.999, max(x[k] for x in corner_summaries)), 4),
                )
                for k in ("1X", "X2", "12")
            }
            btts_bounds = {
                k: (
                    round(max(0.001, min(x[k] for x in corner_summaries)), 4),
                    round(min(0.999, max(x[k] for x in corner_summaries)), 4),
                )
                for k in ("yes", "no")
            }
            exp_h = sum(i * p for i, p in enumerate(c_ft["pmf_h"]))
            exp_a = sum(j * p for j, p in enumerate(c_ft["pmf_a"]))
            exp_t = sum(k * p for k, p in enumerate(c_ft["pmf_tot"]))
            families["GOALS_FT"] = replace(
                ft_fam,
                lambda_home=round(exp_h, 4),
                lambda_away=round(exp_a, 4),
                lambda_total=round(exp_t, 4),
                pmf_home=tuple(c_ft["pmf_h"]),
                pmf_away=tuple(c_ft["pmf_a"]),
                pmf_total=tuple(c_ft["pmf_tot"]),
                lambda_home_low=lh_low,
                lambda_home_high=lh_high,
                lambda_away_low=la_low,
                lambda_away_high=la_high,
                accepted_features_applied=tuple(dict.fromkeys((*ft_fam.accepted_features_applied, "XG_GOALS_SHRINKAGE_BLEND"))),
            )

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

    return replace(
        model,
        families=families,
        outcomes_1x2=outcomes_1x2,
        double_chance=double_chance,
        btts=btts,
        outcomes_1x2_bounds=outcomes_1x2_bounds,
        double_chance_bounds=double_chance_bounds,
        btts_bounds=btts_bounds,
        coherence_checks=coherence,
    )
