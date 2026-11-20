"""Evaluador de mercados Bwin y comparador lado a lado con motor_probabilistico.py (Etapa 4+).

Soporta evaluación dinámica de todas las líneas .5 de Goles (FT/1ª parte, total y por equipo),
Córners, Tarjetas, Tiros, 1X2, Doble Oportunidad, Ganador sin empate (DNB con devolución por empate)
y Ambos equipos marcan (BTTS), manteniendo separación estricta entre:
- PICK_ACCIONABLE
- CANDIDATO_CONDICIONAL
- NO_BET_POR_PRECIO
- NO_BET_POR_EVIDENCIA
- BLOQUEO_TECNICO
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from bet_engine.analytics import AnalyticsEngineResult
from bet_engine.context.observation_store import HistoricalTeamMatch, MatchObservationBundle
from bet_engine.modeling import MatchProbabilisticModel
from bet_engine.modeling.distributions import dixon_coles_joint_grid
from .market_equivalence import audit_snapshot_equivalences, canonicalize_selection_key

_DYNAMIC_OU_KEY_RE = re.compile(
    r"^(?:(h1)_)?(?:(home|away)_)?(over|under)_(\d+_\d+)_(goals|corners|cards|shots|on_target|fouls)$"
)

_METRIC_TO_FAMILY = {
    ("goals", False): ("GOALS_FT", "goals", "OVER_UNDER_HALF_LINE_REGULATION"),
    ("goals", True): ("GOALS_H1", "goals_h1", "OVER_UNDER_HALF_LINE_FIRST_HALF"),
    ("corners", False): ("CORNERS", "corners", "OVER_UNDER_HALF_LINE_REGULATION"),
    ("cards", False): ("YELLOW_CARDS", "yellow", "OVER_UNDER_HALF_LINE_REGULATION"),
    ("shots", False): ("SHOTS", "shots", "OVER_UNDER_HALF_LINE_REGULATION"),
    ("on_target", False): ("ON_TARGET", "on_target", "OVER_UNDER_HALF_LINE_REGULATION"),
    ("fouls", False): ("FOULS", "fouls", "OVER_UNDER_HALF_LINE_REGULATION"),
}


@dataclass(frozen=True)
class EvaluatedMarketOption:
    """Evaluación cuantitativa y comparativa de una selección de mercado."""

    market_family: str
    selection_key: str
    line: float | None
    odd: float | None
    break_even_prob: float | None
    p_model: float | None
    p_low: float | None
    p_high: float | None
    ev_model: float | None
    ev_conservative_low: float | None
    legacy_p6_hit_rate: float | None
    legacy_p6_n: int
    delta_vs_legacy_p6: float | None
    classification: str  # QUANTITATIVELY_EVALUABLE_EDGE | INVESTIGABLE_CANDIDATE | NO_EDGE_OR_HIGH_UNCERTAINTY | NO_ODDS | MERCADO_NO_MODELADO
    tactical_warnings: tuple[str, ...] = ()
    settlement_rule: str = "STANDARD_REGULATION"
    decision_category: str = "NO_BET_POR_EVIDENCIA"  # PICK_ACCIONABLE | CANDIDATO_CONDICIONAL | NO_BET_POR_PRECIO | NO_BET_POR_EVIDENCIA | BLOQUEO_TECNICO | MERCADO_NO_MODELADO
    fair_odd: float | None = None
    p_win_unconditional: float | None = None
    p_push_refund: float | None = None
    pending_conditions: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    canonical_contract_id: str | None = None
    equivalent_best_selection: str | None = None
    equivalent_best_odd: float | None = None
    raw_sample_n: int | None = None
    effective_sample_n: float | None = None
    calibration_status: str = "JOINT_MATCH_CALIBRATION_NOT_ACCREDITED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def epistemic_evaluation_state(self) -> str:
        """Mapea el resultado cuantitativo a la taxonomía explícita sin alterar decision_category ni modelos."""
        if self.classification == "MERCADO_NO_MODELADO" or self.decision_category == "MERCADO_NO_MODELADO":
            return "MODEL_UNAVAILABLE"
        if self.classification == "NO_ODDS" or self.odd is None or self.odd <= 1.0:
            return "NO_MARKET_CAPTURED"
        if self.decision_category == "NO_BET_POR_EVIDENCIA":
            return "INSUFFICIENT_STATISTICAL_EVIDENCE"
        if self.decision_category in ("PICK_ACCIONABLE", "CANDIDATO_CONDICIONAL"):
            return "MODEL_EVALUATED_VALUE_CANDIDATE"
        return "MODEL_EVALUATED_NO_VALUE"

    @property
    def formal_decision_classification(self) -> str:
        """Clasificación explícita de 5 estados: distingue precio vs incertidumbre vs modelo vs bloqueo operativo."""
        return classify_option_formal_decision(self)


FORMAL_DECISION_CLASSIFICATIONS: tuple[str, ...] = (
    "BET_AUTORIZADO",
    "NO_BET_POR_PRECIO",
    "NO_BET_POR_INCERTIDUMBRE",
    "MODELO_NO_DISPONIBLE",
    "BLOQUEO_OPERATIVO",
)


def classify_option_formal_decision(opt: EvaluatedMarketOption) -> str:
    """Clasifica una opción evaluada separando precio acreditado, incertidumbre/calibración, modelo y bloqueo operativo."""
    if opt.decision_category == "MERCADO_NO_MODELADO" or opt.classification == "MERCADO_NO_MODELADO":
        return "MODELO_NO_DISPONIBLE"
    if opt.decision_category == "BLOQUEO_TECNICO" or opt.classification == "NO_ODDS" or opt.odd is None or opt.odd <= 1.0:
        return "BLOQUEO_OPERATIVO"
    if opt.decision_category == "PICK_ACCIONABLE":
        return "BET_AUTORIZADO"
    if (
        opt.decision_category in ("CANDIDATO_CONDICIONAL", "NO_BET_POR_EVIDENCIA")
        or opt.calibration_status != "CALIBRATED_OUT_OF_SAMPLE"
    ):
        return "NO_BET_POR_INCERTIDUMBRE"
    return "NO_BET_POR_PRECIO"


def _legacy_p6_over_rate(
    home_matches: tuple[HistoricalTeamMatch, ...],
    away_matches: tuple[HistoricalTeamMatch, ...],
    *,
    metric: str,
    scope: str,
    line: float,
) -> tuple[float | None, int]:
    """Reproduce la frecuencia empírica P-1..P-6 evitando doble conteo de H2H en scope='total'."""
    threshold = math.floor(line + 1e-9)
    vals: list[float] = []
    seen_fids: set[int] = set()
    if scope in ("home", "total"):
        for m in home_matches[:6]:
            if scope == "home":
                v = m.own_metrics.get(metric)
                if v is not None:
                    vals.append(float(v))
            else:
                if m.fixture_id in seen_fids:
                    continue
                vo = m.own_metrics.get(metric)
                vc = m.conceded_metrics.get(metric)
                if vo is not None and vc is not None:
                    seen_fids.add(m.fixture_id)
                    vals.append(float(vo) + float(vc))
    if scope in ("away", "total"):
        for m in away_matches[:6]:
            if scope == "away":
                v = m.own_metrics.get(metric)
                if v is not None:
                    vals.append(float(v))
            else:
                if m.fixture_id in seen_fids:
                    continue
                vo = m.own_metrics.get(metric)
                vc = m.conceded_metrics.get(metric)
                if vo is not None and vc is not None:
                    seen_fids.add(m.fixture_id)
                    vals.append(float(vo) + float(vc))
    if not vals:
        return None, 0
    hits = sum(1 for v in vals if v > threshold)
    return round(hits / len(vals), 4), len(vals)


def _family_calibration_info(
    family: str,
    accredited_families: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    if family in accredited_families:
        return True, "CALIBRATED_OUT_OF_SAMPLE"
    if family in (
        "GOALS_FT",
        "MATCH_RESULT_1X2",
        "DOUBLE_CHANCE",
        "BTTS",
        "DRAW_NO_BET",
        "EUROPEAN_HANDICAP_1X2",
        "RESULT_AND_BTTS",
        "BTTS_AND_EITHER_WIN",
        "BTTS_AND_TOTAL_2_5",
        "EXACT_SCORE_FT",
    ):
        return False, "JOINT_MATCH_CALIBRATION_NOT_ACCREDITED"
    if "H1" in family:
        return False, "FIRST_HALF_JOINT_CALIBRATION_NOT_ACCREDITED"
    return False, "LIMITED_PREDICTIVE_EVIDENCE_FAMILY_NOT_CALIBRATED"


def _effective_n_for_family(fdist: Any, scope: str = "total") -> float | None:
    hn = getattr(fdist, "home_n_eff", None)
    an = getattr(fdist, "away_n_eff", None)
    if scope == "home":
        return round(float(hn), 4) if hn is not None else None
    if scope == "away":
        return round(float(an), 4) if an is not None else None
    if hn is not None and an is not None:
        return round(min(float(hn), float(an)), 4)
    return None


def _classify_unmodelled_parsed_selection(sel_key: str) -> tuple[str, str, str]:
    if sel_key.startswith("eh_"):
        return (
            "EUROPEAN_HANDICAP_1X2",
            f"NO_MODELO_VALIDO:OFFSET_HANDICAP_EUROPEO_FUERA_DE_SOPORTE({sel_key})",
            "UNVERIFIED_UNMODELLED_HANDICAP_OFFSET",
        )
    if sel_key == "first_goal_none":
        return (
            "FIRST_TEAM_TO_SCORE",
            "NO_MODELO_VALIDO:MERCADO_PRIMER_GOL_SIN_MODELO_PROPIO(equivalencia_0_0_auditada)",
            "FIRST_GOAL_REGULATION_TIME_UNMODELLED",
        )
    if sel_key == "h1_first_goal_none":
        return (
            "FIRST_TEAM_TO_SCORE_H1",
            "NO_MODELO_VALIDO:MERCADO_PRIMER_GOL_1H_SIN_MODELO_PROPIO(equivalencia_0_0_auditada)",
            "FIRST_GOAL_FIRST_HALF_UNMODELLED",
        )
    if _DYNAMIC_OU_KEY_RE.match(sel_key):
        return (
            "OVER_UNDER_UNMODELLED",
            f"NO_MODELO_VALIDO:LINEA_O_METRICA_NO_MODELADA({sel_key})",
            "UNVERIFIED_INTEGER_PUSH_OR_METRIC",
        )
    return (
        "UNMODELLED_MARKET",
        f"NO_MODELO_VALIDO:SELECCION_PARSEADA_SIN_ESTIMADOR({sel_key})",
        "UNMODELLED_SELECTION",
    )


def _classify_option(
    *,
    odd: float | None,
    p_mod: float,
    ev_m: float | None,
    ev_lo: float | None,
    fair_odd: float | None,
    sample_n: int,
    tactical_warnings: tuple[str, ...] = (),
    leg_hit_rate: float | None = None,
    leg_n: int = 0,
    is_over_line: bool = False,
    lineups_confirmed_complete: bool = False,
    lineup_status: str = "LINEUPS_PENDING",
    calibration_accredited: bool = False,
    calibration_status: str = "JOINT_MATCH_CALIBRATION_NOT_ACCREDITED",
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Asigna clasificación, categoría de estado y flags de riesgo sin convertir EV conservador positivo en accionable automático."""
    if odd is None or odd <= 1.0:
        return "NO_ODDS", "BLOQUEO_TECNICO", ("SIN_CUOTA_BWIN_EN_SNAPSHOT",), ()

    assert ev_m is not None and ev_lo is not None
    pending: list[str] = []
    risk_flags: list[str] = []
    insufficient_evidence = False
    if sample_n < 4:
        insufficient_evidence = True
        pending.append(f"MUESTRA_HISTORICA_CORTA(n={sample_n}<4)")
        risk_flags.append(f"MUESTRA_INSUFICIENTE(n={sample_n}<4)")

    historical_zero_risk = False
    if is_over_line and leg_n >= 4 and leg_hit_rate is not None and leg_hit_rate == 0.0:
        historical_zero_risk = True
        risk_flag = f"RIESGO_HISTORICO_0_DE_{leg_n}(muestra={leg_n};revisar_competicion_y_incertidumbre)"
        risk_flags.append(risk_flag)
        pending.append(risk_flag)

    high_tail_sensitivity = p_mod < 0.05
    if high_tail_sensitivity:
        risk_flags.append("COLA_ESTADISTICA_ALTA_SENSIBILIDAD_PRECIO")

    if not lineups_confirmed_complete:
        risk_flags.append(f"SIN_22_TITULARES_CONFIRMADOS({lineup_status})")

    small_sample_or_uncalibrated = (
        sample_n < 10
        or "SMALL_SAMPLE_EXPLORATORY_ONLY" in tactical_warnings
        or not calibration_accredited
    )

    if (
        lineups_confirmed_complete
        and calibration_accredited
        and ev_lo > 0.0
        and not insufficient_evidence
        and not small_sample_or_uncalibrated
        and not historical_zero_risk
        and not high_tail_sensitivity
    ):
        cls_label = "QUANTITATIVELY_EVALUABLE_EDGE"
        dec_cat = "PICK_ACCIONABLE"
        pending.append("VERIFICAR_VIGENCIA_CUOTA_PREPARTIDO")
    elif ev_m > 0.02 or (ev_lo > 0.0 and not insufficient_evidence):
        cls_label = "INVESTIGABLE_CANDIDATE"
        if insufficient_evidence:
            dec_cat = "NO_BET_POR_EVIDENCIA"
        else:
            dec_cat = "CANDIDATO_CONDICIONAL"
            if not lineups_confirmed_complete:
                pending.append(f"BLOQUEADO_ACCIONABLE_SIN_22_TITULARES:{lineup_status}")
            if not calibration_accredited:
                pending.append(f"CALIBRACION_NO_ACREDITADA:{calibration_status}")
            if high_tail_sensitivity:
                pending.append("COLA_ESTADISTICA_BAJA_PROBABILIDAD_NO_ACCIONABLE")
            if ev_lo > 0.0:
                if sample_n < 10 or historical_zero_risk:
                    pending.append(f"MUESTRA_CORTA_O_RIESGO_HISTORICO_NO_ACCIONABLE(n={sample_n})")
            else:
                pending.append("EV_CENTRAL_POSITIVO_CON_BANDA_INFERIOR_NEGATIVA_PENDIENTE_CALIBRACION")
            pending.append("REQUIERE_CONTRASTE_CUALITATIVO_Y_XI")
    else:
        cls_label = "NO_EDGE_OR_HIGH_UNCERTAINTY"
        dec_cat = "NO_BET_POR_EVIDENCIA" if insufficient_evidence else "NO_BET_POR_PRECIO"
        if ev_m > 0.0 and not insufficient_evidence:
            pending.append("EV_CENTRAL_MARGINAL_NO_MATERIAL(<=2%)")
        elif fair_odd is not None and not insufficient_evidence:
            pending.append(f"PRECIO_INSUFICIENTE_REQUIERE_CUOTA_MAYOR_A_{fair_odd:.2f}")

    return cls_label, dec_cat, tuple(dict.fromkeys(pending)), tuple(dict.fromkeys(risk_flags))


