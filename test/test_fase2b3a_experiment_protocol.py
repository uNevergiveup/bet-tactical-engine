from __future__ import annotations

import unittest
from datetime import datetime, timezone

from bet_engine.context.observation_store import HistoricalTeamMatch
from bet_engine.modeling.distributions import (
    DEFAULT_PRIORS,
    MODEL_VERSION,
    _estimate_team_rate_detailed,
    _resolve_prior,
)
from tools.run_fase2b3a_experiments import (
    VariantSpec,
    _estimate_side_rate_variant,
    _filter_competition_history,
    _fit_empirical_priors_asof,
)


def _mk_match(
    fixture_id: int,
    team_id: int,
    opp_id: int,
    kickoff_utc: str,
    available_at_utc: str,
    *,
    venue: str = "HOME",
    league_id: int = 39,
    league_name: str = "Premier League",
    season: int = 2026,
    stratum: str = "DOMESTIC_LEAGUE",
    goals_for: float = 2.0,
    goals_ag: float = 1.0,
) -> HistoricalTeamMatch:
    return HistoricalTeamMatch(
        fixture_id=fixture_id,
        team_id=team_id,
        opponent_id=opp_id,
        kickoff_utc=kickoff_utc,
        available_at_utc=available_at_utc,
        league_id=league_id,
        league_name=league_name,
        season=season,
        competition_stratum=stratum,
        venue=venue,
        status="FT",
        coach_id=None,
        coach_name=None,
        red_card_distortion=False,
        own_metrics={"goals": goals_for},
        conceded_metrics={"goals": goals_ag},
        has_detailed_stats=True,
        source="TEST",
    )


