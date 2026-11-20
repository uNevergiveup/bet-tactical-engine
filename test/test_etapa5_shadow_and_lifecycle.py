"""Pruebas integrales offline de la Etapa 5.1 (Shadow Mode, ciclo T-24h preanalyze/status/finalize,
planificador secuencial por proximidad, recuperación ante fallos y backtesting multi-serie).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bet_engine.context import ObservationStore
from bet_engine.contracts import ProximityPlanState
from bet_engine.modeling import WalkForwardEvaluator
from bet_engine.orchestration import (
    PrematchLifecycleController,
    ShadowPipelineRunner,
)


ROOT = Path(__file__).resolve().parent.parent


class TestStage5ShadowAndLifecycle(unittest.TestCase):
    def test_multi_series_corpus_walk_forward_statistical_closure(self) -> None:
        p1 = ROOT / "outputs" / "reconciliation_real_20261111" / "coverage_models_final" / "preselection_cache_recovered.json"
        p2 = (
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
        bundles = []
        for p in (p1, p2):
            d = json.loads(p.read_text(encoding="utf-8"))
            for m in d["matches"]:
                bundles.append(ObservationStore.from_preselection_match(m, cutoff_utc=d["generated_at_utc"]))

        wf_goals = WalkForwardEvaluator.evaluate_multi_series_corpus(bundles, metric="goals", line=1.5)
        self.assertGreaterEqual(wf_goals.out_of_sample_n, 1500)
        self.assertGreaterEqual(wf_goals.effective_teams_n, 100)
        self.assertEqual(wf_goals.temporal_leakage_violations, 0)
        self.assertEqual(wf_goals.calibration_status, "OUT_OF_SAMPLE_BASELINE_SUPERIOR_PENDING_RESAMPLING_AUDIT")
        # En el corpus grande (N>2000), el nuevo modelo supera en Brier, Log-Loss, ECE y MAE al motor anterior
        self.assertGreater(wf_goals.brier_improvement_vs_legacy_p6, 0.0)
        self.assertGreater(wf_goals.log_loss_improvement_vs_legacy_p6, 0.0)
        self.assertLess(wf_goals.new_model_ece, wf_goals.legacy_p6_ece)
        self.assertLess(wf_goals.new_model_mae, wf_goals.legacy_p6_mae)

        # En córners (N=24), se mantiene explícitamente como familia con evidencia predictiva limitada
        wf_corners = WalkForwardEvaluator.evaluate_multi_series_corpus(bundles, metric="corners", line=4.5)
        self.assertEqual(
            wf_corners.calibration_status,
            "LIMITED_PREDICTIVE_EVIDENCE_FAMILY_NOT_CALIBRATED",
        )
        self.assertGreater(wf_corners.brier_improvement_vs_legacy_p6, 0.0)

    def test_shadow_mode_preserves_20_matches_and_compares_with_legacy(self) -> None:
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
        d = json.loads(snap_20.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_preselection_match(d["matches"][0], cutoff_utc=d["generated_at_utc"])

        runner = ShadowPipelineRunner(min_lead_minutes=45)
        report = runner.run_shadow_on_bundle(
            bundle,
            odds_map={"over_2_5_goals": 2.10, "over_9_5_corners": 1.90},
        )
        self.assertEqual(report.mode, "SHADOW_NON_INTRUSIVE")
        self.assertEqual(report.status, "COMPLETED_SHADOW")
        self.assertEqual(report.historical_depth_comparison["new_home_matches_n"], 20)
        self.assertEqual(report.historical_depth_comparison["legacy_p6_home_matches_n"], 6)
        self.assertEqual(report.historical_depth_comparison["extra_matches_preserved_home"], 14)
        self.assertTrue(report.model_coherence["all_coherent"])
        self.assertGreater(report.ignored_exploratory_in_p_model, 0)

    def test_resilience_to_missing_odds_codex_failure_and_live_guard(self) -> None:
        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        bundle = ObservationStore.from_match_engine_json(json.loads(snap_path.read_text(encoding="utf-8")))
        runner = ShadowPipelineRunner(min_lead_minutes=45)

        # 1) Recuperación ante fallo de Codex y cuotas ausentes
        deg_report = runner.run_shadow_on_bundle(
            bundle,
            odds_map={},
            simulate_codex_failure=True,
        )
        self.assertEqual(deg_report.status, "DEGRADED_SHADOW")
        self.assertTrue(any("DEGRADED_CODEX_INVESTIGATOR_RECOVERED" in r for r in deg_report.degradations_recovered))
        self.assertIn("DEGRADED_MISSING_BWIN_ODDS_PRICE_PENDING", deg_report.degradations_recovered)
        self.assertEqual(deg_report.decision, "NO_BET")
        self.assertIn("PRECIO_PENDIENTE_DE_VERIFICACION_SIN_CUOTAS_BWIN", deg_report.no_bet_reasons)

        # 2) Bloqueo estricto ante estado LIVE ("1H")
        live_report = runner.run_shadow_on_bundle(bundle, target_status="1H")
        self.assertEqual(live_report.status, "BLOCKED_PREMATCH_GUARD")
        self.assertTrue(live_report.live_data_blocked)
        self.assertEqual(live_report.decision, "NO_BET")

    def test_t24_preanalyze_status_and_sequential_finalize_flow(self) -> None:
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
        d = json.loads(snap_20.read_text(encoding="utf-8"))
        b1 = ObservationStore.from_preselection_match(d["matches"][0], cutoff_utc=d["generated_at_utc"])
        b2 = ObservationStore.from_preselection_match(d["matches"][1], cutoff_utc=d["generated_at_utc"])

        # Ajustamos kickoffs para el día objetivo 2026-11-16 (después de las 13:00 Europe/Madrid = 12:00Z)
        b1 = replace(b1, event_id="ev_1", kickoff_utc="2026-11-16T16:00:00+00:00")
        b2 = replace(b2, event_id="ev_2", kickoff_utc="2026-11-16T18:00:00+00:00")

        with tempfile.TemporaryDirectory() as td:
            ctrl = PrematchLifecycleController(
                Path(td),
                available_from_local="13:00",
                timezone_name="Europe/Madrid",
                min_quality_score=40.0,
                min_lead_minutes=45,
                safety_margin_minutes=10,
            )

            # 1) Preanálisis T-24h (día anterior)
            pre_res = ctrl.preanalyze(
                target_date_local="2026-11-16",
                bundles=[b1, b2],
                generated_at_utc="2026-11-15T19:00:00+00:00",
            )
            self.assertFalse(pre_res["t24_snapshot"]["final_picks_generated"])

            plan_dict = pre_res["plan_state"]
            from bet_engine.contracts import MatchScheduleCandidate, ScheduleExecutionStatus

            plan = ProximityPlanState(
                plan_id=plan_dict["plan_id"],
                target_date_local=plan_dict["target_date_local"],
                available_from_local=plan_dict["available_from_local"],
                timezone_name=plan_dict["timezone_name"],
                min_quality_score=plan_dict["min_quality_score"],
                candidates=tuple(
                    MatchScheduleCandidate(
                        **{**c, "execution_status": ScheduleExecutionStatus(c["execution_status"])}
                    )
                    for c in plan_dict["candidates"]
                ),
            )

            # 2) Consulta de estado a las 14:00Z (15:00 Europe/Madrid): debe seleccionar ev_1 por proximidad
            st1 = ctrl.status(plan_state=plan, now_utc="2026-11-16T14:00:00+00:00")
            self.assertEqual(st1["next_action"]["status"], "SELECTED_BY_PROXIMITY")
            self.assertEqual(st1["next_action"]["selected_event_id"], "ev_1")

            # 3) Finalize de ev_1 cerca del kickoff (14:30Z -> 90m antes del inicio >= 45m)
            rep1, plan_after_1, st_after_1 = ctrl.finalize(
                plan_state=plan,
                bundle=b1,
                now_utc="2026-11-16T14:30:00+00:00",
                updated_odds_map={"over_2_5_goals": 2.05},
            )
            self.assertIn(rep1.status, ("COMPLETED_SHADOW", "DEGRADED_SHADOW"))
            self.assertIn("ev_1", plan_after_1.completed_event_ids)
            # La cola recalculada ahora propone automáticamente ev_2 como siguiente
            self.assertEqual(st_after_1["next_action"]["selected_event_id"], "ev_2")


if __name__ == "__main__":
    unittest.main()
