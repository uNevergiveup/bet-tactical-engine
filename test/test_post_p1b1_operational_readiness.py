"""Pruebas operativas offline post P1-B1 para eventos 7909713 (Argentina-Costa Rica)
y 7909714 (Chile-Colombia): CLI de casos, preparación independiente, conflictos de franja,
límites por defecto 6 vs explícito 10/20 y puertas P0/Shadow sin picks accionables.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import config.constants as constants
import prematch_case
import prematch_multi
from bet_engine.context import ObservationStore
from bet_engine.contracts.multi_slot import (
    MultiMatchExecutionState,
    MultiSlotContractError,
    SlotCandidateRecord,
    TimeSlotSpec,
    WindowOrchestrationPlan,
)
from bet_engine.orchestration import MultiSlotOrchestrator, OrchestratorConfig, ShadowPipelineRunner
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt

ROOT = Path(__file__).resolve().parent.parent


class TestPostP1B1OperationalReadiness(unittest.TestCase):
    def test_delegated_event_id_accepted_and_mismatch_rejected(self) -> None:
        sel = ROOT / "outputs/matches/7909714/selection/selected_match.json"
        if not sel.is_file():
            self.skipTest("Snapshot 7909714 no presente")
        calls: list[tuple[str, list[str], bool]] = []

        def fake_delegate(root: Path, selected: Path, action: str, extra: list[str], confirm_external: bool) -> int:
            calls.append((action, extra, confirm_external))
            return 0

        with mock.patch.object(prematch_case, "delegate", side_effect=fake_delegate):
            rc = prematch_case.main(["acquire-lineups", "--selected", str(sel), "--event-id", "7909714"])
            self.assertEqual(rc, 0)
            self.assertEqual(calls[-1], ("acquire-lineups", [], True))

            rc_bad = prematch_case.main(["acquire-lineups", "--selected", str(sel), "--event-id", "7909713"])
            self.assertEqual(rc_bad, 2)

    def test_prepare_independent_event_7909713_offline_and_manifest_sync(self) -> None:
        pre_src = ROOT / "outputs/discovery/2026-11-17_1500_0300/evaluations/20261117T140046103296Z_6e3badff0eef/prospective_trial/enriched/preselection.json"
        bwin_src = ROOT / "outputs/discovery/2026-11-17_1500_0300/evaluations/20261117T140046103296Z_6e3badff0eef/prospective_trial/bwin/bwin_discovery.json"
        if not pre_src.is_file() or not bwin_src.is_file():
            self.skipTest("Discovery congelado 2026-11-17 no presente")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            res = prematch_case.prepare_independent_event(
                tmp,
                event_id="7909713",
                preselection_path=pre_src,
                bwin_discovery_path=bwin_src,
                now_utc="2026-11-17T21:48:00+00:00",
            )
            self.assertEqual(res["event_id"], "7909713")
            self.assertEqual(res["fixture_id"], 1647372)
            self.assertEqual(res["status"], "CASE_CREATED_PENDING_ACQUISITION")
            self.assertTrue(res["requires_external_acquisition"])
            self.assertFalse((tmp / "outputs/orchestrator").exists())

            st = prematch_case.status(tmp, "7909713")
            self.assertEqual(st["event_id"], "7909713")
            self.assertEqual(st["fixture_id"], 1647372)
            self.assertEqual(st["status"], "CASE_CREATED_PENDING_ACQUISITION")
            self.assertFalse(st["actionable_bet"])

            # Simula manifest PREPARADO escrito por prematch_multi y re-sincroniza enlaces canónicos
            mdir = tmp / "outputs/matches/7909713"
            (mdir / "manifest.json").write_text(
                json.dumps({
                    "event_id": "7909713",
                    "fixture_id": 1647372,
                    "url": res["bwin_url"],
                    "status": "PREPARADO",
                    "inputs": {},
                    "steps": {"scraper": 0, "contexto": 0},
                }),
                encoding="utf-8",
            )
            res2 = prematch_case.prepare_independent_event(
                tmp,
                event_id="7909713",
                preselection_path=pre_src,
                bwin_discovery_path=bwin_src,
                now_utc="2026-11-17T21:49:00+00:00",
            )
            self.assertEqual(res2["status"], "PREPARADO")
            m_doc = json.loads((mdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("canonical_case", m_doc)
            self.assertIn("selected_match", m_doc)

    def test_orchestrator_acquire_fails_closed_when_slot_occupied(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = OrchestratorConfig(
                window_day=date(2026, 11, 17),
                online=True,
                confirm_external=True,
                offline=False,
            )
            orch = MultiSlotOrchestrator(tmp, cfg)
            plan = WindowOrchestrationPlan(
                window_label="2026-11-17_1500_0300",
                window_day="2026-11-17",
                window_start_utc="2026-11-17T14:00:00+00:00",
                window_end_utc="2026-11-18T02:00:00+00:00",
                window_start_madrid="2026-11-17T15:00:00+01:00",
                window_end_madrid="2026-11-18T03:00:00+01:00",
                slots=[
                    TimeSlotSpec(
                        slot_id="FRANJA_C",
                        label="Franja C (21:00-03:00)",
                        start_hhmm="21:00",
                        end_hhmm="03:00",
                        slot_index=2,
                        start_utc="2026-11-17T20:00:00+00:00",
                        end_utc="2026-11-18T02:00:00+00:00",
                        start_madrid="2026-11-17T21:00:00+01:00",
                        end_madrid="2026-11-18T03:00:00+01:00",
                    )
                ],
                candidates=[
                    SlotCandidateRecord(
                        slot_id="FRANJA_C",
                        fixture_id=1640519,
                        event_id=7909714,
                        kickoff_utc="2026-11-17T23:00:00+00:00",
                        kickoff_madrid="2026-11-18T00:00:00+01:00",
                        league_name="Friendlies",
                        global_rank=9,
                        slot_rank=1,
                        quality_score=32.0,
                        eligibility_status="ELIGIBLE",
                        execution_state=MultiMatchExecutionState.WAITING_RESEARCH,
                    ),
                    SlotCandidateRecord(
                        slot_id="FRANJA_C",
                        fixture_id=1647372,
                        event_id=7909713,
                        kickoff_utc="2026-11-17T23:00:00+00:00",
                        kickoff_madrid="2026-11-18T00:00:00+01:00",
                        league_name="Friendlies",
                        global_rank=10,
                        slot_rank=2,
                        quality_score=30.0,
                        eligibility_status="ELIGIBLE",
                        execution_state=MultiMatchExecutionState.RANKED,
                    ),
                ],
                active_by_slot={"FRANJA_C": 1640519},
            )
            orch.save_plan(plan)
            with self.assertRaisesRegex(MultiSlotContractError, "SLOT_OCCUPIED:FRANJA_C"):
                orch.acquire_events(now_utc=datetime(2026, 11, 17, 21, 50, tzinfo=timezone.utc), event_id=7909713)

            odds_file = tmp / "cuotas_bwin_7909714.txt"
            odds_file.write_text("Captura: 2026-11-17T15:50:00+01:00\n", encoding="utf-8")
            sel_file = tmp / "selected_match.json"
            sel_file.write_text("{}", encoding="utf-8")
            fake_art = {
                "operational_stage": "DATA_READY_WAITING_RESEARCH",
                "manifest_status": "PREPARADO",
                "data_ready": True,
                "odds_ready": True,
                "context_ready": True,
                "research_ready": False,
                "selected_match_path": str(sel_file),
                "odds_path": str(odds_file),
                "bwin_url": "https://example.invalid/7909714",
            }
            with mock.patch(
                "bet_engine.orchestration.multi_orchestrator.inspect_operational_case_artifacts",
                return_value=fake_art,
            ):
                details = orch._operational_slot_details(plan, plan.candidates[0])
            self.assertIn(
                f".venv/bin/python -B prematch_case.py refresh-odds --selected {sel_file}",
                details["recommended_commands"],
            )
            self.assertIn(
                f".venv/bin/python -B prematch_case.py acquire-lineups --selected {sel_file}",
                details["recommended_commands"],
            )
            self.assertTrue(
                all("--event-id" not in c for c in details["recommended_commands"] if "acquire-lineups" in c)
            )

    def test_default_20_history_and_shadow_non_accredited_gates_on_7909714(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PREMATCH_DEEP_HISTORY_LIMIT", None)
            self.assertEqual(constants.resolve_deep_history_limit(), 20)
            self.assertEqual(constants.MICRO_HISTORY_LIMIT, 6)
        self.assertEqual(constants.resolve_deep_history_limit(6), 6)
        self.assertEqual(constants.resolve_deep_history_limit(10), 10)
        self.assertEqual(constants.resolve_deep_history_limit(20), 20)

        me_path = ROOT / "outputs/matches/7909714/inputs/match_engine_7909714.json"
        odds_path = ROOT / "outputs/matches/7909714/inputs/cuotas_bwin_7909714.txt"
        if not me_path.is_file() or not odds_path.is_file():
            self.skipTest("Snapshots 7909714 no presentes")

        me_payload = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(me_payload, case_dir=me_path.parent.parent)
        self.assertEqual(len(bundle.home_matches), 6)
        self.assertEqual(len(bundle.away_matches), 6)

        odds = parse_bwin_odds_txt(odds_path, home_name=bundle.home_name, away_name=bundle.away_name)
        rep = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15).run_shadow_on_bundle(
            bundle,
            target_status="NS",
            odds_map=odds,
            bwin_txt_path=odds_path,
        )
        self.assertEqual(rep.decision, "NO_BET")
        self.assertEqual(rep.decision_breakdown_counts["PICK_ACCIONABLE"], 0)
        self.assertGreater(rep.decision_breakdown_counts["CANDIDATO_CONDICIONAL"], 0)
        self.assertTrue(
            all(
                not c["actionable_pick"] and c["calibration_status"] != "CALIBRATED_OUT_OF_SAMPLE"
                for c in rep.top_investigable_candidates
            )
        )


if __name__ == "__main__":
    unittest.main()
