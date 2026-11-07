"""Offline tests for the Phase 1 data foundation.  No real network/browser."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import bwin_discovery as bwin
import prematch_phase1 as phase1


UTC = timezone.utc


def fixture(fid=1, home="Sevilla", away="Alaves", *, gender="M", category="ELIGIBLE",
            kickoff="2099-11-07T20:00:00+00:00"):
    return {"fixture_id": fid, "home": home, "away": away, "home_id": 10 + fid,
            "away_id": 20 + fid, "league": "La Liga", "league_id": 140,
            "season": 2099, "round": "Regular Season - 1", "country": "Spain",
            "gender": gender, "category_status": category,
            "kickoff_utc": kickoff,
            "kickoff_madrid": datetime.fromisoformat(kickoff).astimezone(phase1.MADRID).isoformat(),
            "status": "NS", "venue": "Test", "city": "Test"}


def event(eid="7000001", text="Sevilla Alaves", url=None):
    return {"bwin_event_id": eid,
            "bwin_url": url or f"https://www.bwin.es/es/sports/eventos/sevilla-alaves-2:{eid}",
            "text": text, "text_observed_at_utc": "2099-11-07T10:00:00+00:00",
            "source_pages": ["https://www.bwin.es/es/sports/futbol-4"]}


def observed_match(row, *, status="IDENTITY_VERIFIED_AUTOMATICALLY",
                   identity="NAME_AND_ABSOLUTE_TIME_MATCH", eid="7000001"):
    return {**row, "status": status, "identity_status": identity,
            "bwin_event_id": eid,
            "bwin_url": f"https://www.bwin.es/es/sports/eventos/sevilla-alaves-2:{eid}",
            "team_order_status": "HOME_AWAY",
            "category_compatibility": "COMPATIBLE_OR_UNKNOWN",
            "competition_similarity": 1.0,
            "inspection": {"event_id_from_url": eid,
                           "title_teams_match": True,
                           "competition_match_score": 1.0,
                           "event_start_machine_readable": row["kickoff_madrid"]}}


class BwinInputTests(unittest.TestCase):
    def test_lightweight_fixtures_input_and_top_zero(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixtures.json"
            path.write_text(json.dumps({"fixtures": [fixture(1), fixture(2, "Betis", "Getafe")]}))
            ready, skipped = bwin.read_candidates(path, [], 0, False,
                                                  datetime(2099, 11, 7, 10, tzinfo=phase1.MADRID))
            self.assertEqual([x["fixture_id"] for x in ready], [1, 2])
            self.assertEqual(skipped, [])
            self.assertEqual(ready[0]["home_id"], 11)

    def test_home_away_inversion_is_rejected(self):
        row = fixture()
        self.assertEqual(len(bwin.match_catalog(row, [event(text="Sevilla Alaves")])), 1)
        self.assertEqual(bwin.match_catalog(row, [event(text="Alaves Sevilla")]), [])

    def test_gender_and_non_adult_conflicts_are_rejected(self):
        male = fixture(gender="M")
        self.assertEqual(bwin.match_catalog(male, [event(text="Sevilla Women Alaves Women")]), [])
        self.assertEqual(bwin.match_catalog(male, [event(text="Sevilla U19 Alaves U19")]), [])
        female = fixture(gender="F")
        found = bwin.match_catalog(female, [event(text="Sevilla Women Alaves Women")])
        self.assertEqual(found[0]["category_compatibility"], "FEMALE_CORROBORATED")
        unknown = fixture(gender="UNKNOWN")
        self.assertEqual(bwin.match_catalog(unknown, [event()])[0]["category_compatibility"],
                         "CATEGORY_UNCONFIRMED")

    def test_homonyms_remain_ambiguous(self):
        row = fixture()
        candidates = [event("7000001"), event("7000002")]
        possible = bwin.match_catalog(row, candidates)
        self.assertEqual(len(possible), 2)
        self.assertEqual(bwin.summarize(row, possible, browser=None, timeout=1, inspect=False)["status"],
                         "AMBIGUOUS_MULTIPLE_EVENTS")

    def test_explicitly_excluded_categories_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixtures.json"
            path.write_text(json.dumps({"fixtures": [fixture(category="EXCLUDED")]}))
            ready, skipped = bwin.read_candidates(path, [], 0, False,
                                                  datetime(2099, 11, 7, 10, tzinfo=phase1.MADRID))
            self.assertEqual(ready, [])
            self.assertEqual(skipped, [])

    def test_absolute_time_alone_does_not_bypass_competition_check(self):
        row = fixture()
        possible = bwin.match_catalog(row, [event(text="La Liga Sevilla Alaves")])
        observed = {"identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                    "competition_match_score": 0.2, "market_headings": {}}
        with mock.patch.object(bwin, "inspect_event", return_value=observed):
            result = bwin.summarize(row, possible, browser=None, timeout=1, inspect=True)
        self.assertEqual(result["status"], "EVENT_REQUIRES_REVIEW")
        self.assertEqual(result["inspection"]["identity_status"],
                         "CATEGORY_OR_COMPETITION_UNVERIFIED")

    def test_catalog_visits_duplicate_page_once_per_cycle(self):
        class Page:
            def __init__(self): self.visits = []
            def goto(self, url, **kwargs): self.visits.append(url); return None
            def wait_for_timeout(self, value): pass
            def title(self): return "Bwin"
            def close(self): pass
        class Browser:
            def __init__(self): self.page = Page()
            def new_page(self, **kwargs): return self.page
        browser = Browser()
        with mock.patch.object(bwin, "browser_collect_anchors", return_value=[]):
            catalog, diagnostics = bwin.fetch_catalog(
                browser, bwin.BASE, ["/es/sports/futbol-4", "/es/sports/futbol-4"], 1, 1000)
        self.assertEqual(catalog, [])
        self.assertEqual(len(browser.page.visits), 1)
        self.assertEqual(len(diagnostics), 1)


class IdentityTests(unittest.TestCase):
    def map(self, fixtures, matches):
        return phase1.identity_map({"generated_at_utc": "2099-11-07T10:00:00+00:00",
                                    "fixtures": fixtures},
                                   {"generated_at_utc": "2099-11-07T10:01:00+00:00",
                                    "matches": matches, "events": [], "pages": []})

    def test_absolute_time_is_verified(self):
        row = fixture()
        result = self.map([row], [observed_match(row)])
        self.assertEqual(result["counts"]["verified"], 1)
        self.assertEqual(result["verified"][0]["bwin_event_id"], "7000001")

    def test_api_fixture_can_be_deep_collected_without_bwin_binding(self):
        row = fixture()
        result = self.map([row], [])
        self.assertEqual(result["counts"]["verified"], 0)
        self.assertEqual(result["counts"]["api_fixture_verified"], 1)
        self.assertEqual(result["api_verified"][0]["bwin_binding_status"], "UNVERIFIED")

    def test_declared_verification_must_be_reproducible(self):
        row = fixture()
        wrong_time = observed_match(row)
        wrong_time["inspection"]["event_start_machine_readable"] = "2099-11-07T23:00:00+01:00"
        result = self.map([row], [wrong_time])
        self.assertEqual(result["counts"]["verified"], 0)
        self.assertEqual(result["pending"][0]["reason"], "DECLARED_VERIFICATION_NOT_REPRODUCIBLE")
        self.assertEqual(result["pending"][0]["evidence"]["revalidated"]
                         ["absolute_time_difference_minutes"], 120.0)

        wrong_event = observed_match(row)
        wrong_event["inspection"]["event_id_from_url"] = "7999999"
        result = self.map([row], [wrong_event])
        self.assertEqual(result["counts"]["verified"], 0)
        self.assertFalse(result["pending"][0]["evidence"]["revalidated"]["event_id_matches"])

    def test_order_and_category_conflicts_cannot_be_verified(self):
        row = fixture()
        inverse = observed_match(row); inverse["team_order_status"] = "AWAY_HOME_CONFLICT"
        result = self.map([row], [inverse])
        self.assertEqual(result["counts"]["verified"], 0)
        female_conflict = observed_match(row); female_conflict["category_compatibility"] = "GENDER_CONFLICT"
        result = self.map([row], [female_conflict])
        self.assertEqual(result["counts"]["verified"], 0)
        missing_competition = observed_match(row); missing_competition["competition_similarity"] = 0.0
        missing_competition["inspection"]["competition_match_score"] = 0.0
        result = self.map([row], [missing_competition])
        self.assertEqual(result["counts"]["verified"], 0)

    def test_female_requires_corroborated_category_and_unknown_stays_pending(self):
        female = fixture(gender="F")
        corroborated = observed_match(female)
        corroborated["category_compatibility"] = "FEMALE_CORROBORATED"
        self.assertEqual(self.map([female], [corroborated])["counts"]["verified"], 1)
        not_visible = observed_match(female)
        not_visible["category_compatibility"] = "FEMALE_NOT_VISIBLE"
        self.assertEqual(self.map([female], [not_visible])["counts"]["verified"], 0)
        unknown = fixture(gender="UNKNOWN")
        unknown_match = observed_match(unknown)
        unknown_match["category_compatibility"] = "CATEGORY_UNCONFIRMED"
        self.assertEqual(self.map([unknown], [unknown_match])["counts"]["verified"], 0)

    def test_relative_and_unknown_time_are_not_verified(self):
        row1, row2 = fixture(1), fixture(2, "Betis", "Getafe")
        matches = [observed_match(row1, status="EVENT_FOUND_TIME_CORROBORATED",
                                  identity="NAME_AND_RELATIVE_TIME_CORROBORATED"),
                   observed_match(row2, status="EVENT_FOUND_NEEDS_TIME_CONFIRMATION",
                                  identity="NAME_MATCH_TIME_NOT_MACHINE_VERIFIED", eid="7000002")]
        result = self.map([row1, row2], matches)
        self.assertEqual(result["counts"]["verified"], 0)
        self.assertEqual({x["identity_status"] for x in result["pending"]},
                         {"TIME_CORROBORATED_NOT_VERIFIED", "NEEDS_TIME_CONFIRMATION"})

    def test_duplicate_event_and_category_review_are_ambiguous(self):
        row1, row2 = fixture(1), fixture(2, "Betis", "Getafe")
        result = self.map([row1, row2], [observed_match(row1), observed_match(row2)])
        self.assertEqual(result["counts"]["verified"], 0)
        self.assertTrue(all(x["identity_status"] == "AMBIGUOUS" for x in result["pending"]))
        review = fixture(3, category="REVIEW")
        result = self.map([review], [observed_match(review, eid="7000003")])
        self.assertEqual(result["pending"][0]["reason"], "CATEGORY_REQUIRES_REVIEW")

    def test_not_found_is_distinct_from_catalog_incomplete(self):
        row1, row2 = fixture(1), fixture(2, "Betis", "Getafe")
        missing = {**row1, "status": "NOT_FOUND_IN_SCANNED_PAGES",
                   "identity_status": "NOT_VERIFIED", "bwin_event_id": None}
        result = self.map([row1, row2], [missing])
        self.assertEqual(result["counts"]["not_found"], 1)
        self.assertEqual(result["pending"][0]["identity_status"], "CATALOG_INCOMPLETE")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache"
        self.clock_value = datetime(2099, 11, 7, 10, tzinfo=UTC)
        self.calls = []

    def clock(self):
        return self.clock_value

    def transport(self, endpoint, params, headers, timeout):
        self.calls.append((endpoint, dict(params)))
        return 200, {}, {"results": 1, "paging": {"current": 1, "total": 1},
                         "response": [{"endpoint": endpoint, "params": params}], "errors": []}

    def client(self, **kwargs):
        return phase1.Phase1ApiClient(self.cache, offline=False, max_new_calls=kwargs.pop("max_new_calls", 10),
                                      key="test", transport=self.transport, clock=self.clock,
                                      sleeper=lambda _: None, **kwargs)

    def test_miss_then_hit_and_repetition_no_new_call(self):
        first = self.client(); result = first.get("teams", id=10)
        self.assertEqual(result.source, "api"); self.assertEqual(first.new_calls, 1)
        second = phase1.Phase1ApiClient(self.cache, offline=True, clock=self.clock)
        cached = second.get("teams", id=10)
        self.assertEqual(cached.source, "cache")
        self.assertEqual(second.new_calls, 0)

    def test_distinct_parameters_have_distinct_keys(self):
        client = self.client()
        self.assertNotEqual(client.path_for("teams", {"id": 1}), client.path_for("teams", {"id": 2}))

    def test_expired_positive_cache_is_refetched(self):
        first = self.client(); first.get("teams", id=10)
        self.clock_value += timedelta(days=8)
        second = self.client(); result = second.get("teams", id=10)
        self.assertEqual(result.source, "api")
        self.assertEqual(len(self.calls), 2)

    def test_generate_context_legacy_cache_is_reused_read_only(self):
        params = {"fixture": "44"}
        key = phase1.hashlib.sha256(json.dumps(
            {"endpoint": "fixtures/statistics", "params": params},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.cache.parent / "data" / "cache" / f"{key}.json"
        path.parent.mkdir(parents=True)
        payload = {"get": "fixtures/statistics", "parameters": params, "errors": [],
                   "paging": {"current": 1, "total": 1}, "results": 2,
                   "response": [{"team": {"id": 1}}, {"team": {"id": 2}}]}
        path.write_text(json.dumps(payload))
        stamp = self.clock_value.timestamp(); os.utime(path, (stamp, stamp))
        before = path.read_bytes()
        client = phase1.Phase1ApiClient(self.cache, offline=True, clock=self.clock)
        result = client.get("fixtures/statistics", fixture=44)
        self.assertEqual(result.source, "legacy_generate_context")
        self.assertEqual(client.new_calls, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_preselection_legacy_cache_is_reused_with_timestamp(self):
        params = {"id": "77"}; _, query = phase1.normalized_params(params)
        key = phase1.hashlib.sha256(f"teams?{query}".encode()).hexdigest()
        path = self.cache.parent / "data" / "cache" / "preselect_api" / f"{key}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"retrieved_at_utc": self.clock_value.isoformat(),
                                    "response": [{"team": {"id": 77}}]}))
        client = phase1.Phase1ApiClient(self.cache, offline=True, clock=self.clock)
        result = client.get("teams", id=77)
        self.assertEqual(result.source, "legacy_preselection")
        self.assertEqual(client.new_calls, 0)

    def test_offline_miss_and_corrupt_json(self):
        client = phase1.Phase1ApiClient(self.cache, offline=True, clock=self.clock)
        with self.assertRaises(phase1.CacheMissOffline): client.get("teams", id=99)
        path = client.path_for("teams", {"id": 99}); path.parent.mkdir(parents=True)
        path.write_text("{bad")
        with self.assertRaises(phase1.CacheMissOffline): client.get("teams", id=99)
        self.assertTrue(any(x["status"] == "CACHE_CORRUPT" for x in client.ledger))

    def test_empty_response_expires_quickly_and_can_recover(self):
        def empty(endpoint, params, headers, timeout):
            return 200, {}, {"results": 0, "paging": {"current": 1, "total": 1},
                             "response": [], "errors": []}
        first = phase1.Phase1ApiClient(self.cache, offline=False, max_new_calls=2, key="x",
                                       transport=empty, clock=self.clock, sleeper=lambda _: None)
        self.assertEqual(first.get("fixtures/statistics", fixture=1).completeness, "EMPTY_PROVISIONAL")
        self.clock_value += timedelta(minutes=4)
        self.assertEqual(phase1.Phase1ApiClient(self.cache, offline=True, clock=self.clock)
                         .get("fixtures/statistics", fixture=1).source, "cache")
        self.clock_value += timedelta(minutes=2)
        recovered = self.client().get("fixtures/statistics", fixture=1)
        self.assertEqual(recovered.response[0]["endpoint"], "fixtures/statistics")

    def test_one_team_statistics_response_is_partial_and_short_lived(self):
        first = self.client()
        result = first.get("fixtures/statistics", fixture=44)
        self.assertEqual(result.completeness, "PARTIAL_RESPONSE")
        document = json.loads(result.cache_path.read_text())
        self.assertEqual(document["ttl_seconds"], phase1.PARTIAL_TTL_SECONDS)
        self.assertEqual(document["validity_policy"]["kind"], "SHORT_PARTIAL")

    def test_budget_exhaustion_counts_real_calls(self):
        client = self.client(max_new_calls=1)
        client.get("teams", id=1)
        with self.assertRaises(phase1.BudgetExhausted): client.get("teams", id=2)
        self.assertEqual(client.new_calls, 1)

    def test_budgeted_resume_reuses_first_call_before_fetching_next(self):
        first = self.client(max_new_calls=1)
        first.get("teams", id=1)
        with self.assertRaises(phase1.BudgetExhausted):
            first.get("teams", id=2)
        second = self.client(max_new_calls=1)
        self.assertEqual(second.get("teams", id=1).source, "cache")
        self.assertEqual(second.get("teams", id=2).source, "api")
        self.assertEqual(second.new_calls, 1)
        self.assertEqual([call[1]["id"] for call in self.calls], ["1", "2"])

    def test_raw_payload_and_hash_are_preserved(self):
        client = self.client(); result = client.get("fixtures/events", fixture=1)
        document = json.loads(result.cache_path.read_text())
        self.assertEqual(document["payload_sha256"], phase1.sha_bytes(phase1.canonical_json(document["raw_payload"])))
        self.assertIn("paging", document["raw_payload"])

    def test_pagination_is_cacheable_and_budgeted(self):
        def paged(endpoint, params, headers, timeout):
            page = int(params.get("page", 1))
            self.calls.append((endpoint, dict(params)))
            return 200, {}, {"results": 1, "paging": {"current": page, "total": 2},
                             "response": [{"page": page}], "errors": []}
        client = phase1.Phase1ApiClient(self.cache, offline=False, max_new_calls=2, key="x",
                                        transport=paged, clock=self.clock, sleeper=lambda _: None)
        collector = phase1.DeepCollector(client, history=5, cutoff=self.clock_value)
        results = collector.request_pages("players", {"team": 10, "season": 2099},
                                          scope={"api_fixture_id": 1})
        self.assertEqual([x.response[0]["page"] for x in results], [1, 2])
        self.assertEqual(client.statistics()["http_calls_by_endpoint"], {"players": 2})


class DataAndIntegrationTests(unittest.TestCase):
    def test_limited_deep_pilot_prioritizes_historical_detail_and_blocks_markets(self):
        target = fixture()
        calls = []
        def transport(endpoint, params, headers, timeout):
            calls.append((endpoint, dict(params)))
            if endpoint == "fixtures" and params.get("team"):
                response = [{"fixture": {"id": 100, "date": "2099-11-01T20:00:00+00:00",
                                          "status": {"short": "FT"}},
                             "teams": {"home": {"id": 11}, "away": {"id": 21}}}]
            elif endpoint == "fixtures" and params.get("ids"):
                response = [{"fixture": {"id": 100}, "teams": {"home": {"id": 11}, "away": {"id": 21}}}]
            elif endpoint in {"fixtures/statistics", "fixtures/players", "fixtures/lineups"}:
                response = [{"team": {"id": 11}}, {"team": {"id": 21}}]
            elif endpoint == "fixtures/events":
                response = [{"type": "Card"}, {"type": "Goal"}]
            else:
                response = [{"ok": True}]
            return 200, {}, {"results": len(response), "paging": {"current": 1, "total": 1},
                             "response": response, "errors": []}
        with tempfile.TemporaryDirectory() as td:
            client = phase1.Phase1ApiClient(Path(td), offline=False, max_new_calls=15,
                                            key="test", transport=transport, sleeper=lambda _: None)
            collector = phase1.DeepCollector(client, history=5, cutoff=datetime(2099, 1, 1, tzinfo=UTC))
            output = collector.collect_fixture({"api_fixture_id": target["fixture_id"],
                                                "home_team_id": target["home_id"],
                                                "away_team_id": target["away_id"],
                                                "league_id": target["league_id"], "season": target["season"],
                                                "kickoff_utc": target["kickoff_utc"],
                                                "bwin_event_id": None, "bwin_binding_status": "UNVERIFIED"})
        self.assertLessEqual(len(calls), 15)
        self.assertIn("fixtures/statistics", [name for name, _ in calls[:8]])
        self.assertIn("fixtures/players", output["historical_detail_status"]["100"])
        self.assertEqual(output["bwin_binding_status"], "UNVERIFIED")
        self.assertFalse(output["market_identity_ready"])
        self.assertEqual(output["status"], "INCOMPLETE_CONTEXT")

    def test_shared_team_request_is_reused_and_attributed_to_both_fixtures(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            def transport(endpoint, params, headers, timeout):
                calls.append((endpoint, dict(params)))
                return 200, {}, {"results": 1, "paging": {"current": 1, "total": 1},
                                 "response": [{"team": {"id": int(params["id"])}}], "errors": []}
            client = phase1.Phase1ApiClient(Path(td), offline=False, max_new_calls=2, key="x",
                                            transport=transport, sleeper=lambda _: None)
            collector = phase1.DeepCollector(client, history=5, cutoff=datetime(2099, 1, 1, tzinfo=UTC))
            a = collector.request("teams", {"id": 10}, scope={"api_fixture_id": 1, "team_id": 10})
            b = collector.request("teams", {"id": 10}, scope={"api_fixture_id": 2, "team_id": 10})
            self.assertIs(a, b)
            self.assertEqual(len(calls), 1)
            coverage = collector.coverage([])
            self.assertEqual(coverage["fixture_endpoint_coverage"]["1"]["teams"]["AVAILABLE"], 1)
            self.assertEqual(coverage["fixture_endpoint_coverage"]["2"]["teams"]["AVAILABLE"], 1)
            self.assertEqual(coverage["reuse"]["total_reuses"], 1)

    def test_phase1_output_can_be_reused_on_resume(self):
        row = fixture()
        source = {"schema": phase1.SCHEMA, "stage": "LIGHTWEIGHT_DISCOVERY",
                  "generated_at_utc": "2099-11-07T10:00:00+00:00",
                  "fixtures": [row], "counts": {"eligible": 1, "review": 0, "excluded": 0}}
        canonical = phase1.canonical_fixtures(source, None)
        self.assertEqual(canonical["counts"]["eligible"], 1)
        self.assertEqual(canonical["eligible"][0]["fixture_id"], 1)

    def test_live_fixture_is_excluded_before_identity(self):
        row = fixture(); row["status"] = "1H"
        source = {"schema_version": "1.0-discovery", "fixtures": [row]}
        canonical = phase1.canonical_fixtures(source, None)
        self.assertEqual(canonical["excluded"][0]["category_reason"], "NOT_PREMATCH")

    def test_temporal_filter_deduplicates_and_excludes_target_future(self):
        target = datetime(2099, 11, 7, 20, tzinfo=UTC)
        def row(fid, when, status="FT"):
            return {"fixture": {"id": fid, "date": when, "status": {"short": status}},
                    "teams": {"home": {"id": 10}, "away": {"id": 20}}}
        rows = [row(1, "2099-11-01T20:00:00+00:00"), row(1, "2099-11-01T20:00:00+00:00"),
                row(999, "2099-11-02T20:00:00+00:00"),
                row(2, "2099-11-08T20:00:00+00:00"), row(3, "2099-11-03T20:00:00+00:00", "1H")]
        selected = phase1.final_historical_rows(rows, target, 999, 10, 20)
        self.assertEqual([x["fixture"]["id"] for x in selected], [1])

    def test_null_is_not_zero_but_observed_zero_counts(self):
        rows = [{"statistics": [{"type": "Total Shots", "value": 0},
                                 {"type": "Corner Kicks", "value": None}]}]
        coverage = phase1.stat_metric_coverage(rows)
        self.assertEqual(coverage["shots"], 1)
        self.assertEqual(coverage["corners"], 0)

    def test_cross_midnight_and_dst(self):
        begin, finish = phase1.window_bounds(date(2026, 3, 28), time(15), time(3))
        self.assertEqual(begin.isoformat(), "2026-03-28T15:00:00+01:00")
        self.assertEqual(finish.isoformat(), "2026-03-29T03:00:00+02:00")
        self.assertGreater((finish.astimezone(UTC) - begin.astimezone(UTC)).total_seconds(), 0)

    def test_active_window_uses_previous_day_before_cross_midnight_end(self):
        current = datetime(2026, 11, 7, 2, 59, tzinfo=phase1.MADRID)
        self.assertEqual(phase1.active_window_day(current, time(15), time(3)), date(2026, 11, 6))
        self.assertEqual(phase1.active_window_day(current.replace(hour=3), time(15), time(3)),
                         date(2026, 11, 7))

    def test_historical_identity_remains_auditable_but_deep_collection_is_blocked(self):
        row = fixture(kickoff="2020-01-01T20:00:00+00:00")
        result = phase1.identity_map({"generated_at_utc": "2026-11-07T10:00:00+00:00",
                                      "fixtures": [row]},
                                     {"generated_at_utc": "2026-11-07T10:01:00+00:00",
                                      "matches": [observed_match(row)], "events": [], "pages": []})
        self.assertEqual(result["verified"][0]["temporal_status_at_map_build"], "HISTORICAL_OR_STARTED")
        with tempfile.TemporaryDirectory() as td:
            client = phase1.Phase1ApiClient(Path(td), offline=True)
            collector = phase1.DeepCollector(client, history=5, cutoff=datetime(2026, 11, 7, tzinfo=UTC))
            blocked = collector.collect_fixture(result["verified"][0])
        self.assertEqual(blocked["status"], "BLOCKED_TARGET_NOT_FUTURE")

    def test_offline_end_to_end_writes_all_outputs_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "fixtures.json"; bwin_path = root / "bwin.json"
            row = fixture()
            source.write_text(json.dumps({"schema_version": "1.0-discovery",
                                          "generated_at_utc": "2099-11-07T10:00:00+00:00",
                                          "window": {"start_madrid": "2099-11-07T15:00:00+01:00",
                                                     "end_madrid": "2099-11-08T03:00:00+01:00"},
                                          "fixtures": [row], "warnings": []}))
            bwin_path.write_text(json.dumps({"generated_at_utc": "2099-11-07T10:01:00+00:00",
                                             "catalog_observed": [event()], "pages": [{"catalog_page": "p", "status": "OK"}],
                                             "matches": [observed_match(row)]}))
            out = root / "phase1"
            code = phase1.main(["--root", str(root), "--date", "2099-11-07",
                                "--fixtures-input", str(source), "--bwin-input", str(bwin_path),
                                "--output-dir", str(out), "--max-matches", "1"])
            self.assertEqual(code, 0)
            for name in ("fixtures_discovered.json", "bwin_catalog.json", "fixture_identity_map.json",
                         "api_coverage.json", "raw_manifest.json", "phase1_summary.json", "phase1_summary.md"):
                self.assertTrue((out / name).is_file(), name)
            self.assertTrue((out / "phase1_execution.log").is_file())
            summary = json.loads((out / "phase1_summary.json").read_text())
            self.assertEqual(summary["api_usage"]["new_http_calls"], 0)
            self.assertFalse(summary["prohibitions"]["prediction_engines_executed"])
            self.assertEqual(summary["counts"]["identity_verified"], 1)


if __name__ == "__main__":
    unittest.main()
