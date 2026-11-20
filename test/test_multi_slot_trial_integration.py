import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_prospective_trial as trial
import prematch_runtime as runtime
from bet_engine.orchestration.multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorConfig,
    _build_prospective_trial_args,
    resolve_or_generate_preselection,
)
from prematch_orchestrator import main as cli_main


def _sample_fixtures() -> list[dict]:
    return [
        {
            "fixture_id": 7001,
            "home": "Real A",
            "away": "Visit A",
            "home_id": 101,
            "away_id": 102,
            "league_id": 140,
            "league_name": "LaLiga",
            "season": 2026,
            "region": "EUROPE",
            "status": "NS",
            "kickoff_utc": "2026-11-17T15:30:00+00:00",
            "kickoff_madrid": "2026-11-17T16:30:00+01:00",
            "data_readiness_score": 88,
            "research_priority_score": 81.5,
            "preliminary_coverage_score": 80.0,
            "region_priority": 0,
            "details_checked": True,
        },
        {
            "fixture_id": 7002,
            "home": "Club B",
            "away": "Club B2",
            "home_id": 201,
            "away_id": 202,
            "league_id": 135,
            "league_name": "Serie A",
            "season": 2026,
            "region": "EUROPE",
            "status": "NS",
            "kickoff_utc": "2026-11-17T18:30:00+00:00",
            "kickoff_madrid": "2026-11-17T19:30:00+01:00",
            "data_readiness_score": 84,
            "research_priority_score": 77.0,
            "preliminary_coverage_score": 78.0,
            "region_priority": 0,
            "details_checked": True,
        },
        {
            "fixture_id": 7003,
            "home": "Late C",
            "away": "Late C2",
            "home_id": 301,
            "away_id": 302,
            "league_id": 39,
            "league_name": "Premier League",
            "season": 2026,
            "region": "EUROPE",
            "status": "NS",
            "kickoff_utc": "2026-11-17T22:15:00+00:00",
            "kickoff_madrid": "2026-11-17T23:15:00+01:00",
            "data_readiness_score": 90,
            "research_priority_score": 85.0,
            "preliminary_coverage_score": 85.0,
            "region_priority": 0,
            "details_checked": True,
        },
    ]


def _install_stage_scripts(root: Path) -> None:
    for script in (
        "discover_fixtures.py",
        "filter_major_leagues.py",
        "preselect_matches.py",
        "prematch_pattern_engine.py",
        "prematch_pattern_discovery.py",
        "bwin_discovery.py",
    ):
        (root / script).write_text("# stub\n", encoding="utf-8")


