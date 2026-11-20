"""Pruebas específicas P1-A: construcción de muestras, diferenciación de rojas,
consistencia de ranking/taxonomía Shadow y trazabilidad de mercados no modelados.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bet_engine.analytics.registry import build_feature_rows
from bet_engine.context import ObservationStore
from bet_engine.context.observation_store import _classify_stratum, _detect_early_red_card
from bet_engine.markets import BwinMarketEvaluator
from bet_engine.modeling import CoherentMatchModeler
from bet_engine.modeling.distributions import _estimate_team_rate_detailed, _resolve_prior
from bet_engine.orchestration import ShadowPipelineRunner
from bet_engine.orchestration.shadow_pipeline import audit_bwin_catalog_txt, parse_bwin_odds_txt

ROOT = Path(__file__).resolve().parent.parent


class TestP1AControlledCorrections(unittest.TestCase):
    def test_a_classify_stratum_national_friendlies_positive_and_negative(self) -> None:
        # Casos positivos: selecciones nacionales y amistosos internacionales reales de API-Football
        positives = [
            ("Friendlies", None, 10, "World"),
            ("Friendlies", "Cup", 10, "World"),
            ("Friendlies Women", None, None, "World"),
            ("International Friendlies", None, None, "World"),
            ("Friendly International", None, None, None),
            ("UEFA Nations League", None, 5, "World"),
            ("World Cup", None, 1, "World"),
            ("World Cup - Qualification Europe", None, 32, "World"),
            ("Euro Championship", None, 4, "World"),
            ("Copa America", None, 9, "World"),
            ("Africa Cup of Nations", None, 6, "World"),
            ("AFCON", None, None, "World"),
            ("CONCACAF Gold Cup", None, None, "World"),
            ("Asian Cup", None, None, "World"),
        ]
        for name, ltype, lid, country in positives:
            with self.subTest(name=name, lid=lid):
                self.assertEqual(
                    _classify_stratum(name, ltype, league_id=lid, country=country),
                    "NATIONAL_TEAM",
                )

        # Casos negativos: clubes y torneos domésticos/continentales NO deben clasificarse como NATIONAL_TEAM
        negatives = [
            ("Club Friendlies", None, 667, "World"),
            ("Club Friendlies Women", None, None, "World"),
            ("FIFA Club World Cup", None, 15, "World"),
            ("Premier League", "League", 39, "England"),
            ("La Liga", "League", 140, "Spain"),
            ("Copa del Rey", "Cup", 143, "Spain"),
            ("UEFA Champions League", "Cup", 2, "World"),
            ("UEFA Europa League", "Cup", 3, "World"),
        ]
        for name, ltype, lid, country in negatives:
            with self.subTest(neg_name=name, lid=lid):
                self.assertNotEqual(
                    _classify_stratum(name, ltype, league_id=lid, country=country),
                    "NATIONAL_TEAM",
                )

        self.assertEqual(_classify_stratum("FIFA Club World Cup", None), "CONTINENTAL_CLUB")
        self.assertEqual(_classify_stratum("Premier League", "League"), "DOMESTIC_LEAGUE")
        self.assertEqual(_classify_stratum("Copa del Rey", "Cup"), "DOMESTIC_CUP")

    def test_b_red_card_differentiation_and_policy_isolation(self) -> None:
        from bet_engine.context.observation_store import _analyze_red_card_events

        # 1) Roja temprana propia (min 35) vs roja temprana rival (min 46) vs roja tardía (min 86)
        ev_own = [{"type": "Card", "detail": "Red Card", "team": {"id": 10}, "time": {"elapsed": 35, "extra": None}}]
        r_own = _analyze_red_card_events(ev_own, 10, own_red_stat=1.0, opp_red_stat=0.0)
        self.assertEqual(r_own["distortion_side"], "OWN_EARLY")
        self.assertTrue(r_own["early_own_red"])
        self.assertFalse(r_own["early_opp_red"])
        self.assertEqual(r_own["own_first_red_minute"], 35)
        self.assertEqual(r_own["own_affected_minutes"], 55.0)
        self.assertEqual(r_own["opp_affected_minutes"], 0.0)
        self.assertEqual(r_own["data_availability"], "VERIFIED_EVENT_TIMELINE")

        ev_opp = [{"type": "Card", "detail": "Second Yellow card - Red", "team": {"id": 20}, "time": {"elapsed": 46, "extra": None}}]
        r_opp = _analyze_red_card_events(ev_opp, 10, own_red_stat=0.0, opp_red_stat=1.0)
        self.assertEqual(r_opp["distortion_side"], "OPPONENT_EARLY")
        self.assertFalse(r_opp["early_own_red"])
        self.assertTrue(r_opp["early_opp_red"])
        self.assertEqual(r_opp["opp_first_red_minute"], 46)
        self.assertEqual(r_opp["opp_affected_minutes"], 44.0)

        ev_late = [{"type": "Card", "detail": "Red Card", "team": {"id": 10}, "time": {"elapsed": 86, "extra": None}}]
        r_late = _analyze_red_card_events(ev_late, 10, own_red_stat=1.0, opp_red_stat=0.0)
        self.assertEqual(r_late["distortion_side"], "LATE_ONLY")
        self.assertFalse(r_late["early_own_red"])
        self.assertFalse(r_late["early_any_red"])
        self.assertEqual(r_late["own_affected_minutes"], 4.0)

        # Compatibilidad hacia atrás de _detect_early_red_card
        self.assertTrue(_detect_early_red_card(ev_own, 10))
        self.assertTrue(_detect_early_red_card(ev_opp, 10))
        self.assertFalse(_detect_early_red_card(ev_late, 10))

        # 2) Caso real congelado 7905487: 1528920 tuvo roja RIVAL (Noruega min 46); 1528941 roja tardía Portugal min 86
        ctx_path = ROOT / "outputs/matches/7905487/context_updates/05239425bf28e513b86bec42/match_engine_7905487.json"
        if not ctx_path.is_file():
            self.skipTest("Snapshot 7905487 no presente")
        payload = json.loads(ctx_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)

        m_1528920 = next(m for m in bundle.home_matches if m.fixture_id == 1528920)
        self.assertEqual(m_1528920.red_card_detail["distortion_side"], "OPPONENT_EARLY")
        self.assertFalse(m_1528920.red_card_detail["early_own_red"])
        self.assertTrue(m_1528920.red_card_detail["early_opp_red"])
        self.assertEqual(m_1528920.red_card_detail["opp_first_red_minute"], 46)
        self.assertEqual(m_1528920.red_card_detail["opp_affected_minutes"], 44.0)

        m_1528941 = next(m for m in bundle.away_matches if m.fixture_id == 1528941)
        self.assertEqual(m_1528941.red_card_detail["distortion_side"], "LATE_ONLY")
        self.assertEqual(m_1528941.red_card_detail["own_first_red_minute"], 86)

        # build_feature_rows propaga el detalle sin romper compatibilidad
        frows = build_feature_rows(bundle.home_matches, side="home")
        row_920 = next(r for r in frows if r["fixture_id"] == 1528920)
        self.assertEqual(row_920["score_state"]["red_card_side"], "OPPONENT_EARLY")

        # El modo por defecto 'exclude_any' mantiene n_own=5 en Gales; la política diferenciada queda aislada bajo opt-in
        est_def = _estimate_team_rate_detailed(
            bundle.home_matches,
            bundle.away_matches,
            metric="goals",
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum="NATIONAL_TEAM",
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", "NATIONAL_TEAM", "HOME"),
        )
        self.assertEqual(est_def["n_own"], 5)
        est_iso = _estimate_team_rate_detailed(
            bundle.home_matches,
            bundle.away_matches,
            metric="goals",
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum="NATIONAL_TEAM",
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", "NATIONAL_TEAM", "HOME"),
            red_card_policy="differentiated_weight",
        )
        self.assertEqual(est_iso["n_own"], 6)
        self.assertNotEqual(est_iso["lambda"], est_def["lambda"])

    def test_c_d_e_7905487_shadow_ranking_taxonomy_unmodelled_and_snapshot_audit(self) -> None:
        ctx_path = ROOT / "outputs/matches/7905487/context_updates/05239425bf28e513b86bec42/match_engine_7905487.json"
        txt_path = ROOT / "outputs/matches/7905487/inputs/cuotas_bwin_7905487.txt"
        if not (ctx_path.is_file() and txt_path.is_file()):
            self.skipTest("Snapshot 7905487 no presente")

        payload = json.loads(ctx_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)
        mm = payload.get("match") or {}
        odds_map = parse_bwin_odds_txt(
            txt_path,
            home_name=mm.get("home_name") or bundle.home_name,
            away_name=mm.get("away_name") or bundle.away_name,
            home_aliases=(bundle.home_name, mm.get("home_name_raw") or ""),
            away_aliases=(bundle.away_name, mm.get("away_name_raw") or ""),
        )

        # A: El amistoso 1542180 ahora se clasifica como NATIONAL_TEAM
        m_friendly = next(m for m in bundle.home_matches if m.fixture_id == 1542180)
        self.assertEqual(m_friendly.competition_stratum, "NATIONAL_TEAM")

        report = ShadowPipelineRunner(min_lead_minutes=0).run_shadow_on_bundle(
            bundle,
            target_status="NS",
            odds_map=odds_map,
            bwin_txt_path=txt_path,
        )

        # D: Toda clave parseada en odds_map aparece trazablemente en market_comparisons
        mc_by_key = {m["selection_key"]: m for m in report.market_comparisons}
        for unm_key in ("eh_p3_away", "eh_p3_draw", "eh_p3_home", "first_goal_none", "h1_first_goal_none"):
            self.assertIn(unm_key, odds_map)
            self.assertIn(unm_key, mc_by_key, f"Falta {unm_key} en market_comparisons")
            row = mc_by_key[unm_key]
            self.assertEqual(row["decision_category"], "MERCADO_NO_MODELADO")
            self.assertEqual(row["classification"], "MERCADO_NO_MODELADO")
            self.assertIsNone(row["p_model"])
            self.assertTrue(row["pending_conditions"])

        # Cobertura del catálogo reconcilia exactamente: modelados + no modelados == 669
        cat = report.catalog_coverage_summary
        self.assertEqual(cat["total_selections_in_txt"], 669)
        self.assertEqual(
            cat["modelled_selections_with_odds"] + cat["unmodelled_selections_total"],
            cat["total_selections_in_txt"],
        )
        self.assertEqual(sum(cat["unmodelled_by_category"].values()), cat["unmodelled_selections_total"])

        # C: Existen opciones con ev_conservative_low > 0 pero NO son PICK_ACCIONABLE y el motivo NO dice EV_CONSERVADOR_NEGATIVO
        pos_cons = [
            m for m in report.market_comparisons
            if m.get("ev_conservative_low") is not None and m["ev_conservative_low"] > 0.0
        ]
        self.assertGreater(len(pos_cons), 0)
        self.assertEqual(report.decision_breakdown_counts["PICK_ACCIONABLE"], 0)
        self.assertFalse(any("EV_CONSERVADOR_NEGATIVO" in r for r in report.no_bet_reasons))
        self.assertTrue(any("EV_CONSERVADOR_POSITIVO" in r for r in report.no_bet_reasons))

        # C: Equivalentes contractuales deduplicados en top_investigable_candidates manteniendo sus cuotas originales
        cands = report.top_investigable_candidates
        self.assertEqual(len(cands), 3)
        contract_ids = [c.get("canonical_contract_id") for c in cands if c.get("canonical_contract_id")]
        self.assertEqual(len(contract_ids), len(set(contract_ids)))

        # Priorización coherente: las opciones con EV conservador positivo superan a home_win (-0.0744)
        self.assertTrue(all(c["ev_conservative_positive"] for c in cands))
        self.assertGreater(cands[0]["ev_conservative_low"], 0.0)
        for c in cands:
            self.assertIn("effective_sample_n", c)
            self.assertIn("calibration_status", c)
            self.assertIn("equivalent_quotes", c)
            self.assertFalse(c["actionable_pick"])
            # Todas las cuotas originales siguen preservadas en market_comparisons
            for eq_q in c["equivalent_quotes"]:
                self.assertIn(eq_q["selection_key"], mc_by_key)

    def test_e_separation_of_probability_impact_and_unaccredited_no_pick(self) -> None:
        from dataclasses import replace

        # Verificar que sin cambiar el estrato previo, las correcciones B (default), C y D producen 0.0000 cambio en p_model,
        # y que un candidato con EV conservador positivo y n>=10 pero calibración no acreditada NO pasa a PICK_ACCIONABLE.
        ctx_path = ROOT / "outputs/matches/7905487/context_updates/05239425bf28e513b86bec42/match_engine_7905487.json"
        if not ctx_path.is_file():
            self.skipTest("Snapshot 7905487 no presente")
        payload = json.loads(ctx_path.read_text(encoding="utf-8"))
        bundle_new = ObservationStore.from_match_engine_json(payload)
        # Reconstruimos la versión con estrato previo ('DOMESTIC_LEAGUE' en 1542180) para aislar el impacto exacto de A
        legacy_home_matches = tuple(
            replace(m, competition_stratum="DOMESTIC_LEAGUE") if m.fixture_id == 1542180 else m
            for m in bundle_new.home_matches
        )
        bundle_prev_stratum = replace(bundle_new, home_matches=legacy_home_matches)

        model_prev_stratum = CoherentMatchModeler.build_model(bundle_prev_stratum)
        model_new_stratum = CoherentMatchModeler.build_model(bundle_new)

        # Sin el fix de estrato A, las correcciones B (por defecto), C y D mantienen idénticas las tasas baseline (1.3045 / 1.5607)
        self.assertAlmostEqual(model_prev_stratum.families["GOALS_FT"].lambda_home, 1.3045, places=4)
        self.assertAlmostEqual(model_prev_stratum.families["GOALS_FT"].lambda_away, 1.5607, places=4)
        self.assertAlmostEqual(model_prev_stratum.outcomes_1x2["home_win"], 0.3144, places=4)

        # Con el fix de estrato A ('Friendlies' -> NATIONAL_TEAM, comp_weight 0.25 -> 0.50 en 1542180), el delta es acotado y trazable
        self.assertAlmostEqual(model_new_stratum.families["GOALS_FT"].lambda_home, 1.3036, places=4)
        self.assertAlmostEqual(model_new_stratum.families["GOALS_FT"].lambda_away, 1.565, places=4)

        # Incluso con XI confirmado, n>=10 y ev_lo>0, sin calibración acreditada no se promueve a PICK_ACCIONABLE
        padded_home = tuple(replace(bundle_new.home_matches[0], fixture_id=9000 + i) for i in range(12))
        padded_away = tuple(replace(bundle_new.away_matches[0], fixture_id=9100 + i) for i in range(12))
        padded_bundle = replace(bundle_new, home_matches=padded_home, away_matches=padded_away)
        padded_model = CoherentMatchModeler.build_model(padded_bundle)
        opts_unacc = BwinMarketEvaluator.evaluate_standard_markets(
            padded_bundle,
            padded_model,
            odds_map={"draw": 9.0},
            lineups_confirmed_complete=True,
            lineup_status="LINEUPS_API_CONFIRMED",
        )
        draw_unacc = next(o for o in opts_unacc if o.selection_key == "draw")
        self.assertGreater(draw_unacc.ev_conservative_low or -1.0, 0.0)
        self.assertEqual(draw_unacc.decision_category, "CANDIDATO_CONDICIONAL")
        self.assertTrue(any("CALIBRACION_NO_ACREDITADA" in p for p in draw_unacc.pending_conditions))


if __name__ == "__main__":
    unittest.main()
