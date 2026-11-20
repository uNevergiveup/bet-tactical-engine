import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from bet_engine.context.lineup_manager import MANUAL_LINEUP_SCHEMA
from bet_engine.contracts.multi_slot import MultiMatchExecutionState, SlotCandidateRecord
from bet_engine.orchestration.multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorAdapters,
    OrchestratorConfig,
)


def _xi_players(team_prefix: str, start_num: int = 1) -> list[dict]:
    positions = ["G", "D", "D", "D", "D", "M", "M", "M", "M", "F", "F"]
    return [
        {
            "player_id": start_num * 100 + idx,
            "name": f"{team_prefix} Player {idx}",
            "number": idx,
            "position": positions[idx - 1],
        }
        for idx in range(1, 12)
    ]


def _manual_xi_doc(
    *,
    event_id: int,
    fixture_id: int,
    home_id: int,
    away_id: int,
    home_name: str,
    away_name: str,
    kickoff_utc: str,
    captured_at_utc: str,
) -> dict:
    return {
        "schema_version": MANUAL_LINEUP_SCHEMA,
        "source": "MANUAL_CONFIRMED_SCREENSHOT",
        "source_kind": "MANUAL_VERIFIED",
        "confirmed": True,
        "event_id": str(event_id),
        "fixture_id": fixture_id,
        "kickoff_utc": kickoff_utc,
        "captured_at_utc": captured_at_utc,
        "teams": {
            "home": {
                "team_id": home_id,
                "team_name": home_name,
                "starters": _xi_players(f"H{fixture_id}", 1),
                "substitutes": [],
            },
            "away": {
                "team_id": away_id,
                "team_name": away_name,
                "starters": _xi_players(f"A{fixture_id}", 2),
                "substitutes": [],
            },
        },
    }


def _api_xi_response(home_id: int, away_id: int, home_name: str, away_name: str, fixture_id: int) -> list[dict]:
    return [
        {
            "team": {"id": home_id, "name": home_name},
            "startXI": [{"player": {"id": 1000 + i, "name": p["name"], "number": p["number"], "pos": p["position"]}} for i, p in enumerate(_xi_players(f"H{fixture_id}", 1))],
            "substitutes": [],
        },
        {
            "team": {"id": away_id, "name": away_name},
            "startXI": [{"player": {"id": 2000 + i, "name": p["name"], "number": p["number"], "pos": p["position"]}} for i, p in enumerate(_xi_players(f"A{fixture_id}", 2))],
            "substitutes": [],
        },
    ]


def _base_match(
    *,
    fixture_id: int,
    event_id: int | None,
    home: str,
    away: str,
    kickoff_utc: str,
    quality: float = 75.0,
    readiness: float = 85.0,
    league_name: str = "Liga Demo",
    odds_captured: bool = True,
) -> dict:
    return {
        "fixture_id": fixture_id,
        "bwin_event_id": event_id,
        "home": home,
        "away": away,
        "home_id": fixture_id * 10 + 1,
        "away_id": fixture_id * 10 + 2,
        "league_id": 140,
        "league_name": league_name,
        "season": 2026,
        "kickoff_utc": kickoff_utc,
        "data_readiness_score": readiness,
        "research_priority_score": quality,
        "details_checked": True,
        "odds_captured": odds_captured,
        "odds_snapshot_text": f"MATCH {home} vs {away}\n1 1.95\nX 3.40\n2 4.10\n",
    }


def _write_preselection(root: Path, day_label: str, matches: list[dict], subdir: str = "run_001") -> Path:
    pre_path = root / "outputs" / "discovery" / day_label / "evaluations" / subdir / "prospective_trial" / "enriched" / "preselection.json"
    pre_path.parent.mkdir(parents=True, exist_ok=True)
    pre_path.write_text(
        json.dumps({"generated_at_utc": "2026-11-17T12:00:00+00:00", "matches": matches}, indent=2),
        encoding="utf-8",
    )
    return pre_path


