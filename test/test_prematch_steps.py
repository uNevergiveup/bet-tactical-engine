#!/usr/bin/env python3
"""Pruebas del ejecutor modular PREMATCH (prematch_steps.py)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bwin_manual_links as bml
import prematch_steps as ps


def _make_fixture(
    fid: int,
    *,
    home: str,
    away: str,
    home_id: int,
    away_id: int,
    league: str = "Premier League",
    league_id: int = 39,
    country: str = "England",
    kickoff_utc: str = "2026-11-20T20:00:00+00:00",
    kickoff_madrid: str = "2026-11-20T21:00:00+01:00",
    status: str = "NS",
) -> dict:
    return {
        "fixture_id": fid,
        "kickoff_utc": kickoff_utc,
        "kickoff_madrid": kickoff_madrid,
        "status": status,
        "country": country,
        "league": league,
        "league_id": league_id,
        "season": 2026,
        "round": "Regular Season - 12",
        "home": home,
        "away": away,
        "home_id": home_id,
        "away_id": away_id,
        "venue": "Test Stadium",
        "city": "Test City",
    }


def _make_preselection_match(fx: dict) -> dict:
    def _hist(team_id: int, opp_id: int, is_home: bool) -> list[dict]:
        rows = []
        for idx in range(6):
            rows.append({
                "fixture_id": 900000 + team_id * 10 + idx,
                "kickoff_utc": f"2026-10-{10 + idx:02d}T18:00:00+00:00",
                "league_id": fx["league_id"],
                "season": 2026,
                "is_home": is_home if idx % 2 == 0 else not is_home,
                "goals_for": 2,
                "goals_against": 1,
                "opponent_id": opp_id,
                "opponent": "Opponent",
                "detail_available": True,
                "statistics": {
                    "shots_total": 14,
                    "shots_on_goal": 6,
                    "corners": 6,
                    "yellow_cards": 2,
                    "red_cards": 0,
                    "fouls": 11,
                    "offsides": 2,
                    "possession": 55.0,
                },
            })
        return rows

    return {
        **fx,
        "data_readiness_score": 88,
        "data_readiness_status": "READY",
        "details_checked": True,
        "evidence_tier": "TIER_1",
        "home_evidence": {"history": {"completed_total": 6}, "standings": {"rank": 3}, "details": {}},
        "away_evidence": {"history": {"completed_total": 6}, "standings": {"rank": 5}, "details": {}},
        "table_context": {"available": True, "home_rank": 3, "away_rank": 5},
        "home_history": _hist(fx["home_id"], fx["away_id"], True),
        "away_history": _hist(fx["away_id"], fx["home_id"], False),
        "family_evidence": {
            "goals": {"status": "SUFFICIENT", "home_n": 6, "away_n": 6},
            "corners": {"status": "SUFFICIENT", "home_n": 6, "away_n": 6},
            "shots": {"status": "SUFFICIENT", "home_n": 6, "away_n": 6},
            "cards": {"status": "SUFFICIENT", "home_n": 6, "away_n": 6},
        },
        "statistical_observations": {
            "home": [
                {
                    "fixture_id": 9100 + i,
                    "kickoff_utc": f"2026-10-{10 + i:02d}T18:00:00+00:00",
                    "league_id": fx["league_id"],
                    "season": 2026,
                    "is_home": i % 2 == 0,
                    "own": {"goals": 2, "corners": 6, "shots_total": 14, "shots_on_goal": 6, "cards": 2, "fouls": 11, "offsides": 2},
                    "conceded": {"goals": 1, "corners": 3, "shots_total": 8, "shots_on_goal": 3, "cards": 2, "fouls": 12, "offsides": 1},
                }
                for i in range(6)
            ],
            "away": [
                {
                    "fixture_id": 9200 + i,
                    "kickoff_utc": f"2026-10-{10 + i:02d}T18:00:00+00:00",
                    "league_id": fx["league_id"],
                    "season": 2026,
                    "is_home": i % 2 == 1,
                    "own": {"goals": 1, "corners": 4, "shots_total": 10, "shots_on_goal": 4, "cards": 2, "fouls": 12, "offsides": 1},
                    "conceded": {"goals": 2, "corners": 6, "shots_total": 15, "shots_on_goal": 6, "cards": 2, "fouls": 10, "offsides": 2},
                }
                for i in range(6)
            ],
        },
    }


class TestPrematchSteps(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.eval_dir = self.root / "eval_run"
        self.eval_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config/prematch_windows.json").write_text(
            json.dumps({
                "schema_version": "1.0-prematch-windows",
                "timezone": "Europe/Madrid",
                "default_profile": "normal",
                "profiles": {"normal": {"start": "15:00", "end": "03:00"}},
            }),
            encoding="utf-8",
        )
        self.asof = "2026-11-20T12:00:00+00:00"
        self.fx_male = _make_fixture(1001, home="Arsenal", away="Chelsea", home_id=42, away_id=49)
        self.fx_female = _make_fixture(
            1002,
            home="Barcelona W",
            away="Real Madrid W",
            home_id=501,
            away_id=502,
            league="Primera División Femenina",
            league_id=142,
            country="Spain",
        )
        self.fx_youth = _make_fixture(
            1003,
            home="Arsenal U21",
            away="Chelsea U21",
            home_id=601,
            away_id=602,
            league="Premier League 2",
            league_id=702,
            country="England",
        )
        self.discovery_doc = {
            "schema_version": "1.0-discovery",
            "generated_at_utc": self.asof,
            "source": "TEST",
            "scope": "ALL_AVAILABLE_COMPETITIONS_UNFILTERED",
            "window": {
                "start_madrid": "2026-11-20T15:00:00+01:00 Europe/Madrid (hora civil)",
                "end_madrid": "2026-11-21T03:00:00+01:00 Europe/Madrid (hora civil)",
                "timezone": "Europe/Madrid",
                "inclusive_end": True,
            },
            "request_days": [{"date": "2026-11-20", "source": "test", "returned": 3}],
            "counts": {"api_records_received": 3, "prematch_matches": 3, "competitions": 3, "excluded": 0},
            "fixtures": [self.fx_male, self.fx_female, self.fx_youth],
            "fixture_audit": [],
            "warnings": [],
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_discovery(self) -> Path:
        p = self.eval_dir / "fixtures.json"
        p.write_text(json.dumps(self.discovery_doc, indent=2), encoding="utf-8")
        return p

    def test_01_02_06_discover_reuse_and_filter_all_statuses(self) -> None:
        self._write_discovery()
        # Resume discover without hitting API
        rc = ps.main(["discover", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
        self.assertEqual(rc, 0)
        disc_step = json.loads((self.eval_dir / "steps/discover.json").read_text(encoding="utf-8"))
        self.assertEqual(disc_step["status"], "REUSED_EXISTING")

        # Filter with top-tier-and-senior-cups
        rc = ps.main(["filter", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
        self.assertEqual(rc, 0)
        major = json.loads((self.eval_dir / "fixtures_major.json").read_text(encoding="utf-8"))
        self.assertEqual(major["policy"]["competition_tier"], "TOP_TIER_AND_SENIOR_CUPS")
        eligible_ids = {f["fixture_id"] for f in major["fixtures"]}
        excluded_ids = {f["fixture_id"] for f in major["excluded"]}
        self.assertIn(1001, eligible_ids)
        self.assertIn(1002, eligible_ids)
        self.assertIn(1003, excluded_ids)

        filter_md = (self.eval_dir / "steps/filter.md").read_text(encoding="utf-8")
        self.assertIn("Arsenal", filter_md)
        self.assertIn("Barcelona W", filter_md)
        self.assertIn("Arsenal U21", filter_md)
        self.assertIn("YOUTH", filter_md)

    def test_03_04_05_bwin_bind_by_event_id_identity_and_women(self) -> None:
        self._write_discovery()
        ps.main(["filter", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])

        catalog_path = self.eval_dir / "catalog.json"
        catalog_path.write_text(
            json.dumps({
                "catalog_observed": [
                    {
                        "bwin_event_id": "777001",
                        "bwin_url": "https://www.bwin.es/es/sports/eventos/arsenal-chelsea-2:777001",
                        "text": "Arsenal - Chelsea Hoy / 21:00",
                    },
                    {
                        "bwin_event_id": "777002",
                        "bwin_url": "https://www.bwin.es/es/sports/eventos/barcelona-f-real-madrid-f-2:777002",
                        "text": "Barcelona F - Real Madrid F Hoy / 21:00",
                    },
                ]
            }),
            encoding="utf-8",
        )

        # Bind 1001 using bare Event ID resolved from local catalog
        rc = ps.main([
            "bwin-bind",
            "--root", str(self.root),
            "--eval-dir", str(self.eval_dir),
            "--asof", self.asof,
            "--fixture-id", "1001",
            "--event-id", "777001",
            "--catalog-snapshot", str(catalog_path),
        ])
        self.assertEqual(rc, 0)

        # Bind 1002 (female) using full URL
        rc = ps.main([
            "bwin-bind",
            "--root", str(self.root),
            "--eval-dir", str(self.eval_dir),
            "--asof", self.asof,
            "--fixture-id", "1002",
            "--url", "https://www.bwin.es/es/sports/eventos/barcelona-f-real-madrid-f-2:777002",
        ])
        self.assertEqual(rc, 0)

        # Wrong identity: bind 1002 with male Arsenal-Chelsea URL -> conflict (exit code 2)
        rc_bad = ps.main([
            "bwin-bind",
            "--root", str(self.root),
            "--eval-dir", str(self.eval_dir),
            "--asof", self.asof,
            "--fixture-id", "1002",
            "--url", "https://www.bwin.es/es/sports/eventos/arsenal-chelsea-2:777999",
        ])
        self.assertEqual(rc_bad, 2)

        # Unresolvable bare Event ID must NOT fabricate a URL
        rc_unres = ps.main([
            "bwin-bind",
            "--root", str(self.root),
            "--eval-dir", str(self.eval_dir),
            "--asof", self.asof,
            "--fixture-id", "1001",
            "--event-id", "9999999",
        ])
        self.assertEqual(rc_unres, 2)

    def test_07_08_09_10_11_12_partial_stats_patterns_codex_model_and_immutability(self) -> None:
        self._write_discovery()
        ps.main(["filter", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])

        # Mark 1001 as BWIN_MANUAL_LINK_VERIFIED while 1002 remains PENDING_MANUAL
        bwin_dir = self.eval_dir / "prospective_trial/bwin"
        bwin_dir.mkdir(parents=True, exist_ok=True)
        (bwin_dir / "manual_bwin_links.json").write_text(
            json.dumps({
                "schema_version": bml.MANUAL_LINKS_SCHEMA,
                "links": {
                    "1001": {
                        "fixture_id": 1001,
                        "extracted_event_id": "777001",
                        "bwin_url": "https://www.bwin.es/es/sports/eventos/arsenal-chelsea-2:777001",
                        "identity_status": "BWIN_MANUAL_LINK_VERIFIED",
                    }
                },
            }),
            encoding="utf-8",
        )

        # bwin-scan + bwin-list
        self.assertEqual(ps.main(["bwin-scan", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--no-browser"]), 0)
        self.assertEqual(ps.main(["bwin-list", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--include-verified"]), 0)
        recon = json.loads((bwin_dir / "bwin_scan_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(recon["counts"]["verified"], 1)
        self.assertEqual(recon["counts"]["pending_manual"], 1)

        # Partial continuation: stats-fetch runs ONLY for verified 1001 (mocked evaluator)
        major_doc = json.loads((self.eval_dir / "fixtures_major.json").read_text(encoding="utf-8"))
        pre_match_1001 = _make_preselection_match(self.fx_male)
        fake_pre = {
            "schema_version": "1.3-preselection",
            "generated_at_utc": self.asof,
            "source_filter_sha256": hashlib.sha256((self.eval_dir / "fixtures_major.json").read_bytes()).hexdigest(),
            "methodology": {"sample_history_cap": 20, "sample_details_cap": 8},
            "counts": {"evaluated": 1, "fully_evaluated": 1, "api_errors": 0},
            "api_usage": {"requests_sent": 0, "cache_hits": 0},
            "api_errors": {},
            "matches": [pre_match_1001],
            "excluded_stale": [],
            "source_batches": [],
        }
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", return_value=fake_pre) as mock_eval:
            rc = ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
            self.assertEqual(rc, 0)
            mock_eval.assert_called_once()
            self.assertEqual(mock_eval.call_args.kwargs["fixture_ids"], {1001})

        # Resume stats-fetch does NOT call API again
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator") as mock_eval2:
            rc = ps.main(["stats-fetch", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
            self.assertEqual(rc, 0)
            mock_eval2.assert_not_called()

        # Patterns step
        rc = ps.main(["patterns", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.eval_dir / "prospective_trial/tactical/tactical_signal_engine.json").is_file())

        # Odds capture & immutability preservation
        snap_src = self.eval_dir / "snap1.txt"
        snap_src.write_text(
            "Event ID: 777001\nURL: https://www.bwin.es/es/sports/eventos/arsenal-chelsea-2:777001\n"
            "Generado: 2026-11-20T12:00:00+00:00\n\nResultado del partido\n1\n2.10\nX\n3.40\n2\n3.50\n",
            encoding="utf-8",
        )
        rc = ps.main(["odds-capture", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--fixture-id", "1001", "--snapshot-file", str(snap_src)])
        self.assertEqual(rc, 0)
        canonical_txt = self.root / "outputs/matches/777001/inputs/cuotas_bwin_777001.txt"
        self.assertTrue(canonical_txt.is_file())
        orig_bytes = canonical_txt.read_bytes()

        # Second odds-capture with new file must NOT overwrite canonical_txt
        snap_src2 = self.eval_dir / "snap2.txt"
        snap_src2.write_text(
            "Event ID: 777001\nURL: https://www.bwin.es/es/sports/eventos/arsenal-chelsea-2:777001\n"
            "Generado: 2026-11-20T12:05:00+00:00\n\nResultado del partido\n1\n2.20\nX\n3.30\n2\n3.30\n",
            encoding="utf-8",
        )
        rc = ps.main(["odds-capture", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--fixture-id", "1001", "--snapshot-file", str(snap_src2)])
        self.assertEqual(rc, 0)
        self.assertEqual(canonical_txt.read_bytes(), orig_bytes)

        # Reasoning package for Codex (isolated under outputs/matches/777001)
        rc = ps.main(["reasoning-package", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--fixture-id", "1001"])
        self.assertEqual(rc, 0)
        codex_md = self.root / "outputs/matches/777001/codex_reasoning_input.md"
        self.assertTrue(codex_md.is_file())
        self.assertIn("Arsenal – Chelsea", codex_md.read_text(encoding="utf-8"))

        # Model step (B0 operational + CAND_2B3B_V1 experimental)
        rc = ps.main(["model", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof, "--fixture-id", "1001"])
        self.assertEqual(rc, 0)
        model_json = self.root / "outputs/matches/777001/model/model_report_777001.json"
        self.assertTrue(model_json.is_file())

        # Status command
        rc = ps.main(["status", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])
        self.assertEqual(rc, 0)

    def test_13_research_without_bwin_and_qualitative_import_guards(self) -> None:
        self._write_discovery()
        ps.main(["filter", "--root", str(self.root), "--eval-dir", str(self.eval_dir), "--asof", self.asof])

        pre_match_1002 = _make_preselection_match(self.fx_female)
        fake_pre = {
            "schema_version": "1.3-preselection",
            "generated_at_utc": self.asof,
            "source_filter_sha256": hashlib.sha256((self.eval_dir / "fixtures_major.json").read_bytes()).hexdigest(),
            "methodology": {"sample_history_cap": 20, "sample_details_cap": 8},
            "counts": {"evaluated": 1, "fully_evaluated": 1, "api_errors": 0},
            "api_usage": {"requests_sent": 0, "cache_hits": 0},
            "api_errors": {},
            "matches": [pre_match_1002],
            "excluded_stale": [],
            "source_batches": [],
        }
        with patch("config.constants.API_KEY", "dummy-key", create=True), \
             patch("config.constants.FOOTBALL_BASE_URL", "https://example.invalid", create=True), \
             patch("config.constants.REQUEST_TIMEOUT", 10, create=True), \
             patch("preselect_matches.run_evaluator", return_value=fake_pre):
            rc = ps.main([
                "stats-fetch",
                "--root", str(self.root),
                "--eval-dir", str(self.eval_dir),
                "--asof", self.asof,
                "--fixture-id", "1002",
                "--research-without-bwin",
            ])
            self.assertEqual(rc, 0)

        research_pre = json.loads((self.eval_dir / "prospective_trial/research_only/preselection.json").read_text(encoding="utf-8"))
        self.assertFalse(research_pre["matches"][0]["commercial_eligible"])
        self.assertTrue(research_pre["matches"][0]["research_only_without_bwin"])

        rc = ps.main([
            "reasoning-package",
            "--root", str(self.root),
            "--eval-dir", str(self.eval_dir),
            "--asof", self.asof,
            "--fixture-id", "1002",
            "--research-without-bwin",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "outputs/matches/research_fixture_1002/codex_reasoning_input.md").is_file())

        # Qualitative import with mismatched fixture_id must fail
        bad_q = self.eval_dir / "bad_qual.json"
        bad_q.write_text(json.dumps({
            "fixture_id": 9999,
            "analysis_cutoff_utc": self.asof,
            "provenance": ["local_test"],
        }), encoding="utf-8")
        self.assertEqual(
            ps.main([
                "reasoning-package",
                "--root", str(self.root),
                "--eval-dir", str(self.eval_dir),
                "--asof", self.asof,
                "--fixture-id", "1002",
                "--research-without-bwin",
                "--import-qualitative", str(bad_q),
            ]),
            2,
        )

        # Valid qualitative import succeeds
        good_q = self.eval_dir / "good_qual.json"
        good_q.write_text(json.dumps({
            "fixture_id": 1002,
            "analysis_cutoff_utc": self.asof,
            "provenance": ["codex_manual_review"],
            "summary": "Análisis táctico cualitativo sin probabilidades inventadas.",
        }), encoding="utf-8")
        self.assertEqual(
            ps.main([
                "reasoning-package",
                "--root", str(self.root),
                "--eval-dir", str(self.eval_dir),
                "--asof", self.asof,
                "--fixture-id", "1002",
                "--research-without-bwin",
                "--import-qualitative", str(good_q),
            ]),
            0,
        )
        self.assertTrue((self.root / "outputs/matches/research_fixture_1002/qualitative_imported.json").is_file())


if __name__ == "__main__":
    unittest.main()
