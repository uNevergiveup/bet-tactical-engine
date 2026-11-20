"""Plugins tácticos verificados para PatternRegistry (Etapa 3).

Integra capacidades auditadas de v1044 (SoS), v1046 (rojas tempranas, H1/H2, eficacia xG),
v1050 (arquetipos y duelos), cronología marcador/minuto y contexto arbitral/calendario.
"""

from __future__ import annotations

import statistics
from typing import Any

from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    _canonical_coach_list,
)
from bet_engine.contracts import (
    PatternScope,
    PrematchAvailability,
    StatisticalPattern,
    TacticalHypothesis,
)

from .registry import PluginOutput, reconstruct_score_state_timeline


def _select_controlled_segment(
    matches: tuple[HistoricalTeamMatch, ...],
    bundle: MatchObservationBundle,
    *,
    metric: str,
    min_exact: int = 4,
) -> tuple[list[HistoricalTeamMatch], str, list[str]]:
    """Prioriza competición exacta y régimen de entrenador actual sin mezclar a ciegas."""
    warnings: list[str] = []
    exact = [
        m
        for m in matches
        if m.league_id == bundle.league_id
        and m.season == bundle.season
        and m.own_metrics.get(metric) is not None
    ]
    all_valid = [m for m in matches if m.own_metrics.get(metric) is not None]
    if len(exact) >= min_exact:
        selected = exact
        seg_label = "exact_competition"
    else:
        selected = all_valid
        seg_label = "all_competitions_mixed"
        if any(m.league_id != bundle.league_id or m.season != bundle.season for m in selected):
            warnings.append("COMPETITION_MIX_HETEROGENEOUS")

    _, real_shift = _canonical_coach_list(selected)
    if real_shift:
        warnings.append("COACH_REGIME_MIXED")
    return selected, seg_label, warnings


class MultiWindowMatchupPlugin:
    """Compara L5/L10/L20, localía equivalente y cruce producción vs concesión rival."""

    plugin_id = "multi_window_matchup"
    domain_family = "MATCHUP_MULTIWINDOW"

    METRICS = ("goals", "corners", "shots", "on_target", "fouls", "yellow")

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        patterns: list[StatisticalPattern] = []
        skipped: list[str] = []

        for metric in self.METRICS:
            h_matches, h_seg, h_warn = _select_controlled_segment(
                bundle.home_matches, bundle, metric=metric, min_exact=4
            )
            a_matches, a_seg, a_warn = _select_controlled_segment(
                bundle.away_matches, bundle, metric=metric, min_exact=4
            )
            if len(h_matches) < 4 or len(a_matches) < 4:
                skipped.append(f"{metric}:INSUFFICIENT_SAMPLE(h={len(h_matches)},a={len(a_matches)})")
                continue

            h_own_vals = [float(m.own_metrics[metric]) for m in h_matches if m.own_metrics.get(metric) is not None]
            a_con_vals = [float(m.conceded_metrics[metric]) for m in a_matches if m.conceded_metrics.get(metric) is not None]
            if len(h_own_vals) < 4 or len(a_con_vals) < 4:
                continue

            h_mean = statistics.fmean(h_own_vals)
            a_con_mean = statistics.fmean(a_con_vals)
            h_l5 = statistics.fmean(h_own_vals[:5]) if len(h_own_vals) >= 5 else h_mean
            h_prev5 = statistics.fmean(h_own_vals[5:10]) if len(h_own_vals) >= 10 else None

            flags = list(dict.fromkeys(h_warn + a_warn))
            if abs(h_mean - a_con_mean) > max(1.0, 0.35 * max(h_mean, a_con_mean, 1.0)):
                flags.append("OWN_VS_RIVAL_CONCESSION_DIVERGENCE")
            if h_prev5 is not None and abs(h_l5 - h_prev5) >= max(0.8, 0.25 * max(h_prev5, 1.0)):
                flags.append("RECENT_L5_VS_PREV5_SHIFT")

            # Segmento estricto localía (home en casa vs away fuera) si n >= 3
            h_home_vals = [
                float(m.own_metrics[metric])
                for m in h_matches
                if m.venue == "HOME" and m.own_metrics.get(metric) is not None
            ]
            a_away_con_vals = [
                float(m.conceded_metrics[metric])
                for m in a_matches
                if m.venue == "AWAY" and m.conceded_metrics.get(metric) is not None
            ]

            exact_h = sum(1 for m in h_matches if m.league_id == bundle.league_id and m.season == bundle.season)
            mix = {
                "home_exact_competition_pct": round((exact_h / len(h_matches)) * 100.0, 2),
                "home_segment": h_seg,
                "away_segment": a_seg,
            }

            total_n = len(h_matches) + len(a_matches)
            patterns.append(
                StatisticalPattern(
                    pattern_id=f"pat_matchup_{bundle.fixture_id}_{metric}",
                    pattern_scope=PatternScope.DESCRIPTIVE_SPLIT,
                    target_metric=f"match.{metric}",
                    condition_dsl={
                        "op": "all",
                        "clauses": [
                            {"field": "context.league_id", "cmp": "eq", "value": bundle.league_id},
                        ],
                    },
                    sample_total=len(bundle.home_matches) + len(bundle.away_matches),
                    sample_valid=total_n,
                    coverage_observed_pct=round(
                        (total_n / max(1, len(bundle.home_matches) + len(bundle.away_matches))) * 100.0, 2
                    ),
                    estimates={
                        "home_own_mean": round(h_mean, 4),
                        "home_own_l5_mean": round(h_l5, 4),
                        "home_own_prev5_mean": round(h_prev5, 4) if h_prev5 is not None else None,
                        "away_conceded_mean": round(a_con_mean, 4),
                        "home_venue_own_mean": round(statistics.fmean(h_home_vals), 4) if len(h_home_vals) >= 3 else None,
                        "away_venue_conceded_mean": (
                            round(statistics.fmean(a_away_con_vals), 4) if len(a_away_con_vals) >= 3 else None
                        ),
                    },
                    supporting_fixture_ids=tuple(m.fixture_id for m in h_matches[:5]),
                    contradiction_flags=tuple(flags),
                    competition_mix=mix,
                    exploratory_only=True,
                )
            )

        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=tuple(patterns),
            skipped_reasons=tuple(skipped),
            telemetry={"evaluated_metrics": len(patterns), "skipped": len(skipped)},
        )


