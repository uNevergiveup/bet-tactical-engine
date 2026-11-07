import tempfile
import unittest
from pathlib import Path
import prematch_phase1 as f
import prematch_phase2 as p
from test_prematch_phase2_player_integrity import target, setup_history, history, write_cache, player, CUT
import json


class LongitudinalTests(unittest.TestCase):
    def test_real_enriched_cache_accumulates_and_replays_identically(self):
        root = Path(__file__).resolve().parent
        pre = root / "outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json"
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td, patch("socket.socket.connect", side_effect=AssertionError("network")):
            out = Path(td)
            p.run(pre, out, [1550188], CUT, cache_root=root / "outputs/.api_football_history_cache")
            first = (out / "player_patterns_1550188.json").read_bytes()
            doc = json.loads(first)
            self.assertTrue(sum(x["appearances"] >= 5 for x in doc["patterns"]) >= 3)
            self.assertEqual(p.validate_player_artifact(doc)["status"], "PASS")
            p.run(pre, out, [1550188], CUT, cache_root=root / "outputs/.api_football_history_cache")
            self.assertEqual(first, (out / "player_patterns_1550188.json").read_bytes())

    def test_five_games_partial_minutes_and_missing_metric(self):
        rows = [{"player_id": 7, "team_id": 1, "fixture_id": i, "minutes": minutes,
                 "kickoff_utc": f"2026-10-{i+10}T12:00:00Z", "appeared": minutes > 0,
                 "metrics": {"minutes": minutes, "shots": shots}}
                for i, (minutes, shots) in enumerate([(90, 2), (20, 1), (0, 0), (80, None), (90, 3)])]
        pl = p.build_player_patterns(target(), rows, {})["patterns"][0]
        self.assertEqual(pl["appearances"], 4)
        self.assertEqual(pl["minutes_observed"], 280)
        self.assertEqual(pl["metrics"]["shots"]["eligible_minutes"], 200)
        self.assertEqual(pl["metrics"]["shots"]["per90"], 2.7)
        self.assertNotEqual(pl["evidence_status"], "SUPPORTED_PATTERN")

    def test_budget_exhaustion_and_resume_use_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m)
            write_cache(root, "fixtures/players", "fixture=10", [])
            pre = root / "pre.json"; pre.write_text(json.dumps({"matches": [m]}))
            transport = lambda *args: (200, {}, {"response": [{"team": {"id": 1}, "players": [player()]}]})
            client = f.Phase1ApiClient(root, offline=False, max_new_calls=1, key="simulated", transport=transport)
            report = f.collect_longitudinal(client, pre, 100, CUT, root / "out", 5)
            self.assertEqual(report["api_calls"], 1)
            again = f.Phase1ApiClient(root, offline=True)
            report = f.collect_longitudinal(again, pre, 100, CUT, root / "out2", 5)
            self.assertEqual(report["api_calls"], 0)


if __name__ == "__main__": unittest.main()
