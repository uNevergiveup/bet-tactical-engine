import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import prematch_case
import prematch_daily
import prematch_daily_csv
import prematch_phase1
import prematch_single

REPO_ROOT = Path(__file__).resolve().parents[1]


class PostChelseaPsgP0Tests(unittest.TestCase):
    def _make_single_event_env(
        self,
        root: Path,
        *,
        eid: str = "7913441",
        fid: int = 1638301,
        with_report: bool = True,
        with_lineups_22: bool = True,
        lead_minutes: int = 30,
        odds_age_minutes: int = 2,
    ):
        now_utc = datetime.now(timezone.utc)
        ko_utc = now_utc + timedelta(minutes=lead_minutes)
        ko_madrid = ko_utc.astimezone(prematch_single.TZ)
        cap_utc = now_utc - timedelta(minutes=odds_age_minutes)
        cap_madrid = cap_utc.astimezone(prematch_single.TZ)

        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "constants.py").write_text("# test\n", encoding="utf-8")
        for s in ("prematch_multi.py", "scraper_bwin.py", "generate_context.py"):
            if not (root / s).exists():
                (root / s).write_text("# stub\n", encoding="utf-8")

        sel_dir = root / f"sel_{eid}"
        sel_dir.mkdir(parents=True, exist_ok=True)
        sel_path = sel_dir / "selected_match.json"
        dec_path = sel_dir / "selection_decision.json"
        bwin_url = f"https://www.bwin.es/es/sports/eventos/chelsea-f-psg-f-2:{eid}"
        sel_doc = {
            "schema_version": "1.0-selected-single-fixture",
            "status": "SELECTED_FOR_DEEP_RESEARCH",
            "fixture_id": fid,
            "bwin_event_id": eid,
            "bwin_url": bwin_url,
            "home": "Chelsea W",
            "away": "Paris Saint Germain W",
            "kickoff_madrid": ko_madrid.isoformat(),
            "selection_asof_madrid": now_utc.astimezone(prematch_single.TZ).isoformat(),
            "selection_tier": "A",
            "research_eligible": True,
            "eligible": True,
            "research_missing_evidence": [],
            "hard_block_reasons": [],
            "market_families_ready_for_research": ["1X2"],
        }
        sel_path.write_text(json.dumps(sel_doc), encoding="utf-8")
        dec_doc = {
            "status": "SELECTED",
            "provisional_or_selected": {
                "fixture_id": fid,
                "bwin_event_id": eid,
                "bwin_url": bwin_url,
                "home": "Chelsea W",
                "away": "Paris Saint Germain W",
                "kickoff_madrid": ko_madrid.isoformat(),
                "selection_tier": "A",
                "research_eligible": True,
                "eligible": True,
                "research_missing_evidence": [],
                "hard_block_reasons": [],
                "market_families_ready": ["1X2"],
            },
        }
        dec_path.write_text(json.dumps(dec_doc), encoding="utf-8")

        edir = root / "outputs" / "matches" / eid
        (edir / "inputs").mkdir(parents=True, exist_ok=True)
        (edir / "instructions").mkdir(parents=True, exist_ok=True)
        (edir / "logs").mkdir(parents=True, exist_ok=True)

        odds_path = edir / "inputs" / f"cuotas_bwin_{eid}.txt"
        odds_path.write_text(
            f"Captura: {cap_madrid.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"Event ID: {eid}\nURL: {bwin_url}\n1X2\n1: 1.80\n",
            encoding="utf-8",
        )
        ctx_json = edir / "inputs" / f"match_engine_{eid}.json"
        ctx_txt = edir / "inputs" / f"match_engine_{eid}.txt"
        ctx_payload = {
            "generated_at": now_utc.isoformat(),
            "fixture": {
                "fixture_id": fid, "date": ko_utc.isoformat(),
                "home_id": 1853, "home_name": "Chelsea W",
                "away_id": 1667, "away_name": "Paris Saint Germain W",
                "league_id": 525, "season": 2026, "league_name": "UWCL",
            },
            "match": {
                "event_id": int(eid), "url": bwin_url,
                "home_name": "Chelsea W", "away_name": "Paris Saint Germain W",
                "home_gender": "F", "away_gender": "F",
            },
        }
        if with_lineups_22:
            ctx_payload["context"] = {
                "lineups": {
                    "response": [
                        {
                            "team": {"id": 1853, "name": "Chelsea W"},
                            "formation": "3-4-3",
                            "startXI": [{"player": {"id": 1000 + i, "name": f"Home {i}", "number": i + 1, "pos": "M"}} for i in range(11)],
                            "substitutes": [],
                        },
                        {
                            "team": {"id": 1667, "name": "Paris Saint Germain W"},
                            "formation": "4-4-2",
                            "startXI": [{"player": {"id": 2000 + i, "name": f"Away {i}", "number": i + 1, "pos": "M"}} for i in range(11)],
                            "substitutes": [],
                        },
                    ]
                }
            }
        else:
            ctx_payload["context"] = {"lineups": {"response": []}}
        ctx_json.write_text(json.dumps(ctx_payload), encoding="utf-8")
        ctx_txt.write_text(
            f"Event ID: {eid}\nFixture ID: {fid}\n"
            f"LINEUPS DEL FIXTURE OBJETIVO\n============================\n"
            f"{json.dumps(ctx_payload['context']['lineups'])}\n",
            encoding="utf-8",
        )
        ident_path = edir / "inputs" / "match_identity.json"
        from match_identity import identity_from_payload
        ident_doc = identity_from_payload(ctx_payload)
        ident_doc["identity_status"] = "FIXTURE_AND_BWIN_TIME_VERIFIED"
        ident_path.write_text(json.dumps(ident_doc), encoding="utf-8")
        manifest = {
            "event_id": eid,
            "url": bwin_url,
            "status": "PREPARADO",
            "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
            "inputs": {
                "odds": {"path": str(odds_path), "sha256": prematch_single.sha(odds_path)},
                "context": {"path": str(ctx_txt), "sha256": prematch_single.sha(ctx_txt)},
                "context_json": {"path": str(ctx_json), "sha256": prematch_single.sha(ctx_json)},
                "identity": {"path": str(ident_path), "sha256": prematch_single.sha(ident_path)},
            },
        }
        if with_lineups_22:
            manifest["lineup_state"] = {
                "status": "LINEUPS_API_CONFIRMED",
                "confirmed_complete_22": True,
                "provider_confirmed": True,
            }
        (edir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (edir / "single_execution.json").write_text(
            json.dumps({
                "fixture_id": fid,
                "bwin_event_id": eid,
                "bwin_url": bwin_url,
                "kickoff_madrid": ko_madrid.isoformat(),
                "selected_path": str(sel_path),
                "selected_sha256": prematch_single.sha(sel_path),
                "decision_path": str(dec_path),
                "decision_sha256": prematch_single.sha(dec_path),
                "steps": {},
            }),
            encoding="utf-8",
        )
        if with_report:
            (edir / "instructions" / f"reporte_cualitativo_{eid}.md").write_text(
                f"# Reporte\n- Event ID: {eid}\n- Fixture ID: {fid}\n- Partido: Chelsea W vs Paris Saint Germain W\n",
                encoding="utf-8",
            )
        return sel_path, edir, eid

    def test_01_temporal_constants_and_cli_minimum_guards(self):
        self.assertEqual(prematch_single.PREPARE_MIN_LEAD_MINUTES, 45)
        self.assertEqual(prematch_single.REFRESH_MIN_LEAD_MINUTES, 5)
        self.assertEqual(prematch_single.FINISH_MIN_LEAD_MINUTES, 5)
        with tempfile.TemporaryDirectory() as tmp:
            sel_path, _, _ = self._make_single_event_env(Path(tmp))
            with self.assertRaises(SystemExit):
                prematch_single.main(["--selected", str(sel_path), "--min-finish-lead-minutes", "2", "status"])
            with self.assertRaises(SystemExit):
                prematch_single.main(["--selected", str(sel_path), "--min-refresh-lead-minutes", "3", "status"])
            with self.assertRaises(SystemExit):
                prematch_single.main(["--selected", str(sel_path), "--min-lead-minutes", "20", "status"])

    def test_02_close_precheck_reports_all_pending_before_refresh(self):
        # Cases 4 (XI incomplete), 6 (report absent) detected together before refresh
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sel_path, edir, _ = self._make_single_event_env(
                root, with_report=False, with_lineups_22=False, lead_minutes=30
            )
            with mock.patch.object(prematch_single, "cmd_refresh") as mock_refresh, \
                 mock.patch.object(prematch_single, "cmd_finish") as mock_finish:
                code = prematch_single.main([
                    "--root", str(root), "--selected", str(sel_path), "--confirm-external", "close"
                ])
            self.assertEqual(code, 2)
            mock_refresh.assert_not_called()
            mock_finish.assert_not_called()
            state = json.loads((edir / "single_execution.json").read_text(encoding="utf-8"))
            self.assertEqual(state["steps"]["close"]["result"], "BLOCKED_PRECHECK")
            reqs = {r["requirement"] for r in state["steps"]["close"]["pending_requirements"]}
            self.assertIn("report", reqs)
            self.assertIn("lineups", reqs)

    def test_03_close_executes_refresh_then_finish_and_verifies_motor_odds_sha(self):
        # Cases 1, 2, 3, 5, 8, 9, 21 (consecutive duplicate closure blocked), 22 (retry after partial failure)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sel_path, edir, eid = self._make_single_event_env(
                root, with_report=True, with_lineups_22=True, lead_minutes=30, odds_age_minutes=20
            )
            # Case 9: isolated finish fails on 20-minute-old odds (> 15 min max)
            code_stale = prematch_single.main(["--root", str(root), "--selected", str(sel_path), "finish"])
            self.assertEqual(code_stale, 2)

            # Case 10 & 22: first close attempt fails in refresh, then retry succeeds
            with mock.patch.object(prematch_single, "cmd_refresh", side_effect=ValueError("SCRAPER_FAILED")), \
                 mock.patch.object(prematch_single, "cmd_finish") as mock_finish:
                code_fail = prematch_single.main([
                    "--root", str(root), "--selected", str(sel_path), "--confirm-external", "close"
                ])
            self.assertEqual(code_fail, 2)
            mock_finish.assert_not_called()

            call_order = []

            def fake_refresh(ctx, args):
                call_order.append("refresh")
                now_m = prematch_single.local_now()
                odds_path = edir / "inputs" / f"cuotas_bwin_{eid}.txt"
                odds_path.write_text(
                    f"Captura: {now_m.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                    f"Event ID: {eid}\nURL: {ctx['url']}\n1X2\n1: 1.85\n",
                    encoding="utf-8",
                )
                m = json.loads((edir / "manifest.json").read_text(encoding="utf-8"))
                m["inputs"]["odds"]["sha256"] = prematch_single.sha(odds_path)
                (edir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
                return 0

            def fake_finish(ctx, args):
                call_order.append("finish")
                m = json.loads((edir / "manifest.json").read_text(encoding="utf-8"))
                odds_sha = m["inputs"]["odds"]["sha256"]
                (edir / "validation").mkdir(parents=True, exist_ok=True)
                (edir / "validation" / f"validacion_{eid}.json").write_text(
                    json.dumps({"status": "APTO_VALIDACION_BASICA", "issues": []}), encoding="utf-8"
                )
                (edir / "probabilistic").mkdir(parents=True, exist_ok=True)
                (edir / "probabilistic" / f"ranking_probabilistico_{eid}.json").write_text(
                    json.dumps({
                        "event_id": eid,
                        "odds_availability": "CAPTURA_DISPONIBLE",
                        "input_snapshots": {"odds": {"sha256": odds_sha}},
                    }),
                    encoding="utf-8",
                )
                codex_p = edir / f"CODEX_{eid}.md"
                codex_p.write_text(f"# Codex {eid}\n", encoding="utf-8")
                m["status"] = "FINALIZADO"
                m["validation_state"] = "APTO_VALIDACION_BASICA"
                m["codex_prompt"] = str(codex_p)
                m["finish_completed_at"] = prematch_single.iso_utc()
                m["eligible_for_actionable_pick"] = True
                (edir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
                return 0

            with mock.patch.object(prematch_single, "cmd_refresh", side_effect=fake_refresh), \
                 mock.patch.object(prematch_single, "cmd_finish", side_effect=fake_finish):
                code = prematch_single.main([
                    "--root", str(root), "--selected", str(sel_path), "--confirm-external", "close"
                ])
            self.assertEqual(code, 0)
            self.assertEqual(call_order, ["refresh", "finish"])
            state = json.loads((edir / "single_execution.json").read_text(encoding="utf-8"))
            self.assertEqual(state["steps"]["close"]["result"], "FINALIZADO_COORDINADO")
            self.assertIn("ranking_json", state["steps"]["close"]["stages"]["finish"]["artifacts"])

            # Case 21: second consecutive close on already valid final closure is blocked in precheck
            with mock.patch.object(prematch_single, "cmd_refresh") as mock_ref2:
                code_dup = prematch_single.main([
                    "--root", str(root), "--selected", str(sel_path), "--confirm-external", "close"
                ])
            self.assertEqual(code_dup, 2)
            mock_ref2.assert_not_called()

    def test_04_report_other_event_corrupted_sha_and_incompatible_identity_blocked(self):
        # Cases 7 (report from another event), 11 (wrong Bwin URL), 12 (incompatible API identity), 13 (tampered SHA), 25 (cross-event isolation)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sel_a, edir_a, eid_a = self._make_single_event_env(root, eid="7913441", fid=1638301)
            sel_b, edir_b, eid_b = self._make_single_event_env(root, eid="7901472", fid=1492456)

            # Case 7 & 25: passing event B's report to event A is rejected in precheck
            rep_b = edir_b / "instructions" / f"reporte_cualitativo_{eid_b}.md"
            code_wrong_rep = prematch_single.main([
                "--root", str(root), "--selected", str(sel_a), "--confirm-external",
                "close", "--report", str(rep_b)
            ])
            self.assertEqual(code_wrong_rep, 2)
            st_a = json.loads((edir_a / "single_execution.json").read_text(encoding="utf-8"))
            codes = {r["code"] for r in st_a["steps"]["close"]["pending_requirements"]}
            self.assertTrue(codes & {"REPORT_EVENT_ID_CONFLICT", "REPORT_FIXTURE_ID_CONFLICT"})

            # Case 12: incompatible API fixture_id in match_identity.json
            id_path = edir_a / "inputs" / "match_identity.json"
            id_doc = json.loads(id_path.read_text(encoding="utf-8"))
            id_doc["api_fixture_id"] = 9999999
            id_path.write_text(json.dumps(id_doc), encoding="utf-8")
            m_a = json.loads((edir_a / "manifest.json").read_text(encoding="utf-8"))
            m_a["inputs"]["identity"]["sha256"] = prematch_single.sha(id_path)
            (edir_a / "manifest.json").write_text(json.dumps(m_a), encoding="utf-8")
            code_bad_id = prematch_single.main([
                "--root", str(root), "--selected", str(sel_a), "--confirm-external", "close"
            ])
            self.assertEqual(code_bad_id, 2)
            st_a2 = json.loads((edir_a / "single_execution.json").read_text(encoding="utf-8"))
            codes2 = {r["code"] for r in st_a2["steps"]["close"]["pending_requirements"]}
            self.assertTrue(codes2 & {"IDENTITY_VALIDATION_FAILED", "MANIFEST_INVALID_OR_MISSING"})

            # Case 13: tampered SHA in context file
            (edir_b / "inputs" / f"match_engine_{eid_b}.txt").write_text("TAMPERED\n", encoding="utf-8")
            code_tampered = prematch_single.main([
                "--root", str(root), "--selected", str(sel_b), "--confirm-external", "close"
            ])
            self.assertEqual(code_tampered, 2)
            st_b = json.loads((edir_b / "single_execution.json").read_text(encoding="utf-8"))
            codes_b = {r["code"] for r in st_b["steps"]["close"]["pending_requirements"]}
            self.assertIn("MANIFEST_INVALID_OR_MISSING", codes_b)

            # Case 11: wrong Bwin URL in selected_match.json
            sel_doc_b = json.loads(sel_b.read_text(encoding="utf-8"))
            sel_doc_b["bwin_url"] = "https://www.bwin.es/es/sports/eventos/wrong-2:9999999"
            sel_b.write_text(json.dumps(sel_doc_b), encoding="utf-8")
            self.assertEqual(prematch_single.main(["--root", str(root), "--selected", str(sel_b), "status"]), 2)

    def test_05_temporal_boundaries_t4_post_kickoff_and_mid_finish_expiry_revokes_codex_prompt(self):
        # Cases 18 (margin < T-5), 19 (start valid -> finish after kickoff revokes CODEX prompt), 20 (post-kickoff)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Case 18: margin 4 min (< T-5)
            sel_t4, edir_t4, _ = self._make_single_event_env(root, eid="7913441", fid=1638301, lead_minutes=4)
            self.assertEqual(
                prematch_single.main(["--root", str(root), "--selected", str(sel_t4), "--confirm-external", "close"]),
                2,
            )
            st_t4 = json.loads((edir_t4 / "single_execution.json").read_text(encoding="utf-8"))
            self.assertEqual(st_t4["steps"]["close"]["pending_requirements"][0]["code"], "INSUFFICIENT_PREMATCH_MARGIN")

            # Case 20: kickoff already started (-2 min)
            sel_pk, edir_pk, _ = self._make_single_event_env(root, eid="7913442", fid=1638302, lead_minutes=-2)
            self.assertEqual(
                prematch_single.main(["--root", str(root), "--selected", str(sel_pk), "--confirm-external", "close"]),
                2,
            )
            st_pk = json.loads((edir_pk / "single_execution.json").read_text(encoding="utf-8"))
            self.assertEqual(st_pk["steps"]["close"]["pending_requirements"][0]["code"], "POST_KICKOFF")

            # Case 19: start valid at T-6, but clock reaches kickoff+1m when finish completes -> revokes CODEX prompt!
            sel_exp, edir_exp, eid_exp = self._make_single_event_env(root, eid="7913443", fid=1638303, lead_minutes=8)
            ko_dt = datetime.fromisoformat(json.loads(sel_exp.read_text())["kickoff_madrid"])

            def fake_run_slow_multi(cmd, r, env=None, log=None):
                # prematch_multi.py wrote CODEX prompt and FINALIZADO just before clock crossed kickoff
                m = json.loads((edir_exp / "manifest.json").read_text(encoding="utf-8"))
                rep_p = edir_exp / "inputs" / f"reporte_cualitativo_{eid_exp}.md"
                shutil.copy2(edir_exp / "instructions" / f"reporte_cualitativo_{eid_exp}.md", rep_p)
                m["inputs"]["report"] = {"path": str(rep_p), "sha256": prematch_single.sha(rep_p)}
                codex_p = edir_exp / f"CODEX_{eid_exp}.md"
                codex_p.write_text("# Actionable prompt that must be revoked\n", encoding="utf-8")
                m["status"] = "FINALIZADO"
                m["codex_prompt"] = str(codex_p)
                m["eligible_for_actionable_pick"] = True
                (edir_exp / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
                # Now advance simulated clock past kickoff
                os.environ["PREMATCH_NOW_UTC"] = (ko_dt + timedelta(minutes=1)).astimezone(timezone.utc).isoformat()
                return 0

            try:
                with mock.patch.object(prematch_single, "run", side_effect=fake_run_slow_multi):
                    rc = prematch_single.main(["--root", str(root), "--selected", str(sel_exp), "finish"])
                self.assertEqual(rc, 3)
                self.assertFalse((edir_exp / f"CODEX_{eid_exp}.md").exists())
                m_after = json.loads((edir_exp / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(m_after["status"], "EXPIRADO_POST_KICKOFF")
                self.assertIsNone(m_after["codex_prompt"])
                self.assertFalse(m_after["eligible_for_actionable_pick"])
                self.assertEqual(m_after["final_eligibility"], "EXPIRED_POST_KICKOFF_RETROSPECTIVE_ONLY")
            finally:
                os.environ.pop("PREMATCH_NOW_UTC", None)

    def test_06_lineup_flags_mutual_exclusion_and_manual_sync_auto_reconciliation(self):
        # Cases 4 (allow-unconfirmed-lineups -> non-actionable), 23 (manual XI sync on prematch_single case), 24 (Shadow re-evaluation)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sel_path, edir, eid = self._make_single_event_env(root, with_report=True, with_lineups_22=False, lead_minutes=30)

            # Mutually exclusive flags rejected
            with self.assertRaises(SystemExit):
                prematch_single.main([
                    "--root", str(root), "--selected", str(sel_path), "--confirm-external",
                    "close", "--require-confirmed-lineups", "--allow-unconfirmed-lineups"
                ])

            # Sync manual lineups on prematch_single case works without case.json
            self.assertFalse((edir / "case.json").exists())
            res_empty = prematch_case.sync_manual_lineups(root, eid)
            self.assertEqual(res_empty["inbox_state"], "MANUAL_INBOX_EMPTY")

            # Drop valid manual 22-starter JSON into lineups/manual/ -> evaluate_close_prerequisites auto-syncs it!
            manual_json = Path(res_empty["expected_json_path"])
            manual_payload = {
                "schema_version": "1.0-manual-lineup-confirm",
                "event_id": eid,
                "fixture_id": 1638301,
                "lineup_status": "LINEUPS_MANUAL_VERIFIED",
                "source": "UEFA_OFFICIAL_PRESS_KIT",
                "source_url": "https://www.uefa.com/uwcl/match/1638301/lineups",
                "source_kind": "MANUAL_VERIFIED",
                "primary_evidence_verified": True,
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "teams": {
                    "home": {
                        "team_name": "Chelsea W", "team_id": 1853, "formation": "3-4-3",
                        "starters": [{"name": f"Chelsea Starter {i}", "number": i + 1, "pos": "M"} for i in range(11)],
                    },
                    "away": {
                        "team_name": "Paris Saint Germain W", "team_id": 1667, "formation": "4-4-2",
                        "starters": [{"name": f"PSG Starter {i}", "number": i + 1, "pos": "M"} for i in range(11)],
                    },
                },
            }
            manual_json.write_text(json.dumps(manual_payload), encoding="utf-8")
            ctx = prematch_single.load_context(Namespace(
                root=root, selected=sel_path, decision=None
            ))
            pre = prematch_single.evaluate_close_prerequisites(
                ctx, Namespace(
                    min_refresh_lead_minutes=5, min_finish_lead_minutes=5,
                    lineup_cutoff_minutes=15, allow_unconfirmed_lineups=False, report=None
                )
            )
            self.assertTrue(pre["ok"], msg=str(pre["pending_requirements"]))
            self.assertTrue(pre["details"]["lineups_confirmed_22"])

    def test_07_resolve_qualitative_report_path_and_confirm_external_and_refresh_without_preflight(self):
        # Cases for uniform report path, confirm-external propagation, and refresh-existing-odds without preflight
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eid = "7913441"
            edir = root / "outputs" / "matches" / eid
            (edir / "instructions").mkdir(parents=True, exist_ok=True)
            (edir / "inputs").mkdir(parents=True, exist_ok=True)
            (root / "instructions").mkdir(parents=True, exist_ok=True)

            explicit = root / "custom_report.md"
            explicit.write_text("# Custom", encoding="utf-8")
            self.assertEqual(prematch_single.resolve_qualitative_report_path(root, edir, eid, explicit), explicit)

            r_root = root / "instructions" / f"reporte_cualitativo_{eid}.md"
            r_root.write_text("root", encoding="utf-8")
            self.assertEqual(prematch_single.resolve_qualitative_report_path(root, edir, eid), r_root)

            r_in = edir / "inputs" / f"reporte_cualitativo_{eid}.md"
            r_in.write_text("inputs", encoding="utf-8")
            self.assertEqual(prematch_single.resolve_qualitative_report_path(root, edir, eid), r_in)

            r_dir_inst = edir / "instructions" / f"reporte_cualitativo_{eid}.md"
            r_dir_inst.write_text("dir_inst", encoding="utf-8")
            self.assertEqual(prematch_single.resolve_qualitative_report_path(root, edir, eid), r_dir_inst)

    def test_08_real_end_to_end_prematch_cycle_7913441_with_injectable_clock(self):
        """Section 6.3: Real E2E executing real validate_prematch.py, motor_probabilistico.py, and Shadow without mocks."""
        real_7913441 = REPO_ROOT / "outputs" / "matches" / "7913441"
        real_ctx_dir = real_7913441 / "context_updates" / "4bde42efcc81581fded11234"
        real_report = real_7913441 / "instructions" / "reporte_cualitativo_7913441.md"
        real_odds = real_7913441 / "inputs" / "cuotas_bwin_7913441.txt"
        self.assertTrue(real_ctx_dir.is_dir() and real_report.is_file() and real_odds.is_file())

        # Simulated clock: 2026-11-18T19:42:00+00:00 (20:42:00+01:00 Madrid, T-18 min before 21:00:00+01:00 kickoff)
        sim_now_utc = "2026-11-18T19:42:00+00:00"
        sim_capture_madrid = "2026-11-18T20:42:00+01:00"
        eid = "7913441"
        fid = 1638301
        bwin_url = "https://www.bwin.es/es/sports/eventos/chelsea-f-psg-f-2:7913441"

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # Symlink real engine modules & config into isolated workspace so real subprocesses execute
            (ws / "config").mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "config" / "constants.py", ws / "config" / "constants.py")
            for mod_file in (
                "prematch_multi.py", "prematch_single.py", "prematch_case.py",
                "validate_prematch.py", "motor_probabilistico.py", "match_identity.py",
                "prematch_paths.py", "prematch_cache_record.py", "prematch_context_delta.py",
                "prematch_execution_control.py", "prematch_prospective_trial.py",
                "prematch_daily.py", "bwin_market_preflight.py", "generate_context.py",
            ):
                if (REPO_ROOT / mod_file).is_file():
                    (ws / mod_file).symlink_to(REPO_ROOT / mod_file)
            if (REPO_ROOT / "bet_engine").is_dir():
                (ws / "bet_engine").symlink_to(REPO_ROOT / "bet_engine")

            # Deterministic offline scraper_bwin.py that writes the real Bwin market snapshot stamped at sim_capture_madrid
            raw_odds_lines = real_odds.read_text(encoding="utf-8").splitlines()
            refreshed_lines = [
                f"Captura: {sim_capture_madrid}" if line.startswith("Captura:") else line
                for line in raw_odds_lines
            ]
            refreshed_odds_payload = "\n".join(refreshed_lines) + "\n"
            payload_src = ws / "_refreshed_odds_payload.txt"
            payload_src.write_text(refreshed_odds_payload, encoding="utf-8")
            (ws / "scraper_bwin.py").write_text(
                "import os, pathlib, shutil\n"
                f"src = pathlib.Path({str(payload_src)!r})\n"
                "eid = os.environ['PREMATCH_EVENT_ID']\n"
                "dest = pathlib.Path(os.environ['PREMATCH_EVENT_DATA_DIR']) / f'cuotas_bwin_{eid}.txt'\n"
                "dest.parent.mkdir(parents=True, exist_ok=True)\n"
                "shutil.copy2(src, dest)\n"
                "print(f'TXT: {dest}')\n",
                encoding="utf-8",
            )

            # Prepare match directory with real context, identity, 22 starters, initial 20:38 odds, and real report
            edir = ws / "outputs" / "matches" / eid
            (edir / "inputs").mkdir(parents=True, exist_ok=True)
            (edir / "instructions").mkdir(parents=True, exist_ok=True)
            (edir / "logs").mkdir(parents=True, exist_ok=True)

            odds_target = edir / "inputs" / f"cuotas_bwin_{eid}.txt"
            shutil.copy2(real_odds, odds_target)
            ctx_txt = edir / "inputs" / f"match_engine_{eid}.txt"
            shutil.copy2(real_ctx_dir / f"match_engine_{eid}.txt", ctx_txt)
            ctx_json = edir / "inputs" / f"match_engine_{eid}.json"
            shutil.copy2(real_ctx_dir / f"match_engine_{eid}.json", ctx_json)
            ident_target = edir / "inputs" / "match_identity.json"
            shutil.copy2(real_ctx_dir / "match_identity.json", ident_target)
            rep_target = edir / "instructions" / f"reporte_cualitativo_{eid}.md"
            shutil.copy2(real_report, rep_target)

            sel_dir = ws / "selection_pipeline"
            sel_dir.mkdir(parents=True, exist_ok=True)
            sel_path = sel_dir / "selected_match.json"
            dec_path = sel_dir / "selection_decision.json"
            sel_doc = {
                "schema_version": "1.0-selected-single-fixture",
                "status": "SELECTED_FOR_DEEP_RESEARCH",
                "fixture_id": fid,
                "bwin_event_id": eid,
                "bwin_url": bwin_url,
                "home": "Chelsea W",
                "away": "Paris Saint Germain W",
                "kickoff_madrid": "2026-11-18T21:00:00+01:00",
                "selection_asof_madrid": "2026-11-18T19:50:00+01:00",
                "selection_tier": "A",
                "research_eligible": True,
                "eligible": True,
                "research_missing_evidence": [],
                "hard_block_reasons": [],
                "market_families_ready_for_research": ["corners", "fouls", "goals"],
            }
            sel_path.write_text(json.dumps(sel_doc, indent=2), encoding="utf-8")
            dec_doc = {
                "status": "SELECTED",
                "provisional_or_selected": {
                    "fixture_id": fid,
                    "bwin_event_id": eid,
                    "bwin_url": bwin_url,
                    "home": "Chelsea W",
                    "away": "Paris Saint Germain W",
                    "kickoff_madrid": "2026-11-18T21:00:00+01:00",
                    "selection_tier": "A",
                    "research_eligible": True,
                    "eligible": True,
                    "research_missing_evidence": [],
                    "hard_block_reasons": [],
                    "market_families_ready": ["corners", "fouls", "goals"],
                },
            }
            dec_path.write_text(json.dumps(dec_doc, indent=2), encoding="utf-8")

            manifest = {
                "event_id": eid,
                "url": bwin_url,
                "prepared_at": "2026-11-18T18:51:02.657514+00:00",
                "status": "PREPARADO",
                "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
                "inputs": {
                    "odds": {"path": str(odds_target), "sha256": prematch_single.sha(odds_target)},
                    "context": {"path": str(ctx_txt), "sha256": prematch_single.sha(ctx_txt)},
                    "context_json": {"path": str(ctx_json), "sha256": prematch_single.sha(ctx_json)},
                    "identity": {"path": str(ident_target), "sha256": prematch_single.sha(ident_target)},
                },
                "lineup_state": {
                    "status": "LINEUPS_API_CONFIRMED",
                    "lineup_structural_status": "COMPLETE_22_STRUCTURED",
                    "confirmed_complete_22": True,
                    "provider_confirmed": True,
                    "primary_official_verified": False,
                    "official_source_tier": "STRUCTURED_PROVIDER",
                    "source_origin": "API_FOOTBALL",
                    "acquired_at_utc": "2026-11-18T19:34:24.616116+00:00",
                },
            }
            (edir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (edir / "single_execution.json").write_text(
                json.dumps({
                    "schema_version": "0.2-single-operation-windows",
                    "fixture_id": fid,
                    "bwin_event_id": eid,
                    "bwin_url": bwin_url,
                    "kickoff_madrid": "2026-11-18T21:00:00+01:00",
                    "selected_path": str(sel_path),
                    "selected_sha256": prematch_single.sha(sel_path),
                    "decision_path": str(dec_path),
                    "decision_sha256": prematch_single.sha(dec_path),
                    "steps": {},
                }, indent=2),
                encoding="utf-8",
            )

            # Execute real close with --shadow-mode at simulated T-18m
            rc = prematch_single.main([
                "--root", str(ws),
                "--selected", str(sel_path),
                "--now-utc", sim_now_utc,
                "--confirm-external",
                "close",
                "--shadow-mode",
            ])
            self.assertEqual(rc, 0)

            m_final = json.loads((edir / "manifest.json").read_text(encoding="utf-8"))
            st_final = json.loads((edir / "single_execution.json").read_text(encoding="utf-8"))
            val_final = json.loads((edir / "validation" / f"validacion_{eid}.json").read_text(encoding="utf-8"))
            rank_final = json.loads((edir / "probabilistic" / f"ranking_probabilistico_{eid}.json").read_text(encoding="utf-8"))
            sh_final = json.loads((edir / "shadow" / f"shadow_summary_{eid}.json").read_text(encoding="utf-8"))

            self.assertEqual(m_final["status"], "FINALIZADO")
            self.assertEqual(val_final["status"], "APTO_VALIDACION_BASICA")
            self.assertEqual(m_final["validation_summary"]["blocks"], 0)
            self.assertEqual(
                rank_final["input_snapshots"]["odds"]["sha256"],
                m_final["inputs"]["odds"]["sha256"],
            )
            self.assertEqual(sh_final["odds_sha256"], m_final["inputs"]["odds"]["sha256"])
            self.assertTrue(Path(m_final["codex_prompt"]).is_file())
            self.assertTrue(m_final["eligible_for_actionable_pick"])
            self.assertEqual(st_final["steps"]["close"]["result"], "FINALIZADO_COORDINADO")

            # Persist E2E audit artifact for certification deliverables
            audit_dir = REPO_ROOT / "audit" / "reparacion_integral_prematch"
            audit_dir.mkdir(parents=True, exist_ok=True)
            e2e_doc = {
                "schema_version": "1.0-e2e-prematch-certification",
                "executed_at_utc": datetime.now(timezone.utc).isoformat(),
                "simulated_clock_utc": sim_now_utc,
                "fixture_id": fid,
                "bwin_event_id": eid,
                "match": "Chelsea W vs Paris Saint Germain W",
                "kickoff_madrid": "2026-11-18T21:00:00+01:00",
                "returncode": rc,
                "manifest_status": m_final["status"],
                "validation_summary": m_final["validation_summary"],
                "lineup_state": m_final["lineup_state"],
                "shadow_sync_state": m_final.get("shadow_sync_state"),
                "eligible_for_actionable_pick": m_final["eligible_for_actionable_pick"],
                "close_stages": st_final["steps"]["close"]["stages"],
                "input_hashes": {k: (v or {}).get("sha256") for k, v in (m_final.get("inputs") or {}).items()},
                "motor_odds_sha256_verified": rank_final["input_snapshots"]["odds"]["sha256"] == m_final["inputs"]["odds"]["sha256"],
                "shadow_odds_sha256_verified": sh_final["odds_sha256"] == m_final["inputs"]["odds"]["sha256"],
            }
            (audit_dir / "E2E_PREMATCH_RESULT.json").write_text(
                json.dumps(e2e_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
