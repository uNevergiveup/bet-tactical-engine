#!/usr/bin/env python3
"""Iteration 1.2 tests reproducing and verifying P0-C1 through P0-C4."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import prematch_case
import prematch_single
from bet_engine.orchestration.adapters import run_shadow_for_event_dir
import test_p0_temporal_and_shadow_sync as _p0_sync_mod


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_confirmed_22_lineup_payload(home_id: int = 26, away_id: int = 29) -> list[dict]:
    def _team_lu(tid: int, name: str, base_pid: int) -> dict:
        return {
            "team": {"id": tid, "name": name},
            "coach": {"id": tid * 10, "name": f"Coach {name}"},
            "formation": "4-3-3",
            "startXI": [
                {"player": {"id": base_pid + i, "name": f"{name} P{i}", "number": i + 1, "pos": "M"}}
                for i in range(11)
            ],
            "substitutes": [],
        }
    return [_team_lu(home_id, "Argentina", 1000), _team_lu(away_id, "Costa Rica", 2000)]


class TestP0C1OddsFreshnessInShadow(unittest.TestCase):
    def test_missing_capture_header_is_excluded_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
            # Remove Captura: line completely
            lines = [ln for ln in odds_txt.read_text(encoding="utf-8").splitlines() if not ln.startswith("Captura:")]
            odds_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
            m_path = ev_dir / "manifest.json"
            m = json.loads(m_path.read_text(encoding="utf-8"))
            m["inputs"]["odds"]["sha256"] = _sha256(odds_txt)
            m_path.write_text(json.dumps(m), encoding="utf-8")

            res = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T21:12:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertTrue(res["ok"])
            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text(encoding="utf-8"))
            self.assertEqual(sh["odds_freshness_status"], "MISSING_ODDS_CAPTURE_TIMESTAMP")
            self.assertEqual(sh["odds_selection_count"], 0)
            self.assertIsNone(sh["prices_evaluated_at_utc"])

    def test_invalid_or_naive_capture_timestamp_is_excluded_not_loaded(self) -> None:
        for bad_cap in ("not-a-timestamp", "2026-11-17T21:10:00"):  # invalid and naive without tz
            with self.subTest(bad_cap=bad_cap):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    ev_dir, odds_txt, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
                    txt = odds_txt.read_text(encoding="utf-8").replace(
                        "Captura: 2026-11-17T22:10:00+01:00", f"Captura: {bad_cap}"
                    )
                    odds_txt.write_text(txt, encoding="utf-8")
                    m_path = ev_dir / "manifest.json"
                    m = json.loads(m_path.read_text(encoding="utf-8"))
                    m["inputs"]["odds"]["sha256"] = _sha256(odds_txt)
                    m_path.write_text(json.dumps(m), encoding="utf-8")

                    res = run_shadow_for_event_dir(
                        root, "7909713", historical=False,
                        now_utc="2026-11-17T21:12:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
                    )
                    self.assertTrue(res["ok"])
                    sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text(encoding="utf-8"))
                    self.assertEqual(sh["odds_freshness_status"], "INVALID_ODDS_CAPTURE_TIMESTAMP")
                    self.assertEqual(sh["odds_selection_count"], 0)
                    self.assertIsNone(sh["prices_evaluated_at_utc"])

    def test_old_capture_without_now_utc_is_expired_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
            # Clear differential_refresh so bundle.cutoff_utc falls back to old generated_at
            ctx_p = ev_dir / "inputs" / "match_engine_7909713.json"
            ctx_d = json.loads(ctx_p.read_text(encoding="utf-8"))
            ctx_d.pop("differential_refresh", None)
            ctx_p.write_text(json.dumps(ctx_d), encoding="utf-8")
            m_p = ev_dir / "manifest.json"
            m_d = json.loads(m_p.read_text(encoding="utf-8"))
            m_d["inputs"]["context_json"]["sha256"] = _sha256(ctx_p)
            m_p.write_text(json.dumps(m_d), encoding="utf-8")

            # Without passing now_utc, wall-clock in 2026-11-18 is > 30m after 2026-11-17T21:10Z
            res = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc=None, max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertTrue(res["ok"])
            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text(encoding="utf-8"))
            self.assertTrue(sh["odds_freshness_status"].startswith("EXPIRED_ODDS_EXCLUDED"))
            self.assertEqual(sh["odds_selection_count"], 0)

    def test_historical_replay_excludes_post_cutoff_or_post_kickoff_odds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
            # 23:15 UTC is AFTER 23:00 UTC kickoff and AFTER 22:34 UTC cutoff!
            txt = odds_txt.read_text(encoding="utf-8").replace(
                "Captura: 2026-11-17T22:10:00+01:00", "Captura: 2026-11-18T00:15:00+01:00"
            )
            odds_txt.write_text(txt, encoding="utf-8")
            m_path = ev_dir / "manifest.json"
            m = json.loads(m_path.read_text(encoding="utf-8"))
            m["inputs"]["odds"]["sha256"] = _sha256(odds_txt)
            m_path.write_text(json.dumps(m), encoding="utf-8")

            res = run_shadow_for_event_dir(
                root, "7909713", historical=True,
                now_utc="2026-11-17T22:34:00+00:00", max_odds_age_minutes=120, min_lead_minutes=0,
            )
            self.assertTrue(res["ok"])
            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text(encoding="utf-8"))
            self.assertEqual(sh["odds_freshness_status"], "POST_CUTOFF_OR_LIVE_ODDS_EXCLUDED")
            self.assertEqual(sh["odds_selection_count"], 0)

    def test_missing_manifest_odds_sha256_blocks_in_prematch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, _, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
            m_path = ev_dir / "manifest.json"
            m = json.loads(m_path.read_text(encoding="utf-8"))
            m["inputs"]["odds"].pop("sha256", None)
            m_path.write_text(json.dumps(m), encoding="utf-8")

            res = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T21:12:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertFalse(res["ok"])
            self.assertIn("ODDS_MANIFEST_SHA_MISSING", res["error"])


class TestP0C2ShadowIntegrityInEvaluateClosure(unittest.TestCase):
    def _stage_finalized_22_starters(self, root: Path) -> tuple[Path, dict, dict]:
        ev_dir, odds_txt, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
        # Ensure context_json has 22 structured starters and fresh odds within T-60..T-5
        ctx_path = ev_dir / "inputs" / "match_engine_7909713.json"
        ctx_doc = json.loads(ctx_path.read_text(encoding="utf-8"))
        ctx_doc.setdefault("context", {})["lineups"] = {"response": _make_confirmed_22_lineup_payload()}
        ctx_path.write_text(json.dumps(ctx_doc), encoding="utf-8")

        fresh_odds = _p0_sync_mod._make_odds_txt(
            "7909713", "Argentina", "Costa Rica", "2026-11-17T23:45:00+01:00",
            home_odd=1.055, draw_odd=15.5, away_odd=29.0, extra_selections=10,
        )
        odds_txt.write_text(fresh_odds, encoding="utf-8")

        m_path = ev_dir / "manifest.json"
        m = json.loads(m_path.read_text(encoding="utf-8"))
        m["status"] = "FINALIZADO"
        m["finish_completed_at"] = "2026-11-17T22:50:00+00:00"
        m["inputs"]["context_json"]["sha256"] = _sha256(ctx_path)
        m["inputs"]["odds"]["sha256"] = _sha256(odds_txt)
        m_path.write_text(json.dumps(m), encoding="utf-8")
        case = json.loads((ev_dir / "case.json").read_text(encoding="utf-8"))
        return ev_dir, case, m

    def test_shadow_absent_unreadable_missing_hashes_stale_and_valid(self) -> None:
        now_dt = datetime.fromisoformat("2026-11-17T22:51:00+00:00")

        # 1) Shadow absent: final_prematch_valid stays True (temporal closure), but eligible_for_actionable_pick MUST be False
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, case, m = self._stage_finalized_22_lineup(root) if False else self._stage_finalized_22_starters(root)
            c_absent = prematch_case._evaluate_closure(ev_dir, case, m, now_override=now_dt)
            self.assertTrue(c_absent["final_prematch_valid"])
            self.assertFalse(c_absent["shadow_present"])
            self.assertFalse(c_absent["shadow_verified"])
            self.assertFalse(c_absent["eligible_for_actionable_pick"])
            self.assertIn("SHADOW_ABSENT", c_absent["shadow_integrity_reasons"])

        # 2) Run valid Shadow -> all hashes match -> eligible_for_actionable_pick=True
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, case, m = self._stage_finalized_22_starters(root)
            res = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T22:50:00+00:00", max_odds_age_minutes=30, min_lead_minutes=5,
            )
            self.assertTrue(res["ok"])
            c_valid = prematch_case._evaluate_closure(ev_dir, case, m, now_override=now_dt)
            self.assertTrue(c_valid["final_prematch_valid"])
            self.assertTrue(c_valid["shadow_present"])
            self.assertTrue(c_valid["shadow_verified"])
            self.assertFalse(c_valid["shadow_stale"])
            self.assertTrue(c_valid["eligible_for_actionable_pick"])

            sh_path = ev_dir / "shadow" / "shadow_summary_7909713.json"
            valid_sh = json.loads(sh_path.read_text(encoding="utf-8"))

            # 3) Missing odds_sha256 in Shadow -> not verified & never actionable (preserves historical final_prematch_valid)
            sh_no_odds_sha = dict(valid_sh)
            sh_no_odds_sha.pop("odds_sha256", None)
            sh_path.write_text(json.dumps(sh_no_odds_sha), encoding="utf-8")
            c_no_odds = prematch_case._evaluate_closure(ev_dir, case, m, now_override=now_dt)
            self.assertFalse(c_no_odds["eligible_for_actionable_pick"])
            self.assertFalse(c_no_odds["shadow_verified"])
            self.assertIn("SHADOW_ODDS_HASH_MISSING", c_no_odds["shadow_integrity_reasons"])

            # 4) Missing context_json_sha256 in Shadow -> not verified & never actionable
            sh_no_ctx_sha = dict(valid_sh)
            sh_no_ctx_sha.pop("context_json_sha256", None)
            sh_path.write_text(json.dumps(sh_no_ctx_sha), encoding="utf-8")
            c_no_ctx = prematch_case._evaluate_closure(ev_dir, case, m, now_override=now_dt)
            self.assertFalse(c_no_ctx["eligible_for_actionable_pick"])
            self.assertFalse(c_no_ctx["shadow_verified"])
            self.assertIn("SHADOW_CONTEXT_HASH_MISSING", c_no_ctx["shadow_integrity_reasons"])

            # 5) Mismatched hash vs manifest
            sh_mismatch = dict(valid_sh)
            sh_mismatch["odds_sha256"] = "0" * 64
            sh_path.write_text(json.dumps(sh_mismatch), encoding="utf-8")
            c_mis = prematch_case._evaluate_closure(ev_dir, case, m, now_override=now_dt)
            self.assertFalse(c_mis["eligible_for_actionable_pick"])
            self.assertTrue(c_mis["shadow_stale"])
            self.assertFalse(c_mis["final_prematch_valid"])

            # 6) Shadow older than odds_refresh_at_utc
            m_refreshed = dict(m, odds_refresh_at_utc="2026-11-17T22:50:30+00:00")
            sh_older = dict(valid_sh, probabilities_computed_at_utc="2026-11-17T22:50:00+00:00", prices_evaluated_at_utc="2026-11-17T22:50:00+00:00")
            sh_path.write_text(json.dumps(sh_older), encoding="utf-8")
            c_old = prematch_case._evaluate_closure(ev_dir, case, m_refreshed, now_override=now_dt)
            self.assertFalse(c_old["eligible_for_actionable_pick"])
            self.assertTrue(c_old["shadow_stale"])
            self.assertFalse(c_old["final_prematch_valid"])

            # 7) Unreadable JSON
            sh_path.write_text("{broken json", encoding="utf-8")
            c_bad = prematch_case._evaluate_closure(ev_dir, case, m, now_override=now_dt)
            self.assertEqual(c_bad["shadow_status"], "UNREADABLE")
            self.assertFalse(c_bad["eligible_for_actionable_pick"])
            self.assertTrue(c_bad["shadow_stale"])
            self.assertFalse(c_bad["final_prematch_valid"])


class TestP0C3AndC4TimestampsAndOperationalRefresh(unittest.TestCase):
    def test_authentic_timestamps_and_null_prices_when_no_odds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, _, _ = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
            res = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T21:12:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertTrue(res["ok"])
            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text(encoding="utf-8"))
            p_ts = datetime.fromisoformat(sh["probabilities_computed_at_utc"])
            pr_ts = datetime.fromisoformat(sh["prices_evaluated_at_utc"])
            self.assertLessEqual(p_ts, pr_ts)
            self.assertEqual(sh["timestamp_granularity"], "IN_PROCESS_SEQUENTIAL_SHADOW_RUN")
            self.assertEqual(sh["shadow_report"]["probabilities_computed_at_utc"], sh["probabilities_computed_at_utc"])
            self.assertEqual(sh["shadow_report"]["prices_evaluated_at_utc"], sh["prices_evaluated_at_utc"])

            # When odds are expired, prices_evaluated_at_utc MUST be None (not fabricated!)
            res_exp = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T22:50:00+00:00", max_odds_age_minutes=15, min_lead_minutes=0,
            )
            self.assertTrue(res_exp["ok"])
            sh_exp = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(sh_exp["probabilities_computed_at_utc"])
            self.assertIsNone(sh_exp["prices_evaluated_at_utc"])

    def test_cmd_refresh_and_refresh_context_distinguish_shadow_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, selected_json = _p0_sync_mod.TestP0BShadowSyncAndSnapshots()._stage_event(root)
            new_odds = _p0_sync_mod._make_odds_txt(
                "7909713", "Argentina", "Costa Rica", "2026-11-17T23:48:54+01:00",
                home_odd=1.055, draw_odd=15.5, away_odd=29.0, extra_selections=10,
            )
            ctx = {
                "root": root, "directory": ev_dir, "event_id": "7909713", "fixture_id": 1647372,
                "url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "home": "Argentina", "away": "Costa Rica",
                "kickoff": datetime.fromisoformat("2026-11-18T00:00:00+01:00"),
                "selection": json.loads(selected_json.read_text(encoding="utf-8")),
                "selected_path": selected_json, "selected_sha": _sha256(selected_json),
                "decision_path": ev_dir / "selection" / "selection_decision.json",
                "decision_sha": _sha256(ev_dir / "selection" / "selection_decision.json"),
            }
            args = SimpleNamespace(min_refresh_lead_minutes=5, max_odds_age_minutes=30, multi=False)

            def fake_run(cmd, cwd, env=None, log=None):
                odds_txt.write_text(new_odds, encoding="utf-8")
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(f"TXT: {odds_txt}\n", encoding="utf-8")
                return 0

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 mock.patch("prematch_single.ensure_prematch"), \
                 mock.patch("prematch_single.ensure_root"), \
                 mock.patch("prematch_single.run", side_effect=fake_run), \
                 mock.patch("prematch_single.local_now", return_value=datetime.fromisoformat("2026-11-17T23:49:00+01:00")), \
                 mock.patch("prematch_single.iso_utc", return_value="2026-11-17T22:49:00+00:00"), \
                 mock.patch("bet_engine.orchestration.run_shadow_for_event_dir", return_value={"ok": False, "error": "SIMULATED_FAIL"}):
                rc = prematch_single.cmd_refresh(ctx, args)

            self.assertEqual(rc, 0)
            out_txt = buf.getvalue()
            self.assertNotIn("Shadow actualizados", out_txt)
            self.assertIn("SHADOW_FAILED", out_txt)
            se = json.loads((ev_dir / "single_execution.json").read_text(encoding="utf-8"))
            self.assertEqual(se["steps"]["refresh-odds"]["result"], "ODDS_UPDATED_SHADOW_FAILED")
            manifest = json.loads((ev_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["shadow_sync_state"], "SHADOW_FAILED")

            # Also test cmd_refresh_context when Shadow returns ok=False
            snap_file = ev_dir / "context_acquisitions" / "snap.json"
            snap_file.parent.mkdir(parents=True, exist_ok=True)
            snap_file.write_text("{}", encoding="utf-8")
            snap_sha = _sha256(snap_file)
            ctx_args = SimpleNamespace(
                min_refresh_lead_minutes=5, snapshot=snap_file, snapshot_sha256=snap_sha, lineup_cutoff_minutes=15,
            )
            fake_out_dir = ev_dir / "context_updates" / "run1"
            fake_out_dir.mkdir(parents=True, exist_ok=True)
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2), \
                 mock.patch("prematch_single.ensure_prematch"), \
                 mock.patch("prematch_single.local_now", return_value=datetime.fromisoformat("2026-11-17T23:49:30+01:00")), \
                 mock.patch("prematch_context_delta.refresh_lineups", return_value=(manifest, fake_out_dir)), \
                 mock.patch("bet_engine.orchestration.run_shadow_for_event_dir", return_value={"ok": False, "error": "SIMULATED_CTX_FAIL"}):
                rc2 = prematch_single.cmd_refresh_context(ctx, ctx_args)

            self.assertEqual(rc2, 0)
            self.assertNotIn("REVALIDATION_AND_SHADOW_UPDATED", buf2.getvalue())
            se2 = json.loads((ev_dir / "single_execution.json").read_text(encoding="utf-8"))
            self.assertEqual(se2["steps"]["refresh-context"]["result"], "CONTEXT_UPDATED_SHADOW_FAILED")


if __name__ == "__main__":
    unittest.main()
