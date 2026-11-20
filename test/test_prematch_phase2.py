import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

import prematch_phase2


def fixture(n=5, post=False):
    cutoff = datetime(2026, 11, 7, 10, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        at = cutoff - timedelta(days=i + 1)
        rows.append({
            "fixture_id": 9000 + i, "team_id": 1, "kickoff_utc": at.isoformat(),
            "league_id": 99, "season": 2026, "status": "FT", "venue": "HOME",
            "statistics_requested": True, "statistics_request_status": "VALID",
            "own": {"goals": 1, "corners": 5, "shots": 10, "on_target": 4, "fouls": 8, "yellow": 2, "offsides": 1, "xg": 1.2},
            "conceded": {"goals": 0, "corners": 3, "shots": 7, "on_target": 2, "fouls": 10, "yellow": 1, "offsides": 1, "xg": 0.7},
        })
    other = []
    for i in range(n):
        at = cutoff - timedelta(days=i + 2)
        other.append({**rows[i], "fixture_id": 9100 + i, "team_id": 2, "venue": "AWAY", "kickoff_utc": at.isoformat()})
    if post:
        rows.append({**rows[0], "fixture_id": 9999, "kickoff_utc": (cutoff + timedelta(hours=1)).isoformat()})
    return {"fixture_id": 1550188, "home": "A", "away": "B", "home_id": 1, "away_id": 2,
            "kickoff_madrid": "2026-11-07T15:00:00+01:00", "analysis_cutoff_utc": cutoff.isoformat(),
            "league_id": 99, "season": 2026, "gender": "M", "statistical_observations": {"home": rows, "away": other},
            "market_identity_ready": False}


class Phase2Tests(unittest.TestCase):
    def test_offline_status_and_null_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            pre = Path(td) / "pre.json"; out = Path(td) / "out"
            pre.write_text(json.dumps({"generated_at_utc": "2026-11-07T10:00:00+00:00", "matches": [fixture(3)]}))
            result = prematch_phase2.run(pre, out)
            self.assertEqual(result["status"], "FASE_2_READY_FOR_TEST")
            self.assertEqual(result["api_calls"], 0)
            doc = json.loads((out / "patterns_1550188.json").read_text())
            self.assertTrue(any(p["phase2_status"] == "INSUFFICIENT_SAMPLE" for p in doc["patterns"]))
            self.assertFalse(doc["market_identity_ready"])

    def test_post_cutoff_is_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            pre = Path(td) / "pre.json"; out = Path(td) / "out"
            pre.write_text(json.dumps({"generated_at_utc": "2026-11-07T10:00:00+00:00", "matches": [fixture(5, True)]}))
            prematch_phase2.run(pre, out)
            doc = json.loads((out / "patterns_1550188.json").read_text())
            ids = {str(fid) for p in doc["patterns"] for fid in p["evidence_fixture_ids"]["own"]}
            self.assertNotIn("9999", ids)

    def test_fixture_filter_and_no_network_flag(self):
        with tempfile.TemporaryDirectory() as td:
            pre = Path(td) / "pre.json"; out = Path(td) / "out"
            pre.write_text(json.dumps({"generated_at_utc": "2026-11-07T10:00:00+00:00", "matches": [fixture()]}))
            with self.assertRaises(ValueError):
                prematch_phase2.run(pre, out, [123])
            with self.assertRaises(SystemExit):
                prematch_phase2.main(["--preselection", str(pre), "--output-dir", str(out), "--online"])

    def test_player_cache_is_normalized_without_cross_team_leakage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); cache = root / "fixtures_players"; cache.mkdir()
            payload = {"query": "fixture=9000", "response": [
                {"team": {"id": 1, "name": "A"}, "players": [{"player": {"id": 7, "name": "P"}, "statistics": [{"games": {"minutes": 20, "substitute": True, "position": "F"}, "shots": {"total": 2, "on": 1}, "goals": {"total": 0, "assists": 0}}]}]},
                {"team": {"id": 2, "name": "B"}, "players": [{"player": {"id": 99, "name": "Other"}, "statistics": [{"games": {"minutes": 90, "substitute": False}, "shots": {"total": 9}}]}]},
            ]}
            (cache / "x.json").write_text(json.dumps(payload))
            m = fixture(1)
            # El contrato corregido exige identidad del histórico y alineación;
            # games.substitute por sí solo no verifica una entrada de suplente.
            from test_prematch_phase2_player_integrity import write_cache
            m["statistical_observations"]["home"][0]["opponent_id"] = 3
            write_cache(root, "fixtures", "id=9000", [{"fixture": {"id": 9000,
                "date": m["statistical_observations"]["home"][0]["kickoff_utc"], "status": {"short": "FT"}},
                "league": {"id": 99, "season": 2026}, "teams": {"home": {"id": 1}, "away": {"id": 3}}}])
            write_cache(root, "fixtures/lineups", "fixture=9000", [{"team": {"id": 1},
                "startXI": [{"player": {"id": x}} for x in range(10, 21)], "substitutes": [{"player": {"id": 7}}]}])
            obs, cov = prematch_phase2.collect_player_observations(m, root, datetime(2026, 11, 7, 10, tzinfo=timezone.utc))
            self.assertEqual(cov["9000"], "AVAILABLE")
            self.assertNotIn(99, {r["player_id"] for r in obs})
            self.assertEqual(sum(r["player_id"] == 7 for r in obs), 1)
            actual = next(r for r in obs if r["player_id"] == 7)
            self.assertTrue(actual["substitute"])
            self.assertEqual(actual["metrics"]["shots"], 2)

    def test_player_patterns_require_matches_and_valid_minutes(self):
        m = fixture(3)
        rows = [{"player_id": 7, "player_name": "P", "team_id": 1, "team": "A", "fixture_id": i,
                 "kickoff_utc": f"2026-11-0{i}T10:00:00+00:00", "venue": "HOME", "minutes": 90,
                 "starter": True, "substitute": False, "metrics": {"shots": 2, "minutes": 90}}
                for i in range(1, 4)]
        doc = prematch_phase2.build_player_patterns(m, rows, {str(i): "AVAILABLE" for i in range(1, 4)}, 3, 180)
        self.assertEqual(doc["patterns"][0]["evidence_status"], "WEAK_PATTERN")
        self.assertEqual(doc["patterns"][0]["metrics"]["shots"]["per90"], 2.0)

    def test_player_missing_cache_is_explicit_and_post_cutoff_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "fixtures_players").mkdir()
            m = fixture(2, True)
            obs, cov = prematch_phase2.collect_player_observations(m, root, datetime(2026, 11, 7, 10, tzinfo=timezone.utc))
            self.assertEqual(obs, [])
            self.assertIn("NOT_REQUESTED", cov.values())


if __name__ == "__main__":
    unittest.main()