class SoSAdjustedPlugin:
    """Integra Strength of Schedule con contracción bayesiana (lógica validada de v1044).

    Evita mezclar ligas diferentes al calcular la línea base de referencia y exige que toda
    referencia del rival sea anterior al partido histórico evaluado (as-of rule).
    """

    plugin_id = "sos_adjusted_v1044"
    domain_family = "STRENGTH_OF_SCHEDULE"
    SHRINK_K = 6.0
    FACTOR_RANGE = (0.75, 1.25)

    def __init__(self, external_reference_pool: tuple[HistoricalTeamMatch, ...] = ()) -> None:
        self.external_reference_pool = external_reference_pool

    @staticmethod
    def build_pool_from_bundles(bundles: list[MatchObservationBundle]) -> tuple[HistoricalTeamMatch, ...]:
        """Construye un pool compartido de partidos históricos deduplicados desde varios bundles."""
        pool: list[HistoricalTeamMatch] = []
        seen: set[tuple[int, int]] = set()
        for b in bundles:
            for m in tuple(b.home_matches) + tuple(b.away_matches):
                key = (m.fixture_id, m.team_id)
                if key not in seen:
                    seen.add(key)
                    pool.append(m)
        return tuple(pool)

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        from bet_engine.contracts.epistemic import parse_utc_timestamp

        patterns: list[StatisticalPattern] = []
        skipped: list[str] = []

        cut_dt = parse_utc_timestamp(bundle.cutoff_utc)
        # Construimos pool deduplicado por (fixture_id, team_id) de la misma liga/temporada y <= cutoff_utc
        pool_by_team_fix: dict[tuple[int, int], HistoricalTeamMatch] = {}
        all_candidates = (
            tuple(bundle.home_matches)
            + tuple(bundle.away_matches)
            + tuple(self.external_reference_pool)
        )
        for m in all_candidates:
            if (
                m.league_id == bundle.league_id
                and m.season == bundle.season
                and m.fixture_id != bundle.fixture_id
                and parse_utc_timestamp(m.available_at_utc) <= cut_dt
            ):
                pool_by_team_fix[(m.fixture_id, m.team_id)] = m

        exact_list = sorted(pool_by_team_fix.values(), key=lambda x: x.kickoff_utc)
        unique_fixtures = len({m.fixture_id for m in exact_list})
        if unique_fixtures < 6:
            skipped.append(f"SOS_REFERENCE_POOL_THIN(unique_fixtures={unique_fixtures}<6)")
            return PluginOutput(
                plugin_id=self.plugin_id,
                domain_family=self.domain_family,
                skipped_reasons=tuple(skipped),
                telemetry={"pool_size": unique_fixtures, "team_rows": len(exact_list)},
            )

        for metric in ("goals", "corners", "shots"):
            base_vals = [
                float(m.conceded_metrics[metric])
                for m in exact_list
                if m.conceded_metrics.get(metric) is not None
            ]
            if len(base_vals) < 6 or statistics.fmean(base_vals) <= 0:
                skipped.append(f"{metric}:SOS_BASELINE_UNAVAILABLE(n={len(base_vals)})")
                continue
            baseline = statistics.fmean(base_vals)

            for side, side_matches in (("home", bundle.home_matches), ("away", bundle.away_matches)):
                same_comp = [
                    m
                    for m in side_matches
                    if m.league_id == bundle.league_id
                    and m.season == bundle.season
                    and m.own_metrics.get(metric) is not None
                ]
                if len(same_comp) < 4:
                    skipped.append(f"{side}.{metric}:INSUFFICIENT_SAME_COMP_SAMPLE(n={len(same_comp)})")
                    continue
                raw_mean = statistics.fmean(float(m.own_metrics[metric]) for m in same_comp)
                opp_adj_weights: list[float] = []
                for m in same_comp:
                    m_ko = parse_utc_timestamp(m.kickoff_utc)
                    # Regla as-of: solo partidos del rival disponibles ANTES del kickoff de m
                    opp_con_vals = [
                        float(p.conceded_metrics[metric])
                        for p in exact_list
                        if p.team_id == m.opponent_id
                        and p.fixture_id != m.fixture_id
                        and parse_utc_timestamp(p.available_at_utc) <= m_ko
                        and p.conceded_metrics.get(metric) is not None
                    ]
                    if opp_con_vals:
                        raw_ratio = statistics.fmean(opp_con_vals) / baseline
                        w = len(opp_con_vals) / (len(opp_con_vals) + self.SHRINK_K)
                        factor = max(self.FACTOR_RANGE[0], min(self.FACTOR_RANGE[1], 1.0 + w * (raw_ratio - 1.0)))
                        opp_adj_weights.append(factor)

                if not opp_adj_weights:
                    skipped.append(f"{side}.{metric}:NO_PRIOR_OPPONENT_REFERENCE_IN_POOL")
                    continue

                avg_opp_generosity = statistics.fmean(opp_adj_weights)
                sos_adjusted_mean = raw_mean / avg_opp_generosity

                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_sos_{bundle.fixture_id}_{side}_{metric}",
                        pattern_scope=PatternScope.DESCRIPTIVE_SPLIT,
                        target_metric=f"{side}.own.{metric}",
                        condition_dsl={
                            "op": "all",
                            "clauses": [
                                {"field": "context.league_id", "cmp": "eq", "value": bundle.league_id},
                                {"field": "context.season", "cmp": "eq", "value": bundle.season},
                            ],
                        },
                        sample_total=len(side_matches),
                        sample_valid=len(same_comp),
                        coverage_observed_pct=round((len(same_comp) / max(1, len(side_matches))) * 100.0, 2),
                        estimates={
                            "raw_mean": round(raw_mean, 4),
                            "sos_adjusted_mean": round(sos_adjusted_mean, 4),
                            "opponent_generosity_factor": round(avg_opp_generosity, 4),
                            "pool_baseline_mean": round(baseline, 4),
                            "matched_opponents_n": len(opp_adj_weights),
                            "unique_pool_fixtures": unique_fixtures,
                        },
                        contradiction_flags=(
                            ("SOS_DIFFERS_GT_10PCT",)
                            if abs(sos_adjusted_mean - raw_mean) > 0.10 * max(raw_mean, 0.5)
                            else ()
                        ),
                        competition_mix={"exact_competition_pct": 100.0},
                        exploratory_only=True,
                    )
                )

        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=tuple(patterns),
            skipped_reasons=tuple(skipped),
            telemetry={"pool_size": unique_fixtures, "team_rows": len(exact_list), "patterns": len(patterns)},
        )


