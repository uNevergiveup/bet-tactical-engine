"""Pruebas de regresión y comparativa Antes/Después para la Primera Implementación:
Cobertura dinámica de mercados Bwin, liquidación DNB con devolución por empate y taxonomía de 5 estados.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bet_engine.context import ObservationStore
from bet_engine.markets import BwinMarketEvaluator
from bet_engine.modeling import CoherentMatchModeler
from bet_engine.orchestration import ShadowPipelineRunner
from bet_engine.orchestration.shadow_pipeline import audit_bwin_catalog_txt, parse_bwin_odds_txt

ROOT = Path(__file__).resolve().parent.parent


class TestStage1MarketCoverageAndTaxonomy(unittest.TestCase):
    def test_dnb_settlement_explicitly_accounts_for_draw_refund(self) -> None:
        snap_path = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        if not snap_path.is_file():
            self.skipTest("Snapshot 7909704 no presente")
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)
        model = CoherentMatchModeler.build_model(bundle)

        opts = BwinMarketEvaluator.evaluate_standard_markets(
            bundle,
            model,
            odds_map={"dnb_home": 1.65, "dnb_away": 2.37},
        )
        by_key = {o.selection_key: o for o in opts}
        dnb_h = by_key["dnb_home"]

        p_hw = model.outcomes_1x2["home_win"]
        p_d = model.outcomes_1x2["draw"]
        expected_p_cond = round(p_hw / (1.0 - p_d), 4)
        expected_ev = round(p_hw * 1.65 + p_d - 1.0, 4)

        self.assertEqual(dnb_h.settlement_rule, "DRAW_NO_BET_PUSH_REFUND_ON_DRAW")
        self.assertEqual(dnb_h.p_model, expected_p_cond)
        self.assertEqual(dnb_h.p_win_unconditional, round(p_hw, 4))
        self.assertEqual(dnb_h.p_push_refund, round(p_d, 4))
        self.assertEqual(dnb_h.ev_model, expected_ev)
        # EV central positivo (+3.4%) NO se convierte automáticamente en PICK_ACCIONABLE
        self.assertGreater(dnb_h.ev_model, 0.0)
        self.assertEqual(dnb_h.decision_category, "CANDIDATO_CONDICIONAL")
        self.assertNotEqual(dnb_h.decision_category, "PICK_ACCIONABLE")

    def test_dynamic_coverage_on_immutable_7909704_and_7905477_snapshots(self) -> None:
        for eid, prev_quoted_count in (("7909704", 15), ("7905477", 6)):
            me_path = ROOT / f"outputs/matches/{eid}/inputs/match_engine_{eid}.json"
            txt_path = ROOT / f"outputs/matches/{eid}/inputs/cuotas_bwin_{eid}.txt"
            if not (me_path.is_file() and txt_path.is_file()):
                self.skipTest(f"Snapshot {eid} no presente")

            payload = json.loads(me_path.read_text(encoding="utf-8"))
            bundle = ObservationStore.from_match_engine_json(payload)
            match_meta = payload.get("match") or {}
            odds_map = parse_bwin_odds_txt(
                txt_path,
                home_name=match_meta.get("home_name") or bundle.home_name,
                away_name=match_meta.get("away_name") or bundle.away_name,
                home_aliases=(bundle.home_name, match_meta.get("home_name_raw") or ""),
                away_aliases=(bundle.away_name, match_meta.get("away_name_raw") or ""),
            )

            # Verificar que mercados prioritarios están ahora mapeados y evaluados
            for required_key in (
                "home_win",
                "draw",
                "away_win",
                "double_chance_1x",
                "double_chance_x2",
                "double_chance_12",
                "dnb_home",
                "dnb_away",
                "home_over_0_5_goals",
                "home_under_0_5_goals",
                "away_over_0_5_goals",
                "away_under_0_5_goals",
                "over_0_5_goals",
                "under_0_5_goals",
                "over_1_5_goals",
                "under_1_5_goals",
                "over_2_5_goals",
                "under_2_5_goals",
                "over_6_5_corners",
                "under_6_5_corners",
                "over_7_5_corners",
                "under_7_5_corners",
                "over_8_5_corners",
                "under_8_5_corners",
            ):
                self.assertIn(required_key, odds_map, f"Falta {required_key} en {eid}")

            runner = ShadowPipelineRunner(min_lead_minutes=0)
            report = runner.run_shadow_on_bundle(
                bundle,
                target_status="NS",
                odds_map=odds_map,
                bwin_txt_path=txt_path,
            )
            quoted_after = [m for m in report.market_comparisons if m.get("odd") is not None]
            self.assertGreater(len(quoted_after), prev_quoted_count * 3)

            # Ninguna prop individual de jugador se contamina como mercado de equipo
            self.assertGreater(
                report.catalog_coverage_summary["unmodelled_by_category"]["PLAYER_PROPS_REQUIRES_XI_AND_EXPOSURE"],
                0,
            )
            # Conserva hasta 3 candidatos investigables con cuota, probabilidades, EV central y condiciones
            self.assertEqual(len(report.top_investigable_candidates), 3)
            for cand in report.top_investigable_candidates:
                self.assertIsNotNone(cand["odd"])
                self.assertIsNotNone(cand["p_model"])
                self.assertIsNotNone(cand["ev_model"])
                self.assertIsNotNone(cand["fair_odd"])
                self.assertIsInstance(cand["pending_conditions"], list)
                self.assertFalse(cand["actionable_pick"])

            if eid == "7909704":
                by_key = {m["selection_key"]: m for m in report.market_comparisons}
                # Estonia Más de 1,5 goles (0/6 propios): 0/N NO es veto absoluto sino evidencia de riesgo (Etapa 2)
                self.assertEqual(by_key["home_over_1_5_goals"]["decision_category"], "CANDIDATO_CONDICIONAL")
                self.assertTrue(
                    any(f.startswith("RIESGO_HISTORICO_0_DE_6") for f in by_key["home_over_1_5_goals"]["risk_flags"])
                )
                # Bulgaria Menos de 0,5 goles (@ 2.15) está evaluado con cuota; tras P1-A (_classify_stratum
                # reconoce 'Friendlies' id=10 como NATIONAL_TEAM en 1540354 con comp_weight 0.50 vs 0.25),
                # la probabilidad coherente exacta pasa de 0.3932 a 0.3858 sin tocar priors/shrinkage.
                self.assertEqual(by_key["away_under_0_5_goals"]["odd"], 2.15)
                self.assertAlmostEqual(by_key["away_under_0_5_goals"]["p_model"], 0.3858, places=4)


if __name__ == "__main__":
    unittest.main()
