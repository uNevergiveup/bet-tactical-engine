"""Pruebas offline de la Etapa 4: Coherencia matemática Equipo <-> Partido,
modelos especializados (Dixon-Coles / Binomial Negativa), bloqueo de patrones exploratorios,
Walk-Forward (Brier, Log-Loss, ECE, MAE) y comparación frente al motor anterior P-1..P-6.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bet_engine.analytics import build_stage3_registry
from bet_engine.context import ObservationStore
from bet_engine.contracts import EpistemicContractError
from bet_engine.markets import BwinMarketEvaluator
from bet_engine.modeling import CoherentMatchModeler, WalkForwardEvaluator


ROOT = Path(__file__).resolve().parent.parent


class TestStage4CoherenceAndSpecialization(unittest.TestCase):
    def test_coherence_and_exploratory_isolation_on_real_7903872(self) -> None:
        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        bundle = ObservationStore.from_match_engine_json(json.loads(snap_path.read_text(encoding="utf-8")))
        analytics = build_stage3_registry().run_all(bundle)

        model_base = CoherentMatchModeler.build_model(bundle)
        self.assertTrue(model_base.coherence_checks["all_coherent"])

        # Suma 1X2 = 1.0 y BTTS = 1.0
        self.assertAlmostEqual(sum(model_base.outcomes_1x2.values()), 1.0, places=3)
        self.assertAlmostEqual(sum(model_base.btts.values()), 1.0, places=3)

        # Goles usa Dixon-Coles; Faltas con sobredispersión observada en 7903872 usa Binomial Negativa
        self.assertEqual(model_base.families["GOALS_FT"].model_type, "BIVARIATE_DIXON_COLES_POISSON")
        self.assertEqual(model_base.families["FOULS"].model_type, "NEGATIVE_BINOMIAL_CONVOLUTION")

        # Pasar todos los patrones exploratorios/mecanismos NO puede alterar ni un decimal de p_model
        exploratory = (
            analytics.prematch_patterns
            + analytics.internal_mechanisms
            + analytics.descriptive_splits
            + analytics.tactical_hypotheses
        )
        model_with_exp = CoherentMatchModeler.build_model(bundle, exploratory_artifacts=exploratory)
        self.assertEqual(model_with_exp.ignored_exploratory_count, len(exploratory))
        for fam in model_base.families:
            self.assertEqual(
                model_base.families[fam].prob_over("total", 2.5)["p_over"],
                model_with_exp.families[fam].prob_over("total", 2.5)["p_over"],
            )

        # Intentar infiltrar un StatisticalPattern en accepted_features lanza EpistemicContractError
        with self.assertRaises(EpistemicContractError):
            CoherentMatchModeler.build_model(
                bundle,
                accepted_features=(analytics.prematch_patterns[0],),  # type: ignore[arg-type]
            )

    def test_walk_forward_out_of_sample_and_comparison_vs_legacy_p6(self) -> None:
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
        data = json.loads(snap_20.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_preselection_match(data["matches"][0], cutoff_utc=data["generated_at_utc"])

        wf_home = WalkForwardEvaluator.evaluate_series(bundle.home_matches, metric="goals", line=1.5)
        self.assertEqual(wf_home.out_of_sample_n, 16)
        # Regla 7A: con n=16 (<25), no puede declararse CALIBRATED_OUT_OF_SAMPLE aunque supere tests
        self.assertEqual(wf_home.calibration_status, "OUT_OF_SAMPLE_EVALUATED_LIMITED_SAMPLE_NOT_DECLARED_CALIBRATED")
        self.assertIsNotNone(wf_home.new_model_brier)
        self.assertIsNotNone(wf_home.legacy_p6_brier)
        self.assertIsNotNone(wf_home.new_model_log_loss)
        self.assertIsNotNone(wf_home.legacy_p6_log_loss)
        # El modelo con contracción EB evita probabilidades extremas 0/1 de P-1..P-6, mejorando Log-Loss
        self.assertLessEqual(wf_home.new_model_log_loss, wf_home.legacy_p6_log_loss)

    def test_market_evaluator_comparison_with_legacy_on_real_snapshots(self) -> None:
        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        bundle = ObservationStore.from_match_engine_json(json.loads(snap_path.read_text(encoding="utf-8")))
        analytics = build_stage3_registry().run_all(bundle)
        model = CoherentMatchModeler.build_model(bundle)

        options = BwinMarketEvaluator.evaluate_standard_markets(
            bundle,
            model,
            odds_map={"over_2_5_goals": 2.15, "over_9_5_corners": 1.95, "over_4_5_cards": 1.85},
            analytics_result=analytics,
        )
        self.assertGreaterEqual(len(options), 19)
        by_key = {o.selection_key: o for o in options}
        self.assertIsNotNone(by_key["over_2_5_goals"].ev_model)
        self.assertIsNotNone(by_key["over_2_5_goals"].legacy_p6_hit_rate)
        self.assertIn("HIGH_CARD_REFEREE", by_key["over_4_5_cards"].tactical_warnings)


if __name__ == "__main__":
    unittest.main()
