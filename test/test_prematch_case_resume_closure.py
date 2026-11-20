"""Regresión offline para reanudación de caso canónico, acquire-lineups, finalize --shadow-mode y cierre T-60/T-15."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import prematch_case as case
import prematch_multi
import prematch_single
from bet_engine.orchestration import run_shadow_for_event_dir

ROOT = Path(__file__).resolve().parent.parent
MADRID = ZoneInfo("Europe/Madrid")
EVENT_7909704 = ROOT / "outputs/matches/7909704"


class TestPrematchCaseResumeAndClosure(unittest.TestCase):
    @unittest.skipUnless(EVENT_7909704.is_dir(), "Caso real 7909704 no presente")
    def test_existing_7909704_distinguishes_preliminary_evaluation_from_final_closure(self):
        st = case.status(ROOT, "7909704")
        self.assertEqual(st["status"], "FINALIZADO")
        self.assertEqual(st["closure_stage"], "FINAL_PREMATCH_VALID")
        self.assertTrue(st["final_prematch_valid"])
        self.assertIn(
            st["closure_checks"]["lineups_status"],
            {"LINEUPS_PENDING", "LINEUPS_PROBABLE_ONLY", "LINEUPS_UNAVAILABLE_FINAL"},
        )
        self.assertFalse(st["eligible_for_actionable_pick"])
        self.assertFalse(st["eligible_for_final_analysis"])

        # Also verify the archived preliminary manifest (13:14 CET odds, 12:22 UTC finish) is classified as PRELIMINARY_EVALUATION_ONLY
        arch_dir = EVENT_7909704 / "archives/refresh_20261116T163548866497Z"
        arch_manifests = sorted(arch_dir.glob("manifest.*.json"))
        arch_odds = sorted(arch_dir.glob("cuotas_bwin_7909704.*.txt"))
        if arch_manifests and arch_odds:
            c_doc = json.loads((EVENT_7909704 / "case.json").read_text(encoding="utf-8"))
            m_arch = json.loads(arch_manifests[0].read_text(encoding="utf-8"))
            m_arch["inputs"]["odds"]["path"] = str(arch_odds[0].resolve())
            prelim_eval = case._evaluate_closure(EVENT_7909704, c_doc, m_arch)
            self.assertEqual(prelim_eval["closure_stage"], "PRELIMINARY_EVALUATION_ONLY")
            self.assertFalse(prelim_eval["final_prematch_valid"])

    def test_closure_classification_recognizes_valid_t60_t15_closure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eid = "7990001"
            d = root / "outputs/matches" / eid
            (d / "inputs").mkdir(parents=True)
            (d / "selection").mkdir(parents=True)

            ko = (datetime.now(timezone.utc) + timedelta(minutes=30)).astimezone(MADRID)
            cap = (ko - timedelta(minutes=10)).astimezone(MADRID)
            fin = (ko - timedelta(minutes=6)).astimezone(timezone.utc)
            url = f"https://www.bwin.es/es/sports/eventos/test-a-test-b-2:{eid}"

            odds = d / "inputs" / f"cuotas_bwin_{eid}.txt"
            odds.write_text(f"Event ID: {eid}\nURL: {url}\nCaptura: {cap.isoformat()}\n", encoding="utf-8")
            sel = d / "selection/selected_match.json"
            dec = d / "selection/selection_decision.json"
            hoff = d / "handoff.json"
            for p in (sel, dec, hoff):
                p.write_text("{}", encoding="utf-8")

            c_doc = {
                "schema_version": case.SCHEMA,
                "event_id": eid,
                "fixture_id": 123456,
                "bwin_url": url,
                "home": {"name": "A", "api_team_id": 1},
                "away": {"name": "B", "api_team_id": 2},
                "competition": {"league_id": 5, "name": "UNL", "season": 2026},
                "kickoff_madrid": ko.isoformat(),
                "selected": case.reference(sel),
                "source_handoff": case.reference(hoff),
            }
            m_doc = {
                "schema_version": case.SCHEMA,
                "event_id": eid,
                "url": url,
                "status": "FINALIZADO",
                "finish_completed_at": fin.isoformat(),
                "inputs": {"odds": case.reference(odds)},
            }
            (d / "case.json").write_text(json.dumps(c_doc), encoding="utf-8")
            (d / "manifest.json").write_text(json.dumps(m_doc), encoding="utf-8")

            st = case.status(root, eid)
            self.assertEqual(st["closure_stage"], "FINAL_PREMATCH_VALID")
            self.assertTrue(st["final_prematch_valid"])
            self.assertEqual(st["closure_checks"]["closure_reasons"], [])
            self.assertEqual(st["next_action"], "export-handoff")

    @unittest.skipUnless(EVENT_7909704.is_dir(), "Caso real 7909704 no presente")
    def test_preliminary_resume_blocks_stale_odds_then_completes_final_closure_and_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for item in ROOT.iterdir():
                if item.name in ("outputs", ".git", ".venv", "__pycache__"):
                    continue
                target = root / item.name
                if not target.exists():
                    os.symlink(item, target)
            dst = root / "outputs/matches/7909704"
            dst.parent.mkdir(parents=True)
            shutil.copytree(EVENT_7909704, dst)

            # Rebind internal selection/handoff/manifest paths into isolated tmp root so real repo is untouched.
            sel_src = EVENT_7909704 / "selection/20261116T131906324229/selected_match.json"
            sel_dst = dst / "selection/20261116T131906324229/selected_match.json"
            dec_dst = sel_dst.parent / "selection_decision.json"
            hoff_dst = dst / "handoff_snapshot.json"
            hoff_dst.write_text("{}", encoding="utf-8")

            c_doc = json.loads((dst / "case.json").read_text(encoding="utf-8"))
            c_doc["selected"] = case.reference(sel_dst)
            c_doc["source_handoff"] = case.reference(hoff_dst)
            (dst / "case.json").write_text(json.dumps(c_doc, indent=2), encoding="utf-8")

            m_path = dst / "manifest.json"
            m_doc = json.loads(m_path.read_text(encoding="utf-8"))
            for key in ("odds", "context", "context_json", "identity", "report"):
                old_p = Path(m_doc["inputs"][key]["path"])
                rel = old_p.relative_to(EVENT_7909704)
                m_doc["inputs"][key]["path"] = str((dst / rel).resolve())
            m_doc["canonical_case"] = case.reference(dst / "case.json")
            m_doc["source_handoff"] = case.reference(hoff_dst)
            m_doc["selected_match"] = case.reference(sel_dst)
            ev_cache = dst / "probabilistic/evidence_cache_7909704.json"
            m_doc["evidence_cache"] = case.reference(ev_cache)
            # Initialize tmp copy in the preliminary state (13:14:35 CET odds, 12:22:34 UTC finish)
            odds_path = dst / "inputs/cuotas_bwin_7909704.txt"
            txt = odds_path.read_text(encoding="utf-8")
            old_line = next(line for line in txt.splitlines() if line.startswith("Captura:"))
            odds_path.write_text(txt.replace(old_line, "Captura: 2026-11-16T13:14:35+01:00"), encoding="utf-8")
            m_doc["inputs"]["odds"]["sha256"] = hashlib.sha256(odds_path.read_bytes()).hexdigest()
            m_doc["finish_completed_at"] = "2026-11-16T12:22:34.806233+00:00"
            m_path.write_text(json.dumps(m_doc, indent=2), encoding="utf-8")

            se_path = dst / "single_execution.json"
            se_doc = json.loads(se_path.read_text(encoding="utf-8"))
            se_doc["selected_path"] = str(sel_dst)
            se_doc["decision_path"] = str(dec_dst)
            se_path.write_text(json.dumps(se_doc, indent=2), encoding="utf-8")

            ko_madrid = datetime.fromisoformat("2026-11-16T18:00:00+01:00")
            now_t25 = datetime.fromisoformat("2026-11-16T17:35:00+01:00")
            ctx = prematch_single.load_context(
                type("A", (), {"root": root, "selected": sel_dst, "decision": None})()
            )
            f_args = type("F", (), {
                "multi": Path("prematch_multi.py"),
                "historical": False,
                "min_finish_lead_minutes": 5,
                "max_odds_age_minutes": 60,
                "report": dst / "inputs/reporte_cualitativo_7909704.md",
                "shadow_mode": True,
            })()

            # 1) Preliminary status with stale 13:14 CET odds must block finish without altering artifacts.
            with patch.object(prematch_single, "local_now", return_value=now_t25):
                with self.assertRaisesRegex(ValueError, "Cuotas antiguas"):
                    prematch_single.cmd_finish(ctx, f_args)
            self.assertFalse(list((dst / "archives").glob("preliminary_closure_*")))

            # 2) Simulate fresh odds capture inside T-60 (T-30 = 17:30 CET, age=5m at 17:35 CET).
            cap_t30 = ko_madrid - timedelta(minutes=30)
            odds_path = dst / "inputs/cuotas_bwin_7909704.txt"
            txt = odds_path.read_text(encoding="utf-8")
            old_line = next(line for line in txt.splitlines() if line.startswith("Captura:"))
            odds_path.write_text(txt.replace(old_line, f"Captura: {cap_t30.isoformat()}"), encoding="utf-8")
            m_doc["inputs"]["odds"]["sha256"] = hashlib.sha256(odds_path.read_bytes()).hexdigest()
            m_path.write_text(json.dumps(m_doc, indent=2), encoding="utf-8")

            # Preliminary resume runs finish cleanly, preserves preliminary archive, runs Legacy + Shadow, and non-XI markets remain evaluated.
            now_utc = now_t25.astimezone(timezone.utc)
            orig_dt = datetime
            class FixedDatetime(orig_dt):
                @classmethod
                def now(cls, tz=None):
                    return now_utc if tz is None else now_utc.astimezone(tz)

            real_execute = prematch_multi.execute

            def execute_at_fixed_time(r, ev, stage, args_list):
                if stage == "validacion":
                    return 0
                return real_execute(r, ev, stage, args_list)

            def run_multi_in_process(cmd, cwd_root, env=None, log=None):
                with patch.object(prematch_multi, "execute", side_effect=execute_at_fixed_time):
                    ok = prematch_multi.finish_one(
                        cwd_root,
                        "7909704",
                        cmd[0],
                        override_report=str(f_args.report),
                        historical=False,
                        shadow_mode=True,
                    )
                return 0 if ok else 1

            with patch.object(prematch_single, "local_now", return_value=now_t25), \
                 patch.object(prematch_multi, "utc_now_dt", return_value=now_utc), \
                 patch.object(prematch_multi, "utc_now", return_value=now_utc.isoformat()), \
                 patch.object(prematch_single, "run", side_effect=run_multi_in_process), \
                 patch.object(case, "datetime", FixedDatetime):
                rc = prematch_single.cmd_finish(ctx, f_args)
                self.assertEqual(rc, 0)
                st_after = case.status(root, "7909704")

            self.assertEqual(len(list((dst / "archives").glob("preliminary_closure_*"))), 1)
            self.assertEqual(st_after["closure_stage"], "FINAL_PREMATCH_VALID")
            self.assertTrue(st_after["final_prematch_valid"])
            self.assertEqual(st_after["next_action"], "export-handoff")

            ranking = json.loads((dst / "probabilistic/ranking_probabilistico_7909704.json").read_text(encoding="utf-8"))
            rows = ranking if isinstance(ranking, list) else ranking.get("ranking", [])
            non_xi_rows = [r for r in rows if (r.get("parsed_market") or {}).get("type") != "player"]
            player_rows = [r for r in rows if (r.get("parsed_market") or {}).get("type") == "player"]
            self.assertGreater(len(non_xi_rows), 100)
            self.assertTrue(all(g.get("xi_status") == "HISTORICO_XI_OBJETIVO_DESCONOCIDO" for r in player_rows[:5] for g in (r.get("evidence") or {}).get("groups", [])))
            self.assertTrue((dst / "shadow/shadow_summary_7909704.json").is_file())

            # 3) A second finalize after a valid FINAL_PREMATCH_VALID closure must fail closed (no overwrite/duplicate).
            with patch.object(prematch_single, "local_now", return_value=now_t25), \
                 patch.object(case, "datetime", FixedDatetime):
                with self.assertRaisesRegex(ValueError, "FINAL_PREMATCH_CLOSURE_ALREADY_VALID"):
                    prematch_single.cmd_finish(ctx, f_args)

    def test_prematch_case_cli_forwards_acquire_lineups_and_finalize_shadow_flags(self):
        calls = []

        def fake_delegate(root, selected, action, extra, confirm_external):
            calls.append((action, extra, confirm_external))
            return 0

        sel = ROOT / "outputs/matches/7909704/selection/20261116T131906324229/selected_match.json"
        with patch.object(case, "delegate", side_effect=fake_delegate):
            rc1 = case.main(["acquire-lineups", "--selected", str(sel)])
            rc2 = case.main(["finalize", "--selected", str(sel), "--shadow-mode", "--report", "foo.md"])
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(calls[0], ("acquire-lineups", [], True))
        self.assertEqual(calls[1], ("finish", ["--shadow-mode", "--report", "foo.md"], False))

        # Unknown flags must still fail closed on non-delegated subcommands like status.
        with self.assertRaises(SystemExit) as ctx:
            case.main(["status", "--event-id", "7909704", "--bogus-flag"])
        self.assertEqual(ctx.exception.code, 2)

    @unittest.skipUnless(EVENT_7909704.is_dir(), "Caso real 7909704 no presente")
    def test_shadow_inside_t30_window_completes_when_legacy_finish_uses_5m_lead(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for item in ROOT.iterdir():
                if item.name in ("outputs", ".git", ".venv", "__pycache__"):
                    continue
                target = root / item.name
                if not target.exists():
                    os.symlink(item, target)
            dst_match = root / "outputs/matches/7909704"
            dst_match.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(EVENT_7909704, dst_match)

            # Simulate T-25 min before 18:00 CET kickoff (17:35 CET = 16:35 UTC) with fresh 17:30 CET odds.
            now_t25 = datetime.fromisoformat("2026-11-16T17:35:00+01:00")
            cap_t30 = datetime.fromisoformat("2026-11-16T17:30:00+01:00")
            odds_path = dst_match / "inputs/cuotas_bwin_7909704.txt"
            txt = odds_path.read_text(encoding="utf-8")
            old_line = next(line for line in txt.splitlines() if line.startswith("Captura:"))
            odds_path.write_text(txt.replace(old_line, f"Captura: {cap_t30.isoformat()}"), encoding="utf-8")
            m_path = dst_match / "manifest.json"
            m_doc = json.loads(m_path.read_text(encoding="utf-8"))
            m_doc["inputs"]["odds"]["path"] = str(odds_path)
            m_doc["inputs"]["odds"]["sha256"] = hashlib.sha256(odds_path.read_bytes()).hexdigest()
            m_path.write_text(json.dumps(m_doc, indent=2), encoding="utf-8")

            # Previously hardcoded min_lead=45 would block at T-25; explicit T-5 succeeds cleanly.
            sh_res = run_shadow_for_event_dir(
                root,
                "7909704",
                historical=False,
                now_utc=now_t25.astimezone(timezone.utc),
                min_lead_minutes=prematch_single.FINISH_MIN_LEAD_MINUTES,
            )
            self.assertEqual(sh_res["status"], "COMPLETED_SHADOW")
            summary = json.loads((dst_match / "shadow/shadow_summary_7909704.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["odds_freshness_status"], "LOADED")
            self.assertEqual(summary["shadow_report"]["lead_minutes_before_kickoff"], 25.0)
