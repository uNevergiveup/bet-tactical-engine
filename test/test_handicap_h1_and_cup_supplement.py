"""Tests de regresión para Hándicap 1X2 / Hándicap 1-2, poda de alto margen,
Dixon-Coles H1 + xG blend y suplemento doméstico ponderado en Champions/Copas.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.context.observation_store import HistoricalTeamMatch, MatchObservationBundle
from bet_engine.markets.bwin_catalog import extract_bwin_catalog
from bet_engine.markets.bwin_evaluator import BwinMarketEvaluator
from bet_engine.markets.market_equivalence import are_contracts_equivalent, audit_snapshot_equivalences
from bet_engine.modeling.distributions import CoherentMatchModeler
from bet_engine.modeling.tactical_enhancements import build_h1_dixon_coles_grids, enhance_match_model
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt
from market_quality_gate import _model_availability, market_kind
from prematch_tactical_signal_engine import analyze_fixture, filtered_observations
from preselect_matches import is_cup_or_continental_fixture, select_detail_games

ROOT = Path(__file__).resolve().parent.parent


def _make_hist_match(fid: int, team_id: int, opp_id: int, days_ago: int, *, xg: float | None = 1.6) -> HistoricalTeamMatch:
    ko = (datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)).isoformat()
    own = {"goals": 1.0, "goals_h1": 0.0, "corners": 5.0, "shots": 13.0, "on_target": 4.0, "fouls": 12.0, "yellow": 2.0}
    con = {"goals": 1.0, "goals_h1": 0.0, "corners": 4.0, "shots": 11.0, "on_target": 4.0, "fouls": 13.0, "yellow": 2.0}
    if xg is not None:
        own["xg"] = xg
        con["xg"] = 1.1
    return HistoricalTeamMatch(
        fixture_id=fid,
        team_id=team_id,
        opponent_id=opp_id,
        kickoff_utc=ko,
        available_at_utc=ko,
        league_id=2,
        league_name="UEFA Champions League",
        season=2026,
        competition_stratum="CONTINENTAL_CLUB",
        venue="HOME" if days_ago % 2 == 0 else "AWAY",
        status="FT",
        coach_id=100 + team_id,
        coach_name="Coach",
        red_card_distortion=False,
        own_metrics=own,
        conceded_metrics=con,
        has_detailed_stats=True,
        source="API_FOOTBALL",
    )


class TestHandicapsEquivalencesAndEnhancements(unittest.TestCase):
    def test_1_dash_period_collision_fixed_and_handicap_1_2_parsed(self) -> None:
        body = "\n".join(
            [
                "Event ID: 7990001",
                "Captura: 2026-11-25T16:00:00+01:00",
                "",
                "MERCADO: Doble oportunidad | Tiempo reglamentario",
                "Querétaro FC o X : 1.36",
                "X o Club Necaxa : 1.65",
                "Querétaro FC o Club Necaxa : 1.28",
                "MERCADO: Hándicap 1X2 (-1) - Tiempo reglamentario",
                "Querétaro FC -1 : 3.75",
                "Hándicap empate - Querétaro FC (-1) : 4.00",
                "Club Necaxa 1 : 1.67",
                "MERCADO: Hándicap 1X2 (-1) - 1ª parte",
                "Querétaro FC -1 : 7.25",
                "Hándicap empate - Querétaro FC (-1) : 3.50",
                "Club Necaxa 1 : 1.44",
                "MERCADO: Hándicap 1X2 (2) - Tiempo reglamentario",
                "Querétaro FC 2 : 1.14",
                "Hándicap empate - Querétaro FC (2) : 6.50",
                "Club Necaxa -2 : 9.00",
                "MERCADO: Hándicap 1-2 (-0,5)",
                "Querétaro FC (-0,5) : 2.10",
                "Club Necaxa (0,5) : 1.66",
                "MERCADO: Hándicap 1-2 (1,5)",
                "Querétaro FC (1,5) : 1.16",
                "Club Necaxa (-1,5) : 4.80",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bwin.txt"
            p.write_text(body, encoding="utf-8")
            odds = parse_bwin_odds_txt(p, home_name="Querétaro FC", away_name="Club Necaxa")
            cat = extract_bwin_catalog(p, event_id="7990001", home_name="Querétaro FC", away_name="Club Necaxa")

        # Antes se borraba eh_m1_* por colisión con '1ª parte'
        self.assertEqual(odds["eh_m1_home"], 3.75)
        self.assertEqual(odds["eh_m1_away"], 1.67)
        self.assertEqual(odds["ah_home_m0_5"], 2.10)
        self.assertEqual(odds["ah_away_p0_5"], 1.66)
        self.assertEqual(odds["ah_home_p1_5"], 1.16)

        # Equivalencias contractuales exactas entre Doble Oportunidad, Hándicap 1X2 y Hándicap 1-2
        self.assertTrue(are_contracts_equivalent("double_chance_x2", "eh_m1_away"))
        self.assertTrue(are_contracts_equivalent("double_chance_x2", "ah_away_p0_5"))
        self.assertTrue(are_contracts_equivalent("eh_p2_home", "ah_home_p1_5"))
        eq = audit_snapshot_equivalences(odds)
        by_cid = {g["contract_id"]: g for g in eq["equivalent_groups"]}
        self.assertEqual(by_cid["FT_DRAW_OR_AWAY"]["best_selection_key"], "eh_m1_away")
        self.assertEqual(by_cid["FT_DRAW_OR_AWAY"]["best_odd"], 1.67)
        self.assertEqual(by_cid["FT_HOME_PLUS_1_5"]["best_selection_key"], "ah_home_p1_5")

        # En el catálogo comercial: H1 lleva temporal_scope=H1 y 'Hándicap empate' queda fuera de modelo
        h1_entries = [e for e in cat["entries"] if "1ª parte" in e["market_header_literal"]]
        self.assertTrue(all(e["temporal_scope"] == "H1" for e in h1_entries))
        ft_draw = next(e for e in cat["entries"] if "Hándicap empate - Querétaro FC (-1)" == e["selection_literal"] and e["temporal_scope"] == "FT")
        self.assertIsNone(ft_draw["canonical_key"])
        self.assertEqual(ft_draw["commercial_coverage"], "OFFERED_NOT_MODELED")
        ah_entry = next(e for e in cat["entries"] if e["selection_literal"] == "Club Necaxa (0,5)")
        self.assertEqual(ah_entry["canonical_key"], "ah_away_p0_5")
        self.assertEqual(ah_entry["commercial_coverage"], "OFFERED_AND_MODELED")

        # Quality gate reconoce Hándicap 1X2 positivo sin '+' y Hándicap 1-2, excluyendo 'empate'
        self.assertEqual(_model_availability("Hándicap 1X2 (1) - Tiempo reglamentario", market_kind("Hándicap 1X2 (1)"), "Club 1")[0], True)
        self.assertEqual(_model_availability("Hándicap 1X2 (1) - Tiempo reglamentario", market_kind("Hándicap 1X2 (1)"), "Hándicap empate")[0], False)
        self.assertEqual(_model_availability("Hándicap 1-2 (0,5)", market_kind("Hándicap 1-2 (0,5)"), "Club (0,5)")[0], True)

    def test_2_evaluator_models_directional_handicaps_blocks_eh_draw_and_prunes_exact_scores(self) -> None:
        hm = tuple(_make_hist_match(100 + i, 10, 20 + i, i + 1, xg=1.85) for i in range(8))
        am = tuple(_make_hist_match(200 + i, 20, 30 + i, i + 1, xg=1.15) for i in range(8))
        bundle = MatchObservationBundle(
            event_id="7990002",
            fixture_id=990002,
            league_id=2,
            season=2026,
            kickoff_utc="2026-11-25T20:00:00+00:00",
            cutoff_utc="2026-11-25T17:00:00+00:00",
            home_id=10,
            away_id=20,
            home_name="Home FC",
            away_name="Away FC",
            home_matches=hm,
            away_matches=am,
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={"home": {}, "away": {}},
            source_sha256="0" * 64,
        )
        base_model = CoherentMatchModeler.build_model(bundle)
        enh_model = enhance_match_model(bundle, base_model)
        self.assertIn("XG_GOALS_SHRINKAGE_BLEND", enh_model.families["GOALS_FT"].accepted_features_applied)
        self.assertIn("H1_DIXON_COLES_RHO", enh_model.families["GOALS_H1"].accepted_features_applied)
        self.assertTrue(enh_model.coherence_checks["all_coherent"])
        h1_c, _ = build_h1_dixon_coles_grids(enh_model.families["GOALS_H1"])
        # Dixon-Coles con rho < 0 eleva el peso táctico del 0-0 al descanso respecto a independencia pura
        indep_00 = enh_model.families["GOALS_H1"].pmf_home[0] * enh_model.families["GOALS_H1"].pmf_away[0]
        self.assertGreater(h1_c[0][0], indep_00 * 0.99)

        odds = {
            "eh_m1_home": 3.40,
            "eh_m1_draw": 3.90,
            "eh_m1_away": 1.72,
            "eh_p3_away": 1.12,
            "ah_away_p0_5": 1.75,
            "ah_away_p1_5": 1.28,
            "eh_m2_away": 1.25,
            "exact_score_2_1": 9.50,
            "exact_score_0_0": 10.50,
            "under_0_5_goals": 9.75,
        }
        opts = {o.selection_key: o for o in BwinMarketEvaluator.evaluate_standard_markets(bundle, enh_model, odds_map=odds)}
        self.assertEqual(opts["eh_m1_draw"].decision_category, "MERCADO_NO_MODELADO")
        self.assertEqual(opts["exact_score_2_1"].decision_category, "MERCADO_NO_MODELADO")
        self.assertIsNotNone(opts["eh_m1_away"].p_model)
        self.assertIsNotNone(opts["ah_away_p0_5"].p_model)
        self.assertAlmostEqual(opts["eh_m1_away"].p_model, opts["ah_away_p0_5"].p_model, places=4)
        self.assertEqual(opts["eh_m1_away"].equivalent_best_selection, "ah_away_p0_5")
        self.assertAlmostEqual(opts["eh_m2_away"].p_model, opts["ah_away_p1_5"].p_model, places=4)

    def test_3_champions_short_sample_supplements_only_senior_domestic_league(self) -> None:
        cutoff = datetime(2026, 11, 25, 16, 0, tzinfo=timezone.utc)
        ucl_fixture = {
            "fixture_id": 1635724,
            "home": "Shakhtar Donetsk",
            "away": "Fenerbahçe",
            "home_id": 550,
            "away_id": 611,
            "league_id": 2,
            "league": "UEFA Champions League",
            "season": 2026,
            "kickoff_madrid": "2026-11-25T21:00:00+01:00",
            "analysis_cutoff_utc": cutoff.isoformat(),
        }
        self.assertTrue(is_cup_or_continental_fixture(ucl_fixture))
        games = [
            # 4 partidos UCL (insuficiente para 6+)
            *[
                {
                    "id": 10 + i,
                    "date_utc": (cutoff - timedelta(days=7 * (i + 1))).isoformat(),
                    "status": "FT",
                    "league_id": 2,
                    "league_name": "UEFA Champions League",
                    "league_type": "Cup",
                    "season": 2026,
                }
                for i in range(4)
            ],
            # 1 partido de Copa doméstica y 1 amistoso -> DEBEN EXCLUIRSE siempre
            {
                "id": 90,
                "date_utc": (cutoff - timedelta(days=5)).isoformat(),
                "status": "FT",
                "league_id": 335,
                "league_name": "Ukrainian Cup",
                "league_type": "Cup",
                "season": 2026,
            },
            {
                "id": 91,
                "date_utc": (cutoff - timedelta(days=4)).isoformat(),
                "status": "FT",
                "league_id": 667,
                "league_name": "Club Friendlies",
                "league_type": "Cup",
                "season": 2026,
            },
            # 4 partidos de Liga doméstica adulta -> SÍ suplementan hasta llegar al tope 8
            *[
                {
                    "id": 50 + i,
                    "date_utc": (cutoff - timedelta(days=3 * (i + 1))).isoformat(),
                    "status": "FT",
                    "league_id": 333,
                    "league_name": "Premier League",
                    "league_type": "League",
                    "season": 2026,
                }
                for i in range(4)
            ],
        ]
        selected, meta = select_detail_games(games, 2, 2026, cutoff, 8, allow_domestic_supplement=True)
        self.assertEqual(meta["same_competition_selected_n"], 4)
        self.assertEqual(meta["domestic_supplement_n"], 4)
        self.assertEqual(meta["alternative_sources_used"], ["DOMESTIC_LEAGUE_SUPPLEMENT"])
        self.assertNotIn(90, meta["selected_fixture_ids"])
        self.assertNotIn(91, meta["selected_fixture_ids"])

        # Verificar ponderación diferencial en prematch_tactical_signal_engine (ADN faltas > dominancia córners)
        obs = {"home": [], "away": []}
        for i in range(4):
            obs["home"].append(
                {
                    "fixture_id": 10 + i,
                    "team_id": 550,
                    "opponent_id": 700 + i,
                    "kickoff_utc": (cutoff - timedelta(days=10 + i)).isoformat(),
                    "league_id": 2,
                    "season": 2026,
                    "venue": "HOME" if i % 2 == 0 else "AWAY",
                    "status": "FT",
                    "statistics_requested": True,
                    "own": {"corners": 4.5, "fouls": 13.0, "shots": 11.5, "on_target": 4.0, "yellow": 2.5, "offsides": 1.5, "goals": 1.0, "xg": 1.1},
                    "conceded": {"corners": 4.5, "fouls": 13.0, "shots": 12.0, "on_target": 4.0, "yellow": 2.0, "offsides": 1.5, "goals": 1.0, "xg": 1.1},
                }
            )
            obs["home"].append(
                {
                    "fixture_id": 50 + i,
                    "team_id": 550,
                    "opponent_id": 800 + i,
                    "kickoff_utc": (cutoff - timedelta(days=2 + i)).isoformat(),
                    "league_id": 333,
                    "season": 2026,
                    "venue": "HOME" if i % 2 == 0 else "AWAY",
                    "status": "FT",
                    "statistics_requested": True,
                    "supplement_source": "DOMESTIC_LEAGUE_SUPPLEMENT",
                    "own": {"corners": 8.0, "fouls": 13.0, "shots": 17.0, "on_target": 6.0, "yellow": 2.5, "offsides": 2.0, "goals": 2.0, "xg": 2.0},
                    "conceded": {"corners": 2.5, "fouls": 13.0, "shots": 7.0, "on_target": 2.5, "yellow": 2.0, "offsides": 1.5, "goals": 0.5, "xg": 0.7},
                }
            )
        for i in range(7):
            obs["away"].append(
                {
                    "fixture_id": 200 + i,
                    "team_id": 611,
                    "opponent_id": 900 + i,
                    "kickoff_utc": (cutoff - timedelta(days=5 + i)).isoformat(),
                    "league_id": 2,
                    "season": 2026,
                    "venue": "AWAY" if i % 2 == 0 else "HOME",
                    "status": "FT",
                    "statistics_requested": True,
                    "own": {"corners": 4.5, "fouls": 13.0, "shots": 13.0, "on_target": 4.5, "yellow": 2.5, "offsides": 1.5, "goals": 1.5, "xg": 1.4},
                    "conceded": {"corners": 5.5, "fouls": 13.0, "shots": 13.0, "on_target": 4.5, "yellow": 2.5, "offsides": 1.5, "goals": 1.5, "xg": 1.5},
                }
            )
        clean_h, _ = filtered_observations(obs["home"], ucl_fixture, "home", cutoff)
        self.assertEqual(len(clean_h), 8)
        analyzed = analyze_fixture(ucl_fixture, cutoff, obs)
        home_corners = next(s for s in analyzed["signals"] if s["signal_id"] == "1635724:home:corners")
        home_fouls = next(s for s in analyzed["signals"] if s["signal_id"] == "1635724:home:fouls")
        # Córners usa peso conservador (0.35 -> n_eff=5.4, media amortiguada < 5.5), faltas usa peso ADN (0.70 -> n_eff=6.8)
        self.assertLess(home_corners["own"]["mean"], 5.5)
        self.assertGreater(home_fouls["uncertainty"]["effective_n"], home_corners["uncertainty"]["effective_n"])
        self.assertIn("DOMESTIC_LEAGUE_SUPPLEMENT_WEIGHTED_BY_METRIC_TRANSFERABILITY", home_corners["limitations"])


if __name__ == "__main__":
    unittest.main()
