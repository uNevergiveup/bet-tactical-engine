#!/usr/bin/env python3
"""Pruebas focalizadas de separación de ventanas PREMATCH, Markdown URLs y límites T-46..post-kickoff."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bwin_discovery as bd
import bwin_manual_links as bml
import bwin_market_preflight as pf
import prematch_daily as daily
import prematch_multi as multi
import prematch_prospective_trial as trial
import prematch_single as single


ROOT = Path(__file__).resolve().parent.parent


class FakeBrowser:
    def close(self) -> None:
        pass


class FakePlaywright:
    def __init__(self, counter: dict[str, int] | None = None) -> None:
        self.counter = counter

    def __enter__(self):
        if self.counter is not None:
            self.counter["launches"] = self.counter.get("launches", 0) + 1
        return type(
            "P",
            (),
            {"chromium": type("C", (), {"launch": staticmethod(lambda **kw: FakeBrowser())})()},
        )()

    def __exit__(self, *args) -> None:
        pass


class PrematchWindowBoundaryTests(unittest.TestCase):
    def _build_eval_fixture(self, td: str, kickoff_utc: datetime) -> dict:
        root = Path(td)
        eval_dir = root / "outputs/discovery/2026-11-20_1500_0300/evaluations/eval_1"
        base = eval_dir / "prospective_trial"
        ko_m = kickoff_utc.astimezone(trial.TZ).isoformat()
        fixture = {
            "fixture_id": 1628569,
            "home": "Ranchers Bees",
            "away": "Doma United",
            "home_id": 101,
            "away_id": 102,
            "league": "NPFL",
            "league_id": 399,
            "season": 2026,
            "country": "Nigeria",
            "gender": "M",
            "kickoff_madrid": ko_m,
            "kickoff_utc": kickoff_utc.isoformat(),
            "analysis_cutoff_utc": (kickoff_utc - timedelta(hours=3)).isoformat(),
            "status": "NS",
            "filter_status": "ELIGIBLE",
            "competition_category": "SENIOR_MALE_TOP_TIER",
            "details_checked": True,
            "competition_evidence": {"goals": {"matches": 12}},
            "home_evidence": {
                "last_matches_count": 12,
                "history": {
                    "same_competition_total": 12,
                    "same_competition_goals_for": {"n": 12, "mean": 1.6, "cv": 0.25},
                    "same_competition_goals_against": {"n": 12, "mean": 1.1, "cv": 0.25},
                },
                "details": {},
            },
            "away_evidence": {
                "last_matches_count": 12,
                "history": {
                    "same_competition_total": 12,
                    "same_competition_goals_for": {"n": 12, "mean": 1.4, "cv": 0.25},
                    "same_competition_goals_against": {"n": 12, "mean": 1.3, "cv": 0.25},
                },
                "details": {},
            },
        }
        trial.dump(eval_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": [fixture]})
        trial.dump(
            eval_dir / "fixtures_major.json",
            {"schema_version": "1.0-major-leagues", "fixtures": [fixture], "review": [], "excluded": []},
        )
        pre_path = base / "enriched/preselection.json"
        trial.dump(
            pre_path,
            {
                "schema_version": "1.3-preselection",
                "generated_at_utc": (kickoff_utc - timedelta(hours=3)).isoformat(),
                "matches": [fixture],
            },
        )
        url = "https://www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001"
        return {
            "root": root,
            "eval_dir": eval_dir,
            "base": base,
            "pre_path": pre_path,
            "fixture": fixture,
            "eid": "7990001",
            "fid": 1628569,
            "url": url,
            "ko_utc": kickoff_utc,
            "ko_m": ko_m,
        }

    def _mock_summary_for(self, env: dict, now_utc: datetime) -> dict:
        return {
            "fixture_id": env["fid"],
            "home": "Ranchers Bees",
            "away": "Doma United",
            "home_id": 101,
            "away_id": 102,
            "league": "NPFL",
            "league_id": 399,
            "season": 2026,
            "gender": "M",
            "kickoff_madrid": env["ko_m"],
            "bwin_event_id": env["eid"],
            "bwin_url": env["url"],
            "status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
            "category_corroboration_status": "CORROBORATED",
            "inspection": {
                "status": "OPENED",
                "page_title": "Ranchers Bees - Doma United | NPFL",
                "title_teams_match": True,
                "event_id_from_url": env["eid"],
                "event_start_machine_readable": env["ko_m"],
                "kickoff_difference_minutes": 0.0,
                "observed_at_utc": now_utc.isoformat(),
                "page_category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                "competition_match_score": 1.0,
                "supporting_identity_checks": {
                    "competition_compatible": True,
                    "category_compatible": True,
                },
                "market_headings": {},
            },
        }

    def test_markdown_and_invalid_urls_rejected_explicitly(self) -> None:
        ko_utc = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        now_utc = ko_utc - timedelta(minutes=25)
        with tempfile.TemporaryDirectory() as td:
            env = self._build_eval_fixture(td, ko_utc)
            url = env["url"]
            for bad_md in (
                f"[{url}]({url})",
                f"[Ranchers Bees - Doma United]({url})",
                f"[{url}]",
            ):
                with self.assertRaisesRegex(bml.ManualBwinLinkError, "MANUAL_BWIN_URL_MARKDOWN_FORMAT_FORBIDDEN"):
                    bml.parse_manual_bwin_url(bad_md)
                with self.assertRaisesRegex(bml.ManualBwinLinkError, "MANUAL_BWIN_URL_MARKDOWN_FORMAT_FORBIDDEN"):
                    bml.register_manual_bwin_urls(env["eval_dir"], [(env["fid"], bad_md)], now_utc=now_utc)

            cli = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "prematch_daily.py"),
                    "--root",
                    str(env["root"]),
                    "--eval-dir",
                    str(env["eval_dir"]),
                    "--bind-bwin-url",
                    "--manual-bwin-url",
                    f"{env['fid']}=[{url}]({url})",
                    "--now-utc",
                    now_utc.isoformat(),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(cli.returncode, 2)
            self.assertIn("MANUAL_BWIN_URL_MARKDOWN_FORMAT_FORBIDDEN", cli.stderr)

            for bad_scheme_or_host in (
                url.replace("https://", "http://"),
                "https://evil.example.com/es/sports/eventos/ranchers-bees-doma-united-2:7990001",
                "https://user:pass@www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001",
                f"{url} extra",
            ):
                with self.assertRaises(bml.ManualBwinLinkError):
                    bml.parse_manual_bwin_url(bad_scheme_or_host)

            parsed = bml.parse_manual_bwin_url(url)
            self.assertEqual(parsed["bwin_event_id"], env["eid"])

    def test_bind_verify_scrape_allowed_to_t5_independently_of_prepare_t45(self) -> None:
        ko_utc = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        for minutes_before in (46, 44, 31, 29, 15, 6):
            now_utc = ko_utc - timedelta(minutes=minutes_before)
            now_m = now_utc.astimezone(trial.TZ)
            with self.subTest(minutes_before=minutes_before), tempfile.TemporaryDirectory() as td:
                env = self._build_eval_fixture(td, ko_utc)
                target_txt = env["root"] / "outputs/matches" / env["eid"] / "inputs" / f"cuotas_bwin_{env['eid']}.txt"

                def fake_trial_run(root_arg, script_name, cmd_pre, logs_dir):
                    self.assertEqual(script_name, "bwin_market_preflight.py")
                    # Verifica que scrape manual NO hereda 45 de prepare sino 5 min
                    idx_min = cmd_pre.index("--min-lead-minutes")
                    self.assertEqual(cmd_pre[idx_min + 1], "5")
                    target_txt.parent.mkdir(parents=True, exist_ok=True)
                    target_txt.write_text(
                        f"Event ID: {env['eid']}\n"
                        f"URL: {env['url']}\n"
                        f"Captura: {now_m.isoformat()}\n"
                        "Acordeones: 1 | Selecciones: 2\n"
                        "MERCADO: Total de goles | Tiempo reglamentario\n"
                        "Más de 2,5 : 1.95\n"
                        "Menos de 2,5 : 1.85\n",
                        encoding="utf-8",
                    )
                    rewritten = [arg for arg in cmd_pre if arg != "--run-scraper"]
                    rewritten.append("--reuse-existing")
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc_pre = pf.main(rewritten)
                    self.assertEqual(rc_pre, 0)

                stdout_buf = io.StringIO()
                launches = {"launches": 0}
                with patch("playwright.sync_api.sync_playwright", side_effect=lambda: FakePlaywright(launches)), \
                     patch("bwin_discovery.summarize", return_value=self._mock_summary_for(env, now_utc)), \
                     patch.object(trial, "run", side_effect=fake_trial_run), \
                     contextlib.redirect_stdout(stdout_buf):
                    rc = daily.main([
                        "--root", str(env["root"]),
                        "--eval-dir", str(env["eval_dir"]),
                        "--bind-bwin-url",
                        "--verify-manual-bwin",
                        "--scrape-manual-bwin",
                        "--confirm-external",
                        "--manual-bwin-url", f"{env['fid']}={env['url']}",
                        "--now-utc", now_utc.isoformat(),
                    ])
                self.assertEqual(rc, 0, stdout_buf.getvalue())
                payload = json.loads(stdout_buf.getvalue())
                self.assertEqual(launches["launches"], 1)
                self.assertEqual(payload["operation_windows"]["preparation_min_lead_minutes"], 45)
                self.assertEqual(payload["operation_windows"]["manual_bwin_min_lead_minutes"], 5)
                self.assertEqual(len(payload["live_verification"]["verified"]), 1)
                self.assertEqual(payload["scrape_results"][0]["status"], "CAPTURED_VERIFIED")
                stored = bml.load_manual_links(env["eval_dir"])["links"][str(env["fid"])]
                self.assertEqual(stored["identity_status"], "BWIN_MANUAL_LINK_VERIFIED")
                self.assertEqual(stored["operational_status"], "CLEAR")
                self.assertIsNone(stored["operational_block"])

    def test_temporal_rejections_at_t4_and_post_kickoff_preserve_pending_identity(self) -> None:
        ko_utc = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            env = self._build_eval_fixture(td, ko_utc)
            # 1) Bloquear intento de bajar el suelo manual por debajo de T-5
            t10 = ko_utc - timedelta(minutes=10)
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc_floor = daily.main([
                    "--root", str(env["root"]),
                    "--eval-dir", str(env["eval_dir"]),
                    "--bind-bwin-url",
                    "--manual-bwin-url", f"{env['fid']}={env['url']}",
                    "--manual-bwin-min-lead", "4",
                    "--now-utc", t10.isoformat(),
                ])
            self.assertEqual(rc_floor, 2)
            self.assertIn("MANUAL_BWIN_MINIMUM_5_MINUTES", err_buf.getvalue())

            # 2) T-4 en CLI (--bind-bwin-url --verify-manual-bwin --scrape-manual-bwin):
            # conserva identidad pendiente, bloquea operacionalmente y NO fabrica BWIN_IDENTITY_CONFLICT.
            t4 = ko_utc - timedelta(minutes=4)
            out_t4 = io.StringIO()
            launches = {"launches": 0}
            with patch("playwright.sync_api.sync_playwright", side_effect=lambda: FakePlaywright(launches)), \
                 patch.object(trial, "run", side_effect=AssertionError("No debe invocar scraper en T-4")), \
                 contextlib.redirect_stdout(out_t4):
                rc_t4 = daily.main([
                    "--root", str(env["root"]),
                    "--eval-dir", str(env["eval_dir"]),
                    "--bind-bwin-url",
                    "--verify-manual-bwin",
                    "--scrape-manual-bwin",
                    "--confirm-external",
                    "--manual-bwin-url", f"{env['fid']}={env['url']}",
                    "--now-utc", t4.isoformat(),
                ])
            self.assertEqual(rc_t4, 2)
            self.assertEqual(launches["launches"], 0)
            payload_t4 = json.loads(out_t4.getvalue())
            self.assertEqual(payload_t4["registration"]["temporal_blocked_count"], 1)
            self.assertEqual(payload_t4["registration"]["conflict_count"], 0)
            self.assertEqual(len(payload_t4["live_verification"]["temporal_blocked"]), 1)
            self.assertEqual(payload_t4["live_verification"]["failed"], [])
            self.assertEqual(payload_t4["scrape_results"][0]["status"], "OPERATIONAL_BLOCK_TEMPORAL")
            stored_t4 = bml.load_manual_links(env["eval_dir"])["links"][str(env["fid"])]
            self.assertEqual(stored_t4["identity_status"], "BWIN_MANUAL_LINK_PENDING_VALIDATION")
            self.assertEqual(stored_t4["conflict_reasons"], [])
            self.assertEqual(stored_t4["operational_status"], "OPERATIONAL_BLOCK_TEMPORAL")
            self.assertEqual(stored_t4["operational_block"]["reason"], "PREMATCH_MANUAL_LEAD_WINDOW_EXPIRED")

            # 3) Después del kickoff (T+1): prohibido registrar y verificación directa preserva identidad pendiente
            t_after = ko_utc + timedelta(minutes=1)
            with self.assertRaisesRegex(bml.ManualBwinLinkError, "MANUAL_BWIN_MATCH_ALREADY_STARTED"):
                bml.register_manual_bwin_urls(env["eval_dir"], [(env["fid"], env["url"])], now_utc=t_after)

            err_after = io.StringIO()
            with contextlib.redirect_stderr(err_after):
                rc_after = daily.main([
                    "--root", str(env["root"]),
                    "--eval-dir", str(env["eval_dir"]),
                    "--bind-bwin-url",
                    "--verify-manual-bwin",
                    "--confirm-external",
                    "--manual-bwin-url", f"{env['fid']}={env['url']}",
                    "--now-utc", t_after.isoformat(),
                ])
            self.assertEqual(rc_after, 2)
            self.assertIn("MANUAL_BWIN_MATCH_ALREADY_STARTED", err_after.getvalue())

            with patch("playwright.sync_api.sync_playwright", side_effect=lambda: FakePlaywright(launches)):
                ver_after = bml.verify_manual_bwin_links_online(env["eval_dir"], [env["fid"]], now_utc=t_after)
            self.assertEqual(launches["launches"], 0)
            self.assertEqual(len(ver_after["temporal_blocked"]), 1)
            self.assertEqual(ver_after["failed"], [])
            stored_after = bml.load_manual_links(env["eval_dir"])["links"][str(env["fid"])]
            self.assertEqual(stored_after["identity_status"], "BWIN_MANUAL_LINK_PENDING_VALIDATION")
            self.assertEqual(stored_after["conflict_reasons"], [])
            self.assertEqual(stored_after["operational_status"], "OPERATIONAL_BLOCK_POST_KICKOFF")
            self.assertEqual(stored_after["operational_block"]["reason"], "POST_KICKOFF_PREMATCH_FORBIDDEN")

    def test_full_horizon_matrix_t46_to_post_kickoff_by_operation(self) -> None:
        ko_utc = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        ko_m = ko_utc.astimezone(trial.TZ)
        horizons = [
            ("T-46", -46, True, True, True, True),
            ("T-44", -44, False, True, True, True),
            ("T-31", -31, False, True, True, True),
            ("T-29", -29, False, True, True, True),
            ("T-15", -15, False, True, True, True),
            ("T-6", -6, False, True, True, True),
            ("T-4", -4, False, False, False, False),
            ("T+1", +1, False, False, False, False),
        ]

        for label, offset_min, exp_prepare, exp_bind, exp_capture, exp_finish in horizons:
            now_utc = ko_utc + timedelta(minutes=offset_min)
            now_m = now_utc.astimezone(trial.TZ)
            with self.subTest(horizon=label), tempfile.TemporaryDirectory() as td:
                env = self._build_eval_fixture(td, ko_utc)
                ctx = {
                    "event_id": env["eid"],
                    "fixture_id": env["fid"],
                    "url": env["url"],
                    "home": "Ranchers Bees",
                    "away": "Doma United",
                    "kickoff": ko_m,
                    "selection_time": ko_m - timedelta(hours=3),
                    "root": env["root"],
                    "directory": env["root"] / "outputs/matches" / env["eid"],
                }
                identity_path = ctx["directory"] / "inputs/match_identity.json"
                identity_path.parent.mkdir(parents=True, exist_ok=True)
                identity_path.write_text(
                    json.dumps(
                        {
                            "bwin_event_id": env["eid"],
                            "api_fixture_id": env["fid"],
                            "kickoff_utc": ko_utc.isoformat(),
                        }
                    ),
                    encoding="utf-8",
                )
                state = {"event_id": env["eid"], "inputs": {}}

                # 1) Preparación (T-45 se mantiene sin cambios)
                with patch.object(single, "local_now", return_value=now_m):
                    if exp_prepare:
                        self.assertGreaterEqual(single.ensure_prematch(ctx, min_lead=single.PREPARE_MIN_LEAD_MINUTES), 45.0)
                    else:
                        with self.assertRaises(ValueError):
                            single.ensure_prematch(ctx, min_lead=single.PREPARE_MIN_LEAD_MINUTES)

                # 2) Vinculación manual (registro + verificación online hasta T-5)
                if offset_min > 0:
                    with self.assertRaises(bml.ManualBwinLinkError):
                        bml.register_manual_bwin_urls(env["eval_dir"], [(env["fid"], env["url"])], now_utc=now_utc)
                else:
                    reg = bml.register_manual_bwin_urls(env["eval_dir"], [(env["fid"], env["url"])], now_utc=now_utc)
                    launches = {"launches": 0}
                    with patch("playwright.sync_api.sync_playwright", side_effect=lambda: FakePlaywright(launches)), \
                         patch("bwin_discovery.summarize", return_value=self._mock_summary_for(env, now_utc)):
                        ver = bml.verify_manual_bwin_links_online(env["eval_dir"], [env["fid"]], now_utc=now_utc)
                    if exp_bind:
                        self.assertEqual(reg["temporal_blocked_count"], 0)
                        self.assertEqual(len(ver["verified"]), 1)
                        self.assertEqual(launches["launches"], 1)
                    else:
                        self.assertEqual(reg["temporal_blocked_count"], 1)
                        self.assertEqual(len(ver["temporal_blocked"]), 1)
                        self.assertEqual(launches["launches"], 0)
                        stored = bml.load_manual_links(env["eval_dir"])["links"][str(env["fid"])]
                        self.assertEqual(stored["identity_status"], "BWIN_MANUAL_LINK_PENDING_VALIDATION")

                # 3) Captura (bwin_market_preflight validate_match + refresh-odds T-5)
                match_rec = {
                    "fixture_id": env["fid"],
                    "home": "Ranchers Bees",
                    "away": "Doma United",
                    "kickoff_madrid": ko_m.isoformat(),
                    "bwin_event_id": env["eid"],
                    "bwin_url": env["url"],
                    "status": "IDENTITY_VERIFIED_AUTOMATICALLY",
                    "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                    "inspection": self._mock_summary_for(env, now_utc)["inspection"],
                }
                checked = pf.validate_match(match_rec, [], now_m, bml.MANUAL_BWIN_MIN_LEAD_MINUTES, 180)
                if exp_capture:
                    self.assertEqual(checked["status"], "READY_FOR_SCRAPER")
                else:
                    self.assertEqual(checked["status"], "BLOCKED")
                    self.assertIn("ALREADY_STARTED_OR_INSUFFICIENT_PREMATCH_MARGIN", checked["reasons"])

                # 4) Finalización (finish T-5 y final_odds_check)
                with patch.object(single, "local_now", return_value=now_m):
                    if exp_finish:
                        rem = single.ensure_prematch(ctx, min_lead=single.FINISH_MIN_LEAD_MINUTES)
                        self.assertGreaterEqual(rem, 5.0)
                        multi_rem = multi.ensure_event_operation_window(
                            env["root"], env["eid"], state, min_lead=multi.FINISH_MIN_LEAD_MINUTES, operation="finish", now=now_m
                        )
                        self.assertGreaterEqual(multi_rem, 5.0)
                    else:
                        with self.assertRaises(ValueError):
                            single.ensure_prematch(ctx, min_lead=single.FINISH_MIN_LEAD_MINUTES)
                        with self.assertRaises(ValueError):
                            multi.ensure_event_operation_window(
                                env["root"], env["eid"], state, min_lead=multi.FINISH_MIN_LEAD_MINUTES, operation="finish", now=now_m
                            )


if __name__ == "__main__":
    unittest.main()