class TestMultiSlotTrialIntegration(unittest.TestCase):
    DAY = date(2026, 11, 17)
    LABEL = "2026-11-17_1500_0300"

    def test_optional_team_filter_none_vs_empty_normalized_consistently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ns_none = SimpleNamespace(
                start=time(15, 0),
                end=time(3, 0),
                horizon_hours=0,
                evaluation_budget=0,
                discovery_budget=0,
                max_matches=5,
                bwin_top=5,
                history=20,
                details=8,
                min_lead=45,
                competition_scope="adult-open",
                team=None,
                coverage_rules=None,
            )
            ns_empty = SimpleNamespace(**{**vars(ns_none), "team": []})

            self.assertEqual(trial._batch_configuration(ns_none), trial._batch_configuration(ns_empty))
            self.assertEqual(trial._batch_configuration(ns_none)["team_filter"], [])
            self.assertEqual(
                trial._evaluation_request(self.DAY, ns_none),
                trial._evaluation_request(self.DAY, ns_empty),
            )

            # Namespace generado por V3.0 cumple todas las claves esperadas sin None en team
            orch_args = _build_prospective_trial_args(
                OrchestratorConfig(window_day=self.DAY, min_lead_minutes=45),
                now_utc=datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(orch_args.team, [])
            self.assertGreaterEqual(orch_args.bwin_top, 1)
            self.assertEqual(orch_args.competition_scope, "adult-open")
            self.assertEqual(trial._batch_configuration(orch_args)["team_filter"], [])

            # reusable_preparation no debe fallar si recibe team=None en args heredado
            folder = root / "outputs/discovery" / self.LABEL
            (folder / "prospective_trial").mkdir(parents=True)
            with self.assertRaisesRegex(trial.TrialError, "MISSING_PREPARATION_SUMMARY"):
                runtime.reusable_preparation(root, folder, self.DAY, ns_none)

    def test_resolve_or_generate_preselection_runs_trial_prepare_then_reuses_and_ranks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install_stage_scripts(root)
            fixtures = _sample_fixtures()
            calls: list[str] = []

            def fake_trial_run(r: Path, script: str, args: list[str], logs: Path) -> Path:
                calls.append(script)
                if script == "discover_fixtures.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(
                        out_dir / "fixtures.json",
                        {"schema_version": "1.0-discovery", "fixtures": fixtures, "fixture_audit": fixtures},
                    )
                elif script == "filter_major_leagues.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    raw_path = out_dir / "fixtures.json"
                    trial.dump(
                        out_dir / "fixtures_major.json",
                        {
                            "schema_version": "1.0-major-leagues",
                            "source_sha256": trial.sha(raw_path),
                            "policy": {"competition_tier": "ADULT_OPEN"},
                            "fixtures": fixtures,
                            "excluded": [],
                            "review": [],
                        },
                    )
                elif script == "preselect_matches.py":
                    in_path = Path(args[args.index("--input") + 1])
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    batch_doc = trial.read(in_path)
                    trial.dump(
                        out_dir / "preselection.json",
                        {
                            "schema_version": "1.3-preselection",
                            "generated_at_utc": "2026-11-17T11:05:00+00:00",
                            "source_filter_sha256": trial.sha(in_path),
                            "methodology": {"sample_history_cap": 20, "sample_details_cap": 8},
                            "counts": {"input_filter": len(batch_doc["fixtures"])},
                            "api_request_ledger": [],
                            "matches": batch_doc["fixtures"],
                        },
                    )
                elif script == "prematch_pattern_discovery.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(out_dir / "pattern_discovery.json", {"patterns": []})
                    trial.dump(out_dir / "research_queue_sport.json", {"queue": []})
                elif script == "prematch_pattern_engine.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    pre_p = Path(args[args.index("--preselection") + 1])
                    trial.dump(
                        out_dir / "pattern_engine.json",
                        {"data_sources": {"preselection_sha256": trial.sha(pre_p)}, "fixtures": []},
                    )
                elif script == "bwin_discovery.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(
                        out_dir / "bwin_discovery.json",
                        {
                            "generated_at_utc": "2026-11-17T11:06:00+00:00",
                            "catalog_observed": [],
                            "matches": [
                                {"fixture_id": f["fixture_id"], "status": "MATCHED", "bwin_event_id": 9000 + i}
                                for i, f in enumerate(fixtures, 1)
                            ],
                        },
                    )
                return logs / f"{script}.log"

            def fake_save_report(pre_doc: dict, staging: Path) -> None:
                staging.mkdir(parents=True, exist_ok=True)
                trial.dump(staging / "preselection.json", pre_doc)
                (staging / "preselection.csv").write_text("fixture_id\n", encoding="utf-8")
                (staging / "preselection.md").write_text("# Preselection\n", encoding="utf-8")

            def fake_tactical_engine(prepath: Path, pattern_path: Path, out_dir: Path, r: Path, **kwargs) -> dict:
                out_dir.mkdir(parents=True, exist_ok=True)
                return {"fixtures": {str(f["fixture_id"]): {"priority_score": f["research_priority_score"]} for f in fixtures}}

            def fake_identity_audit(matches, catalog, pages, sources, **kwargs):
                return {"summary": {"matched": len(matches)}, "matches": []}

            cfg = OrchestratorConfig(
                window_day=self.DAY,
                offline=False,
                online=True,
                confirm_external=True,
                min_lead_minutes=45,
            )
            now_ref = datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc)

            with patch.object(trial, "run", side_effect=fake_trial_run), \
                 patch("preselect_matches.save_report", side_effect=fake_save_report), \
                 patch("prematch_tactical_signal_engine.run_engine", side_effect=fake_tactical_engine), \
                 patch("bwin_identity_audit.audit", side_effect=fake_identity_audit), \
                 patch("bwin_identity_audit.write_reports"):
                pre_path, auto_gen = resolve_or_generate_preselection(root, cfg, now_utc=now_ref)
                self.assertTrue(auto_gen)
                self.assertIsNotNone(pre_path)
                self.assertTrue(pre_path.is_file())
                self.assertIn("evaluations", str(pre_path))
                self.assertEqual(
                    calls,
                    [
                        "discover_fixtures.py",
                        "filter_major_leagues.py",
                        "preselect_matches.py",
                        "prematch_pattern_discovery.py",
                        "prematch_pattern_engine.py",
                        "bwin_discovery.py",
                    ],
                )

                # Segunda llamada: debe reutilizar preselection.json sin invocar discovery/filtros ni API
                calls.clear()
                pre_path_2, auto_gen_2 = resolve_or_generate_preselection(root, cfg, now_utc=now_ref)
                self.assertFalse(auto_gen_2)
                self.assertEqual(pre_path_2, pre_path)
                self.assertEqual(calls, [])

            # El ranking V3.0 consume la preselección generada sin errores
            orch = MultiSlotOrchestrator(root, OrchestratorConfig(window_day=self.DAY))
            plan = orch.init_or_load_plan(now_utc=now_ref)
            summary = orch.render_summary(plan)
            self.assertEqual(summary["total_candidates"], 3)
            self.assertEqual(summary["eligible_candidates"], 3)
            self.assertEqual([t["fixture_id"] for t in summary["top_3_global"]], [7003, 7001, 7002])

    def test_resumes_interrupted_pre_enrichment_snapshot_without_repeating_discovery_or_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install_stage_scripts(root)
            fixtures = _sample_fixtures()
            win_dir = root / "outputs" / "discovery" / self.LABEL
            base = win_dir / "prospective_trial"
            base.mkdir(parents=True)

            # Simular exactamente el estado real del 17/11 11:51 CET tras fallar antes de enrichment_batches/
            trial.dump(win_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": fixtures})
            raw_sha = trial.sha(win_dir / "fixtures.json")
            from filter_major_leagues import FILTER_IMPLEMENTATION
            major_doc = {
                "schema_version": "1.0-major-leagues",
                "filter_implementation": FILTER_IMPLEMENTATION,
                "source_sha256": raw_sha,
                "fixtures": fixtures,
                "excluded": [],
                "review": [],
            }
            trial.dump(win_dir / "fixtures_major.json", major_doc)
            major_sha = trial.sha(win_dir / "fixtures_major.json")
            subset_doc = {
                **major_doc,
                "trial_subset": {
                    "source_sha256": major_sha,
                    "count": len(fixtures),
                    "reference_madrid": "2026-11-17T11:51:31+01:00",
                },
            }
            trial.dump(base / "fixtures_major_test.json", subset_doc)
            from prematch_audit import CandidateLedger
            CandidateLedger(base / "candidate_ledger.json", {"horizon_hours": 0}).write()

            calls: list[str] = []

            def fake_trial_run(r: Path, script: str, args: list[str], logs: Path) -> Path:
                calls.append(script)
                if script == "preselect_matches.py":
                    in_path = Path(args[args.index("--input") + 1])
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    batch_doc = trial.read(in_path)
                    trial.dump(
                        out_dir / "preselection.json",
                        {
                            "schema_version": "1.3-preselection",
                            "generated_at_utc": "2026-11-17T11:55:00+00:00",
                            "source_filter_sha256": trial.sha(in_path),
                            "methodology": {"sample_history_cap": 20, "sample_details_cap": 8},
                            "counts": {"input_filter": len(batch_doc["fixtures"])},
                            "api_request_ledger": [],
                            "matches": batch_doc["fixtures"],
                        },
                    )
                elif script == "prematch_pattern_discovery.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(out_dir / "pattern_discovery.json", {"patterns": []})
                    trial.dump(out_dir / "research_queue_sport.json", {"queue": []})
                elif script == "prematch_pattern_engine.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    pre_p = Path(args[args.index("--preselection") + 1])
                    trial.dump(
                        out_dir / "pattern_engine.json",
                        {"data_sources": {"preselection_sha256": trial.sha(pre_p)}, "fixtures": []},
                    )
                elif script == "bwin_discovery.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(
                        out_dir / "bwin_discovery.json",
                        {"generated_at_utc": "2026-11-17T11:56:00+00:00", "catalog_observed": [], "matches": []},
                    )
                return logs / f"{script}.log"

            def fake_save_report(pre_doc: dict, staging: Path) -> None:
                staging.mkdir(parents=True, exist_ok=True)
                trial.dump(staging / "preselection.json", pre_doc)
                (staging / "preselection.csv").write_text("fixture_id\n", encoding="utf-8")
                (staging / "preselection.md").write_text("# Preselection\n", encoding="utf-8")

            def fake_tactical_engine(prepath: Path, pattern_path: Path, out_dir: Path, r: Path, **kwargs) -> dict:
                out_dir.mkdir(parents=True, exist_ok=True)
                return {"fixtures": {str(f["fixture_id"]): {"priority_score": f["research_priority_score"]} for f in fixtures}}

            cfg = OrchestratorConfig(window_day=self.DAY, offline=False, online=True, confirm_external=True)
            with patch.object(trial, "run", side_effect=fake_trial_run), \
                 patch("preselect_matches.save_report", side_effect=fake_save_report), \
                 patch("prematch_tactical_signal_engine.run_engine", side_effect=fake_tactical_engine), \
                 patch("bwin_identity_audit.audit", return_value={"summary": {}, "matches": []}), \
                 patch("bwin_identity_audit.write_reports"):
                pre_path, auto_gen = resolve_or_generate_preselection(
                    root, cfg, now_utc=datetime(2026, 11, 17, 11, 55, tzinfo=timezone.utc)
                )

            self.assertTrue(auto_gen)
            self.assertEqual(pre_path, (base / "enriched" / "preselection.json").resolve())
            self.assertNotIn("discover_fixtures.py", calls)
            self.assertNotIn("filter_major_leagues.py", calls)
            self.assertIn("preselect_matches.py", calls)
            # Los snapshots originales se mantienen intactos
            self.assertEqual(trial.sha(win_dir / "fixtures.json"), raw_sha)
            self.assertEqual(trial.sha(win_dir / "fixtures_major.json"), major_sha)

    def test_full_offline_cli_simulation_plan_preselection_ranking_top3(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pre_path = (
                root / "outputs" / "discovery" / self.LABEL / "prospective_trial" / "enriched" / "preselection.json"
            )
            pre_path.parent.mkdir(parents=True, exist_ok=True)
            trial.dump(
                pre_path,
                {"generated_at_utc": "2026-11-17T11:00:00+00:00", "matches": _sample_fixtures()},
            )

            out_plan = io.StringIO()
            with contextlib.redirect_stdout(out_plan):
                rc1 = cli_main(
                    [
                        "plan",
                        "--root",
                        str(root),
                        "--date",
                        "2026-11-17",
                        "--now-utc",
                        "2026-11-17T11:05:00+00:00",
                    ]
                )
            self.assertEqual(rc1, 0)
            plan_doc = json.loads(out_plan.getvalue())
            self.assertEqual(plan_doc["eligible_candidates"], 3)

            out_top3 = io.StringIO()
            with contextlib.redirect_stdout(out_top3):
                rc2 = cli_main(
                    [
                        "top3",
                        "--root",
                        str(root),
                        "--date",
                        "2026-11-17",
                        "--now-utc",
                        "2026-11-17T11:05:00+00:00",
                    ]
                )
            self.assertEqual(rc2, 0)
            top3_doc = json.loads(out_top3.getvalue())
            self.assertEqual(len(top3_doc["top_3_global"]), 3)
            self.assertEqual(
                {t["slot_id"] for t in top3_doc["top_3_global"]},
                {"FRANJA_A", "FRANJA_B", "FRANJA_C"},
            )

    def test_real_subprocess_chain_propagates_external_confirmation_only_when_authorized(self):
        """Etapa 3 y 4: ejecuta subprocesos reales verificando --confirm-external y bloqueo estricto."""
        import os
        import prematch_execution_control as control
        from prematch_audit import CandidateLedger

        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit_log = root / "child_subprocess_audit.jsonl"
            fixtures = _sample_fixtures()

            # Escribir scripts hijos reales que validan require_external_confirmation de prematch_execution_control
            (root / "discover_fixtures.py").write_text(
                "import json, sys; from pathlib import Path\n"
                f"Path({str(audit_log)!r}).open('a').write(json.dumps({{'script':'discover_fixtures.py','argv':sys.argv[1:]}})+'\\n')\n",
                encoding="utf-8",
            )
            (root / "filter_major_leagues.py").write_text(
                "import json, sys; from pathlib import Path\n"
                f"Path({str(audit_log)!r}).open('a').write(json.dumps({{'script':'filter_major_leagues.py','argv':sys.argv[1:]}})+'\\n')\n",
                encoding="utf-8",
            )
            (root / "preselect_matches.py").write_text(
                "import argparse, hashlib, json, os, sys; from pathlib import Path\n"
                f"sys.path.insert(0, {str(repo_root)!r})\n"
                "from prematch_execution_control import require_external_confirmation\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--input', type=Path, required=True)\n"
                "p.add_argument('--output-dir', type=Path, required=True)\n"
                "p.add_argument('--max-matches', type=int, required=True)\n"
                "p.add_argument('--history-limit', type=int, required=True)\n"
                "p.add_argument('--detail-limit', type=int, required=True)\n"
                "p.add_argument('--coverage-rules', type=Path)\n"
                "p.add_argument('--confirm-external', action='store_true')\n"
                "args = p.parse_args()\n"
                "require_external_confirmation(args.confirm_external)\n"
                f"Path({str(audit_log)!r}).open('a').write(json.dumps({{'script':'preselect_matches.py','argv':sys.argv[1:],'confirm_flag':args.confirm_external,'env_len':len(os.environ.get('PREMATCH_EXTERNAL_CONFIRMATION',''))}})+'\\n')\n"
                "inp = json.loads(args.input.read_text())\n"
                "sha = hashlib.sha256(args.input.read_bytes()).hexdigest()\n"
                "args.output_dir.mkdir(parents=True, exist_ok=True)\n"
                "(args.output_dir / 'preselection.json').write_text(json.dumps({\n"
                "  'schema_version':'1.3-preselection','generated_at_utc':'2026-11-17T11:55:00+00:00',\n"
                "  'source_filter_sha256':sha,'methodology':{'sample_history_cap':20,'sample_details_cap':8},\n"
                "  'counts':{'input_filter':len(inp['fixtures'])},'api_request_ledger':[],'matches':inp['fixtures']\n"
                "}))\n",
                encoding="utf-8",
            )
            (root / "prematch_pattern_discovery.py").write_text(
                "import argparse, json; from pathlib import Path\n"
                "p = argparse.ArgumentParser(); p.add_argument('--preselection'); p.add_argument('--output-dir', type=Path); p.add_argument('--asof')\n"
                "a = p.parse_args(); a.output_dir.mkdir(parents=True, exist_ok=True)\n"
                "(a.output_dir / 'pattern_discovery.json').write_text('{}')\n"
                "(a.output_dir / 'research_queue_sport.json').write_text('{}')\n",
                encoding="utf-8",
            )
            (root / "prematch_pattern_engine.py").write_text(
                "import argparse, hashlib, json; from pathlib import Path\n"
                "p = argparse.ArgumentParser(); p.add_argument('--preselection', type=Path); p.add_argument('--top'); p.add_argument('--min-lead-minutes'); p.add_argument('--output-dir', type=Path); p.add_argument('--asof')\n"
                "a = p.parse_args(); a.output_dir.mkdir(parents=True, exist_ok=True)\n"
                "sha = hashlib.sha256(a.preselection.read_bytes()).hexdigest()\n"
                "(a.output_dir / 'pattern_engine.json').write_text(json.dumps({'data_sources':{'preselection_sha256':sha},'fixtures':[]}))\n",
                encoding="utf-8",
            )
            (root / "bwin_discovery.py").write_text(
                "import argparse, json, os, sys; from pathlib import Path\n"
                f"sys.path.insert(0, {str(repo_root)!r})\n"
                "from prematch_execution_control import require_external_confirmation\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--input', type=Path, required=True)\n"
                "p.add_argument('--headless', action='store_true')\n"
                "p.add_argument('--output-dir', type=Path, required=True)\n"
                "p.add_argument('--fixture-id', type=int, action='append', default=[])\n"
                "p.add_argument('--confirm-external', action='store_true')\n"
                "args = p.parse_args()\n"
                "require_external_confirmation(args.confirm_external)\n"
                f"Path({str(audit_log)!r}).open('a').write(json.dumps({{'script':'bwin_discovery.py','argv':sys.argv[1:],'confirm_flag':args.confirm_external,'env_len':len(os.environ.get('PREMATCH_EXTERNAL_CONFIRMATION',''))}})+'\\n')\n"
                "pre = json.loads(args.input.read_text())\n"
                "args.output_dir.mkdir(parents=True, exist_ok=True)\n"
                "(args.output_dir / 'bwin_discovery.json').write_text(json.dumps({\n"
                "  'generated_at_utc':'2026-11-17T11:56:00+00:00','catalog_observed':[],\n"
                "  'matches':[{'fixture_id':m['fixture_id'],'status':'POSSIBLE_EVENT_FOUND','bwin_event_id':9500+idx} for idx, m in enumerate(pre['matches'], 1)]\n"
                "}))\n",
                encoding="utf-8",
            )

            # Crear snapshot congelado pre-enriquecimiento idéntico al fallo real
            win_dir = root / "outputs" / "discovery" / self.LABEL
            base = win_dir / "prospective_trial"
            base.mkdir(parents=True)
            trial.dump(win_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": fixtures})
            raw_sha = trial.sha(win_dir / "fixtures.json")
            from filter_major_leagues import FILTER_IMPLEMENTATION
            major_doc = {"schema_version": "1.0-major-leagues", "filter_implementation": FILTER_IMPLEMENTATION, "source_sha256": raw_sha, "fixtures": fixtures, "excluded": [], "review": []}
            trial.dump(win_dir / "fixtures_major.json", major_doc)
            major_sha = trial.sha(win_dir / "fixtures_major.json")
            trial.dump(
                base / "fixtures_major_test.json",
                {**major_doc, "trial_subset": {"source_sha256": major_sha, "count": len(fixtures), "reference_madrid": "2026-11-17T11:51:31+01:00"}},
            )
            CandidateLedger(base / "candidate_ledger.json", {"horizon_hours": 0}).write()

            # 1) --online sin --confirm-external debe fallar sin lanzar subprocesos (incluso con token ambiental)
            with patch.dict(os.environ, {"PREMATCH_EXTERNAL_CONFIRMATION": "0" * 64}):
                err_buf = io.StringIO()
                with contextlib.redirect_stderr(err_buf):
                    rc_denied = cli_main(["plan", "--root", str(root), "--date", "2026-11-17", "--online"])
                self.assertEqual(rc_denied, 2)
                self.assertIn("EXTERNAL_CONFIRMATION_REQUIRED", err_buf.getvalue())
                self.assertFalse(audit_log.exists())

            # 2) --dry-run (aun con --online --confirm-external) NO ejecuta subprocesos ni escribe archivos
            out_dry = io.StringIO()
            with contextlib.redirect_stdout(out_dry):
                rc_dry = cli_main(["plan", "--root", str(root), "--date", "2026-11-17", "--online", "--confirm-external", "--dry-run"])
            self.assertEqual(rc_dry, 0)
            self.assertFalse(audit_log.exists())
            self.assertFalse((root / "outputs" / "orchestrator").exists())

            # 3) Control positivo de bloqueo: sin token/flag, require_external_confirmation sigue bloqueando
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PREMATCH_EXTERNAL_CONFIRMATION", None)
                with self.assertRaisesRegex(ValueError, "EXTERNAL_CONFIRMATION_REQUIRED"):
                    control.require_external_confirmation(False)

            # 4) --online --confirm-external --require-bwin real contra subprocesos hijos Python
            out_plan = io.StringIO()
            with patch("preselect_matches.save_report", side_effect=lambda pre_doc, staging: (
                staging.mkdir(parents=True, exist_ok=True),
                trial.dump(staging / "preselection.json", pre_doc),
                (staging / "preselection.csv").write_text("fixture_id\n", encoding="utf-8"),
                (staging / "preselection.md").write_text("# Pre\n", encoding="utf-8"),
            )), patch("prematch_tactical_signal_engine.run_engine", return_value={"fixtures": {str(f["fixture_id"]): {"priority_score": f["research_priority_score"]} for f in fixtures}}), \
                 patch("bwin_identity_audit.audit", return_value={"summary": {}, "matches": []}), \
                 patch("bwin_identity_audit.write_reports"), \
                 contextlib.redirect_stdout(out_plan):
                rc_ok = cli_main([
                    "plan", "--root", str(root), "--date", "2026-11-17",
                    "--start", "15:00", "--end", "03:00",
                    "--online", "--confirm-external", "--require-bwin",
                    "--min-lead", "45", "--lineup-cutoff", "15", "--max-scrapes", "12",
                    "--now-utc", "2026-11-17T11:55:00+00:00",
                ])
            self.assertEqual(rc_ok, 0)
            # Entorno limpio al salir: sin token permanente global
            self.assertNotIn("PREMATCH_EXTERNAL_CONFIRMATION", os.environ)

            records = [json.loads(line) for line in audit_log.read_text().splitlines() if line.strip()]
            scripts_called = [r["script"] for r in records]
            self.assertEqual(scripts_called, ["preselect_matches.py", "bwin_discovery.py"])
            self.assertNotIn("discover_fixtures.py", scripts_called)
            self.assertNotIn("filter_major_leagues.py", scripts_called)
            for rec in records:
                self.assertIn("--confirm-external", rec["argv"])
                self.assertTrue(rec["confirm_flag"])
                self.assertEqual(rec["env_len"], 64)

            plan_summary = json.loads(out_plan.getvalue())
            self.assertEqual(plan_summary["eligible_candidates"], 3)
            self.assertEqual(len(plan_summary["top_3_global"]), 3)
            self.assertTrue(all(row["event_id"] is not None for row in plan_summary["top_3_global"]))

            # 5) top3 y --offline posteriores reutilizan snapshot sin ejecutar ningún subproceso externo
            audit_log.unlink()
            out_top3 = io.StringIO()
            with patch.dict(os.environ, {"PREMATCH_EXTERNAL_CONFIRMATION": "f" * 64}), \
                 contextlib.redirect_stdout(out_top3):
                rc_top3 = cli_main(["top3", "--root", str(root), "--date", "2026-11-17", "--offline"])
            self.assertEqual(rc_top3, 0)
            self.assertFalse(audit_log.exists())
            top3_doc = json.loads(out_top3.getvalue())
            self.assertEqual([x["fixture_id"] for x in top3_doc["top_3_global"]], [7003, 7001, 7002])


if __name__ == "__main__":
    unittest.main()