class RedCardAndHalfDynamicsPlugin:
    """Integra lógica verificada de v1046 sin ligas hardcodeadas:
    - Impacto de tarjetas rojas tempranas (<60') en estadísticas históricas.
    - Dinámica H1 vs H2 en goles.
    - Eficacia de finalización (goals / xG) manteniendo null != 0.
    """

    plugin_id = "red_card_and_halves_v1046"
    domain_family = "HALF_DYNAMICS_AND_RED_CARDS"

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        patterns: list[StatisticalPattern] = []
        skipped: list[str] = []

        for side, matches in (("home", bundle.home_matches), ("away", bundle.away_matches)):
            if len(matches) < 4:
                skipped.append(f"{side}:INSUFFICIENT_SAMPLE(n={len(matches)})")
                continue

            # 1) Distorsión por tarjeta roja
            red_games = [m for m in matches if m.red_card_distortion]
            clean_games = [m for m in matches if not m.red_card_distortion]
            for metric in ("goals", "corners", "shots"):
                all_vals = [float(m.own_metrics[metric]) for m in matches if m.own_metrics.get(metric) is not None]
                clean_vals = [float(m.own_metrics[metric]) for m in clean_games if m.own_metrics.get(metric) is not None]
                if red_games and len(clean_vals) >= 3 and len(all_vals) >= 4:
                    raw_m = statistics.fmean(all_vals)
                    clean_m = statistics.fmean(clean_vals)
                    patterns.append(
                        StatisticalPattern(
                            pattern_id=f"pat_red_clean_{bundle.fixture_id}_{side}_{metric}",
                            pattern_scope=PatternScope.DESCRIPTIVE_SPLIT,
                            target_metric=f"{side}.own.{metric}",
                            condition_dsl={
                                "op": "all",
                                "clauses": [
                                    {"field": "score_state.red_card_distortion", "cmp": "eq", "value": False}
                                ],
                            },
                            sample_total=len(matches),
                            sample_valid=len(clean_vals),
                            coverage_observed_pct=round((len(clean_vals) / len(matches)) * 100.0, 2),
                            estimates={
                                "raw_mean": round(raw_m, 4),
                                "clean_no_red_mean": round(clean_m, 4),
                                "red_distorted_games_n": len(red_games),
                            },
                            contradiction_flags=(
                                ("RED_CARD_DISTORTION_IMPACT",)
                                if abs(clean_m - raw_m) >= 0.12 * max(raw_m, 0.5)
                                else ()
                            ),
                            exploratory_only=True,
                        )
                    )

            # 2) H1 vs H2 goles (sin mezclar ligas si exact >= 4)
            selected, _, warn = _select_controlled_segment(matches, bundle, metric="goals_h1", min_exact=4)
            h1_vals = [float(m.own_metrics["goals_h1"]) for m in selected if m.own_metrics.get("goals_h1") is not None]
            h2_vals = [float(m.own_metrics["goals_h2"]) for m in selected if m.own_metrics.get("goals_h2") is not None]
            if len(h1_vals) >= 4 and len(h2_vals) >= 4:
                h1_m = statistics.fmean(h1_vals)
                h2_m = statistics.fmean(h2_vals)
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_h1_h2_{bundle.fixture_id}_{side}_goals",
                        pattern_scope=PatternScope.DESCRIPTIVE_SPLIT,
                        target_metric=f"{side}.own.goals_h1_vs_h2",
                        condition_dsl=None,
                        sample_total=len(matches),
                        sample_valid=len(h1_vals),
                        coverage_observed_pct=round((len(h1_vals) / len(matches)) * 100.0, 2),
                        estimates={
                            "h1_goals_mean": round(h1_m, 4),
                            "h2_goals_mean": round(h2_m, 4),
                            "h2_minus_h1": round(h2_m - h1_m, 4),
                        },
                        contradiction_flags=tuple(warn),
                        exploratory_only=True,
                    )
                )

            # 3) Eficacia goals / xG (lógica exacta de v1046.finishing)
            gx_pairs = [
                (float(m.own_metrics["goals"]), float(m.own_metrics["xg"]))
                for m in matches
                if m.own_metrics.get("goals") is not None
                and m.own_metrics.get("xg") is not None
                and float(m.own_metrics["xg"]) > 0
            ]
            if len(gx_pairs) >= 4:
                tot_g = sum(g for g, _ in gx_pairs)
                tot_x = sum(x for _, x in gx_pairs)
                ratio = tot_g / tot_x
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_finishing_{bundle.fixture_id}_{side}",
                        pattern_scope=PatternScope.DESCRIPTIVE_SPLIT,
                        target_metric=f"{side}.own.goals_per_xg",
                        condition_dsl=None,
                        sample_total=len(matches),
                        sample_valid=len(gx_pairs),
                        coverage_observed_pct=round((len(gx_pairs) / len(matches)) * 100.0, 2),
                        estimates={
                            "goals_total": round(tot_g, 2),
                            "xg_total": round(tot_x, 3),
                            "goals_per_xg": round(ratio, 4),
                        },
                        contradiction_flags=(
                            ("OVERPERFORMING_XG",) if ratio >= 1.30 else ("UNDERPERFORMING_XG",) if ratio <= 0.72 else ()
                        ),
                        exploratory_only=True,
                    )
                )

        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=tuple(patterns),
            skipped_reasons=tuple(skipped),
            telemetry={"patterns": len(patterns)},
        )


