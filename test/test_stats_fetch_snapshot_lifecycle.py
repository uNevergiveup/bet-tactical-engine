#!/usr/bin/env python3
"""Pruebas de ciclo de vida de snapshots y migración de stats-fetch."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import bwin_manual_links as bml
import preselect_matches as pm
import prematch_steps as ps


class StatsFetchSnapshotLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.eval_dir = self.root / "outputs/discovery/2026-11-20_1500_0300"
        self.eval_dir.mkdir(parents=True, exist_ok=True)
        self.asof = "2026-11-20T16:30:00+00:00"
        self.fx1 = {
            "fixture_id": 1570454,
            "kickoff_utc": "2026-11-20T20:00:00+00:00",
            "kickoff_madrid": "2026-11-20T21:00:00+01:00",
            "status": "NS",
            "country": "Spain",
            "league": "La Liga",
            "league_id": 140,
            "season": 2026,
            "round": "Regular Season - 13",
            "home": "Athletic Club",
            "away": "Espanyol",
            "home_id": 531,
            "away_id": 540,
            "venue": "San Mames",
            "city": "Bilbao",
        }
        self.fx2 = {
            "fixture_id": 1575221,
            "kickoff_utc": "2026-11-20T19:30:00+00:00",
            "kickoff_madrid": "2026-11-20T20:30:00+01:00",
            "status": "NS",
            "country": "Germany",
            "league": "Bundesliga",
            "league_id": 78,
            "season": 2026,
            "round": "Regular Season - 11",
            "home": "Bayern München",
            "away": "1. FC Köln",
            "home_id": 157,
            "away_id": 192,
            "venue": "Allianz Arena",
            "city": "Munich",
        }
        self._write_discovery_and_filter([self.fx1, self.fx2])
        self._bind_verified_bwin([
            (1570454, "7910856", "https://www.bwin.es/es/sports/eventos/athletic-club-espanyol-2:7910856"),
            (1575221, "7906958", "https://www.bwin.es/es/sports/eventos/bayern-m%C3%BAnich-colonia-2:7906958"),
        ])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_discovery_and_filter(self, fixtures: list[dict], gen_ts: str = "2026-11-20T14:16:39+00:00") -> str:
        raw_disc = {
            "schema_version": "1.0-discovery",
            "generated_at_utc": gen_ts,
            "source": "TEST",
            "scope": "ALL_AVAILABLE_COMPETITIONS_UNFILTERED",
            "window": {
                "start_madrid": "2026-11-20T15:00:00+01:00 Europe/Madrid (hora civil)",
                "end_madrid": "2026-11-21T03:00:00+01:00 Europe/Madrid (hora civil)",
                "timezone": "Europe/Madrid",
                "inclusive_end": True,
            },
            "request_days": [{"date": "2026-11-20", "source": "test", "returned": len(fixtures)}],
            "counts": {"api_records_received": len(fixtures), "prematch_matches": len(fixtures), "competitions": 2, "excluded": 0},
            "fixtures": fixtures,
            "fixture_audit": [],
            "warnings": [],
        }
        (self.eval_dir / "fixtures.json").write_text(json.dumps(raw_disc, ensure_ascii=False, indent=2), encoding="utf-8")
        rc = ps.main(["filter", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
        self.assertEqual(rc, 0)
        return hashlib.sha256((self.eval_dir / "fixtures_major.json").read_bytes()).hexdigest()

    def _bind_verified_bwin(self, entries: list[tuple[int, str, str]]) -> None:
        bwin_dir = self.eval_dir / "prospective_trial/bwin"
        bwin_dir.mkdir(parents=True, exist_ok=True)
        links = {
            str(fid): {
                "fixture_id": fid,
                "extracted_event_id": eid,
                "bwin_url": url,
                "identity_status": "BWIN_MANUAL_LINK_VERIFIED",
            }
            for fid, eid, url in entries
        }
        (bwin_dir / "manual_bwin_links.json").write_text(
            json.dumps({"schema_version": bml.MANUAL_LINKS_SCHEMA, "links": links}, ensure_ascii=False),
            encoding="utf-8",
        )
        rc = ps.main(["bwin-scan", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--no-browser"])
        self.assertEqual(rc, 0)

    def _make_match_entry(self, fx: dict) -> dict:
        def _obs(tid: int, opp: int, is_home: bool) -> list[dict]:
            return [
                {
                    "fixture_id": 800000 + tid * 20 + i,
                    "team_id": tid,
                    "opponent_id": opp,
                    "kickoff_utc": f"2026-10-{10 + i:02d}T18:00:00+00:00",
                    "league_id": fx["league_id"],
                    "season": fx["season"],
                    "venue": "HOME" if (is_home if i % 2 == 0 else not is_home) else "AWAY",
                    "status": "FT",
                    "statistics_requested": True,
                    "own": {"goals": 2, "corners": 5, "shots": 12, "fouls": 11, "yellow": 2, "offsides": 1},
                    "conceded": {"goals": 1, "corners": 4, "shots": 9, "fouls": 12, "yellow": 2, "offsides": 2},
                }
                for i in range(6)
            ]

        return {
            "fixture_id": fx["fixture_id"],
            "kickoff_madrid": fx["kickoff_madrid"],
            "home_id": fx["home_id"],
            "away_id": fx["away_id"],
            "home": fx["home"],
            "away": fx["away"],
            "country": fx["country"],
            "league": fx["league"],
            "league_id": fx["league_id"],
            "season": fx["season"],
            "gender": "M",
            "region": "EUROPE",
            "region_priority": 1,
            "analysis_cutoff_utc": self.asof,
            "preliminary_coverage_score": 90,
            "initial_data_gate": "APTO_SEGUN_DATOS_BASICOS",
            "data_readiness_score": 82,
            "data_readiness_status": "DATOS_APROVECHABLES",
            "stability_score": 70.0,
            "research_priority_score": 78.4,
            "details_checked": True,
            "warnings": [],
            "market_profiles": {},
            "statistical_observations": {
                "home": _obs(fx["home_id"], fx["away_id"], True),
                "away": _obs(fx["away_id"], fx["home_id"], False),
            },
            "home_evidence": {"history": {"completed_total": 6, "same_competition_total": 6, "goals_for": {"n": 6, "mean": 2.0}}, "details": {"fixture_details_available": 6, "own": {"corners": {"n": 6, "mean": 5.0}}}, "standings": {"status": "AVAILABLE", "rank": 5}},
            "away_evidence": {"history": {"completed_total": 6, "same_competition_total": 6, "goals_for": {"n": 6, "mean": 2.0}}, "details": {"fixture_details_available": 6, "own": {"corners": {"n": 6, "mean": 5.0}}}, "standings": {"status": "AVAILABLE", "rank": 9}},
        }

    def _fake_report(self, fxs: list[dict], filter_sha: str | None) -> dict:
        return {
            "schema_version": "1.3-preselection",
            "generated_at_utc": self.asof,
            "source_filter_sha256": filter_sha,
            "source_filter_generated_at_utc": json.loads((self.eval_dir / "fixtures_major.json").read_text())["generated_at_utc"],
            "methodology": {"sample_history_cap": 20, "sample_details_cap": 8},
            "counts": {"evaluated": len(fxs), "fully_evaluated": len(fxs), "api_errors": 0, "details_requested": len(fxs), "pending_detail": 0},
            "api_usage": {"requests_sent": 0, "cache_hits": 0},
            "api_errors": {},
            "matches": [self._make_match_entry(fx) for fx in fxs],
        }

    def test_01_legacy_null_fingerprint_archived_never_backfilled_and_never_merged(self) -> None:
        enriched = self.eval_dir / "prospective_trial/enriched"
        enriched.mkdir(parents=True, exist_ok=True)
        legacy_doc = {
            "schema_version": "1.3-preselection",
            "generated_at_utc": "2026-11-20T12:48:57.668597+00:00",
            "source_filter_sha256": None,
            "source_filter_generated_at_utc": "2026-11-20T12:40:08.747944+00:00",
            "counts": {"evaluated": 1, "fully_evaluated": 0, "api_errors": 3},
            "api_errors": {"531": "FROZEN_ASOF_REQUIRES_VERIFIED_PRIOR_CACHE"},
            "api_usage": {"requests_sent": 0, "cache_hits": 0},
            "matches": [self._make_match_entry(self.fx1)],
        }
        legacy_path = enriched / "preselection.json"
        legacy_bytes = json.dumps(legacy_doc, ensure_ascii=False, indent=2).encode("utf-8")
        legacy_path.write_bytes(legacy_bytes)
        legacy_sha = hashlib.sha256(legacy_bytes).hexdigest()

        current_sha = hashlib.sha256((self.eval_dir / "fixtures_major.json").read_bytes()).hexdigest()
        # Direct merge on null-fingerprint base must remain strictly prohibited
        with self.assertRaises(pm.PreselectionError):
            pm.merge_preselection_reports(legacy_doc, self._fake_report([self.fx2], current_sha))

        def _mock_run_eval(doc, client, now, **kwargs):
            chosen = sorted(kwargs["fixture_ids"])
            self.assertEqual(chosen, [1570454, 1575221])
            # Explicitly return None to verify cmd_stats_fetch still binds current_sha
            return self._fake_report([self.fx1, self.fx2], None)

        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", side_effect=_mock_run_eval):
            rc = ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
            self.assertEqual(rc, 0)

        # Archived copy is byte-for-byte identical and STILL has source_filter_sha256=None
        archive_path = enriched / "archive" / f"preselection_{legacy_sha[:12]}.json"
        self.assertTrue(archive_path.is_file())
        self.assertEqual(archive_path.read_bytes(), legacy_bytes)
        self.assertIsNone(json.loads(archive_path.read_text(encoding="utf-8"))["source_filter_sha256"])

        # New report has real fixtures_major.json SHA256 and migration audit
        new_doc = json.loads(legacy_path.read_text(encoding="utf-8"))
        self.assertEqual(new_doc["source_filter_sha256"], current_sha)
        self.assertEqual(new_doc["snapshot_migration"]["migration_reason"], "LEGACY_NULL_FINGERPRINT")
        self.assertFalse(new_doc["snapshot_migration"]["prior_fingerprint_backfilled"])
        self.assertFalse(new_doc["snapshot_migration"]["stats_reused_from_prior_report"])

        # Traceability through patterns step
        rc_pat = ps.main(["patterns", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--no-cache"])
        self.assertEqual(rc_pat, 0)
        stats_step = json.loads((self.eval_dir / "steps/stats_fetch.json").read_text(encoding="utf-8"))
        pat_step = json.loads((self.eval_dir / "steps/patterns.json").read_text(encoding="utf-8"))
        tac_doc = json.loads((self.eval_dir / "prospective_trial/tactical/tactical_signal_engine.json").read_text(encoding="utf-8"))
        self.assertEqual(stats_step["source_filter_sha256"], current_sha)
        self.assertEqual(pat_step["source_filter_sha256"], current_sha)
        self.assertEqual(pat_step["preselection_sha256"], stats_step["preselection_sha256"])
        self.assertEqual(tac_doc["sources"]["preselection_sha256"], stats_step["preselection_sha256"])

    def test_02_real_filter_change_archives_prior_regenerates_then_resumes_cleanly(self) -> None:
        sha_v1 = hashlib.sha256((self.eval_dir / "fixtures_major.json").read_bytes()).hexdigest()
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", side_effect=lambda doc, client, now, **kw: self._fake_report([self.fx1], kw.get("source_filter_sha256"))):
            rc = ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--fixture-id", "1570454"])
            self.assertEqual(rc, 0)

        pre_path = self.eval_dir / "prospective_trial/enriched/preselection.json"
        v1_bytes = pre_path.read_bytes()
        v1_sha = hashlib.sha256(v1_bytes).hexdigest()
        self.assertEqual(json.loads(v1_bytes)["source_filter_sha256"], sha_v1)

        # Regenerate filter with modified kickoff/venue -> real filter SHA change
        fx1_mod = dict(self.fx1, venue="Nuevo San Mames")
        sha_v2 = self._write_discovery_and_filter([fx1_mod, self.fx2], gen_ts="2026-11-20T15:00:00+00:00")
        self.assertNotEqual(sha_v1, sha_v2)

        # Incompatible merge blocked by merge_preselection_reports
        with self.assertRaises(pm.PreselectionError):
            pm.merge_preselection_reports(json.loads(v1_bytes), self._fake_report([self.fx2], sha_v2))
        # Direct merge on same sha WITHOUT allow_new_fixtures=True still blocks unseen fixtures
        with self.assertRaises(pm.PreselectionError):
            pm.merge_preselection_reports(json.loads(v1_bytes), self._fake_report([self.fx2], sha_v1))

        # stats-fetch detects filter change, archives v1, regenerates only requested 1570454 bound to sha_v2
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", side_effect=lambda doc, client, now, **kw: self._fake_report([fx1_mod], kw.get("source_filter_sha256"))) as mock_v2:
            rc = ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--fixture-id", "1570454"])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_v2.call_args.kwargs["fixture_ids"], {1570454})

        archive_v1 = self.eval_dir / "prospective_trial/enriched/archive" / f"preselection_{v1_sha[:12]}.json"
        self.assertTrue(archive_v1.is_file())
        self.assertEqual(archive_v1.read_bytes(), v1_bytes)
        v2_doc = json.loads(pre_path.read_text(encoding="utf-8"))
        self.assertEqual(v2_doc["source_filter_sha256"], sha_v2)
        self.assertEqual(v2_doc["snapshot_migration"]["migration_reason"], "SOURCE_FILTER_CHANGED")

        # Subsequent incremental stats-fetch under SAME sha_v2 merges cleanly for 1575221 only
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", side_effect=lambda doc, client, now, **kw: self._fake_report([self.fx2], kw.get("source_filter_sha256"))) as mock_merge:
            rc = ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_merge.call_args.kwargs["fixture_ids"], {1575221})
        merged_doc = json.loads(pre_path.read_text(encoding="utf-8"))
        self.assertEqual(merged_doc["source_filter_sha256"], sha_v2)
        self.assertEqual({m["fixture_id"] for m in merged_doc["matches"]}, {1570454, 1575221})

    def test_03_live_stats_fetch_uses_unfrozen_client_asof_none_while_frozen_keeps_asof(self) -> None:
        observed_asofs = []

        def _capture_client(doc, client, now, **kw):
            observed_asofs.append(client.asof)
            return self._fake_report([self.fx1, self.fx2], kw.get("source_filter_sha256"))

        # With explicit --asof, client.asof is frozen
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", side_effect=_capture_client):
            self.assertEqual(ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof]), 0)
        self.assertIsNotNone(observed_asofs[-1])

        # Without --asof, client.asof is None (live run allowed to fetch missing/expired API data)
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", side_effect=_capture_client):
            self.assertEqual(ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--refresh"]), 0)
        self.assertIsNone(observed_asofs[-1])

    def test_04_apifootball_reuses_valid_historical_ft_detail_cache_across_batches_and_respects_cutoff(self) -> None:
        cache_dir = self.root / "outputs/data/cache/preselect_api"
        cache_dir.mkdir(parents=True, exist_ok=True)
        old_retrieved = "2026-11-07T10:00:00+00:00"
        future_retrieved = "2026-11-20T18:00:00+00:00"

        def _raw_fix(fid: int) -> dict:
            return {
                "fixture": {"id": fid, "date": "2026-11-05T18:00:00+00:00", "status": {"short": "FT"}},
                "teams": {"home": {"id": 531}, "away": {"id": 540}},
                "goals": {"home": 2, "away": 0},
                "league": {"id": 140, "season": 2026},
                "statistics": [{"team": {"id": 531}, "statistics": [{"type": "Corner Kicks", "value": 6}]}],
            }

        # Valid cached FT detail from another batch (older than 12h, but valid FT fact before cutoff)
        (cache_dir / "valid_batch.json").write_text(
            json.dumps({"retrieved_at_utc": old_retrieved, "endpoint": "fixtures", "parameters": {"ids": "9001-9999"}, "response": [_raw_fix(9001)]}),
            encoding="utf-8",
        )
        # Future-retrieved cache must be ignored at cutoff 16:30Z
        (cache_dir / "future_batch.json").write_text(
            json.dumps({"retrieved_at_utc": future_retrieved, "endpoint": "fixtures", "parameters": {"ids": "9002"}, "response": [_raw_fix(9002)]}),
            encoding="utf-8",
        )

        cutoff = datetime.fromisoformat(self.asof)
        client = pm.ApiFootball("dummy-key", "https://example.invalid", self.root, asof=None)
        fake_resp = Mock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"errors": {}, "paging": {"total": 1}, "response": [_raw_fix(9002)]}
        try:
            with patch.object(client.session, "get", return_value=fake_resp) as mock_get:
                rows = client.get_fixture_details([9001, 9002], cutoff=cutoff)
            mock_get.assert_called_once()
            self.assertEqual(mock_get.call_args.kwargs["params"], {"ids": "9002"})
            self.assertEqual({r["fixture"]["id"] for r in rows}, {9001, 9002})
            self.assertEqual(client.cache_hits, 1)
            self.assertEqual(client.requests_sent, 1)
            self.assertEqual(client.request_ledger[0]["status"], "HISTORICAL_DETAIL_CACHE_HIT")
            self.assertEqual(client.request_ledger[0]["reused_fixture_ids"], [9001])
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
