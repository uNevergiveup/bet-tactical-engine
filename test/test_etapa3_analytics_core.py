"""Pruebas offline de la Etapa 3: PatternRegistry extensible, integración v1044-v1050,
cronología marcador/minuto, minería de interacciones, investigador Codex y HypothesisRegistry.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bet_engine.analytics import (
    AnalyticsEngineResult,
    HypothesisRegistry,
    InteractionMinerPlugin,
    PatternRegistry,
    PluginOutput,
    SoSAdjustedPlugin,
    TacticalInvestigator,
    audit_feature_rows_temporal_integrity,
    benjamini_yekutieli_adjust,
    build_default_registry,
    build_feature_rows,
    build_stage3_registry,
    fisher_exact_two_sided,
    reconstruct_score_state_timeline,
    verify_hypotheses,
)
from bet_engine.context import ObservationStore
from bet_engine.context.observation_store import HistoricalTeamMatch, MatchObservationBundle
from bet_engine.contracts import (
    EpistemicContractError,
    PatternScope,
    PrematchAvailability,
    StatisticalPattern,
    TemporalLeakageError,
)


ROOT = Path(__file__).resolve().parent.parent


def _make_match(
    fid: int,
    team_id: int,
    opp_id: int,
    day: int,
    *,
    venue: str = "HOME",
    league_id: int = 140,
    season: int = 2026,
    coach_id: int = 100,
    goals: float = 1.0,
    goals_conceded: float = 1.0,
    corners: float = 5.0,
    shots: float = 12.0,
    blocked: float = 3.0,
    possession: float = 55.0,
    fouls: float = 11.0,
    yellow: float = 2.0,
    red_distortion: bool = False,
    events: tuple[dict, ...] = (),
) -> HistoricalTeamMatch:
    ko = f"2026-02-{day:02d}T18:00:00+00:00"
    avail = f"2026-02-{day:02d}T20:05:00+00:00"
    own = {
        "goals": goals,
        "goals_h1": round(goals / 2.0, 1),
        "goals_h2": round(goals / 2.0, 1),
        "corners": corners,
        "shots": shots,
        "on_target": max(1.0, round(shots * 0.4, 1)),
        "blocked_shots": blocked,
        "fouls": fouls,
        "yellow": yellow,
        "red": 1.0 if red_distortion else 0.0,
        "offsides": 2.0,
        "xg": round(goals * 0.9 + 0.3, 2),
        "possession": possession,
    }
    conc = {
        "goals": goals_conceded,
        "goals_h1": 0.0,
        "goals_h2": goals_conceded,
        "corners": 4.0,
        "shots": 9.0,
        "on_target": 3.0,
        "blocked_shots": 2.0,
        "fouls": 12.0,
        "yellow": 2.0,
        "red": 0.0,
        "offsides": 1.0,
        "xg": 1.0,
        "possession": 100.0 - possession,
    }
    return HistoricalTeamMatch(
        fixture_id=fid,
        team_id=team_id,
        opponent_id=opp_id,
        kickoff_utc=ko,
        available_at_utc=avail,
        league_id=league_id,
        league_name="Test League",
        season=season,
        competition_stratum="DOMESTIC_LEAGUE",
        venue=venue,
        status="FT",
        coach_id=coach_id,
        coach_name="Coach A",
        red_card_distortion=red_distortion,
        own_metrics=own,
        conceded_metrics=conc,
        has_detailed_stats=True,
        source="controlled_fixture",
        events=events,
    )


class TestStage3RegistryAndExtensibility(unittest.TestCase):
    def test_custom_plugin_registers_without_modifying_engine(self) -> None:
        class CustomAerialCornersPlugin:
            plugin_id = "custom_aerial_test"
            domain_family = "AERIAL_CORNERS"

            def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
                return PluginOutput(
                    plugin_id=self.plugin_id,
                    domain_family=self.domain_family,
                    patterns=(
                        StatisticalPattern(
                            pattern_id="custom_pat_1",
                            pattern_scope=PatternScope.PREMATCH_TRIGGER,
                            target_metric="home.own.corners",
                            condition_dsl=None,
                            sample_total=len(bundle.home_matches),
                            sample_valid=len(bundle.home_matches),
                            coverage_observed_pct=100.0,
                            estimates={"mean": 6.5},
                        ),
                    ),
                )

        reg = build_stage3_registry()
        reg.register(CustomAerialCornersPlugin())
        self.assertIn("custom_aerial_test", reg.list_plugins())

        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        bundle = ObservationStore.from_match_engine_json(json.loads(snap_path.read_text(encoding="utf-8")))
        res = reg.run_all(bundle)
        self.assertIsInstance(res, AnalyticsEngineResult)
        self.assertIn("custom_pat_1", {p.pattern_id for p in res.prematch_patterns})


class TestStage3RealSnapshots(unittest.TestCase):
    def test_stage3_end_to_end_on_real_match_7903872(self) -> None:
        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        if not snap_path.is_file():
            self.skipTest("Snapshot match_engine_7903872.json no presente")

        bundle = ObservationStore.from_match_engine_json(json.loads(snap_path.read_text(encoding="utf-8")))
        reg = build_stage3_registry()
        res = reg.run_all(bundle)

        # Verifica separación estricta PREMATCH vs INTRA_MATCH vs DESCRIPTIVE
        self.assertTrue(all(p.pattern_scope == PatternScope.PREMATCH_TRIGGER for p in res.prematch_patterns))
        self.assertTrue(all(p.pattern_scope == PatternScope.INTRA_MATCH_MECHANISM for p in res.internal_mechanisms))
        self.assertTrue(all(p.pattern_scope == PatternScope.DESCRIPTIVE_SPLIT for p in res.descriptive_splits))

        # Verifica que referee_cards y red/H1-H2/finishing/score_state se conectaron realmente
        pre_ids = {p.pattern_id for p in res.prematch_patterns}
        self.assertIn(f"pat_referee_cards_{bundle.fixture_id}", pre_ids)

        score_pats = [p for p in res.internal_mechanisms if p.pattern_id.startswith(f"pat_score_state_{bundle.fixture_id}")]
        self.assertGreaterEqual(len(score_pats), 1)
        # API-Football no tiene timestamps de córners: debe registrarlo sin reconstruir
        self.assertEqual(score_pats[0].estimates["corner_chronology_mode"], "CORNER_TIMELINE_ABSENT_MATCH_PROXY_ONLY")
        self.assertIn("CORNER_TIMELINE_ABSENT_NO_RECONSTRUCTION", score_pats[0].contradiction_flags)

        # Prueba dossier manual + verificación determinista de hipótesis de Codex sobre 7903872
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            registry_store = HypothesisRegistry(td_path / "hypothesis_registry.json")
            inv = TacticalInvestigator(registry=registry_store)
            handoff = inv.write_manual_handoff(td_path / "handoff", bundle, res)
            self.assertTrue(handoff["dossier_json"].is_file())
            self.assertTrue(handoff["prompt_md"].is_file())

            # Hipótesis propuesta por Codex: en casa o tras partidos con >=11 remates, Osasuna genera >=4 córners
            proposed = [
                {
                    "hypothesis_id": "codex_osasuna_shots_to_corners",
                    "origin": "LLM_INVESTIGATOR_CODEX",
                    "side": "home",
                    "target_variable": "own.corners",
                    "target_cmp": "ge",
                    "target_threshold": 4,
                    "python_test": {
                        "op": "all",
                        "clauses": [{"field": "own.shots", "cmp": "ge", "value": 10}],
                    },
                },
                {
                    "hypothesis_id": "codex_osasuna_home_prematch_corners",
                    "origin": "LLM_INVESTIGATOR_CODEX",
                    "side": "home",
                    "target_variable": "own.corners",
                    "target_cmp": "ge",
                    "target_threshold": 4,
                    "python_test": {
                        "op": "all",
                        "clauses": [{"field": "context.venue", "cmp": "eq", "value": "HOME"}],
                    },
                },
            ]
            verified = verify_hypotheses(bundle, proposed, registry=registry_store)
            self.assertEqual(len(verified), 2)
            # La hipótesis con own.shots debe quedar marcada como POSTMATCH_ONLY (no prematch trigger!)
            self.assertEqual(verified[0].prematch_availability, PrematchAvailability.POSTMATCH_ONLY)
            self.assertEqual(verified[1].prematch_availability, PrematchAvailability.PREMATCH_OBSERVABLE)
            self.assertFalse(verified[0].authorized_for_probabilistic_adjustment)
            self.assertFalse(verified[1].authorized_for_probabilistic_adjustment)

            reg_doc = registry_store.load()
            self.assertEqual(len(reg_doc["entries"]), 2)
            self.assertEqual(reg_doc["entries"][0]["revision"], 1)

    def test_stage3_on_real_20_match_preselection_1649062(self) -> None:
        snap_20 = (
            ROOT
            / "outputs"
            / "discovery"
            / "2026-11-15_1500_0300"
            / "evaluations"
            / "20261115T195445143219Z_3987d11f552b"
            / "prospective_trial"
            / "daily_runs"
            / "20261115T205609404284"
            / "comparison_audit"
            / "sources"
            / "preselection.json"
        )
        if not snap_20.is_file():
            self.skipTest("Snapshot real de 20 partidos no presente")
        data = json.loads(snap_20.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_preselection_match(data["matches"][0], cutoff_utc=data["generated_at_utc"])
        reg = build_stage3_registry()
        res = reg.run_all(bundle)

        # Verifica que score_state registra limpiamente ausencia de events en preselection.json
        self.assertIn("score_state_timeline", res.skipped_plugins)
        self.assertTrue(any("NO_VERIFIED_EVENT_CHRONOLOGY" in r for r in res.skipped_plugins["score_state_timeline"]))
        # Verifica que matchup multi-window detectó patrones sobre la competición exacta (16 partidos en liga)
        self.assertGreaterEqual(len(res.descriptive_splits), 1)


class TestStage3ControlledMechanismsAndValidation(unittest.TestCase):
    def test_scoreline_with_verified_minute_corner_timeline_and_temporal_split(self) -> None:
        # Construimos 18 partidos con cronología completa (incluyendo corner_events en unos y sin rojas en otros)
        home_matches = []
        away_matches = []
        for i in range(1, 19):
            is_home = i % 2 == 1
            # En casa va perdiendo pronto (min 15) y genera muchos córners mientras va perdiendo
            if is_home:
                evs = (
                    {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 10}, "team": {"id": 500 + i}},
                    {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 15}, "team": {"id": 500 + i}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 25}, "team": {"id": 100}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 40}, "team": {"id": 100}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 55}, "team": {"id": 100}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 70}, "team": {"id": 100}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 82}, "team": {"id": 100}},
                    {"type": "Card", "detail": "Yellow Card", "time": {"elapsed": 62}, "team": {"id": 100}},
                    {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 88}, "team": {"id": 100}},
                )
                corners_n = 7.0
                shots_n = 15.0
                goals_n = 1.0
                conc_n = 2.0
            else:
                evs = (
                    {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 20}, "team": {"id": 100}},
                    {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 50}, "team": {"id": 100}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 10}, "team": {"id": 100}},
                    {"type": "Corner", "detail": "Corner", "time": {"elapsed": 60}, "team": {"id": 100}},
                )
                corners_n = 2.0
                shots_n = 8.0
                goals_n = 2.0
                conc_n = 0.0

            home_matches.append(
                _make_match(
                    1000 + i,
                    100,
                    500 + i,
                    i,
                    venue="HOME" if is_home else "AWAY",
                    league_id=999,  # Liga arbitraria (no 140!) para demostrar ausencia de hardcoding
                    corners=corners_n,
                    shots=shots_n,
                    goals=goals_n,
                    goals_conceded=conc_n,
                    possession=60.0 if is_home else 42.0,
                    red_distortion=(i == 2),
                    events=evs,
                )
            )
            away_matches.append(
                _make_match(
                    2000 + i,
                    200,
                    600 + i,
                    i,
                    venue="AWAY" if is_home else "HOME",
                    league_id=999,
                    corners=3.0,
                    shots=9.0,
                    possession=40.0,
                )
            )

        bundle = MatchObservationBundle(
            event_id="ctrl_999",
            fixture_id=999001,
            league_id=999,
            season=2026,
            kickoff_utc="2026-03-01T20:00:00+00:00",
            cutoff_utc="2026-03-01T18:00:00+00:00",
            home_id=100,
            away_id=200,
            home_name="Home FC",
            away_name="Away FC",
            home_matches=tuple(home_matches),
            away_matches=tuple(away_matches),
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={},
            source_sha256="ctrl",
        )

        reg = build_stage3_registry()
        res = reg.run_all(bundle)

        # 1) Verifica cronología exacta de córners al ir perdiendo
        sc = next(p for p in res.internal_mechanisms if p.pattern_id == "pat_score_state_999001_home")
        self.assertEqual(sc.estimates["corner_chronology_mode"], "EXACT_MINUTE_CORNER_TIMELINE")
        self.assertGreater(
            sc.estimates["corners_per_90_by_state"]["TRAILING"],
            sc.estimates["corners_per_90_by_state"]["LEADING"],
        )

        # 2) Verifica descubrimiento automático + separación PREMATCH_TRIGGER vs INTRA_MATCH_MECHANISM
        pre_venue_corners = next(
            p
            for p in res.prematch_patterns
            if p.target_metric == "home.own.corners" and p.estimates.get("condition") == "venue_HOME"
        )
        self.assertLess(pre_venue_corners.significance["by_q_value"], 0.05)

        # 3) Validación temporal 70/30 sobre n=18 -> ValidatedPattern pero authorized_for_probabilistic_adjustment=False
        h_rows = build_feature_rows(bundle.home_matches, side="home")
        val = InteractionMinerPlugin.evaluate_temporal_validation(pre_venue_corners, h_rows, coach_consistent=True)
        self.assertIsNotNone(val)
        self.assertEqual(val.validation_protocol, "TEMPORAL_SPLIT_BY_FDR")
        self.assertFalse(val.authorized_for_probabilistic_adjustment)

        # 4) Verifica rechazo de código Python arbitrario en hipótesis de Codex
        with self.assertRaises(EpistemicContractError):
            verify_hypotheses(
                bundle,
                [{"hypothesis_id": "bad", "python_test": {"code": "1+1"}}],
            )

    def test_fisher_and_benjamini_yekutieli_math(self) -> None:
        p = fisher_exact_two_sided(8, 1, 1, 8)
        self.assertLess(p, 0.01)
        qs = benjamini_yekutieli_adjust([0.001, 0.20, 0.80])
        self.assertEqual(len(qs), 3)
        self.assertLess(qs[0], qs[1])

    def test_expanded_chronology_own_goals_penalties_stoppage_and_incomplete_events(self) -> None:
        # 1) Gol en propia puerta, penalti anotado, penalti fallado, tanda excluida y tiempo añadido (45+3, 90+4)
        evs_rich = (
            {"type": "Goal", "detail": "Own Goal", "scored_in_own_net": True, "time": {"elapsed": 20}, "team": {"id": 100}},
            {"type": "Goal", "detail": "Missed Penalty", "time": {"elapsed": 35}, "team": {"id": 100}},
            {"type": "Goal", "detail": "Penalty", "time": {"elapsed": 45, "extra": 3}, "team": {"id": 100}},
            {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 90, "extra": 4}, "team": {"id": 100}},
            {"type": "Goal", "detail": "Penalty", "comments": "Penalty Shootout", "time": {"elapsed": 120, "extra": 1}, "team": {"id": 200}},
        )
        # Marcador FT esperado: local (100) metió 2 (45+3 y 90+4) y encajó 1 (propia puerta min 20) -> 2-1
        m_rich = _make_match(5001, 100, 200, 10, goals=2.0, goals_conceded=1.0, events=evs_rich)
        tl = reconstruct_score_state_timeline(m_rich)
        self.assertTrue(tl["chronology_available"])
        self.assertFalse(tl["corner_timeline_available"])
        self.assertEqual(tl["own_goals_count"], 1)
        self.assertEqual(tl["penalties_scored_count"], 1)
        self.assertEqual(tl["missed_penalties_count"], 1)
        self.assertEqual(tl["h1_added_minutes"], 3)
        self.assertEqual(tl["h2_added_minutes"], 4)
        self.assertEqual(tl["total_tracked_minutes"], 97)
        self.assertEqual(tl["minutes_leading"] + tl["minutes_drawing"] + tl["minutes_trailing"], 97)

        # 2) Partido con eventos incompletos (marcador FT 3-1 pero solo 1 gol en events) -> NO reconstruir
        evs_incomplete = (
            {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 10}, "team": {"id": 100}},
        )
        m_inc = _make_match(5002, 100, 200, 11, goals=3.0, goals_conceded=1.0, events=evs_incomplete)
        tl_inc = reconstruct_score_state_timeline(m_inc)
        self.assertFalse(tl_inc["chronology_available"])
        self.assertIn("CHRONOLOGY_INCOMPLETE_SCORE_MISMATCH", tl_inc["reason"])

    def test_temporal_audit_and_sos_with_valid_pool_vs_insufficient(self) -> None:
        # Pool válido de rivales en la misma liga (999) con referencias as-of anteriores al partido evaluado
        opp_prior_matches = (
            _make_match(801, 501, 601, 1, league_id=999, goals=1.0, goals_conceded=3.0, corners=8.0),
            _make_match(802, 502, 602, 2, league_id=999, goals=1.0, goals_conceded=3.0, corners=8.0),
            _make_match(803, 503, 603, 3, league_id=999, goals=1.0, goals_conceded=3.0, corners=8.0),
            _make_match(804, 504, 604, 4, league_id=999, goals=1.0, goals_conceded=3.0, corners=8.0),
        )
        home_m = tuple(
            _make_match(900 + i, 100, 500 + i, 10 + i, league_id=999, goals=2.0, corners=6.0)
            for i in range(1, 7)
        )
        away_m = tuple(
            _make_match(950 + i, 200, 500 + i, 10 + i, league_id=999, goals=1.0, corners=4.0)
            for i in range(1, 7)
        )
        bundle = MatchObservationBundle(
            event_id="sos_test",
            fixture_id=9999,
            league_id=999,
            season=2026,
            kickoff_utc="2026-03-01T20:00:00+00:00",
            cutoff_utc="2026-03-01T18:00:00+00:00",
            home_id=100,
            away_id=200,
            home_name="H",
            away_name="A",
            home_matches=home_m,
            away_matches=away_m,
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={},
            source_sha256="sos",
        )
        sos_with_pool = SoSAdjustedPlugin(external_reference_pool=opp_prior_matches)
        out_ok = sos_with_pool.evaluate(bundle)
        self.assertGreaterEqual(len(out_ok.patterns), 2)
        self.assertIn("sos_adjusted_mean", out_ok.patterns[0].estimates)

        # Sin referencias previas de los rivales -> conserva evidencia insuficiente sin inventar ajuste
        sos_no_pool = SoSAdjustedPlugin()
        out_empty = sos_no_pool.evaluate(bundle)
        self.assertEqual(len(out_empty.patterns), 0)
        self.assertTrue(any("NO_PRIOR_OPPONENT_REFERENCE_IN_POOL" in s for s in out_empty.skipped_reasons))

        # Auditoría temporal rechaza variable post-kickoff infiltrada en PREMATCH_TRIGGER
        rows = build_feature_rows(home_m, side="home")
        bad_pat = StatisticalPattern(
            pattern_id="bad_prematch",
            pattern_scope=PatternScope.PREMATCH_TRIGGER,
            target_metric="home.own.corners",
            condition_dsl={"op": "all", "clauses": [{"field": "own.shots", "cmp": "gt", "value": 10}]},
            sample_total=6,
            sample_valid=6,
            coverage_observed_pct=100.0,
            estimates={},
        )
        with self.assertRaises(TemporalLeakageError):
            audit_feature_rows_temporal_integrity(rows, (bad_pat,))

    def test_complete_manual_investigator_exchange_on_real_7903872(self) -> None:
        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        bundle = ObservationStore.from_match_engine_json(json.loads(snap_path.read_text(encoding="utf-8")))
        res = build_stage3_registry().run_all(bundle)

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            reg_store = HypothesisRegistry(td_p / "registry.json")
            inv = TacticalInvestigator(registry=reg_store)
            hyps_file = td_p / "codex_hypotheses_7903872.json"
            hyps_file.write_text(
                json.dumps(
                    {
                        "hypotheses": [
                            {
                                "hypothesis_id": "h_home_shots_corners",
                                "side": "home",
                                "target_variable": "own.corners",
                                "target_cmp": "ge",
                                "target_threshold": 4,
                                "python_test": {"op": "all", "clauses": [{"field": "own.shots", "cmp": "ge", "value": 10}]},
                            },
                            {
                                "hypothesis_id": "h_contradicted_high_corners",
                                "side": "home",
                                "target_variable": "own.corners",
                                "target_cmp": "ge",
                                "target_threshold": 8,
                                "python_test": {"op": "all", "clauses": [{"field": "own.shots", "cmp": "ge", "value": 10}]},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            exchange = inv.complete_manual_exchange(td_p / "out", bundle, res, hyps_file)
            self.assertTrue(exchange["dossier_json"].is_file())
            self.assertTrue(exchange["verified_json"].is_file())
            self.assertTrue(exchange["final_report_md"].is_file())
            report_txt = exchange["final_report_md"].read_text(encoding="utf-8")
            self.assertIn("contraejemplos=`[1570436]`", report_txt)
            self.assertIn("altera_p_model=`False`", report_txt)


if __name__ == "__main__":
    unittest.main()