class TacticalArchetypeDuelPlugin:
    """Clasifica arquetipos tácticos observables (v1050) y formula mecanismos de choque de estilos."""

    plugin_id = "tactical_archetypes_v1050"
    domain_family = "TACTICAL_ARCHETYPES"

    @staticmethod
    def _classify_team_archetype(matches: tuple[HistoricalTeamMatch, ...]) -> tuple[str, dict[str, Any]]:
        clean = [m for m in matches if not m.red_card_distortion]
        poss = [float(m.own_metrics["possession"]) for m in clean if m.own_metrics.get("possession") is not None]
        shots = [float(m.own_metrics["shots"]) for m in clean if m.own_metrics.get("shots") is not None]
        on_t = [float(m.own_metrics["on_target"]) for m in clean if m.own_metrics.get("on_target") is not None]
        corners = [float(m.own_metrics["corners"]) for m in clean if m.own_metrics.get("corners") is not None]

        if min(len(poss), len(shots), len(corners)) < 4:
            return "UNKNOWN_INSUFFICIENT_COVERAGE", {"valid_n": min(len(poss), len(shots), len(corners))}

        p_m = statistics.fmean(poss)
        s_m = statistics.fmean(shots)
        ot_m = statistics.fmean(on_t) if on_t else 0.0
        c_m = statistics.fmean(corners)

        if p_m >= 55.0 and s_m >= 13.0 and c_m >= 5.5:
            arch = "ASEDIO_TERRITORIAL"
        elif p_m >= 55.0 and s_m < 11.0:
            arch = "DOMINADOR_ESTERIL"
        elif p_m <= 45.0 and ot_m >= 4.0:
            arch = "TRANSICION_VERTICAL"
        elif p_m <= 45.0 and s_m < 10.5:
            arch = "BLOQUE_BAJO_REACTIVO"
        else:
            arch = "EQUILIBRADO_MIXTO"

        return arch, {
            "valid_n": min(len(poss), len(shots), len(corners)),
            "possession_mean": round(p_m, 2),
            "shots_mean": round(s_m, 2),
            "on_target_mean": round(ot_m, 2),
            "corners_mean": round(c_m, 2),
        }

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        h_arch, h_stats = self._classify_team_archetype(bundle.home_matches)
        a_arch, a_stats = self._classify_team_archetype(bundle.away_matches)

        if "UNKNOWN" in (h_arch, a_arch):
            return PluginOutput(
                plugin_id=self.plugin_id,
                domain_family=self.domain_family,
                skipped_reasons=(f"ARCHETYPE_COVERAGE_INSUFFICIENT(home={h_arch},away={a_arch})",),
                telemetry={"home_archetype": h_arch, "away_archetype": a_arch},
            )

        hypotheses: list[TacticalHypothesis] = []
        patterns: list[StatisticalPattern] = [
            StatisticalPattern(
                pattern_id=f"pat_archetype_{bundle.fixture_id}",
                pattern_scope=PatternScope.INTRA_MATCH_MECHANISM,
                target_metric="match.tactical_clash",
                condition_dsl=None,
                sample_total=len(bundle.home_matches) + len(bundle.away_matches),
                sample_valid=h_stats["valid_n"] + a_stats["valid_n"],
                coverage_observed_pct=100.0,
                estimates={
                    "home_archetype": h_arch,
                    "away_archetype": a_arch,
                    "home_profile": h_stats,
                    "away_profile": a_stats,
                },
                exploratory_only=True,
            )
        ]

        if h_arch == "ASEDIO_TERRITORIAL" and a_arch in ("BLOQUE_BAJO_REACTIVO", "TRANSICION_VERTICAL"):
            hypotheses.append(
                TacticalHypothesis(
                    hypothesis_id=f"hyp_clash_{bundle.fixture_id}_siege_vs_low_block",
                    origin="PYTHON_TACTICAL_TEMPLATE",
                    prematch_availability=PrematchAvailability.PREMATCH_OBSERVABLE,
                    causal_chain={
                        "local_plan": "Asedio territorial sostenido con alto volumen de remates y presión en campo rival",
                        "visitor_response": f"Estructura {a_arch} protegiendo área y concediendo bandas/rechaces",
                        "counter_adjustment": "Acumulación de tiros bloqueados y saques de esquina locales mientras el marcador no se rompa",
                        "measurable_event": "lag.own.shots_l5 >= 13.0 y own.corners >= 5.5",
                        "refutation_scenario": "Gol temprano visitante que cierre aún más el partido o gol rápido local que reduzca urgencia",
                    },
                    target_market_families=("TEAM_CORNERS", "TOTAL_CORNERS", "TEAM_SHOTS"),
                    python_test={
                        "op": "all",
                        "clauses": [
                            {"field": "lag.own.shots_l5", "cmp": "ge", "value": 12.0},
                            {"field": "own.corners", "cmp": "ge", "value": 5.0},
                        ],
                    },
                    validation_state="PENDING_PYTHON_EVALUATION",
                )
            )

        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=tuple(patterns),
            hypotheses=tuple(hypotheses),
            telemetry={"home_archetype": h_arch, "away_archetype": a_arch},
        )


