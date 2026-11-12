import json
import tempfile
import unittest
from pathlib import Path

import prematch_integral_analysis_v1046 as mod


def row(fid, kickoff, venue="HOME", *, xg=1.0, shots=5.0, missing=False):
    own = {"xg": None if missing else xg, "shots": shots, "on_target": 2.0,
           "corners": 3.0, "fouls": 10.0, "yellow": 1.0, "red": 0.0, "saves": 1.0}
    conceded = {"xg": 0.7, "shots": 4.0, "on_target": 1.0, "corners": 2.0,
                "fouls": 11.0, "yellow": 2.0, "red": 0.0, "saves": 2.0}
    return {"fixture_id": fid, "kickoff_utc": kickoff, "venue": venue,
            "goals_for": 1, "goals_against": 0, "own": own, "conceded": conceded,
            "league_id": 140, "season": 2026, "opponent": {"id": 900 + fid, "name": "Rival"},
            "events": {}, "lineup": {"coach_name": "Coach"}}


def performance(rows_home, rows_away):
    return {"schema": "prematch-match-performance-1.0.4", "event_id_bwin": "7902458",
            "target_fixture_id": 999, "target_kickoff_utc": "2026-11-06T20:00:00+00:00",
            "analysis_cutoff_utc": "2026-11-06T14:00:00+00:00",
            "teams": {"home": {"team_id": 1, "team_name": "Sevilla", "historical_fixtures": rows_home},
                      "away": {"team_id": 2, "team_name": "Alaves", "historical_fixtures": rows_away}}}


class IntegralAnalysisTests(unittest.TestCase):
    def fixture_root(self):
        from prematch_test_fixtures import integral_fixture
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        return integral_fixture(Path(tmp.name))
    def test_temporal_guard_excludes_target_and_future_without_leakage(self):
        rows = [row(999, "2026-11-06T12:00:00+00:00"), row(1000, "2026-11-06T15:00:00+00:00"),
                row(1001, "2026-11-05T12:00:00+00:00", missing=True)]
        out = mod.build(performance(rows, rows))
        self.assertEqual(out["teams"]["home"]["observed_n"], 1)
        reasons = {x["reason"] for x in out["temporal_guard"]["teams"]["home"]["excluded"]}
        self.assertEqual(reasons, {"TARGET_FIXTURE", "POST_CUTOFF"})
        self.assertEqual(out["teams"]["home"]["windows"]["L5"]["metrics"]["xg_for"]["observed_n"], 0)
        self.assertIsNone(out["teams"]["home"]["windows"]["L5"]["metrics"]["xg_for"]["mean"])

    def test_venue_splits_and_denominators_preserve_null(self):
        rows = [row(1, "2026-11-05T12:00:00+00:00", "HOME", missing=True),
                row(2, "2026-11-04T12:00:00+00:00", "AWAY", xg=0.5)]
        out = mod.build(performance(rows, rows))
        split = out["teams"]["home"]["venue_splits"]
        self.assertEqual(split["HOME"]["observed_n"], 1)
        self.assertEqual(split["HOME"]["metrics"]["xg_for"]["missing_n"], 1)
        self.assertEqual(split["AWAY"]["metrics"]["xg_for"]["mean"], 0.5)

    def test_hypotheses_have_counterevidence_and_no_pick_fields(self):
        rows = [row(i, f"2026-11-{i:02d}T12:00:00+00:00") for i in range(1, 6)]
        out = mod.build(performance(rows, rows))
        self.assertTrue(out["matchup"]["hypotheses"])
        for item in out["matchup"]["hypotheses"]:
            self.assertIn(item["label"], {"CONFIRMADO", "PROBABLE", "INFERIDO", "NO_VERIFICADO"})
            self.assertIn("evidence_for", item)
            self.assertIn("evidence_against", item)
            self.assertIn("missing", item)
        self.assertFalse(out["odds_enabled"])
        self.assertNotIn("picks", out)
        self.assertNotIn("probabilities", out)

    def test_competition_scopes_and_concession_is_not_contrary_evidence(self):
        rows = [row(i, f"2026-11-{i:02d}T12:00:00+00:00") for i in range(1, 6)]
        rows[0]["league_id"], rows[0]["league_name"] = 667, "Friendlies Clubs"
        out = mod.build(performance(rows, rows))
        scopes = out["teams"]["home"]["competition_scopes"]
        self.assertEqual(scopes["friendlies"]["L5"]["observed_n"], 1)
        self.assertEqual(scopes["laliga_current"]["L5"]["observed_n"], 4)
        dimension = out["matchup"]["dimensions"][0]
        self.assertTrue(any("contexto, no evidencia contraria" in x for x in dimension["evidence"]["favorable"]))
        self.assertTrue(all("concesión rival" not in x for x in dimension["evidence"]["contrary"]))

    def test_halves_and_target_availability_are_explicit_and_deduplicated(self):
        rows = [row(1, "2026-11-05T12:00:00+00:00")]
        context = {"context": {
            "home_history_deep": {"detailed_fixtures": [{
                "fixture": {"id": 1, "date": "2026-11-05T12:00:00+00:00"},
                "statistics_halves": [{"team": {"id": 1}, "statistics_1h": [{"type": "Total Shots", "value": 4}], "statistics_2h": [{"type": "Total Shots", "value": 6}]}]
            }]},
            "away_history_deep": {"detailed_fixtures": []},
            "injuries": {"response": [
                {"fixture": {"id": 999}, "team": {"id": 1, "name": "Home"}, "player": {"id": 7, "name": "P", "reason": "Injury", "type": "Questionable"}},
                {"fixture": {"id": 999}, "team": {"id": 1, "name": "Home"}, "player": {"id": 7, "name": "P", "reason": "Injury", "type": "Questionable"}},
            ]}
        }}
        out = mod.build(performance(rows, rows), context=context)
        self.assertEqual(out["teams"]["home"]["period_analysis"]["status"], "UTILIZABLE_PARTIAL")
        self.assertEqual(out["teams"]["home"]["period_analysis"]["periods"]["first_half"]["metrics"]["shots"]["mean"], 4.0)
        self.assertEqual(len(out["target_availability"]["home"]), 1)

    def test_sos_hash_is_authentic_in_offline_run(self):
        import hashlib
        root = self.fixture_root()
        out_p, _ = mod.run("7902458", root)
        data = json.loads(out_p.read_text())
        sos_path = Path(data["source_files"]["sos"])
        self.assertEqual(data["sos"]["source_sha256"], hashlib.sha256(sos_path.read_bytes()).hexdigest())

    def test_offline_integration_sevilla_alaves(self):
        root = self.fixture_root()
        out_p, md_p = mod.run("7902458", root)
        self.assertTrue(out_p.exists())
        self.assertTrue(md_p.exists())
        data = json.loads(out_p.read_text())
        self.assertEqual(data["target_fixture_id"], 1570450)
        self.assertEqual(data["teams"]["home"]["name"], "Sevilla")
        self.assertEqual(data["teams"]["away"]["name"], "Alaves")
        self.assertFalse(data["odds_enabled"])
        self.assertEqual(data["sos"]["source_schema"], "prematch-sos-1.0.4.4")


if __name__ == "__main__":
    unittest.main()
