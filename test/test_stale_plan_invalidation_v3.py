import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import filter_major_leagues as fml
import prematch_prospective_trial as trial
from bet_engine.contracts.multi_slot import MultiMatchExecutionState
from bet_engine.orchestration.multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorAdapters,
    OrchestratorConfig,
)
from prematch_orchestrator import main as cli_main


def _old_9_matches() -> list[dict]:
    # 9 candidatos equivalentes al plan obsoleto 2.0 (1 Franja A sin Bwin, 8 Franja B incluyendo Wales/Denmark/Poland con readiness 70)
    rows = [
        {
            "fixture_id": 1628598,
            "bwin_event_id": None,
            "home": "Plateau United",
            "away": "Kun Khalifat FC",
            "league_id": 399,
            "league_name": "NPFL",
            "season": 2026,
            "kickoff_utc": "2026-11-17T15:00:00+00:00",  # 16:00 CET -> FRANJA_A
            "data_readiness_score": 40,
            "statistical_observations": {},
            "details_checked": True,
        },
    ]
    pairs = [
        (1528992, 7912001, "Wales", "Portugal", 70),
        (1528993, 7912002, "Denmark", "Norway", 70),
        (1528998, 7912003, "Poland", "Sweden", 70),
        (1528996, 7912004, "Israel", "Austria", 69),
        (1528999, 7912005, "Bosnia & Herzegovina", "Romania", 70),
        (1528995, 7912006, "Hungary", "Northern Ireland", 68),
        (1528994, 7912007, "Ukraine", "Georgia", 68),
        (1528997, 7912008, "Rep. Of Ireland", "Kosovo", 69),
    ]
    for fid, eid, h, a, rd in pairs:
        rows.append(
            {
                "fixture_id": fid,
                "bwin_event_id": eid,
                "home": h,
                "away": a,
                "league_id": 5,
                "league_name": "UEFA Nations League",
                "season": 2026,
                "kickoff_utc": "2026-11-17T19:45:00+00:00",  # 20:45 CET -> FRANJA_B
                "data_readiness_score": rd,
                "statistical_observations": {},
                "details_checked": True,
            }
        )
    return rows


def _new_21_matches_with_franja_c_friendlies() -> list[dict]:
    rows = list(_old_9_matches())
    # Añadir amistosos absolutos elegibles incluyendo los 4 de Franja C
    franja_c = [
        (1640519, 7915019, "Chile", "Colombia", "2026-11-17T23:00:00+00:00", 80, 74.0),
        (1647372, 7915372, "Argentina", "Costa Rica", "2026-11-17T23:00:00+00:00", 86, 84.0),
        (1647373, 7915373, "Ecuador", "Bolivia", "2026-11-17T23:30:00+00:00", 78, 71.0),
        (1648287, 7915287, "Uruguay", "Dominican Republic", "2026-11-17T23:30:00+00:00", 76, 68.0),
    ]
    for fid, eid, h, a, ko, rd, q in franja_c:
        rows.append(
            {
                "fixture_id": fid,
                "bwin_event_id": eid,
                "home": h,
                "away": a,
                "league_id": 10,
                "league_name": "Friendlies",
                "season": 2026,
                "kickoff_utc": ko,
                "data_readiness_score": rd,
                "research_priority_score": q,
                "details_checked": True,
            }
        )
    return rows


