"""Regresiones de las incidencias reales de Fase 2.1; todo es offline."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import prematch_phase2 as p


CUT = datetime(2026, 11, 7, 0, 35, 29, 242401, tzinfo=timezone.utc)


def target():
    return {"fixture_id": 100, "home_id": 1, "away_id": 2, "home": "A", "away": "B",
            "kickoff_madrid": "2026-11-07T15:00:00+01:00", "analysis_cutoff_utc": CUT.isoformat(),
            "league_id": 99, "season": 2026, "gender": "M", "statistical_observations": {"home": [], "away": []}}


def history(fid=10, tid=1, venue="AWAY", kickoff="2026-11-01T14:00:00Z"):
    return {"fixture_id": fid, "team_id": tid, "opponent_id": 3, "venue": venue,
            "kickoff_utc": kickoff, "status": "FT", "league_id": 99, "season": 2026}


def player(pid=7, minutes=90, shots=2):
    return {"player": {"id": pid, "name": "Same name"}, "statistics": [{
        "games": {"minutes": minutes, "substitute": False, "position": "F"},
        "shots": {"total": shots, "on": 0}, "passes": {"total": 3}, "duels": {"won": 2}}]}


def write_cache(root, endpoint, query, response, fetched="2026-11-07T12:57:52Z", suffix=""):
    folder = root / endpoint.replace("/", "_"); folder.mkdir(exist_ok=True)
    path = folder / (hashlib.sha256((query + suffix).encode()).hexdigest()[:24] + ".json")
    path.write_text(json.dumps({"endpoint": endpoint, "query": query, "response": response,
                                "fetched_at_utc": fetched}))
    return path


def setup_history(root, match, row=None, players=None, bench=None, starters=None, lineup=True):
    row = row or history(); fid = row["fixture_id"]; tid = row["team_id"]
    side = "home" if tid == match["home_id"] else "away"
    match["statistical_observations"][side].append(row)
    ids = {"home": 3, "away": tid} if row["venue"] == "AWAY" else {"home": tid, "away": 3}
    fixture = {"fixture": {"id": fid, "date": row["kickoff_utc"], "status": {"short": "FT", "elapsed": 90}},
               "teams": {k: {"id": v} for k, v in ids.items()}, "league": {"id": 99, "season": 2026}}
    write_cache(root, "fixtures", f"id={fid}", [fixture])
    write_cache(root, "fixtures/players", f"fixture={fid}", [{"team": {"id": tid, "name": "A"}, "players": players if players is not None else [player()]}])
    if lineup:
        st = starters if starters is not None else list(range(7, 18))
        bn = bench or []
        write_cache(root, "fixtures/lineups", f"fixture={fid}", [{"team": {"id": tid},
                    "startXI": [{"player": {"id": x}} for x in st],
                    "substitutes": [{"player": {"id": x}} for x in bn]}])


class PlayerIntegrityTests(unittest.TestCase):
    def test_zero_missing_partial_metrics_have_separate_denominators(self):
        rows = [{"player_id": 7, "team_id": 1, "fixture_id": i, "minutes": 90,
                 "metrics": {"shots": shots, "minutes": 90}, "appeared": True}
                for i, shots in enumerate([0, None, 4])]
        pl = p.build_player_patterns(target(), rows, {}, 3, 180)["patterns"][0]
        metric = pl["metrics"]["shots"]
        self.assertEqual((metric["n"], metric["null_count"], metric["mean_per_match"]), (2, 1, 2))
        self.assertEqual(metric["eligible_minutes"], 180)
        self.assertEqual(metric["per90_quality_status"], "USABLE")
        self.assertEqual(metric["evidence_status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(pl["metrics"]["tackles"]["per90_quality_status"], "INSUFFICIENT_COVERAGE")

    def test_invalid_minutes_and_impossible_shots_preserved_but_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            entries = [player(7, -1), player(8, 91), player(9, None), player(10, 90, 1)]
            entries[-1]["statistics"][0]["shots"]["on"] = 3
            setup_history(root, m, players=entries)
            obs, _ = p.collect_player_observations(m, root, CUT)
            byid = {x["player_id"]: x for x in obs}
            self.assertEqual(byid[7]["raw_minutes"], -1)
            self.assertIsNone(byid[7]["minutes"])
            self.assertIn("INVALID_MINUTES", byid[8]["data_quality_flags"])
            self.assertIsNone(byid[9]["appeared"])
            self.assertEqual(byid[10]["metrics"]["shots_on_target"], 3)
            metric = next(pl for pl in p.build_player_patterns(m, obs, {})["patterns"] if pl["player_id"] == 10)["metrics"]["shots"]
            self.assertEqual(metric["n"], 0)
            self.assertEqual(metric["invalid_count"], 1)

    def test_no_statistics_player_is_retained_with_unknown_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m, players=[{"player": {"id": 7, "name": "P"}, "statistics": []}])
            obs, _ = p.collect_player_observations(m, root, CUT)
            row = next(x for x in obs if x["player_id"] == 7)
            self.assertTrue(row["starter"])
            self.assertIsNone(row["minutes"])
            self.assertFalse(row["performance_eligible"])
            self.assertTrue(all(v is None for v in row["metrics"].values()))

    def test_empty_partial_and_not_requested_are_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            write_cache(root, "fixtures/players", "fixture=10", [])
            obs, cov = p.collect_player_observations(m, root, CUT)
            self.assertEqual((obs, cov["10"]), ([], "EMPTY"))
            write_cache(root, "fixtures/players", "fixture=10", [{"team": {"id": 3}, "players": [player()]}])
            obs, cov = p.collect_player_observations(m, root, CUT)
            self.assertEqual((obs, cov["10"]), ([], "INSUFFICIENT_COVERAGE"))

    def test_competition_kickoff_opponent_and_venue_conflicts_block(self):
        for field, wrong in [("league_id", 100), ("season", 2025), ("venue", "HOME"), ("opponent_id", 9), ("kickoff_utc", "2026-11-02T14:00:00Z")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                root = Path(td); m = target(); setup_history(root, m)
                m["statistical_observations"]["home"][0][field] = wrong
                audit = {}; obs, _ = p.collect_player_observations(m, root, CUT, audit)
                self.assertEqual(obs, [])
                self.assertEqual(audit["rejected_history"][0]["reason"], "FIXTURE_IDENTITY_CONFLICT")

    def test_homonyms_and_transferred_players_not_combined(self):
        rows = [{"player_id": pid, "team_id": tid, "fixture_id": i, "minutes": 90,
                 "player_name": "Same name", "metrics": {"shots": 2}, "appeared": True}
                for i, (pid, tid) in enumerate([(7, 1), (8, 1), (7, 2)])]
        patterns = p.build_player_patterns(target(), rows, {})["patterns"]
        self.assertEqual(len(patterns), 3)
        self.assertTrue(all(x["matches_observed"] == 1 for x in patterns))

    def test_duplicate_normalized_observations_are_rejected_by_aggregator(self):
        row = {"player_id": 7, "team_id": 1, "fixture_id": 10, "minutes": 90, "metrics": {"shots": 2}}
        with self.assertRaisesRegex(ValueError, "DUPLICATE_PLAYER_OBSERVATION"):
            p.build_player_patterns(target(), [row, row], {})

    def test_shared_fixture_keeps_both_team_perspectives(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            a = history(10, 1, "HOME"); a["opponent_id"] = 2
            b = history(10, 2, "AWAY"); b["opponent_id"] = 1
            m["statistical_observations"] = {"home": [a], "away": [b]}
            write_cache(root, "fixtures", "id=10", [{"fixture": {"id": 10, "date": a["kickoff_utc"], "status": {"short": "FT"}},
                "teams": {"home": {"id": 1}, "away": {"id": 2}}, "league": {"id": 99, "season": 2026}}])
            write_cache(root, "fixtures/players", "fixture=10", [{"team": {"id": 1}, "players": [player(7)]}, {"team": {"id": 2}, "players": [player(8)]}])
            obs, _ = p.collect_player_observations(m, root, CUT)
            self.assertEqual({(x["team_id"], x["player_id"]) for x in obs}, {(1, 7), (2, 8)})
            write_cache(root, "fixtures/players", "fixture=10", [{"team": {"id": 1}, "players": [player(7)]}, {"team": {"id": 2}, "players": [player(7)]}])
            audit = {}; obs, cov = p.collect_player_observations(m, root, CUT, audit)
            self.assertTrue(all(not x["performance_eligible"] for x in obs))
            self.assertTrue(all(c["performance_eligible"] == 0 for c in audit["fixture_checks"]))
            self.assertEqual(cov["10"], "INSUFFICIENT_COVERAGE")

    def test_26_starters_or_lineup_overlap_do_not_create_confirmed_starts(self):
        for starters, bench in [(list(range(7, 33)), []), (list(range(7, 18)), [7])]:
            with self.subTest(starters=len(starters)), tempfile.TemporaryDirectory() as td:
                root = Path(td); m = target(); setup_history(root, m, starters=starters, bench=bench)
                obs, _ = p.collect_player_observations(m, root, CUT)
                self.assertTrue(all(r["starter"] is None for r in obs))

    def test_target_future_and_live_history_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m)
            for fid, status, ko in [(100, "FT", "2026-10-01T10:00:00Z"), (11, "2H", "2026-11-07T00:00:00Z"), (12, "FT", "2026-11-08T00:00:00Z")]:
                row = history(fid=fid, kickoff=ko); row["status"] = status
                m["statistical_observations"]["home"].append(row)
            audit = {}; obs, _ = p.collect_player_observations(m, root, CUT, audit)
            self.assertEqual({x["fixture_id"] for x in obs}, {10})
            self.assertEqual(len(audit["rejected_history"]), 3)

    def test_utc_madrid_cet_cest_and_fold_are_same_instants(self):
        self.assertEqual(p.asof_iso("2026-11-07T00:35:29Z"), "2026-11-07T01:35:29+01:00")
        self.assertEqual(p.asof_iso("2026-07-07T23:30:00Z"), "2026-07-08T01:30:00+02:00")
        self.assertEqual(p._dt("2026-10-25T02:30:00+02:00"), p._dt("2026-10-25T00:30:00Z"))
        self.assertNotEqual(p._dt("2026-10-25T02:30:00+02:00"), p._dt("2026-10-25T02:30:00+01:00"))

    def test_minimum_player_thresholds_cannot_promote_two_games(self):
        for matches, minutes in [(2, 180), (3, 0), (0, 0)]:
            with self.assertRaises(ValueError):
                p.build_player_patterns(target(), [], {}, matches, minutes)

    def test_end_time_or_final_status_observed_at_cutoff_proves_completion(self):
        ko = "2026-11-06T22:30:00Z"
        fixture = {"date": ko, "status": {"short": "FT"}, "ended_at_utc": "2026-11-07T00:20:00Z"}
        self.assertEqual(p._completion(fixture, {}, CUT), "EXPLICIT_END_BEFORE_CUTOFF")
        fixture.pop("ended_at_utc")
        self.assertEqual(p._completion(fixture, {"fetched_at_utc": "2026-11-07T00:30:00Z"}, CUT), "FINAL_STATUS_OBSERVED_BY_CUTOFF")
        self.assertIsNone(p._completion(fixture, {"fetched_at_utc": "2026-11-07T12:00:00Z"}, CUT))
        fixture["status"]["short"] = "2H"
        self.assertIsNone(p._completion(fixture, {"fetched_at_utc": "2026-11-07T00:30:00Z"}, CUT))

    def test_fixture_query_and_identity_uniqueness_are_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            f, _ = p.PlayerCache(root).get("fixtures", 10, CUT)
            conflicting = json.loads(json.dumps(f)); conflicting["teams"]["home"]["id"] = 4
            write_cache(root, "fixtures", "ids=10", [conflicting])
            audit = {}; obs, _ = p.collect_player_observations(m, root, CUT, audit)
            self.assertEqual(obs, [])
            self.assertEqual(audit["rejected_history"][0]["reason"], "FIXTURE_IDENTITY_NOT_UNIQUE")

    def test_payload_and_response_disagreement_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            path = next((root / "fixtures_players").glob("*.json")); d = json.loads(path.read_text())
            raw = {"response": []}; d["raw_payload"] = raw
            d["payload_sha256"] = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            path.write_text(json.dumps(d))
            audit = {}; obs, _ = p.collect_player_observations(m, root, CUT, audit)
            self.assertEqual(obs, [])
            self.assertTrue(any(x["reason"] == "CACHE_RESPONSE_CONFLICT" for x in audit["cache_rejections"]))

    def test_wrong_fixture_in_cache_parameters_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            path = next((root / "fixtures_players").glob("*.json")); d = json.loads(path.read_text())
            d["parameters"] = {"fixture": "11"}; path.write_text(json.dumps(d))
            audit = {}; obs, _ = p.collect_player_observations(m, root, CUT, audit)
            self.assertEqual(obs, [])
            self.assertTrue(any(x["reason"] == "CACHE_PARAMETERS_CONFLICT" for x in audit["cache_rejections"]))

    def test_json_validation_rejects_tampered_denominator(self):
        root = Path(__file__).resolve().parent
        doc = json.loads((root / "outputs/phase2/1550188/player_patterns_1550188.json").read_text())
        self.assertEqual(p.validate_player_artifact(doc)["status"], "PASS")
        doc["patterns"][0]["metrics"]["shots"]["eligible_minutes"] += 1
        self.assertIn("METRIC_DENOMINATOR_MISMATCH", p.validate_player_artifact(doc)["errors"])

    def test_statistics_events_contradiction_is_metric_specific(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            write_cache(root, "fixtures/events", "fixture=10", [{"team": {"id": 1}, "player": {"id": 7},
                "type": "Card", "detail": "Yellow Card", "time": {"elapsed": 60}}])
            # Explicit zero differs from the card event; preserve both.
            ps = player(); ps["statistics"][0]["cards"] = {"yellow": 0, "red": 0}
            write_cache(root, "fixtures/players", "fixture=10", [{"team": {"id": 1}, "players": [ps]}])
            obs, _ = p.collect_player_observations(m, root, CUT)
            row = next(x for x in obs if x["player_id"] == 7)
            self.assertEqual(row["metrics"]["yellow"], 0)
            self.assertIn("yellow", row["metric_issues"])
            self.assertNotIn("shots", row["metric_issues"])

    def test_real_cache_pilot_is_idempotent_offline_and_preserves_team_patterns(self):
        root = Path(__file__).resolve().parent
        pre = root / "outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json"
        cache = root / "outputs/.api_football_history_cache"
        with tempfile.TemporaryDirectory() as td, patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), patch("subprocess.run", side_effect=AssertionError("scraper forbidden")):
            out = Path(td)
            # Freeze the original two-history regression even after enrichment.
            import shutil
            original = json.loads((root / "outputs/phase2/1550188/player_patterns_1550188.json").read_text())
            cache = out / "original_cache"
            refs = {ref["path"] for row in original["observations"] for ref in row["provenance"]}
            for name in refs:
                path = Path(name); folder = cache / path.parent.name; folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, folder / path.name)
            p.run(pre, out, [1550188], CUT, cache_root=cache)
            first = {f.name: f.read_bytes() for f in out.glob("*.json")}
            p.run(pre, out, [1550188], CUT, cache_root=cache)
            self.assertEqual(first, {f.name: f.read_bytes() for f in out.glob("*.json")})
            doc = json.loads((out / "player_patterns_1550188.json").read_text())
            self.assertEqual(doc["summary"]["confirmed_starts"], 22)
            self.assertEqual(doc["summary"]["appearances"], 32)
            self.assertEqual(doc["summary"]["bench_unused"], 20)
            self.assertEqual(doc["summary"]["by_status"], {"INSUFFICIENT_SAMPLE": 52})
            self.assertTrue(all(not x["per90_usable"] for pl in doc["patterns"] for x in pl["metrics"].values()))
            for row in doc["observations"]:
                for ref in row["provenance"]:
                    self.assertEqual(p._sha(Path(ref["path"])), ref["sha256"])
            for name in ["coverage_matrix.json", "pattern_discovery.json", "research_queue_sport.json"]:
                baseline = root / "outputs/phase2/1550188/archive_before_2_1_1" / name
                self.assertEqual(json.loads(first[name]), json.loads(baseline.read_text()))

    def test_lineup_overrides_false_substitute_in_realistic_response(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m, players=[player(7, 90), player(8, 64), player(20, 26), player(21, 0)],
                          bench=[20, 21])
            obs, _ = p.collect_player_observations(m, root, CUT)
            byid = {x["player_id"]: x for x in obs}
            self.assertEqual(byid[20]["participation_status"], "SUBSTITUTE_APPEARED")
            self.assertFalse(byid[20]["starter"])
            self.assertEqual(byid[21]["participation_status"], "BENCH_UNUSED")
            self.assertFalse(byid[21]["appeared"])
            self.assertTrue(byid[7]["starter"])
            self.assertTrue(byid[8]["starter"])

    def test_missing_lineup_never_invents_starter_from_minutes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m, players=[player(7, 90), player(8, None), player(9, 0)], lineup=False)
            obs, _ = p.collect_player_observations(m, root, CUT)
            self.assertTrue(all(x["starter"] is None for x in obs))
            self.assertEqual(next(x for x in obs if x["player_id"] == 8)["participation_status"], "UNKNOWN")

    def test_per90_uses_only_paired_metric_exposure(self):
        rows = []
        for fid, minutes, shots in [(10, 90, 2), (11, 90, None), (12, 0, 0)]:
            rows.append({"player_id": 7, "team_id": 1, "player_name": "P", "fixture_id": fid,
                         "kickoff_utc": f"2026-10-{fid}T12:00:00Z", "minutes": minutes,
                         "metrics": {"shots": shots, "minutes": minutes}, "appeared": minutes > 0,
                         "participation_status": "STARTER" if minutes else "BENCH_UNUSED"})
        doc = p.build_player_patterns(target(), rows, {}, 3, 180)
        pl = doc["patterns"][0]; metric = pl["metrics"]["shots"]
        self.assertEqual(metric["per90"], 2)
        self.assertEqual(metric["eligible_minutes"], 90)
        self.assertEqual(metric["n"], 1)
        self.assertEqual(pl["appearances"], 2)
        self.assertEqual(pl["records_observed"], 3)
        self.assertEqual(metric["per90_quality_status"], "LOW_EXPOSURE")

    def test_one_minute_is_calculable_but_not_usable(self):
        row = {"player_id": 7, "team_id": 1, "fixture_id": 10, "minutes": 1,
               "metrics": {"passes": 3, "minutes": 1}, "appeared": True}
        metric = p.build_player_patterns(target(), [row], {}, 3, 180)["patterns"][0]["metrics"]["passes"]
        self.assertEqual(metric["per90"], 270)
        self.assertFalse(metric["per90_usable"])
        self.assertEqual(metric["per90_quality_status"], "LOW_EXPOSURE")

    def test_temporal_uses_earliest_cutoff_and_completed_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m, history(kickoff="2026-11-07T00:00:00Z"))
            obs, _ = p.collect_player_observations(m, root, datetime(2026, 11, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(obs, [])

    def test_unknown_and_zero_minutes_do_not_inflate_sample(self):
        rows = [{"player_id": 7, "team_id": 1, "fixture_id": i, "minutes": value,
                 "metrics": {"shots": 0}, "appeared": True if value else None}
                for i, value in enumerate([0, 0, None])]
        pl = p.build_player_patterns(target(), rows, {}, 3, 180)["patterns"][0]
        self.assertEqual(pl["appearances"], 0)
        self.assertEqual(pl["metrics"]["shots"]["n"], 0)
        self.assertEqual(pl["evidence_status"], "INSUFFICIENT_SAMPLE")

    def test_retrospective_provenance_and_actual_venue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            obs, _ = p.collect_player_observations(m, root, CUT)
            r = obs[0]
            self.assertEqual(r["target_side"], "HOME")
            self.assertEqual(r["venue"], "AWAY")
            self.assertEqual(r["availability_status"], "RETROSPECTIVE_RECONSTRUCTION")
            self.assertEqual(len(r["provenance"][0]["sha256"]), 64)

    def test_duplicate_records_not_summed_and_conflicts_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target()
            setup_history(root, m, players=[player(), player()])
            obs, _ = p.collect_player_observations(m, root, CUT)
            self.assertEqual(sum(x["player_id"] == 7 for x in obs), 1)
            self.assertEqual(next(x for x in obs if x["player_id"] == 7)["metrics"]["shots"], 2)
            setup = [{"team": {"id": 1}, "players": [player(), player(shots=4)]}]
            write_cache(root, "fixtures/players", "fixture=10", setup)
            obs, _ = p.collect_player_observations(m, root, CUT)
            self.assertFalse(next(x for x in obs if x["player_id"] == 7)["performance_eligible"])

    def test_corrupt_payload_sha_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); m = target(); setup_history(root, m)
            path = next((root / "fixtures_players").glob("*.json")); d = json.loads(path.read_text())
            d["raw_payload"] = {"response": d["response"]}; d["payload_sha256"] = "0" * 64
            path.write_text(json.dumps(d))
            obs, _ = p.collect_player_observations(m, root, CUT)
            self.assertEqual(obs, [])


if __name__ == "__main__":
    unittest.main()