class ScoreStateTimelinePlugin:
    """Evalúa patrones condicionados por marcador y minuto SOLO con cronología verificada.

    Registra explícitamente si falta cronología de córners (caso estándar API-Football)
    y nunca reconstruye eventos desconocidos.
    """

    plugin_id = "score_state_timeline"
    domain_family = "SCORELINE_AND_MINUTES"

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        patterns: list[StatisticalPattern] = []
        skipped: list[str] = []

        for side, matches in (("home", bundle.home_matches), ("away", bundle.away_matches)):
            timelines = [(m, reconstruct_score_state_timeline(m)) for m in matches]
            with_chrono = [(m, t) for m, t in timelines if t["chronology_available"]]
            if len(with_chrono) < 3:
                skipped.append(f"{side}:NO_VERIFIED_EVENT_CHRONOLOGY(n={len(with_chrono)})")
                continue

            # 1) Tasas verificadas de goles y tarjetas por estado del marcador (minuto a minuto)
            mins_by_st = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}
            goals_by_st = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}
            cards_by_st = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}
            corner_events_present = all(t["corner_timeline_available"] for _, t in with_chrono)
            corners_by_st = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}

            trailing_heavy_corners: list[float] = []
            non_trailing_corners: list[float] = []

            for m, tl in with_chrono:
                for st in ("LEADING", "DRAWING", "TRAILING"):
                    mins_by_st[st] += int(tl["minutes_leading" if st == "LEADING" else "minutes_drawing" if st == "DRAWING" else "minutes_trailing"] or 0)
                    goals_by_st[st] += int((tl["goals_by_state"] or {}).get(st, 0))
                    cards_by_st[st] += int((tl["cards_by_state"] or {}).get(st, 0))
                    if corner_events_present and tl["corners_by_state"]:
                        corners_by_st[st] += int(tl["corners_by_state"].get(st, 0))

                c_val = m.own_metrics.get("corners")
                m_trail = int(tl["minutes_trailing"] or 0)
                if c_val is not None:
                    if m_trail >= 25:
                        trailing_heavy_corners.append(float(c_val))
                    elif m_trail == 0:
                        non_trailing_corners.append(float(c_val))

            goals_per_90 = {
                st: round((goals_by_st[st] / mins_by_st[st]) * 90.0, 3) if mins_by_st[st] >= 30 else None
                for st in ("LEADING", "DRAWING", "TRAILING")
            }
            cards_per_90 = {
                st: round((cards_by_st[st] / mins_by_st[st]) * 90.0, 3) if mins_by_st[st] >= 30 else None
                for st in ("LEADING", "DRAWING", "TRAILING")
            }

            if corner_events_present:
                corners_per_90 = {
                    st: round((corners_by_st[st] / mins_by_st[st]) * 90.0, 3) if mins_by_st[st] >= 30 else None
                    for st in ("LEADING", "DRAWING", "TRAILING")
                }
                corner_mode = "EXACT_MINUTE_CORNER_TIMELINE"
            else:
                corners_per_90 = None
                corner_mode = "CORNER_TIMELINE_ABSENT_MATCH_PROXY_ONLY"

            patterns.append(
                StatisticalPattern(
                    pattern_id=f"pat_score_state_{bundle.fixture_id}_{side}",
                    pattern_scope=PatternScope.INTRA_MATCH_MECHANISM,
                    target_metric=f"{side}.score_state_dynamics",
                    condition_dsl={
                        "op": "all",
                        "clauses": [{"field": "score_state.minutes_trailing", "cmp": "ge", "value": 25}],
                    },
                    sample_total=len(matches),
                    sample_valid=len(with_chrono),
                    coverage_observed_pct=round((len(with_chrono) / max(1, len(matches))) * 100.0, 2),
                    estimates={
                        "minutes_by_state": mins_by_st,
                        "goals_per_90_by_state": goals_per_90,
                        "cards_per_90_by_state": cards_per_90,
                        "corner_chronology_mode": corner_mode,
                        "corners_per_90_by_state": corners_per_90,
                        "proxy_ft_corners_when_trailing_ge_25m": (
                            round(statistics.fmean(trailing_heavy_corners), 3) if trailing_heavy_corners else None
                        ),
                        "proxy_ft_corners_when_trailing_0m": (
                            round(statistics.fmean(non_trailing_corners), 3) if non_trailing_corners else None
                        ),
                        "trailing_heavy_n": len(trailing_heavy_corners),
                        "non_trailing_n": len(non_trailing_corners),
                    },
                    contradiction_flags=(
                        ("CORNER_TIMELINE_ABSENT_NO_RECONSTRUCTION",) if not corner_events_present else ()
                    ),
                    exploratory_only=True,
                )
            )

        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=tuple(patterns),
            skipped_reasons=tuple(skipped),
            telemetry={"evaluated_sides": len(patterns), "skipped": skipped},
        )


