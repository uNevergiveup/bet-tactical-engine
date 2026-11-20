"""Non-network regression tests for PREMATCH V1.0.4 Phase 2."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import match_performance_engine as perf
import team_form_engine as form
import prematch_depth_analysis as depth
import prematch_depth_collector as col
import prematch_player_evidence as player
import prematch_depth_v1042 as cli


class FakeHTTPResponse:
    def __init__(self, payload):
        self.headers = {}
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Path(__file__).resolve().parent / "fixtures" / "match_engine_7896590.json"
        if not cls.fixture.exists():
            cls.fixture = Path("outputs/matches/7896590/inputs/match_engine_7896590.json")
        if not cls.fixture.exists():
            raise unittest.SkipTest("Se necesita el snapshot Lens–Como 7896590 para estas pruebas; no se descarga")
        cls.raw = cls.fixture.read_bytes()
        cls.source = json.loads(cls.raw)
        cls.p = perf.build(cls.source, input_sha256=hashlib.sha256(cls.raw).hexdigest())
        cls.f = form.build(cls.p, source_sha256="test-hash")

    def test_identity_and_snapshot_cutoff(self):
        self.assertEqual(self.p["target_fixture_id"], 1635681)
        self.assertEqual(self.p["event_id_bwin"], "7896590")
        self.assertEqual(self.p["teams"]["home"]["valid_count"], 6)
        self.assertEqual(self.p["teams"]["away"]["valid_count"], 6)
        self.assertTrue(perf.aware(self.p["analysis_cutoff_utc"], "cutoff") < perf.aware(self.p["target_kickoff_utc"], "kickoff"))

    def test_jackknife_and_incomplete_windows(self):
        d = depth.build(self.p, self.f)
        self.assertFalse(d["teams"]["home"]["coverage"]["L10"])
        self.assertEqual(d["teams"]["home"]["robustness"]["shots_for"]["status"], "EXPLORATORIA_N3_VS_N3")
        self.assertTrue(any(r["fixture_id"] == 1635679 for r in d["teams"]["home"]["xg_advantage_quality"]))
        self.assertEqual(d["matchup"]["away"]["markets"][0]["production_L5"]["mean"], 15.6)

    def test_metric_missing_is_not_zero(self):
        rows = copy.deepcopy(self.p["teams"]["home"]["historical_fixtures"])
        rows[0]["own"]["shots"] = None
        self.assertEqual(depth.avg(rows[:5], "shots_for")["n"], 4)
        self.assertEqual(depth.trend_robustness(rows, "shots_for")["status"], "DATOS_INCOMPLETOS")

    def test_player_nico_paz(self):
        j = player.build(self.source, self.p)
        paz = next(p for p in j["teams"]["away"]["players"] if p["name"] == "N. Paz")
        self.assertEqual(paz["metrics"]["shots_total"]["observed_n"], 5)
        self.assertEqual(paz["metrics"]["shots_total"]["total"], 19.0)
        self.assertGreater(paz["metrics"]["shots_total"]["per90"], 4)

    def test_online_replay_rejected_without_key(self):
        args = SimpleNamespace(input=self.fixture, online=True, output_dir=Path(tempfile.gettempdir()) / "depth_test",
                               cache_dir=Path(tempfile.gettempdir()) / "depth_cache", history=20,
                               max_api_calls=110, include_players=False, include_halves=False, as_of=None)
        with self.assertRaisesRegex(ValueError, "ONLINE_PREMATCH_ONLY"):
            cli.run(args)

    def test_target_not_inserted_from_retrieval(self):
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=1)
        row = self.source["context"]["home_history_deep"]["detailed_fixtures"][0]
        self.assertTrue(col.safe_fixture(row, 116, 1635681, cutoff))
        target = copy.deepcopy(row)
        target["fixture"]["id"] = 1635681
        self.assertFalse(col.safe_fixture(target, 116, 1635681, cutoff))
        target["fixture"]["id"] = 9999
        target["fixture"]["date"] = (cutoff - timedelta(hours=1)).isoformat()
        self.assertFalse(col.safe_fixture(target, 116, 1635681, cutoff))

    def test_offline_does_not_modify_input(self):
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(input=self.fixture, output_dir=Path(td), online=False,
                                   cache_dir=Path(td) / "cache", history=20,
                                   max_api_calls=110, include_players=False, include_halves=False, as_of=None)
            before = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
            cli.run(args)
            self.assertEqual(before, hashlib.sha256(self.fixture.read_bytes()).hexdigest())
            self.assertFalse(list((Path(td) / "cache").glob("**/*")))
            self.assertTrue((Path(td) / "jugadores_historicos_7896590.json").exists())

    def test_fake_online_incremental_to_20_without_network(self):
        current = datetime.now(timezone.utc)
        template = copy.deepcopy(self.source)
        template["generated_at"] = (current - timedelta(minutes=5)).isoformat()
        template["fixture"]["date"] = (current + timedelta(hours=4)).isoformat()
        catalog = {}
        for side in ("home", "away"):
            tid = template["fixture"][f"{side}_id"]
            sample = template["context"][f"{side}_history_deep"]["detailed_fixtures"]
            rows = copy.deepcopy(sample)
            for i in range(14):
                r = copy.deepcopy(sample[-1])
                r["fixture"]["id"] = 9000000 + 1000 * (side == "away") + i
                r["fixture"]["date"] = (datetime(2026, 9, 30, tzinfo=timezone.utc) - timedelta(days=i)).isoformat()
                rows.append(r)
            catalog[tid] = rows
        class FakeClient:
            stat = SimpleNamespace(calls=0, hits=0, max_calls=100)
            def get(self, endpoint, **kwargs):
                if endpoint == "fixtures" and "team" in kwargs:
                    return catalog[kwargs["team"]]
                if endpoint == "fixtures" and "ids" in kwargs:
                    requested = {int(x) for x in kwargs["ids"].split("-")}
                    return [r for data in catalog.values() for r in data if r["fixture"]["id"] in requested]
                return []
        data, report = col.enrich(template, FakeClient(), history=20)
        self.assertEqual(len(data["context"]["home_history_deep"]["detailed_fixtures"]), 20)
        self.assertEqual(len(data["context"]["away_history_deep"]["detailed_fixtures"]), 20)
        p = perf.build(data, input_sha256="synthetic")
        self.assertEqual(p["teams"]["home"]["valid_count"], 20)
        self.assertEqual(p["teams"]["away"]["valid_count"], 20)
        self.assertEqual(report["source_kind"], "API_FOOTBALL_LIVE_COLLECTED_PRE_KICKOFF")

    def test_statistics_empty_cache_is_negative_and_deduplicates_immediate_retry(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            response = FakeHTTPResponse({"response": []})
            client = col.ApiClient(cache_dir, max_calls=3, key="test-key")
            with patch("prematch_depth_collector.urlopen", return_value=response) as network:
                self.assertEqual(client.get("fixtures/statistics", fixture=123), [])
                self.assertEqual(client.get("fixtures/statistics", fixture=123), [])
            self.assertEqual(network.call_count, 1)
            self.assertEqual(client.stat.hits, 1)
            cache_files = list((cache_dir / "fixtures_statistics").glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            self.assertEqual(json.loads(cache_files[0].read_text())["cache_kind"], "negative")

    def test_expired_negative_statistics_cache_recovers_and_becomes_positive(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            empty = FakeHTTPResponse({"response": []})
            client = col.ApiClient(cache_dir, max_calls=3, key="test-key")
            with patch("prematch_depth_collector.urlopen", return_value=empty):
                client.get("fixtures/statistics", fixture=456)
            cache_file = next((cache_dir / "fixtures_statistics").glob("*.json"))
            document = json.loads(cache_file.read_text())
            document["fetched_at_utc"] = (datetime.now(timezone.utc) - col.NEGATIVE_STATISTICS_CACHE_TTL - timedelta(seconds=1)).isoformat()
            cache_file.write_text(json.dumps(document))
            payload = {"response": [{"team": {"id": 1}, "statistics": [{"type": "Total Shots", "value": 7}]}]}
            positive = FakeHTTPResponse(payload)
            recovering = col.ApiClient(cache_dir, max_calls=3, key="test-key")
            with patch("prematch_depth_collector.urlopen", return_value=positive) as network:
                self.assertEqual(recovering.get("fixtures/statistics", fixture=456), payload["response"])
            self.assertEqual(network.call_count, 1)
            self.assertEqual(json.loads(cache_file.read_text())["cache_kind"], "positive")

    def test_positive_statistics_cache_keeps_long_ttl(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            client = col.ApiClient(cache_dir, max_calls=2, key="test-key")
            payload = {"response": [{"team": {"id": 1}, "statistics": [{"type": "Total Shots", "value": 1}]}]}
            response = FakeHTTPResponse(payload)
            with patch("prematch_depth_collector.urlopen", return_value=response):
                client.get("fixtures/statistics", fixture=789)
            cache_file = next((cache_dir / "fixtures_statistics").glob("*.json"))
            document = json.loads(cache_file.read_text())
            document["fetched_at_utc"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            cache_file.write_text(json.dumps(document))
            cached = col.ApiClient(cache_dir, max_calls=1, key="test-key")
            with patch("prematch_depth_collector.urlopen") as network:
                self.assertEqual(cached.get("fixtures/statistics", fixture=789), payload["response"])
            network.assert_not_called()
            self.assertEqual(cached.stat.hits, 1)

    def test_expired_negative_cache_respects_budget(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            client = col.ApiClient(cache_dir, max_calls=1, key="test-key")
            response = FakeHTTPResponse({"response": []})
            with patch("prematch_depth_collector.urlopen", return_value=response):
                client.get("fixtures/statistics", fixture=321)
            cache_file = next((cache_dir / "fixtures_statistics").glob("*.json"))
            document = json.loads(cache_file.read_text())
            document["fetched_at_utc"] = (datetime.now(timezone.utc) - col.NEGATIVE_STATISTICS_CACHE_TTL - timedelta(seconds=1)).isoformat()
            cache_file.write_text(json.dumps(document))
            with self.assertRaises(col.BudgetExhausted):
                client.get("fixtures/statistics", fixture=321)
    def test_synthetic_20_games_and_robustness(self):
        base = copy.deepcopy(self.p["teams"]["away"]["historical_fixtures"][0])
        rows = []
        for i in range(20):
            r = copy.deepcopy(base)
            r["fixture_id"] = 4000000 + i
            r["own"]["shots"] = 20 if i < 5 else 10
            r["kickoff_utc"] = (datetime(2026, 10, 20, tzinfo=timezone.utc) - timedelta(days=i)).isoformat()
            rows.append(r)
        t = depth.trend_robustness(rows, "shots_for")
        self.assertEqual(t["status"], "COMPARACION_L5_VS_L5")
        self.assertEqual(t["recent"]["mean"], 20)
        self.assertEqual(t["previous"]["mean"], 10)
        self.assertFalse(t["sensitive_to_single_match"])


if __name__ == "__main__":
    unittest.main()