def _shadow_stub(decisions_by_fixture: dict[int, tuple[str, dict | None]]):
    called: list[tuple[int, int | None, str]] = []

    def _fn(root: Path, cand: SlotCandidateRecord, now_utc: datetime, cutoff: int, absent: bool) -> dict:
        called.append((cand.fixture_id, cand.event_id, cand.lineup_state))
        tax, sel = decisions_by_fixture.get(cand.fixture_id, ("NO_BET_POR_PRECIO", None))
        ev_dir = root / "outputs" / "matches" / str(cand.event_id) / "analysis"
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "shadow_isolation_marker.json").write_text(
            json.dumps({"fixture_id": cand.fixture_id, "event_id": cand.event_id, "taxonomy": tax}),
            encoding="utf-8",
        )
        return {"status": "OK", "decision_taxonomy": tax, "candidate": sel, "blocking_reasons": []}

    return _fn, called


class TestMultiSlotOrchestratorE2EV3(unittest.TestCase):
    DAY = date(2026, 11, 17)
    LABEL = "2026-11-17_1500_0300"

    def test_scenario_a_three_valid_slots_and_k_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                {
                    **_base_match(fixture_id=101, event_id=9101, home="HomeA1", away="AwayA1", kickoff_utc="2026-11-17T15:30:00+00:00", quality=82.0),
                    "api_lineups_response": _api_xi_response(1011, 1012, "HomeA1", "AwayA1", 101),
                },
                {
                    **_base_match(fixture_id=102, event_id=9102, home="HomeA2", away="AwayA2", kickoff_utc="2026-11-17T16:00:00+00:00", quality=64.0),
                },
                {
                    **_base_match(fixture_id=201, event_id=9201, home="HomeB1", away="AwayB1", kickoff_utc="2026-11-17T18:30:00+00:00", quality=88.0),
                    "api_lineups_response": _api_xi_response(2011, 2012, "HomeB1", "AwayB1", 201),
                },
                {
                    **_base_match(fixture_id=301, event_id=9301, home="HomeC1", away="AwayC1", kickoff_utc="2026-11-17T22:00:00+00:00", quality=79.0),
                    "api_lineups_response": _api_xi_response(3011, 3012, "HomeC1", "AwayC1", 301),
                },
            ]
            _write_preselection(root, self.LABEL, matches)
            sh_fn, sh_calls = _shadow_stub(
                {
                    101: ("PICK_ACCIONABLE", {"market": "TOTAL_GOALS", "selection": "Over 2.5", "odds": 2.05}),
                    201: ("NO_BET_POR_PRECIO", None),
                    301: ("PICK_ACCIONABLE", {"market": "DOUBLE_CHANCE", "selection": "1X", "odds": 1.65}),
                }
            )
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, offline=True, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(shadow_evaluator=sh_fn),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))

            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(plan.get_candidate(102).execution_state, MultiMatchExecutionState.RANKED)
            self.assertEqual(plan.get_candidate(201).execution_state, MultiMatchExecutionState.NO_BET)
            self.assertEqual(plan.get_candidate(201).shadow_decision, "NO_BET_POR_PRECIO")
            self.assertEqual(plan.get_candidate(301).execution_state, MultiMatchExecutionState.FINALIZED)
            # Escenario K: cada partido escribe únicamente en su propia carpeta aislada
            for fid, eid in ((101, 9101), (201, 9201), (301, 9301)):
                marker = json.loads((root / "outputs" / "matches" / str(eid) / "analysis" / "shadow_isolation_marker.json").read_text())
                self.assertEqual(marker["fixture_id"], fid)
                self.assertEqual(marker["event_id"], eid)
            self.assertEqual(len(sh_calls), 3)

    def test_scenario_b_two_eligible_slots_leave_third_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                {
                    **_base_match(fixture_id=101, event_id=9101, home="H1", away="A1", kickoff_utc="2026-11-17T15:30:00+00:00", quality=80.0),
                    "api_lineups_response": _api_xi_response(1011, 1012, "H1", "A1", 101),
                },
                {
                    **_base_match(fixture_id=201, event_id=9201, home="H2", away="A2", kickoff_utc="2026-11-17T18:30:00+00:00", quality=76.0),
                    "api_lineups_response": _api_xi_response(2011, 2012, "H2", "A2", 201),
                },
            ]
            _write_preselection(root, self.LABEL, matches)
            sh_fn, _ = _shadow_stub({101: ("PICK_ACCIONABLE", {"odds": 1.9}), 201: ("PICK_ACCIONABLE", {"odds": 2.0})})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(shadow_evaluator=sh_fn),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))
            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(plan.get_candidate(201).execution_state, MultiMatchExecutionState.FINALIZED)
            summary = orch.render_summary(plan)
            slot_c = next(s for s in summary["slots"] if s["slot_id"] == "FRANJA_C")
            self.assertEqual(slot_c["execution_state"], "EMPTY")
            self.assertIsNone(slot_c["active_or_closed_fixture_id"])

    def test_scenario_c_missing_lineups_at_cutoff_triggers_same_slot_fallback_or_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # C1: En FRANJA_A hay un 2º candidato elegible con tiempo suficiente (>=45m)
            # En FRANJA_B el 2º candidato NO tiene antelación suficiente (<45m) -> franja termina vacía
            matches = [
                _base_match(fixture_id=101, event_id=9101, home="A1", away="B1", kickoff_utc="2026-11-17T15:10:00+00:00", quality=85.0),
                {
                    **_base_match(fixture_id=102, event_id=9102, home="A2", away="B2", kickoff_utc="2026-11-17T16:40:00+00:00", quality=75.0),
                    "api_lineups_response": _api_xi_response(1021, 1022, "A2", "B2", 102),
                },
                _base_match(fixture_id=201, event_id=9201, home="B_one", away="B_two", kickoff_utc="2026-11-17T18:10:00+00:00", quality=82.0),
                _base_match(fixture_id=202, event_id=9202, home="B_three", away="B_four", kickoff_utc="2026-11-17T18:30:00+00:00", quality=70.0),
            ]
            _write_preselection(root, self.LABEL, matches)
            sh_fn, _ = _shadow_stub({102: ("PICK_ACCIONABLE", {"odds": 2.1})})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, min_lead_minutes=45, lineup_cutoff_minutes=15, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(shadow_evaluator=sh_fn),
            )
            # t0 = 14:00 UTC -> todos tienen >= 70m, ambos líderes (101 y 201) se preparan y esperan XI
            orch.run_cycle(now_utc=datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc))

            # t1 = 15:00 UTC:
            # - 101 está en T-10m (pasado T-15m) sin XI -> REJECTED_MISSING_LINEUPS -> fallback a 102 (T-100m >=45m!) -> FINALIZED
            # - 201 está en T-190m (>15m) todavía esperando; avanzamos a t2 = 17:58 UTC (T-12m para 201, T-32m para 202 <45m!)
            orch.run_cycle(now_utc=datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc))
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 17, 58, tzinfo=timezone.utc))

            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.REJECTED_MISSING_LINEUPS)
            self.assertEqual(plan.get_candidate(102).execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(plan.get_candidate(102).fallback_parent_fixture_id, 101)
            self.assertEqual(plan.get_candidate(201).execution_state, MultiMatchExecutionState.REJECTED_MISSING_LINEUPS)
            self.assertEqual(plan.get_candidate(202).execution_state, MultiMatchExecutionState.SKIPPED_INELIGIBLE)

    def test_scenario_d_manual_lineup_recovery_and_shadow_reevaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ko = "2026-11-17T16:30:00+00:00"
            matches = [_base_match(fixture_id=101, event_id=9101, home="Estonia", away="Bulgaria", kickoff_utc=ko, quality=85.0)]
            _write_preselection(root, self.LABEL, matches)
            sh_fn, sh_calls = _shadow_stub({101: ("PICK_ACCIONABLE", {"market": "1X2", "selection": "1", "odds": 2.40})})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(shadow_evaluator=sh_fn),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc))
            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.WAITING_LINEUPS)
            self.assertEqual(len(sh_calls), 0)

            manual_src = root / "tmp_manual_xi.json"
            manual_src.write_text(
                json.dumps(
                    _manual_xi_doc(
                        event_id=9101,
                        fixture_id=101,
                        home_id=1011,
                        away_id=1012,
                        home_name="Estonia",
                        away_name="Bulgaria",
                        kickoff_utc=ko,
                        captured_at_utc="2026-11-17T15:35:00+00:00",
                    )
                ),
                encoding="utf-8",
            )
            updated = orch.attach_manual_lineup_file(
                event_id=9101,
                manual_json_path=manual_src,
                now_utc=datetime(2026, 11, 17, 15, 36, tzinfo=timezone.utc),
            )
            c101 = updated.get_candidate(101)
            self.assertEqual(c101.lineup_state, "LINEUPS_MANUAL_VERIFIED")
            self.assertEqual(c101.execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(len(sh_calls), 1)
            self.assertEqual(sh_calls[0][2], "LINEUPS_MANUAL_VERIFIED")

    def test_scenario_e_probable_lineups_never_become_pick_accionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                {
                    **_base_match(fixture_id=101, event_id=9101, home="HomeP", away="AwayP", kickoff_utc="2026-11-17T16:30:00+00:00", quality=85.0),
                    "qualitative_probable_counts": {"home": 11, "away": 11},
                }
            ]
            _write_preselection(root, self.LABEL, matches)
            # Incluso si Shadow intentara devolver PICK_ACCIONABLE, el guard P0 lo bloquea
            sh_fn, _ = _shadow_stub({101: ("PICK_ACCIONABLE", {"odds": 2.1})})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(shadow_evaluator=sh_fn),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc))
            c101 = plan.get_candidate(101)
            self.assertEqual(c101.lineup_state, "LINEUPS_PROBABLE_ONLY")
            self.assertNotEqual(c101.execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertNotEqual(c101.shadow_decision, "PICK_ACCIONABLE")

    def test_scenario_f_missing_preselection_auto_generated_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            called_gen: list[str] = []

            def _gen_pre(r: Path, d: date, s: str, e: str) -> Path:
                called_gen.append(f"{d.isoformat()}_{s}_{e}")
                matches = [
                    {
                        **_base_match(fixture_id=101, event_id=9101, home="AutoH", away="AutoA", kickoff_utc="2026-11-17T16:00:00+00:00", quality=78.0),
                        "api_lineups_response": _api_xi_response(1011, 1012, "AutoH", "AutoA", 101),
                    }
                ]
                return _write_preselection(r, self.LABEL, matches, subdir="auto_dep")

            sh_fn, _ = _shadow_stub({101: ("PICK_ACCIONABLE", {"odds": 1.85})})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(preselection_provider=_gen_pre, shadow_evaluator=sh_fn),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))
            self.assertEqual(called_gen, ["2026-11-17_15:00_03:00"])
            self.assertTrue(plan.preselection_auto_generated)
            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.FINALIZED)

    def test_scenario_g_midnight_crossing_cet_half_open_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                _base_match(fixture_id=301, event_id=9301, home="Late1", away="Late2", kickoff_utc="2026-11-17T22:55:00+00:00", quality=75.0),  # 23:55 CET -> FRANJA_C
                _base_match(fixture_id=302, event_id=9302, home="After0", away="After1", kickoff_utc="2026-11-17T23:30:00+00:00", quality=72.0),  # 00:30 CET -> FRANJA_C
                _base_match(fixture_id=303, event_id=9303, home="EdgeIn", away="EdgeIn2", kickoff_utc="2026-11-18T01:59:59+00:00", quality=70.0),  # 02:59:59 CET -> FRANJA_C
                _base_match(fixture_id=304, event_id=9304, home="EdgeOut", away="EdgeOut2", kickoff_utc="2026-11-18T02:00:00+00:00", quality=99.0),  # 03:00:00 CET -> FUERA
            ]
            _write_preselection(root, self.LABEL, matches)
            orch = MultiSlotOrchestrator(root, OrchestratorConfig(window_day=self.DAY))
            plan = orch.init_or_load_plan(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))
            self.assertEqual(plan.get_candidate(301).slot_id, "FRANJA_C")
            self.assertEqual(plan.get_candidate(302).slot_id, "FRANJA_C")
            self.assertEqual(plan.get_candidate(303).slot_id, "FRANJA_C")
            self.assertIsNone(plan.get_candidate(304).slot_id)
            self.assertEqual(plan.get_candidate(304).execution_state, MultiMatchExecutionState.SKIPPED_INELIGIBLE)

    def test_scenario_h_interrupt_in_waiting_research_and_resume_without_duplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                {
                    **_base_match(fixture_id=101, event_id=9101, home="ResH", away="ResA", kickoff_utc="2026-11-17T16:30:00+00:00", quality=85.0),
                    "api_lineups_response": _api_xi_response(1011, 1012, "ResH", "ResA", 101),
                }
            ]
            _write_preselection(root, self.LABEL, matches)
            prep_calls: list[int] = []

            def _counting_prep(r: Path, cand: SlotCandidateRecord, raw: dict, now: datetime) -> dict:
                prep_calls.append(cand.fixture_id)
                from bet_engine.orchestration.multi_orchestrator import default_case_preparer

                return default_case_preparer(r, cand, raw, now)

            sh_fn, sh_calls = _shadow_stub({101: ("PICK_ACCIONABLE", {"odds": 1.95})})
            cfg = OrchestratorConfig(window_day=self.DAY, require_research=True, require_full_case_artifacts=False)
            orch1 = MultiSlotOrchestrator(
                root,
                cfg,
                adapters=OrchestratorAdapters(case_preparer=_counting_prep, shadow_evaluator=sh_fn),
            )
            p1 = orch1.run_cycle(now_utc=datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc))
            self.assertEqual(p1.get_candidate(101).execution_state, MultiMatchExecutionState.WAITING_RESEARCH)
            self.assertEqual(len(prep_calls), 1)
            self.assertEqual(len(sh_calls), 0)

            res_file = root / "qualitative_101.md"
            res_file.write_text("# Informe cualitativo verificado\nSin bajas graves.\n", encoding="utf-8")

            # Nuevo objeto orquestador simulando reinicio de proceso / sesión Codex
            orch2 = MultiSlotOrchestrator(
                root,
                cfg,
                adapters=OrchestratorAdapters(case_preparer=_counting_prep, shadow_evaluator=sh_fn),
            )
            orch2.attach_research_file(event_id=9101, research_path=res_file, now_utc=datetime(2026, 11, 17, 14, 15, tzinfo=timezone.utc))
            p2 = orch2.run_cycle(now_utc=datetime(2026, 11, 17, 14, 20, tzinfo=timezone.utc))
            self.assertEqual(p2.get_candidate(101).execution_state, MultiMatchExecutionState.FINALIZED)
            # No repitió preparación ni duplicó Shadow
            self.assertEqual(len(prep_calls), 1)
            self.assertEqual(len(sh_calls), 1)

    def test_scenario_i_provider_failure_isolation_keeps_other_slots_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                _base_match(fixture_id=101, event_id=9101, home="CrashH", away="CrashA", kickoff_utc="2026-11-17T16:00:00+00:00", quality=90.0),
                {
                    **_base_match(fixture_id=102, event_id=9102, home="SafeA2", away="SafeB2", kickoff_utc="2026-11-17T16:30:00+00:00", quality=72.0),
                    "api_lineups_response": _api_xi_response(1021, 1022, "SafeA2", "SafeB2", 102),
                },
                {
                    **_base_match(fixture_id=201, event_id=9201, home="OkB1", away="OkB2", kickoff_utc="2026-11-17T19:00:00+00:00", quality=81.0),
                    "api_lineups_response": _api_xi_response(2011, 2012, "OkB1", "OkB2", 201),
                },
            ]
            _write_preselection(root, self.LABEL, matches)

            def _flaky_prep(r: Path, cand: SlotCandidateRecord, raw: dict, now: datetime) -> dict:
                if cand.fixture_id == 101:
                    raise ConnectionError("SIMULATED_BWIN_TIMEOUT")
                from bet_engine.orchestration.multi_orchestrator import default_case_preparer

                return default_case_preparer(r, cand, raw, now)

            sh_fn, _ = _shadow_stub({102: ("PICK_ACCIONABLE", {"odds": 2.0}), 201: ("NO_BET_POR_EVIDENCIA", None)})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(case_preparer=_flaky_prep, shadow_evaluator=sh_fn),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))
            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.FAILED_TECHNICAL)
            self.assertEqual(plan.get_candidate(102).execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(plan.get_candidate(201).execution_state, MultiMatchExecutionState.NO_BET)

    def test_scenario_j_thirty_low_quality_matches_produce_zero_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                _base_match(
                    fixture_id=1000 + i,
                    event_id=9000 + i,
                    home=f"LowH{i}",
                    away=f"LowA{i}",
                    kickoff_utc=f"2026-11-17T{15 + (i % 7):02d}:00:00+00:00",
                    quality=5.0 + (i % 10),
                    readiness=25.0,
                )
                for i in range(30)
            ]
            _write_preselection(root, self.LABEL, matches)
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, min_quality_score=20.0, min_readiness_score=40.0),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))
            self.assertEqual(len(plan.candidates), 30)
            self.assertTrue(all(c.execution_state == MultiMatchExecutionState.SKIPPED_INELIGIBLE for c in plan.candidates))
            summary = orch.render_summary(plan)
            self.assertTrue(all(s["execution_state"] == "EMPTY" for s in summary["slots"]))

    def test_scenario_l_idempotency_on_repeated_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                {
                    **_base_match(fixture_id=101, event_id=9101, home="IdemH", away="IdemA", kickoff_utc="2026-11-17T16:00:00+00:00", quality=80.0),
                    "api_lineups_response": _api_xi_response(1011, 1012, "IdemH", "IdemA", 101),
                }
            ]
            _write_preselection(root, self.LABEL, matches)
            sh_fn, sh_calls = _shadow_stub({101: ("PICK_ACCIONABLE", {"odds": 1.9})})
            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_full_case_artifacts=False),
                adapters=OrchestratorAdapters(shadow_evaluator=sh_fn),
            )
            p1 = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc))
            p2 = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 5, tzinfo=timezone.utc))
            p3 = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 10, tzinfo=timezone.utc))
            self.assertEqual(len(sh_calls), 1)
            self.assertEqual(len(p3.candidates), 1)
            self.assertEqual(p1.get_candidate(101).execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(p2.get_candidate(101).attempts, 1)
            self.assertEqual(p3.get_candidate(101).attempts, 1)

    def test_scenario_m_resource_and_fallback_budget_caps_prevent_infinite_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                _base_match(fixture_id=101 + i, event_id=9101 + i, home=f"H{i}", away=f"A{i}", kickoff_utc="2026-11-17T16:00:00+00:00", quality=90.0 - i)
                for i in range(5)
            ]
            _write_preselection(root, self.LABEL, matches)
            api_calls_made = 0

            def _fail_all_prep(r: Path, cand: SlotCandidateRecord, raw: dict, now: datetime) -> dict:
                raise RuntimeError("PERSISTENT_PROVIDER_DOWN")

            def _api_counter(cand: SlotCandidateRecord, now: datetime):
                nonlocal api_calls_made
                api_calls_made += 1
                return []

            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, max_slot_fallbacks=1, max_api_calls=2),
                adapters=OrchestratorAdapters(case_preparer=_fail_all_prep, api_lineup_fetcher=_api_counter),
            )
            plan = orch.run_cycle(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc), max_passes=10)
            self.assertEqual(plan.budget.slot_fallbacks_used.get("FRANJA_A", 0), 1)
            # Solo intenta 101 + 1 fallback (102); 103..105 quedan sin tocar por tope de fallback=1
            self.assertEqual(plan.get_candidate(101).execution_state, MultiMatchExecutionState.FAILED_TECHNICAL)
            self.assertEqual(plan.get_candidate(102).execution_state, MultiMatchExecutionState.FAILED_TECHNICAL)
            self.assertEqual(plan.get_candidate(103).execution_state, MultiMatchExecutionState.RANKED)

    def test_cli_subcommands_and_online_safety_block(self):
        import contextlib
        import io
        from prematch_orchestrator import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches = [
                {
                    **_base_match(fixture_id=101, event_id=9101, home="CliH", away="CliA", kickoff_utc="2026-11-17T16:00:00+00:00", quality=80.0),
                    "api_lineups_response": _api_xi_response(1011, 1012, "CliH", "CliA", 101),
                }
            ]
            pre_path = _write_preselection(root, self.LABEL, matches)
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = cli_main(
                    [
                        "plan",
                        "--root",
                        str(root),
                        "--date",
                        "2026-11-17",
                        "--preselection",
                        str(pre_path),
                        "--now-utc",
                        "2026-11-17T13:00:00+00:00",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(out_buf.getvalue())
            self.assertEqual(payload["eligible_candidates"], 1)

            # --online sin --confirm-external (y sin --dry-run) debe bloquear
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc_bad = cli_main(["run", "--root", str(root), "--date", "2026-11-17", "--online"])
            self.assertEqual(rc_bad, 2)
            self.assertIn("EXTERNAL_CONFIRMATION_REQUIRED", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