class RefereeAndScheduleContextPlugin:
    """Conecta perfil arbitral, descanso multicompetición, lookahead y cambios de entrenador."""

    plugin_id = "referee_and_schedule_context"
    domain_family = "CONTEXTUAL_PREMATCH"

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        patterns: list[StatisticalPattern] = []
        ref_ev = next((c for c in bundle.contextual_evidence if c.category == "REFEREE_PROFILE"), None)
        sched_evs = [c for c in bundle.contextual_evidence if c.category == "SCHEDULE_FATIGUE"]
        coach_evs = [c for c in bundle.contextual_evidence if c.category == "COACH_REGIME"]

        if ref_ev is not None:
            prof = ref_ev.facts.get("profile") or {}
            cov = ref_ev.facts.get("coverage") or {}
            cards_avg = prof.get("cards_avg")
            if isinstance(cards_avg, (int, float)):
                h_yellow = [float(m.own_metrics["yellow"]) for m in bundle.home_matches if m.own_metrics.get("yellow") is not None]
                a_yellow = [float(m.own_metrics["yellow"]) for m in bundle.away_matches if m.own_metrics.get("yellow") is not None]
                flag = ("HIGH_CARD_REFEREE",) if cards_avg >= 5.0 else ("LOW_CARD_REFEREE",) if cards_avg <= 3.2 else ()
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_referee_cards_{bundle.fixture_id}",
                        pattern_scope=PatternScope.PREMATCH_TRIGGER,
                        target_metric="match.cards",
                        condition_dsl=None,
                        sample_total=len(bundle.home_matches) + len(bundle.away_matches),
                        sample_valid=len(h_yellow) + len(a_yellow),
                        coverage_observed_pct=100.0 if (h_yellow or a_yellow) else 0.0,
                        estimates={
                            "referee": ref_ev.facts.get("referee"),
                            "referee_cards_avg": cards_avg,
                            "referee_sample_matches": cov.get("matches_found") or prof.get("matches"),
                            "referee_fouls_per_card": prof.get("fouls_per_card"),
                            "home_yellow_mean": round(statistics.fmean(h_yellow), 3) if h_yellow else None,
                            "away_yellow_mean": round(statistics.fmean(a_yellow), 3) if a_yellow else None,
                            "supporting_evidence": {
                                "cards_avg": cards_avg,
                                "red_cards_avg": prof.get("red_cards_avg"),
                                "fouls_per_card_available": prof.get("fouls_per_card") is not None,
                                "team_yellow_obs_n": len(h_yellow) + len(a_yellow),
                            },
                            "inferred_component": (
                                "Extrapolación de tasa arbitral histórica al mercado disciplinario; "
                                "no cuantifica faltas si fouls_per_card es N/D ni altera goles/córners."
                            ),
                            "market_relevance": ["YELLOW_CARDS"],
                        },
                        contradiction_flags=flag,
                        exploratory_only=True,
                    )
                )

        for sev in sched_evs:
            rest = sev.facts.get("rest_hours_all_comps")
            look = sev.facts.get("lookahead_next_fixture_hours")
            next_confirmed = sev.facts.get("next_fixture_confirmed")
            past_congestion = isinstance(rest, (int, float)) and rest < 84.0
            confirmed_lookahead_congestion = (
                isinstance(look, (int, float)) and look < 84.0 and (next_confirmed is not False)
            )
            provisional_unconfirmed_cup = (
                isinstance(look, (int, float)) and look < 84.0 and (next_confirmed is False)
            )
            if past_congestion or confirmed_lookahead_congestion or provisional_unconfirmed_cup:
                side = sev.facts.get("side")
                c_flags = (
                    ("FIXTURE_CONGESTION_ROTATION_RISK",)
                    if (past_congestion or confirmed_lookahead_congestion)
                    else ("PROVISIONAL_CUP_LOOKAHEAD_UNCONFIRMED",)
                )
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_congestion_{bundle.fixture_id}_{side}",
                        pattern_scope=PatternScope.PREMATCH_TRIGGER,
                        target_metric=f"{side}.rotation_fatigue_risk",
                        condition_dsl=None,
                        sample_total=1,
                        sample_valid=1,
                        coverage_observed_pct=100.0,
                        estimates={
                            "rest_hours_all_comps": rest,
                            "prior_competition": sev.facts.get("prior_competition"),
                            "lookahead_next_fixture_hours": look,
                            "next_competition": sev.facts.get("next_competition"),
                            "next_fixture_id": sev.facts.get("next_fixture_id"),
                            "next_fixture_status": sev.facts.get("next_fixture_status"),
                            "next_fixture_venue_present": sev.facts.get("next_fixture_venue_present"),
                            "next_fixture_confirmed": next_confirmed,
                            "supporting_evidence": {
                                "past_rest_lt_84h": past_congestion,
                                "lookahead_lt_84h": isinstance(look, (int, float)) and look < 84.0,
                                "lookahead_confirmed": next_confirmed,
                            },
                            "inferred_component": (
                                "Riesgo de rotación sujeto a confirmación de XI; sin actas de minutos no se mide fatiga individual."
                                if (past_congestion or confirmed_lookahead_congestion)
                                else "Fecha futura de Copa sin sede/confirmación en API: no activar alerta de congestión confirmada."
                            ),
                            "market_relevance": (
                                ["GOALS_FT", "CORNERS", "SHOTS"]
                                if (past_congestion or confirmed_lookahead_congestion)
                                else []
                            ),
                        },
                        contradiction_flags=c_flags,
                        exploratory_only=True,
                    )
                )

        for cev in coach_evs:
            side = cev.facts.get("side")
            status = str(cev.facts.get("coach_status") or "UNKNOWN")
            regime_detected = bool(cev.facts.get("regime_change_detected"))
            regime_suspected = bool(cev.facts.get("regime_change_suspected"))
            hist_under_cur = cev.facts.get("historical_matches_under_current_coach_n")
            prior_regime = bool(cev.facts.get("historical_sample_belongs_to_prior_regime"))
            matches_n = len(bundle.home_matches if side == "home" else bundle.away_matches)

            if status == "COACH_IDENTITY_CONFLICT":
                est = dict(cev.facts)
                est.update(
                    {
                        "supporting_evidence": {
                            "coach_conflict_detail": cev.facts.get("coach_conflict_detail"),
                            "last_historical_coach_name": cev.facts.get("last_historical_coach_name"),
                            "coach_status": status,
                            "source_tier": cev.facts.get("source_tier"),
                        },
                        "inferred_component": (
                            "Contradicción directa entre el técnico reportado por API en el encuentro objetivo "
                            "y la fuente externa; no se acredita identidad oficial del técnico."
                        ),
                        "market_relevance": ["GOALS_FT", "GOALS_H1", "CORNERS", "SHOTS", "YELLOW_CARDS"],
                    }
                )
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_coach_conflict_{bundle.fixture_id}_{side}",
                        pattern_scope=PatternScope.PREMATCH_TRIGGER,
                        target_metric=f"{side}.coach_regime",
                        condition_dsl=None,
                        sample_total=matches_n,
                        sample_valid=matches_n,
                        coverage_observed_pct=100.0,
                        estimates=est,
                        contradiction_flags=("COACH_SOURCE_CONTRADICTION_UNVERIFIED",),
                        exploratory_only=True,
                    )
                )
            elif regime_detected or status in ("NEW_COACH_VERIFIED", "NEW_COACH_SECONDARY_DOCUMENTED"):
                est = dict(cev.facts)
                flags = ["COACH_REGIME_SHIFT_LIMITS_HISTORY_COMPARABILITY"]
                if status in ("NEW_COACH_VERIFIED", "NEW_COACH_SECONDARY_DOCUMENTED") and (
                    hist_under_cur == 0 or prior_regime
                ):
                    flags.append("DEBUT_COACH_PRIOR_REGIME_HISTORY_NON_REPRESENTATIVE")
                    if status == "NEW_COACH_SECONDARY_DOCUMENTED":
                        flags.append("COACH_EVIDENCE_SECONDARY_NOT_PRIMARY_OFFICIAL")
                    inferred_msg = (
                        "Entrenador nuevo con 0 partidos históricos propios en la ventana: las "
                        "observaciones previas pertenecen al régimen anterior y no constituyen partidos "
                        "disputados bajo el técnico actual (no altera p_model)."
                    )
                else:
                    inferred_msg = (
                        "Cambio cronológico de entrenador canónico dentro de la ventana: la estabilidad táctica "
                        "previa puede no extrapolarse al técnico vigente."
                    )
                est.update(
                    {
                        "supporting_evidence": {
                            "current_coach_id": cev.facts.get("current_coach_id"),
                            "current_coach_name": cev.facts.get("current_coach_name"),
                            "last_historical_coach_id": cev.facts.get("last_historical_coach_id"),
                            "last_historical_coach_name": cev.facts.get("last_historical_coach_name"),
                            "coach_status": status,
                            "source_tier": cev.facts.get("source_tier"),
                            "primary_official_verified": cev.facts.get("primary_official_verified"),
                            "coach_provenance": cev.facts.get("coach_provenance"),
                            "historical_matches_under_current_coach_n": hist_under_cur,
                            "historical_sample_belongs_to_prior_regime": prior_regime,
                            "canonical_coaches_in_window": cev.facts.get("canonical_coaches_in_window"),
                        },
                        "inferred_component": inferred_msg,
                        "market_relevance": ["GOALS_FT", "GOALS_H1", "CORNERS", "SHOTS", "YELLOW_CARDS"],
                    }
                )
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_coach_shift_{bundle.fixture_id}_{side}",
                        pattern_scope=PatternScope.PREMATCH_TRIGGER,
                        target_metric=f"{side}.coach_regime",
                        condition_dsl=None,
                        sample_total=matches_n,
                        sample_valid=matches_n,
                        coverage_observed_pct=100.0,
                        estimates=est,
                        contradiction_flags=tuple(flags),
                        exploratory_only=True,
                    )
                )
            elif status == "REGIME_CHANGE_SUSPECTED" or regime_suspected:
                est = dict(cev.facts)
                est.update(
                    {
                        "supporting_evidence": {
                            "suspected_coach_name": cev.facts.get("suspected_coach_name"),
                            "last_historical_coach_id": cev.facts.get("last_historical_coach_id"),
                            "last_historical_coach_name": cev.facts.get("last_historical_coach_name"),
                            "coach_status": status,
                            "coach_provenance": cev.facts.get("coach_provenance"),
                        },
                        "inferred_component": (
                            "Posible cambio de entrenador reportado por fuente secundaria o rumor sin "
                            "confirmación objetiva del encuentro; continuidad del régimen previo no verificada."
                        ),
                        "market_relevance": ["GOALS_FT", "GOALS_H1", "CORNERS", "SHOTS", "YELLOW_CARDS"],
                    }
                )
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_coach_shift_suspected_{bundle.fixture_id}_{side}",
                        pattern_scope=PatternScope.PREMATCH_TRIGGER,
                        target_metric=f"{side}.coach_regime",
                        condition_dsl=None,
                        sample_total=matches_n,
                        sample_valid=matches_n,
                        coverage_observed_pct=100.0,
                        estimates=est,
                        contradiction_flags=("COACH_REGIME_CHANGE_SUSPECTED_UNVERIFIED",),
                        exploratory_only=True,
                    )
                )
            elif status in ("HISTORICAL_ONLY_UNVERIFIED", "UNKNOWN"):
                est = dict(cev.facts)
                flags = ["COACH_CONTINUITY_UNVERIFIED"]
                if status == "UNKNOWN":
                    flags.append("CURRENT_COACH_UNKNOWN")
                est.update(
                    {
                        "supporting_evidence": {
                            "current_coach_id": None,
                            "current_coach_name": None,
                            "last_historical_coach_id": cev.facts.get("last_historical_coach_id"),
                            "last_historical_coach_name": cev.facts.get("last_historical_coach_name"),
                            "last_historical_coach_source": cev.facts.get("last_historical_coach_source"),
                            "coach_status": status,
                            "continuity_verified": False,
                        },
                        "inferred_component": (
                            "No existe confirmación actual de entrenador vigente para este encuentro; la "
                            "identidad histórica se conserva solo para contextualizar la muestra previa."
                        ),
                        "market_relevance": ["GOALS_FT", "GOALS_H1", "CORNERS", "SHOTS", "YELLOW_CARDS"],
                    }
                )
                patterns.append(
                    StatisticalPattern(
                        pattern_id=f"pat_coach_unverified_{bundle.fixture_id}_{side}",
                        pattern_scope=PatternScope.PREMATCH_TRIGGER,
                        target_metric=f"{side}.coach_regime",
                        condition_dsl=None,
                        sample_total=matches_n,
                        sample_valid=matches_n,
                        coverage_observed_pct=100.0,
                        estimates=est,
                        contradiction_flags=tuple(flags),
                        exploratory_only=True,
                    )
                )

        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=tuple(patterns),
            telemetry={"patterns": len(patterns)},
        )


def build_default_registry() -> "PatternRegistry":
    """Construye el PatternRegistry con todos los plugins verificados de la Etapa 3."""
    from .registry import PatternRegistry

    reg = PatternRegistry()
    reg.register(MultiWindowMatchupPlugin())
    reg.register(SoSAdjustedPlugin())
    reg.register(RedCardAndHalfDynamicsPlugin())
    reg.register(TacticalArchetypeDuelPlugin())
    reg.register(ScoreStateTimelinePlugin())
    reg.register(RefereeAndScheduleContextPlugin())
    return reg
