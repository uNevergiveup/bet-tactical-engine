#!/usr/bin/env python3
"""Pruebas unitarias de Iteración 2.1B: modelos experimentales aislados M0..M4,
Elo as-of sin mezcla CLUB/NATIONAL_TEAM, no-regresión de producción y 7909713.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.context import ObservationStore
from bet_engine.modeling import CoherentMatchModeler
from bet_engine.modeling.distributions import MODEL_VERSION
from bet_engine.modeling.experimental_v2_1b import (
    AsOfEloTracker,
    ExperimentalConfig,
    predict_experimental_match,
)

ROOT = Path(__file__).resolve().parent.parent


class TestIteracion21BExperimental(unittest.TestCase):
    def test_elo_tracker_is_strictly_asof_and_isolates_club_vs_national_team(self) -> None:
        t0 = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
        fixtures = [
            {
                "fixture_id": 1,
                "home_id": 10,
                "away_id": 20,
                "hg": 3.0,
                "ag": 0.0,
                "stratum": "CLUB",
                "ko_dt": t0,
                "avail_dt": t0 + timedelta(minutes=125),
            },
            {
                "fixture_id": 2,
                "home_id": 10,  # mismo ID numérico simulando aislamiento por estrato
                "away_id": 30,
                "hg": 0.0,
                "ag": 4.0,
                "stratum": "NATIONAL_TEAM",
                "ko_dt": t0 + timedelta(days=2),
                "avail_dt": t0 + timedelta(days=2, minutes=125),
            },
        ]
        tracker = AsOfEloTracker.build_from_fixtures(fixtures)
        # Antes de disponibilidad de fixture 1: sin historial (1500, 0)
        r_before, n_before = tracker.get_rating_asof("CLUB", 10, t0 + timedelta(minutes=60))
        self.assertEqual((r_before, n_before), (1500.0, 0))

        # Tras fixture 1: CLUB sube > 1500, NATIONAL_TEAM sigue aislado en (1500, 0) antes de fixture 2
        r_club, n_club = tracker.get_rating_asof("CLUB", 10, t0 + timedelta(days=1))
        r_nat, n_nat = tracker.get_rating_asof("NATIONAL_TEAM", 10, t0 + timedelta(days=1))
        self.assertGreater(r_club, 1500.0)
        self.assertEqual(n_club, 1)
        self.assertEqual((r_nat, n_nat), (1500.0, 0))

        # Tras fixture 2: NATIONAL_TEAM baja < 1500 sin contaminar a CLUB
        r_nat_after, _ = tracker.get_rating_asof("NATIONAL_TEAM", 10, t0 + timedelta(days=3))
        r_club_after, _ = tracker.get_rating_asof("CLUB", 10, t0 + timedelta(days=3))
        self.assertLess(r_nat_after, 1500.0)
        self.assertEqual(r_club_after, r_club)

    def test_production_modeler_untouched_and_m0_matches_production_on_7909713(self) -> None:
        me_path = ROOT / "outputs/matches/7909713/inputs/match_engine_7909713.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7909713 no presente")
        payload = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload, case_dir=me_path.parent.parent)
        prod = CoherentMatchModeler.build_model(bundle)
        self.assertEqual(prod.model_version, MODEL_VERSION)
        self.assertEqual(prod.outcomes_1x2["home_win"], 0.6372)

        m0 = predict_experimental_match(
            bundle.home_matches,
            bundle.away_matches,
            home_id=bundle.home_id,
            away_id=bundle.away_id,
            target_ko_dt=datetime.fromisoformat(bundle.kickoff_utc),
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum="NATIONAL_TEAM",
            cfg=ExperimentalConfig(name="M0"),
            history_limit=20,
        )
        self.assertAlmostEqual(m0["p_home"], prod.outcomes_1x2["home_win"], places=4)
        self.assertEqual(m0["lambda_home"], prod.families["GOALS_FT"].lambda_home)
        self.assertEqual(m0["lambda_away"], prod.families["GOALS_FT"].lambda_away)

        # M1 multiplicativo también preserva suma 1X2 = 1.0 sin modificar producción
        m1 = predict_experimental_match(
            bundle.home_matches,
            bundle.away_matches,
            home_id=bundle.home_id,
            away_id=bundle.away_id,
            target_ko_dt=datetime.fromisoformat(bundle.kickoff_utc),
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum="NATIONAL_TEAM",
            cfg=ExperimentalConfig(name="M1", multiplicative=True),
            history_limit=20,
        )
        self.assertAlmostEqual(m1["p_home"] + m1["p_draw"] + m1["p_away"], 1.0, places=5)
        self.assertEqual(CoherentMatchModeler.build_model(bundle).outcomes_1x2["home_win"], 0.6372)


if __name__ == "__main__":
    unittest.main()
