import unittest
from datetime import datetime, timezone

import prematch_predictive_engine as engine


class TemporalGuardTests(unittest.TestCase):

    def test_future_results_excluded_valid_results_preserved(self):
        cutoff = datetime(2026, 11, 7, 22, 55, tzinfo=timezone.utc)

        def observation(fid, kickoff, available):
            return {
                "fixture_id": fid,
                "team_id": 1,
                "league_id": 78,
                "season": 2026,
                "status": "FT",
                "kickoff_utc": kickoff,
                "result_available_at_utc": available,
                "venue": "HOME",
                "own": {"goals": 2},
                "conceded": {"goals": 1},
            }

        fixture = {
            "home_id": 1,
            "away_id": 2,
            "league_id": 78,
            "season": 2026,
            "kickoff_madrid": "2026-11-08T17:30:00+01:00",
            "analysis_cutoff_utc": cutoff.isoformat(),
            "statistical_observations": {
                "home": [
                    observation(100, "2026-11-07T16:00:00+00:00",
                                "2026-11-07T19:00:00+00:00"),
                    observation(200, "2026-11-07T22:30:00+00:00",
                                "2026-11-07T23:30:00+00:00"),
                ]
            },
        }

        rows = engine.available_observations(fixture, "home", cutoff)

        self.assertEqual([r["fixture_id"] for r in rows], [100])

        for block in ("own", "conceded"):
            values = engine.observed_values(
                rows, block, "goals", include_availability=True
            )
            self.assertEqual(len(values), 1)
            self.assertLessEqual(values[0][3], cutoff)


if __name__ == "__main__":
    unittest.main()