class TestFase2B3AExperimentProtocol(unittest.TestCase):
    def test_production_defaults_and_model_version_untouched(self) -> None:
        self.assertEqual(DEFAULT_PRIORS["goals"], (1.56, 1.26))
        self.assertEqual(MODEL_VERSION, "CoherentMatchModeler_baseline_2.0_2.1A")

    def test_empirical_priors_strict_asof_cutoff_excludes_future_and_unavailable(self) -> None:
        all_fixtures = {
            1: {
                "home": _mk_match(
                    1, 10, 20, "2026-08-10T18:00:00+00:00", "2026-08-10T20:10:00+00:00",
                    goals_for=2.0, goals_ag=0.0,
                ),
            },
            2: {
                # Empieza exactamente en el cutoff -> debe excluirse (>= cutoff)
                "home": _mk_match(
                    2, 10, 21, "2026-08-20T18:00:00+00:00", "2026-08-20T20:10:00+00:00",
                    goals_for=9.0, goals_ag=9.0,
                ),
            },
            3: {
                # Kickoff previo al cutoff pero disponible DESPUÉS (AET/tardío) -> debe excluirse
                "home": _mk_match(
                    3, 10, 22, "2026-08-20T17:00:00+00:00", "2026-08-20T19:00:00+00:00",
                    goals_for=7.0, goals_ag=7.0,
                ),
            },
        }
        cutoff = datetime.fromisoformat("2026-08-20T18:00:00+00:00").astimezone(timezone.utc)
        fit = _fit_empirical_priors_asof(all_fixtures, cutoff)
        self.assertEqual(fit["fit_fids"], {1})
        self.assertEqual(fit["fit_n"], 1)
        self.assertEqual(fit["global"], (2.0, 0.0))

    def test_baseline_variant_matches_production_estimator_exactly_including_shared_h2h(self) -> None:
        h2h = _mk_match(
            100, 1, 2, "2026-08-01T15:00:00+00:00", "2026-08-01T17:05:00+00:00",
            venue="HOME", goals_for=3.0, goals_ag=0.0,
        )
        opp_h2h = _mk_match(
            100, 2, 1, "2026-08-01T15:00:00+00:00", "2026-08-01T17:05:00+00:00",
            venue="AWAY", goals_for=0.0, goals_ag=3.0,
        )
        own_hist = (
            h2h,
            _mk_match(101, 1, 3, "2026-08-05T15:00:00+00:00", "2026-08-05T17:05:00+00:00", venue="HOME", goals_for=2.0, goals_ag=1.0),
            _mk_match(102, 1, 4, "2026-08-10T15:00:00+00:00", "2026-08-10T17:05:00+00:00", venue="HOME", goals_for=1.0, goals_ag=0.0),
            _mk_match(103, 1, 5, "2026-08-15T15:00:00+00:00", "2026-08-15T17:05:00+00:00", venue="AWAY", goals_for=0.0, goals_ag=2.0),
        )
        opp_hist = (
            opp_h2h,
            _mk_match(201, 2, 6, "2026-08-06T15:00:00+00:00", "2026-08-06T17:05:00+00:00", venue="AWAY", goals_for=1.0, goals_ag=2.0),
            _mk_match(202, 2, 7, "2026-08-11T15:00:00+00:00", "2026-08-11T17:05:00+00:00", venue="AWAY", goals_for=1.0, goals_ag=1.0),
            _mk_match(203, 2, 8, "2026-08-16T15:00:00+00:00", "2026-08-16T17:05:00+00:00", venue="HOME", goals_for=2.0, goals_ag=0.0),
        )

        b0 = VariantSpec("B0_baseline_frozen", "BASELINE", "ref")
        prod_h = _estimate_team_rate_detailed(
            own_hist, opp_hist, metric="goals",
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", "DOMESTIC_LEAGUE", "HOME"),
            history_limit=20,
        )
        var_h = _estimate_side_rate_variant(
            own_hist, opp_hist,
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", "DOMESTIC_LEAGUE", "HOME"),
            other_venue_prior_mean=_resolve_prior("goals", "DOMESTIC_LEAGUE", "AWAY"),
            history_limit=20,
            spec=b0,
        )
        self.assertAlmostEqual(float(prod_h["lambda"]), var_h["lambda"], places=12)

    def test_competition_filters_friendly_and_cup_policies(self) -> None:
        league_games = [
            _mk_match(i, 1, 10 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00")
            for i in range(1, 7)
        ]
        friendly = _mk_match(
            99, 1, 99, "2026-08-02T15:00:00+00:00", "2026-08-02T17:05:00+00:00",
            league_id=667, league_name="Club Friendlies", stratum="OTHER",
        )
        cup = _mk_match(
            98, 1, 98, "2026-08-03T15:00:00+00:00", "2026-08-03T17:05:00+00:00",
            league_id=45, league_name="FA Cup", stratum="DOMESTIC_CUP",
        )

        # C1 / C2 sobre 6 liga + 1 amistoso -> excluyen el amistoso
        pool = tuple(league_games + [friendly, cup])
        c1 = _filter_competition_history(pool, comp_policy="exclude_friendlies_fallback", target_league_id=39, target_stratum="DOMESTIC_LEAGUE")
        self.assertNotIn(friendly, c1)
        self.assertIn(cup, c1)

        # Fallback en C1 cuando quedan < 4 no-amistosos
        scarce_pool = tuple(league_games[:2] + [friendly])
        c1_fb = _filter_competition_history(scarce_pool, comp_policy="exclude_friendlies_fallback", target_league_id=39, target_stratum="DOMESTIC_LEAGUE")
        c2_st = _filter_competition_history(scarce_pool, comp_policy="exclude_friendlies_strict", target_league_id=39, target_stratum="DOMESTIC_LEAGUE")
        self.assertEqual(len(c1_fb), 3)
        self.assertEqual(len(c2_st), 2)

        # C3 excluye copa doméstica solo si objetivo es liga doméstica y hay >= 6 partidos de liga
        c3_league = _filter_competition_history(pool, comp_policy="restrict_cups_league_ge6", target_league_id=39, target_stratum="DOMESTIC_LEAGUE")
        c3_cup = _filter_competition_history(pool, comp_policy="restrict_cups_league_ge6", target_league_id=45, target_stratum="DOMESTIC_CUP")
        self.assertNotIn(cup, c3_league)
        self.assertIn(cup, c3_cup)

    def test_venue_shrunk_anchor_handles_zero_venue_history_safely(self) -> None:
        away_only = tuple(
            _mk_match(
                i, 1, 10 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00",
                venue="AWAY", goals_for=1.0, goals_ag=1.0,
            )
            for i in range(1, 7)
        )
        opp_home_only = tuple(
            _mk_match(
                100 + i, 2, 20 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00",
                venue="HOME", goals_for=1.0, goals_ag=1.0,
            )
            for i in range(1, 7)
        )
        b4 = VariantSpec("B4_venue_shrunk_70_k4", "B_VENUE", "v", venue_mode="shrunk_anchor_70", venue_blend=0.70, venue_k=4.0)
        res = _estimate_side_rate_variant(
            away_only, opp_home_only,
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=1.56, other_venue_prior_mean=1.26,
            history_limit=20, spec=b4,
        )
        self.assertGreater(res["lambda"], 1.0)
        self.assertLess(res["lambda"], 2.0)


if __name__ == "__main__":
    unittest.main()