class TestStalePlanInvalidationE2E(unittest.TestCase):
    DAY = date(2026, 11, 17)
    LABEL = "2026-11-17_1500_0300"

    def test_stale_plan_20_is_archived_and_rebuilt_for_21_and_subcommands_use_active_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            win_dir = root / "outputs" / "discovery" / self.LABEL

            # 1 y 2. Generar plan V3 con política 2.0-adult-open y 9 candidatos, y persistirlo
            old_pre = win_dir / "prospective_trial" / "enriched" / "preselection.json"
            old_pre.parent.mkdir(parents=True, exist_ok=True)
            trial.dump(
                win_dir / "fixtures_major.json",
                {"schema_version": "1.0-major-leagues", "filter_implementation": "2.0-adult-open", "fixtures": []},
            )
            trial.dump(old_pre, {"generated_at_utc": "2026-11-17T12:00:00+00:00", "matches": _old_9_matches()})
            old_pre_sha = trial.sha(old_pre)

            now_0 = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
            with patch.object(fml, "FILTER_IMPLEMENTATION", "2.0-adult-open"):
                orch_v1 = MultiSlotOrchestrator(root, OrchestratorConfig(window_day=self.DAY, require_bwin=True))
                plan_v1 = orch_v1.init_or_load_plan(now_utc=now_0)

            self.assertEqual(plan_v1.plan_version, 1)
            self.assertEqual(len(plan_v1.candidates), 9)
            self.assertEqual(plan_v1.filter_implementation, "2.0-adult-open")
            self.assertEqual(plan_v1.preselection_sha256, old_pre_sha)
            top3_v1 = [c["fixture_id"] for c in orch_v1.render_summary(plan_v1)["top_3_global"]]
            self.assertEqual(top3_v1, [1528992, 1528993, 1528998])

            # 3. Actualizar política a 2.1 y disponibilizar nueva evaluación con amistosos Franja C
            eval_new = win_dir / "evaluations" / "20261117T135000Z_v21" / "prospective_trial"
            new_pre = eval_new / "enriched" / "preselection.json"
            new_bwin = eval_new / "bwin" / "bwin_discovery.json"
            new_pre.parent.mkdir(parents=True, exist_ok=True)
            new_bwin.parent.mkdir(parents=True, exist_ok=True)
            trial.dump(
                eval_new.parent / "fixtures_major.json",
                {"schema_version": "1.0-major-leagues", "filter_implementation": fml.FILTER_IMPLEMENTATION, "fixtures": []},
            )
            new_matches = _new_21_matches_with_franja_c_friendlies()
            trial.dump(new_pre, {"generated_at_utc": "2026-11-17T13:50:00+00:00", "matches": new_matches})
            trial.dump(
                new_bwin,
                {
                    "matches": [
                        {"fixture_id": m["fixture_id"], "status": "POSSIBLE_EVENT_FOUND" if m.get("bwin_event_id") else "NOT_FOUND_IN_SCANNED_PAGES", "bwin_event_id": m.get("bwin_event_id")}
                        for m in new_matches
                    ]
                },
            )
            new_pre_sha = trial.sha(new_pre)

            # 4 y 5. Ejecutar plan nuevamente -> NO debe devolver silenciosamente el plan 2.0 obsoleto
            out_plan = io.StringIO()
            with contextlib.redirect_stdout(out_plan):
                rc = cli_main([
                    "plan", "--root", str(root), "--date", "2026-11-17",
                    "--require-bwin", "--now-utc", "2026-11-17T13:55:00+00:00",
                ])
            self.assertEqual(rc, 0)
            plan_doc = json.loads(out_plan.getvalue())

            # 6. Plan vigente apunta a la nueva evaluación, versión 2, con Franja C incluida
            self.assertEqual(plan_doc["plan_version"], 2)
            self.assertEqual(plan_doc["parent_plan_id"], plan_v1.plan_id)
            self.assertIn("FILTER_IMPLEMENTATION_CHANGED", plan_doc["rebuild_reason"])
            self.assertEqual(plan_doc["preselection_path"], str(new_pre.resolve()))
            self.assertEqual(plan_doc["preselection_sha256"], new_pre_sha)
            self.assertEqual(plan_doc["total_candidates"], 13)
            franja_c_row = next(s for s in plan_doc["slots"] if s["slot_id"] == "FRANJA_C")
            self.assertEqual(franja_c_row["candidate_count"], 4)

            # 7. Artefactos antiguos preservados intactos (preselección 2.0 + archivo en plan_history)
            self.assertEqual(trial.sha(old_pre), old_pre_sha)
            archived_files = list((orch_v1.plan_dir / "plan_history").glob("*.json"))
            self.assertEqual(len(archived_files), 1)
            archived_v1 = json.loads(archived_files[0].read_text(encoding="utf-8"))
            self.assertEqual(archived_v1["plan_version"], 1)
            self.assertEqual(archived_v1["preselection_sha256"], old_pre_sha)

            # 8. Repetir ejecución de plan -> idempotencia (sigue en plan_version 2, mismo SHA, sin duplicar archivo)
            out_plan_again = io.StringIO()
            with contextlib.redirect_stdout(out_plan_again):
                rc_again = cli_main([
                    "plan", "--root", str(root), "--date", "2026-11-17",
                    "--require-bwin", "--now-utc", "2026-11-17T13:56:00+00:00",
                ])
            self.assertEqual(rc_again, 0)
            self.assertEqual(json.loads(out_plan_again.getvalue())["plan_version"], 2)
            self.assertEqual(len(list((orch_v1.plan_dir / "plan_history").glob("*.json"))), 1)

            # 10. top3, status, step y run consultan la versión vigente (v2)
            for cmd in ("top3", "status", "step", "run"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    args_cmd = [cmd, "--root", str(root), "--date", "2026-11-17"]
                    if cmd != "status":
                        args_cmd += ["--now-utc", "2026-11-17T13:57:00+00:00"]
                    rc_cmd = cli_main(args_cmd)
                self.assertEqual(rc_cmd, 0)
                doc_cmd = json.loads(buf.getvalue())
                self.assertEqual(doc_cmd["plan_version"], 2)

    def test_protected_closed_and_active_dossiers_are_never_overwritten_during_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            win_dir = root / "outputs" / "discovery" / self.LABEL
            old_pre = win_dir / "prospective_trial" / "enriched" / "preselection.json"
            old_pre.parent.mkdir(parents=True, exist_ok=True)
            trial.dump(old_pre, {"generated_at_utc": "2026-11-17T12:00:00+00:00", "matches": _old_9_matches()})

            now_0 = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
            with patch.object(fml, "FILTER_IMPLEMENTATION", "2.0-adult-open"):
                orch = MultiSlotOrchestrator(root, OrchestratorConfig(window_day=self.DAY, require_bwin=True))
                p1 = orch.init_or_load_plan(now_utc=now_0)
                # Cerrar 1528992 como FINALIZED con PICK_ACCIONABLE en Franja B
                c_closed = p1.get_candidate(1528992)
                c_closed.transition_to(MultiMatchExecutionState.SELECTED, now_0, "sel")
                c_closed.transition_to(MultiMatchExecutionState.PREPARING, now_0, "prep")
                c_closed.transition_to(MultiMatchExecutionState.READY_FOR_FINALIZATION, now_0, "xi")
                c_closed.shadow_decision = "PICK_ACCIONABLE"
                c_closed.audit_outcome = "ACTIONABLE_CANDIDATE_READY"
                c_closed.candidate_selection = {"market": "1X2", "selection": "1", "odds": 1.95}
                c_closed.transition_to(MultiMatchExecutionState.FINALIZED, now_0, "closed")
                orch.save_plan(p1)

            # Nueva evaluación 2.1 añade Franja C
            eval_new = win_dir / "evaluations" / "20261117T135500Z_v21" / "prospective_trial"
            new_pre = eval_new / "enriched" / "preselection.json"
            new_pre.parent.mkdir(parents=True, exist_ok=True)
            trial.dump(
                eval_new.parent / "fixtures_major.json",
                {"schema_version": "1.0-major-leagues", "filter_implementation": fml.FILTER_IMPLEMENTATION, "fixtures": []},
            )
            trial.dump(new_pre, {"generated_at_utc": "2026-11-17T13:55:00+00:00", "matches": _new_21_matches_with_franja_c_friendlies()})

            # Migrar bajo política 2.1: debe conservar el expediente FINALIZED de 1528992 y añadir Franja C
            orch_v2 = MultiSlotOrchestrator(root, OrchestratorConfig(window_day=self.DAY, require_bwin=True))
            p2 = orch_v2.init_or_load_plan(now_utc=datetime(2026, 11, 17, 13, 56, tzinfo=timezone.utc))

            self.assertEqual(p2.plan_version, 2)
            self.assertEqual(p2.parent_plan_id, p1.plan_id)
            preserved = p2.get_candidate(1528992)
            self.assertEqual(preserved.execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(preserved.shadow_decision, "PICK_ACCIONABLE")
            self.assertEqual(preserved.candidate_selection, {"market": "1X2", "selection": "1", "odds": 1.95})
            # Los nuevos candidatos de Franja C están incorporados en RANKED
            self.assertEqual(p2.get_candidate(1647372).slot_id, "FRANJA_C")
            self.assertEqual(p2.get_candidate(1647372).execution_state, MultiMatchExecutionState.RANKED)
            # Plan anterior archivado intacto
            archived = json.loads(Path(p2.parent_plan_path).read_text(encoding="utf-8"))
            self.assertEqual(archived["plan_version"], 1)


if __name__ == "__main__":
    unittest.main()