class BwinMarketEvaluator:
    """Cruza MatchProbabilisticModel con cuotas Bwin (estáticas + dinámicas del TXT)."""

    BASELINE_SPECS: tuple[tuple[str, str, str, float, str, str, str], ...] = (
        ("GOALS_FT", "total", "goals", 2.5, "OVER", "over_2_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_FT", "total", "goals", 2.5, "UNDER", "under_2_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_H1", "total", "goals_h1", 0.5, "OVER", "h1_over_0_5_goals", "OVER_UNDER_HALF_LINE_FIRST_HALF"),
        ("GOALS_H1", "total", "goals_h1", 1.5, "OVER", "h1_over_1_5_goals", "OVER_UNDER_HALF_LINE_FIRST_HALF"),
        ("GOALS_FT", "home", "goals", 0.5, "OVER", "home_over_0_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_FT", "home", "goals", 0.5, "UNDER", "home_under_0_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_FT", "away", "goals", 0.5, "OVER", "away_over_0_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_FT", "away", "goals", 0.5, "UNDER", "away_under_0_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_FT", "home", "goals", 1.5, "OVER", "home_over_1_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("GOALS_FT", "away", "goals", 1.5, "OVER", "away_over_1_5_goals", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("CORNERS", "total", "corners", 8.5, "OVER", "over_8_5_corners", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("CORNERS", "total", "corners", 9.5, "OVER", "over_9_5_corners", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("CORNERS", "total", "corners", 9.5, "UNDER", "under_9_5_corners", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("CORNERS", "total", "corners", 10.5, "OVER", "over_10_5_corners", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("CORNERS", "home", "corners", 4.5, "OVER", "home_over_4_5_corners", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("CORNERS", "away", "corners", 4.5, "OVER", "away_over_4_5_corners", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("SHOTS", "home", "shots", 11.5, "OVER", "home_over_11_5_shots", "OVER_UNDER_HALF_LINE_REGULATION"),
        ("YELLOW_CARDS", "total", "yellow", 4.5, "OVER", "over_4_5_cards", "OVER_UNDER_HALF_LINE_REGULATION"),
    )

    @classmethod
    def _build_ou_specs(cls, odds: dict[str, float]) -> list[tuple[str, str, str, float, str, str, str]]:
        seen: dict[str, tuple[str, str, str, float, str, str, str]] = {
            spec[5]: spec for spec in cls.BASELINE_SPECS
        }
        for sel_key in sorted(odds.keys()):
            if sel_key in seen:
                continue
            m = _DYNAMIC_OU_KEY_RE.match(sel_key)
            if not m:
                continue
            h1_prefix, side_prefix, dir_raw, line_slug, metric_slug = m.groups()
            is_h1 = h1_prefix == "h1"
            mapping = _METRIC_TO_FAMILY.get((metric_slug, is_h1))
            if mapping is None:
                continue
            fam_name, model_metric, settlement = mapping
            try:
                line_val = float(line_slug.replace("_", "."))
            except ValueError:
                continue
            # Solo líneas semienteras (.5) tienen liquidación binaria sin push
            if abs((line_val * 2) - round(line_val * 2)) > 1e-6 or round(line_val * 2) % 2 == 0:
                continue
            scope = side_prefix if side_prefix in ("home", "away") else "total"
            direction = "OVER" if dir_raw == "over" else "UNDER"
            seen[sel_key] = (fam_name, scope, model_metric, line_val, direction, sel_key, settlement)
        return list(seen.values())

    @classmethod
    def evaluate_standard_markets(
        cls,
        bundle: MatchObservationBundle,
        model: MatchProbabilisticModel,
        *,
        odds_map: dict[str, float] | None = None,
        analytics_result: AnalyticsEngineResult | None = None,
        lineups_confirmed_complete: bool | None = None,
        lineup_status: str | None = None,
        calibration_accredited_families: set[str] | frozenset[str] = frozenset(),
    ) -> list[EvaluatedMarketOption]:
        if lineups_confirmed_complete is None or lineup_status is None:
            le = getattr(bundle, "target_lineup_evaluation", None) or {}
            if lineups_confirmed_complete is None:
                lineups_confirmed_complete = bool(le.get("confirmed_complete_22"))
            if lineup_status is None:
                lineup_status = str(le.get("status") or "LINEUPS_PENDING")

        def _classify_for_match(fam_name: str, **kw: Any) -> tuple[str, str, tuple[str, ...], tuple[str, ...], str]:
            cal_ok, cal_status = _family_calibration_info(fam_name, calibration_accredited_families)
            cls_l, dec_c, pend, r_fl = _classify_option(
                lineups_confirmed_complete=bool(lineups_confirmed_complete),
                lineup_status=str(lineup_status),
                calibration_accredited=cal_ok,
                calibration_status=cal_status,
                **kw,
            )
            return cls_l, dec_c, pend, r_fl, cal_status

        odds = odds_map or {}
        tactical_flags: list[str] = []
        if analytics_result is not None:
            for p in analytics_result.prematch_patterns:
                tactical_flags.extend(p.contradiction_flags)
        unique_flags = tuple(dict.fromkeys(tactical_flags))

        out: list[EvaluatedMarketOption] = []
        goals_ft = model.families["GOALS_FT"]
        unc_w = goals_ft.uncertainty_rel_width
        sample_ft = min(goals_ft.home_sample_n, goals_ft.away_sample_n)
        eff_ft = _effective_n_for_family(goals_ft, "total")

        # 1) Mercados discretos 3-way, Doble Oportunidad y BTTS (liquidación binaria en tiempo reglamentario)
        dc_1x2_bounds = getattr(model, "outcomes_1x2_bounds", {}) or {}
        dc_dc_bounds = getattr(model, "double_chance_bounds", {}) or {}
        dc_btts_bounds = getattr(model, "btts_bounds", {}) or {}
        discrete_specs = [
            ("MATCH_RESULT_1X2", "home_win", model.outcomes_1x2["home_win"], dc_1x2_bounds.get("home_win"), unc_w, "THREE_WAY_REGULATION_TIME"),
            ("MATCH_RESULT_1X2", "draw", model.outcomes_1x2["draw"], dc_1x2_bounds.get("draw"), unc_w, "THREE_WAY_REGULATION_TIME"),
            ("MATCH_RESULT_1X2", "away_win", model.outcomes_1x2["away_win"], dc_1x2_bounds.get("away_win"), unc_w, "THREE_WAY_REGULATION_TIME"),
            ("DOUBLE_CHANCE", "double_chance_1x", model.double_chance["1X"], dc_dc_bounds.get("1X"), unc_w, "DOUBLE_CHANCE_REGULATION_TIME"),
            ("DOUBLE_CHANCE", "double_chance_x2", model.double_chance["X2"], dc_dc_bounds.get("X2"), unc_w, "DOUBLE_CHANCE_REGULATION_TIME"),
            ("DOUBLE_CHANCE", "double_chance_12", model.double_chance["12"], dc_dc_bounds.get("12"), unc_w, "DOUBLE_CHANCE_REGULATION_TIME"),
            ("BTTS", "btts_yes", model.btts["yes"], dc_btts_bounds.get("yes"), unc_w, "BTTS_REGULATION_TIME"),
            ("BTTS", "btts_no", model.btts["no"], dc_btts_bounds.get("no"), unc_w, "BTTS_REGULATION_TIME"),
        ]
        for fam, sel_key, p_mod, mod_bounds, u_w, settlement in discrete_specs:
            if mod_bounds is not None:
                p_lo = round(max(0.001, min(p_mod, float(mod_bounds[0]))), 4)
                p_hi = round(min(0.999, max(p_mod, float(mod_bounds[1]))), 4)
            else:
                half_band = min(0.22, u_w * 0.45)
                p_lo = round(max(0.001, p_mod - half_band), 4)
                p_hi = round(min(0.999, p_mod + half_band), 4)
            fair = round(1.0 / p_mod, 4) if p_mod > 1e-6 else None
            odd = odds.get(sel_key)
            if odd is not None and odd > 1.0:
                be = round(1.0 / odd, 4)
                ev_m = round(p_mod * odd - 1.0, 4)
                ev_lo = round(p_lo * odd - 1.0, 4)
            else:
                be = ev_m = ev_lo = None
            cls_label, dec_cat, pending, r_flags, cal_st = _classify_for_match(
                fam,
                odd=odd,
                p_mod=p_mod,
                ev_m=ev_m,
                ev_lo=ev_lo,
                fair_odd=fair,
                sample_n=sample_ft,
                tactical_warnings=unique_flags,
            )
            out.append(
                EvaluatedMarketOption(
                    market_family=fam,
                    selection_key=sel_key,
                    line=None,
                    odd=odd,
                    break_even_prob=be,
                    p_model=p_mod,
                    p_low=p_lo,
                    p_high=p_hi,
                    ev_model=ev_m,
                    ev_conservative_low=ev_lo,
                    legacy_p6_hit_rate=None,
                    legacy_p6_n=0,
                    delta_vs_legacy_p6=None,
                    classification=cls_label,
                    tactical_warnings=unique_flags,
                    settlement_rule=settlement,
                    decision_category=dec_cat,
                    fair_odd=fair,
                    p_win_unconditional=p_mod,
                    p_push_refund=0.0,
                    pending_conditions=pending,
                    risk_flags=r_flags,
                    raw_sample_n=sample_ft,
                    effective_sample_n=eff_ft,
                    calibration_status=cal_st,
                )
            )

        # 2) Ganador sin empate / Draw No Bet (con devolución explícita de apuesta en caso de empate)
        p_hw = model.outcomes_1x2["home_win"]
        p_d = model.outcomes_1x2["draw"]
        p_aw = model.outcomes_1x2["away_win"]
        non_draw = max(1e-6, 1.0 - p_d)
        # Propagación epistémica conjunta Dixon-Coles sobre la caja (lh_low..high, la_low..high)
        dnb_corner_outcomes: list[tuple[float, float, float]] = [(p_hw, p_d, p_aw)]
        if (
            goals_ft.lambda_home_low is not None
            and goals_ft.lambda_home_high is not None
            and goals_ft.lambda_away_low is not None
            and goals_ft.lambda_away_high is not None
        ):
            for c_lh in (goals_ft.lambda_home_low, goals_ft.lambda_home_high):
                for c_la in (goals_ft.lambda_away_low, goals_ft.lambda_away_high):
                    g = dixon_coles_joint_grid(c_lh, c_la, rho=-0.04, max_goals=12)
                    mg = len(g) - 1
                    c_hw = c_d = c_aw = 0.0
                    for i in range(mg + 1):
                        for j in range(mg + 1):
                            p = g[i][j]
                            if i > j:
                                c_hw += p
                            elif i == j:
                                c_d += p
                            else:
                                c_aw += p
                    dnb_corner_outcomes.append((c_hw, c_d, c_aw))

        for sel_key, p_win in (("dnb_home", p_hw), ("dnb_away", p_aw)):
            p_cond = round(max(0.001, min(0.999, p_win / non_draw)), 4)
            if len(dnb_corner_outcomes) > 1:
                conds = [
                    (ch if sel_key == "dnb_home" else ca) / max(1e-6, 1.0 - cd)
                    for ch, cd, ca in dnb_corner_outcomes
                ]
                p_lo = round(max(0.001, min(p_cond, min(conds))), 4)
                p_hi = round(min(0.999, max(p_cond, max(conds))), 4)
            else:
                half_band = min(0.22, unc_w * 0.45)
                p_lo = round(max(0.001, p_cond - half_band), 4)
                p_hi = round(min(0.999, p_cond + half_band), 4)
            fair = round(non_draw / p_win, 4) if p_win > 1e-6 else None
            odd = odds.get(sel_key)
            if odd is not None and odd > 1.0:
                be = round(1.0 / odd, 4)
                # Liquidación exacta DNB: +p_win*(odd-1) + p_draw*0 - p_loss*1 = p_win*odd + p_draw - 1
                ev_m = round(p_win * odd + p_d - 1.0, 4)
                if len(dnb_corner_outcomes) > 1:
                    corner_evs = [
                        (ch if sel_key == "dnb_home" else ca) * odd + cd - 1.0
                        for ch, cd, ca in dnb_corner_outcomes
                    ]
                    ev_lo = round(min(corner_evs), 4)
                else:
                    ev_lo = round(non_draw * (p_lo * odd - 1.0), 4)
            else:
                be = ev_m = ev_lo = None
            cls_label, dec_cat, pending, r_flags, cal_st = _classify_for_match(
                "DRAW_NO_BET",
                odd=odd,
                p_mod=p_cond,
                ev_m=ev_m,
                ev_lo=ev_lo,
                fair_odd=fair,
                sample_n=sample_ft,
                tactical_warnings=unique_flags,
            )
            out.append(
                EvaluatedMarketOption(
                    market_family="DRAW_NO_BET",
                    selection_key=sel_key,
                    line=None,
                    odd=odd,
                    break_even_prob=be,
                    p_model=p_cond,
                    p_low=p_lo,
                    p_high=p_hi,
                    ev_model=ev_m,
                    ev_conservative_low=ev_lo,
                    legacy_p6_hit_rate=None,
                    legacy_p6_n=0,
                    delta_vs_legacy_p6=None,
                    classification=cls_label,
                    tactical_warnings=unique_flags,
                    settlement_rule="DRAW_NO_BET_PUSH_REFUND_ON_DRAW",
                    decision_category=dec_cat,
                    fair_odd=fair,
                    p_win_unconditional=round(p_win, 4),
                    p_push_refund=round(p_d, 4),
                    pending_conditions=pending,
                    risk_flags=r_flags,
                    raw_sample_n=sample_ft,
                    effective_sample_n=eff_ft,
                    calibration_status=cal_st,
                )
            )

        # 3) Líneas Over/Under semienteras (.5) de Equipo, Total y 1ª Parte (estáticas + dinámicas del TXT)
        for fam, scope, metric, line, direction, sel_key, settlement in cls._build_ou_specs(odds):
            if fam not in model.families:
                continue
            fdist = model.families[fam]
            probs = fdist.prob_over(scope, line)
            if direction == "OVER":
                p_mod = probs["p_over"]
                p_lo = probs["p_over_low"]
                p_hi = probs["p_over_high"]
            else:
                p_mod = probs["p_under"]
                p_lo = probs["p_under_low"]
                p_hi = probs["p_under_high"]

            leg_over, leg_n = _legacy_p6_over_rate(
                bundle.home_matches,
                bundle.away_matches,
                metric=metric,
                scope=scope,
                line=line,
            )
            leg_rate = (
                leg_over
                if (leg_over is not None and direction == "OVER")
                else round(1.0 - leg_over, 4)
                if leg_over is not None
                else None
            )
            delta = round(p_mod - leg_rate, 4) if leg_rate is not None else None
            fair = round(1.0 / p_mod, 4) if p_mod > 1e-6 else None

            odd = odds.get(sel_key)
            if odd is not None and odd > 1.0:
                be = round(1.0 / odd, 4)
                ev_m = round(p_mod * odd - 1.0, 4)
                ev_lo = round(p_lo * odd - 1.0, 4)
            else:
                be = ev_m = ev_lo = None

            eff_n = (
                fdist.home_sample_n
                if scope == "home"
                else fdist.away_sample_n
                if scope == "away"
                else min(fdist.home_sample_n, fdist.away_sample_n)
            )
            eff_w_n = _effective_n_for_family(fdist, scope)
            cls_label, dec_cat, pending, r_flags, cal_st = _classify_for_match(
                fam,
                odd=odd,
                p_mod=p_mod,
                ev_m=ev_m,
                ev_lo=ev_lo,
                fair_odd=fair,
                sample_n=eff_n,
                tactical_warnings=unique_flags,
                leg_hit_rate=leg_rate,
                leg_n=leg_n,
                is_over_line=(direction == "OVER"),
            )

            out.append(
                EvaluatedMarketOption(
                    market_family=fam,
                    selection_key=sel_key,
                    line=line,
                    odd=odd,
                    break_even_prob=be,
                    p_model=p_mod,
                    p_low=p_lo,
                    p_high=p_hi,
                    ev_model=ev_m,
                    ev_conservative_low=ev_lo,
                    legacy_p6_hit_rate=leg_rate,
                    legacy_p6_n=leg_n,
                    delta_vs_legacy_p6=delta,
                    classification=cls_label,
                    tactical_warnings=unique_flags,
                    settlement_rule=settlement,
                    decision_category=dec_cat,
                    fair_odd=fair,
                    p_win_unconditional=p_mod,
                    p_push_refund=0.0,
                    pending_conditions=pending,
                    risk_flags=r_flags,
                    raw_sample_n=eff_n,
                    effective_sample_n=eff_w_n,
                    calibration_status=cal_st,
                )
            )

        # 4) Transformaciones matemáticas exactas sobre grillas bivariadas Dixon-Coles (FT y H1) si están cotizadas en odds_map
        def _eval_grid_predicate(
            grids: list[list[list[float]]],
            center: list[list[float]],
            pred: Any,
        ) -> tuple[float, float, float]:
            mg = len(center) - 1
            p_c = sum(center[i][j] for i in range(mg + 1) for j in range(mg + 1) if pred(i, j))
            p_c = max(0.0005, min(0.999, p_c))
            if len(grids) <= 1:
                return round(p_c, 4), round(p_c, 4), round(p_c, 4)
            vals = [sum(g[i][j] for i in range(mg + 1) for j in range(mg + 1) if pred(i, j)) for g in grids]
            p_l = max(0.0005, min(p_c, min(vals)))
            p_h = min(0.999, max(p_c, max(vals)))
            return round(p_c, 4), round(p_l, 4), round(p_h, 4)

        def _append_quoted_derived(
            *,
            fam: str,
            skey: str,
            p_mod: float,
            p_lo: float,
            p_hi: float,
            settlement: str,
            s_n: int,
            eff_w: float | None = None,
            push_p: float = 0.0,
            win_uncond: float | None = None,
            ev_lo_override: float | None = None,
        ) -> None:
            odd = odds.get(skey)
            if odd is None or odd <= 1.0:
                return
            be = round(1.0 / odd, 4)
            fair = round((1.0 - push_p) / win_uncond, 4) if (push_p > 0.0 and win_uncond and win_uncond > 1e-6) else (round(1.0 / p_mod, 4) if p_mod > 1e-6 else None)
            if push_p > 0.0 and win_uncond is not None:
                ev_m = round(win_uncond * odd + push_p - 1.0, 4)
                ev_lo = round(ev_lo_override, 4) if ev_lo_override is not None else round((1.0 - push_p) * (p_lo * odd - 1.0), 4)
            else:
                ev_m = round(p_mod * odd - 1.0, 4)
                ev_lo = round(p_lo * odd - 1.0, 4)
            cls_label, dec_cat, pending, r_flags, cal_st = _classify_for_match(
                fam,
                odd=odd,
                p_mod=p_mod,
                ev_m=ev_m,
                ev_lo=ev_lo,
                fair_odd=fair,
                sample_n=s_n,
                tactical_warnings=unique_flags,
            )
            out.append(
                EvaluatedMarketOption(
                    market_family=fam,
                    selection_key=skey,
                    line=None,
                    odd=odd,
                    break_even_prob=be,
                    p_model=p_mod,
                    p_low=p_lo,
                    p_high=p_hi,
                    ev_model=ev_m,
                    ev_conservative_low=ev_lo,
                    legacy_p6_hit_rate=None,
                    legacy_p6_n=0,
                    delta_vs_legacy_p6=None,
                    classification=cls_label,
                    tactical_warnings=unique_flags,
                    settlement_rule=settlement,
                    decision_category=dec_cat,
                    fair_odd=fair,
                    p_win_unconditional=round(win_uncond, 4) if win_uncond is not None else p_mod,
                    p_push_refund=round(push_p, 4),
                    pending_conditions=pending,
                    risk_flags=r_flags,
                    raw_sample_n=s_n,
                    effective_sample_n=eff_w if eff_w is not None else eff_ft,
                    calibration_status=cal_st,
                )
            )

        ft_center, ft_grids = model.joint_grid_with_corners("FT")
        if ft_center:
            mg_ft = len(ft_center) - 1
            # Hándicap Europeo 3-way FT
            for offset_tag, h_off in (("eh_m1", -1), ("eh_m2", -2), ("eh_p1", 1), ("eh_p2", 2)):
                for s_name, pred in (
                    ("home", lambda i, j, o=h_off: (i + o) > j),
                    ("draw", lambda i, j, o=h_off: (i + o) == j),
                    ("away", lambda i, j, o=h_off: (i + o) < j),
                ):
                    sk = f"{offset_tag}_{s_name}"
                    if sk in odds:
                        pm, pl, ph = _eval_grid_predicate(ft_grids, ft_center, pred)
                        _append_quoted_derived(
                            fam="EUROPEAN_HANDICAP_1X2",
                            skey=sk,
                            p_mod=pm,
                            p_lo=pl,
                            p_hi=ph,
                            settlement="THREE_WAY_EUROPEAN_HANDICAP_REGULATION",
                            s_n=sample_ft,
                        )

            # Resultado y BTTS / BTTS & Cualquier equipo gana / BTTS & 2.5 FT
            derived_ft_specs = (
                ("RESULT_AND_BTTS", "result_home_btts_yes", lambda i, j: i > j and i >= 1 and j >= 1, "BINARY_REGULATION_TIME"),
                ("RESULT_AND_BTTS", "result_away_btts_yes", lambda i, j: j > i and i >= 1 and j >= 1, "BINARY_REGULATION_TIME"),
                ("RESULT_AND_BTTS", "result_home_away_no", lambda i, j: i > j and j == 0, "BINARY_REGULATION_TIME"),
                ("RESULT_AND_BTTS", "result_away_home_no", lambda i, j: j > i and i == 0, "BINARY_REGULATION_TIME"),
                ("RESULT_AND_BTTS", "result_draw_btts_yes", lambda i, j: i == j and i >= 1 and j >= 1, "BINARY_REGULATION_TIME"),
                ("RESULT_AND_BTTS", "result_none_score", lambda i, j: i == 0 and j == 0, "BINARY_REGULATION_TIME"),
                ("BTTS_AND_EITHER_WIN", "btts_yes_and_either_win", lambda i, j: i >= 1 and j >= 1 and i != j, "BINARY_REGULATION_TIME"),
                ("BTTS_AND_EITHER_WIN", "btts_yes_and_either_win_no", lambda i, j: not (i >= 1 and j >= 1 and i != j), "BINARY_REGULATION_TIME"),
                ("BTTS_AND_TOTAL_2_5", "btts_yes_and_over_2_5", lambda i, j: i >= 1 and j >= 1 and (i + j) >= 3, "BINARY_REGULATION_TIME"),
                ("BTTS_AND_TOTAL_2_5", "btts_no_and_over_2_5", lambda i, j: (i == 0 or j == 0) and (i + j) >= 3, "BINARY_REGULATION_TIME"),
                ("BTTS_AND_TOTAL_2_5", "btts_yes_and_under_2_5", lambda i, j: i >= 1 and j >= 1 and (i + j) <= 2, "BINARY_REGULATION_TIME"),
                ("BTTS_AND_TOTAL_2_5", "btts_no_and_under_2_5", lambda i, j: (i == 0 or j == 0) and (i + j) <= 2, "BINARY_REGULATION_TIME"),
            )
            for fam, sk, pred, stl in derived_ft_specs:
                if sk in odds:
                    pm, pl, ph = _eval_grid_predicate(ft_grids, ft_center, pred)
                    _append_quoted_derived(fam=fam, skey=sk, p_mod=pm, p_lo=pl, p_hi=ph, settlement=stl, s_n=sample_ft)

            # Marcador exacto FT (cualquier i-j dentro de 0..mg_ft cotizado en Bwin)
            for sk, odd_val in sorted(odds.items()):
                if not sk.startswith("exact_score_"):
                    continue
                parts = sk.split("_")
                if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                    gi, gj = int(parts[2]), int(parts[3])
                    if 0 <= gi <= mg_ft and 0 <= gj <= mg_ft:
                        pm, pl, ph = _eval_grid_predicate(ft_grids, ft_center, lambda i, j, a=gi, b=gj: i == a and j == b)
                        _append_quoted_derived(
                            fam="EXACT_SCORE_FT",
                            skey=sk,
                            p_mod=pm,
                            p_lo=pl,
                            p_hi=ph,
                            settlement="EXACT_SCORE_REGULATION_TIME",
                            s_n=sample_ft,
                        )

        # 5) Mercados bivariados 1ª parte (GOALS_H1) cuando cotizados en odds_map
        h1_fam = model.families.get("GOALS_H1")
        h1_center, h1_grids = model.joint_grid_with_corners("H1")
        if h1_fam is not None and h1_center:
            sample_h1 = min(h1_fam.home_sample_n, h1_fam.away_sample_n)
            eff_h1 = _effective_n_for_family(h1_fam, "total")
            mg_h1 = len(h1_center) - 1
            h1_specs = (
                ("MATCH_RESULT_1X2_H1", "h1_home_win", lambda i, j: i > j, "THREE_WAY_FIRST_HALF"),
                ("MATCH_RESULT_1X2_H1", "h1_draw", lambda i, j: i == j, "THREE_WAY_FIRST_HALF"),
                ("MATCH_RESULT_1X2_H1", "h1_away_win", lambda i, j: i < j, "THREE_WAY_FIRST_HALF"),
                ("DOUBLE_CHANCE_H1", "h1_double_chance_1x", lambda i, j: i >= j, "DOUBLE_CHANCE_FIRST_HALF"),
                ("DOUBLE_CHANCE_H1", "h1_double_chance_x2", lambda i, j: i <= j, "DOUBLE_CHANCE_FIRST_HALF"),
                ("DOUBLE_CHANCE_H1", "h1_double_chance_12", lambda i, j: i != j, "DOUBLE_CHANCE_FIRST_HALF"),
                ("BTTS_H1", "h1_btts_yes", lambda i, j: i >= 1 and j >= 1, "BTTS_FIRST_HALF"),
                ("BTTS_H1", "h1_btts_no", lambda i, j: i == 0 or j == 0, "BTTS_FIRST_HALF"),
            )
            for fam, sk, pred, stl in h1_specs:
                if sk in odds:
                    pm, pl, ph = _eval_grid_predicate(h1_grids, h1_center, pred)
                    _append_quoted_derived(fam=fam, skey=sk, p_mod=pm, p_lo=pl, p_hi=ph, settlement=stl, s_n=sample_h1, eff_w=eff_h1)

            # Ganador sin empate 1ª parte (con devolución explícita por empate al descanso)
            if "h1_dnb_home" in odds or "h1_dnb_away" in odds:
                def _h1_1x2(g: list[list[float]]) -> tuple[float, float, float]:
                    m_i = len(g) - 1
                    hw = sum(g[i][j] for i in range(m_i + 1) for j in range(m_i + 1) if i > j)
                    dr = sum(g[i][i] for i in range(m_i + 1))
                    aw = sum(g[i][j] for i in range(m_i + 1) for j in range(m_i + 1) if i < j)
                    return hw, dr, aw

                c_hw, c_d, c_aw = _h1_1x2(h1_center)
                c_outcomes = [_h1_1x2(g) for g in h1_grids]
                nd1 = max(1e-6, 1.0 - c_d)
                for sk, pw in (("h1_dnb_home", c_hw), ("h1_dnb_away", c_aw)):
                    if sk not in odds:
                        continue
                    odd_v = odds[sk]
                    pc = round(max(0.001, min(0.999, pw / nd1)), 4)
                    conds = [(ch if sk == "h1_dnb_home" else ca) / max(1e-6, 1.0 - cd) for ch, cd, ca in c_outcomes]
                    pl = round(max(0.001, min(pc, min(conds))), 4)
                    ph = round(min(0.999, max(pc, max(conds))), 4)
                    ev_lo_c = min((ch if sk == "h1_dnb_home" else ca) * odd_v + cd - 1.0 for ch, cd, ca in c_outcomes)
                    _append_quoted_derived(
                        fam="DRAW_NO_BET_H1",
                        skey=sk,
                        p_mod=pc,
                        p_lo=pl,
                        p_hi=ph,
                        settlement="DRAW_NO_BET_FIRST_HALF_PUSH_REFUND_ON_DRAW",
                        s_n=sample_h1,
                        eff_w=eff_h1,
                        push_p=c_d,
                        win_uncond=pw,
                        ev_lo_override=ev_lo_c,
                    )

            # Marcador exacto 1ª parte
            for sk, odd_val in sorted(odds.items()):
                if not sk.startswith("h1_exact_score_"):
                    continue
                parts = sk.split("_")
                if len(parts) == 5 and parts[3].isdigit() and parts[4].isdigit():
                    gi, gj = int(parts[3]), int(parts[4])
                    if 0 <= gi <= mg_h1 and 0 <= gj <= mg_h1:
                        pm, pl, ph = _eval_grid_predicate(h1_grids, h1_center, lambda i, j, a=gi, b=gj: i == a and j == b)
                        _append_quoted_derived(
                            fam="EXACT_SCORE_H1",
                            skey=sk,
                            p_mod=pm,
                            p_lo=pl,
                            p_hi=ph,
                            settlement="EXACT_SCORE_FIRST_HALF",
                            s_n=sample_h1,
                            eff_w=eff_h1,
                        )

        # 5b) Trazabilidad explícita de selecciones parseadas en odds_map sin modelo válido (sin silencios)
        evaluated_keys = {o.selection_key for o in out}
        for sel_key, raw_odd in sorted(odds.items()):
            if sel_key in evaluated_keys:
                continue
            if raw_odd is None or raw_odd <= 1.0:
                continue
            unm_fam, unm_reason, unm_settlement = _classify_unmodelled_parsed_selection(sel_key)
            out.append(
                EvaluatedMarketOption(
                    market_family=unm_fam,
                    selection_key=sel_key,
                    line=None,
                    odd=float(raw_odd),
                    break_even_prob=round(1.0 / float(raw_odd), 4),
                    p_model=None,
                    p_low=None,
                    p_high=None,
                    ev_model=None,
                    ev_conservative_low=None,
                    legacy_p6_hit_rate=None,
                    legacy_p6_n=0,
                    delta_vs_legacy_p6=None,
                    classification="MERCADO_NO_MODELADO",
                    tactical_warnings=unique_flags,
                    settlement_rule=unm_settlement,
                    decision_category="MERCADO_NO_MODELADO",
                    fair_odd=None,
                    p_win_unconditional=None,
                    p_push_refund=None,
                    pending_conditions=(unm_reason,),
                    risk_flags=("SELECCION_NO_MODELADA",),
                    raw_sample_n=None,
                    effective_sample_n=None,
                    calibration_status="NOT_APPLICABLE_UNMODELLED",
                )
            )

        # 6) Auditoría de equivalencias contractuales en el mismo snapshot Bwin
        eq_audit = audit_snapshot_equivalences(odds)
        sub_map = eq_audit.get("suboptimal_selections") or {}
        annotated: list[EvaluatedMarketOption] = []
        for opt in out:
            contract = canonicalize_selection_key(opt.selection_key)
            cid = contract.contract_id if contract is not None else None
            sub = sub_map.get(opt.selection_key)
            if sub:
                extra_pending = tuple(
                    dict.fromkeys(
                        (
                            *opt.pending_conditions,
                            f"CONTRATO_EQUIVALENTE_MEJOR_CUOTA({sub['better_selection_key']}@{sub['better_odd']:.2f})",
                        )
                    )
                )
                extra_risk = tuple(dict.fromkeys((*opt.risk_flags, "PRECIO_INFERIOR_A_EQUIVALENTE_EN_SNAPSHOT")))
                annotated.append(
                    replace(
                        opt,
                        canonical_contract_id=cid,
                        equivalent_best_selection=sub["better_selection_key"],
                        equivalent_best_odd=sub["better_odd"],
                        pending_conditions=extra_pending,
                        risk_flags=extra_risk,
                    )
                )
            elif cid is not None:
                annotated.append(replace(opt, canonical_contract_id=cid))
            else:
                annotated.append(opt)
        return annotated
