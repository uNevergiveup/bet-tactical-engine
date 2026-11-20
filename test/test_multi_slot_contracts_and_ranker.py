import unittest
from datetime import date, datetime, timezone

from bet_engine.contracts.multi_slot import (
    MADRID_TZ,
    MultiMatchExecutionState,
    MultiSlotContractError,
    SlotCandidateRecord,
    WindowOrchestrationPlan,
    build_window_slots,
)
from bet_engine.orchestration.multi_ranker import (
    apply_dynamic_ranking_update,
    populate_and_rank_plan,
    select_best_eligible_for_slot,
)


class TestMultiSlotContractsAndRanker(unittest.TestCase):
    def test_midnight_and_dst_slot_boundaries(self):
        # Caso referencia: 2026-11-17 15:00 -> 2026-11-18 03:00 CET (+01:00)
        w_start, w_end, slots = build_window_slots(date(2026, 11, 17), "15:00", "03:00")
        plan = WindowOrchestrationPlan(
            window_label="2026-11-17_1500_0300",
            window_day="2026-11-17",
            window_start_utc=w_start.isoformat(),
            window_end_utc=w_end.isoformat(),
            window_start_madrid=w_start.astimezone(MADRID_TZ).isoformat(),
            window_end_madrid=w_end.astimezone(MADRID_TZ).isoformat(),
            slots=slots,
        )
        # Fronteras exactas semiabiertas [inicio, fin)
        self.assertEqual(plan.assign_slot_id("2026-11-17T15:00:00+01:00"), "FRANJA_A")
        self.assertEqual(plan.assign_slot_id("2026-11-17T17:59:59+01:00"), "FRANJA_A")
        self.assertEqual(plan.assign_slot_id("2026-11-17T18:00:00+01:00"), "FRANJA_B")
        self.assertEqual(plan.assign_slot_id("2026-11-17T20:59:59+01:00"), "FRANJA_B")
        self.assertEqual(plan.assign_slot_id("2026-11-17T21:00:00+01:00"), "FRANJA_C")
        self.assertEqual(plan.assign_slot_id("2026-11-18T02:59:59+01:00"), "FRANJA_C")
        self.assertIsNone(plan.assign_slot_id("2026-11-18T03:00:00+01:00"))
        self.assertIsNone(plan.assign_slot_id("2026-11-17T14:59:59+01:00"))

        # Cambio horario verano/invierno europeo: 2026-10-25 (03:00 vuelve a 02:00) -> duración real 13h
        dst_start, dst_end, dst_slots = build_window_slots(date(2026, 10, 24), "15:00", "03:00")
        self.assertEqual((dst_end - dst_start).total_seconds(), 13 * 3600)
        self.assertEqual(len(dst_slots), 3)

    def test_state_machine_and_duplicate_invariants(self):
        w_start, w_end, slots = build_window_slots(date(2026, 11, 17))
        plan = WindowOrchestrationPlan(
            window_label="2026-11-17_1500_0300",
            window_day="2026-11-17",
            window_start_utc=w_start.isoformat(),
            window_end_utc=w_end.isoformat(),
            window_start_madrid=w_start.astimezone(MADRID_TZ).isoformat(),
            window_end_madrid=w_end.astimezone(MADRID_TZ).isoformat(),
            slots=slots,
            candidates=[
                SlotCandidateRecord(
                    slot_id="FRANJA_A",
                    fixture_id=101,
                    event_id=9001,
                    kickoff_utc="2026-11-17T15:30:00+00:00",
                    kickoff_madrid="2026-11-17T16:30:00+01:00",
                    league_name="Liga Test",
                    execution_state=MultiMatchExecutionState.RANKED,
                ),
                SlotCandidateRecord(
                    slot_id="FRANJA_A",
                    fixture_id=101,
                    event_id=9002,
                    kickoff_utc="2026-11-17T15:45:00+00:00",
                    kickoff_madrid="2026-11-17T16:45:00+01:00",
                    league_name="Liga Test",
                    execution_state=MultiMatchExecutionState.RANKED,
                ),
            ],
        )
        with self.assertRaises(MultiSlotContractError):
            plan.validate_invariants()

        # Transición inválida desde estado cerrado
        now = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
        rec = plan.candidates[0]
        rec.transition_to(MultiMatchExecutionState.SELECTED, now, "ok")
        rec.transition_to(MultiMatchExecutionState.PREPARING, now, "ok")
        rec.transition_to(MultiMatchExecutionState.READY_FOR_FINALIZATION, now, "ok")
        rec.transition_to(MultiMatchExecutionState.FINALIZED, now, "closed")
        with self.assertRaises(MultiSlotContractError):
            rec.transition_to(MultiMatchExecutionState.RANKED, now, "illegal")

    def test_global_slot_ranking_and_dynamic_rerank_preserves_closed_dossiers(self):
        w_start, w_end, slots = build_window_slots(date(2026, 11, 17))
        plan = WindowOrchestrationPlan(
            window_label="2026-11-17_1500_0300",
            window_day="2026-11-17",
            window_start_utc=w_start.isoformat(),
            window_end_utc=w_end.isoformat(),
            window_start_madrid=w_start.astimezone(MADRID_TZ).isoformat(),
            window_end_madrid=w_end.astimezone(MADRID_TZ).isoformat(),
            slots=slots,
        )
        matches = [
            {
                "fixture_id": 1001,
                "home": "Alpha",
                "away": "Beta",
                "league_name": "LaLiga",
                "kickoff_utc": "2026-11-17T15:30:00+00:00",  # 16:30 CET -> FRANJA_A
                "data_readiness_score": 80,
                "research_priority_score": 72.0,
                "bwin_event_id": 5001,
            },
            {
                "fixture_id": 1002,
                "home": "Gamma",
                "away": "Delta",
                "league_name": "Serie A",
                "kickoff_utc": "2026-11-17T16:00:00+00:00",  # 17:00 CET -> FRANJA_A
                "data_readiness_score": 85,
                "research_priority_score": 68.0,
                "bwin_event_id": 5002,
            },
            {
                "fixture_id": 1003,
                "home": "Epsilon",
                "away": "Zeta",
                "league_name": "Premier",
                "kickoff_utc": "2026-11-17T18:30:00+00:00",  # 19:30 CET -> FRANJA_B
                "data_readiness_score": 90,
                "research_priority_score": 81.0,
                "bwin_event_id": 5003,
            },
        ]
        t0 = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
        populate_and_rank_plan(plan, matches, now_utc=t0)

        best_a = select_best_eligible_for_slot(plan, "FRANJA_A")
        self.assertEqual(best_a.fixture_id, 1001)
        self.assertEqual(plan.get_candidate(1003).global_rank, 1)

        # Cerrar 1003 como FINALIZED y reordenar dinámicamente FRANJA_A porque 1001 pierde Bwin
        c1003 = plan.get_candidate(1003)
        c1003.transition_to(MultiMatchExecutionState.SELECTED, t0, "picked")
        c1003.transition_to(MultiMatchExecutionState.PREPARING, t0, "prep")
        c1003.transition_to(MultiMatchExecutionState.READY_FOR_FINALIZATION, t0, "ready")
        c1003.transition_to(MultiMatchExecutionState.FINALIZED, t0, "closed")

        t1 = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)
        apply_dynamic_ranking_update(
            plan,
            now_utc=t1,
            reason="BWIN_MARKET_LOST_FOR_1001",
            bwin_links={1001: {"status": "BLOCKED_NO_MARKET", "event_id": None}},
        )
        self.assertEqual(plan.get_candidate(1003).execution_state, MultiMatchExecutionState.FINALIZED)
        self.assertEqual(plan.get_candidate(1001).execution_state, MultiMatchExecutionState.SKIPPED_INELIGIBLE)
        self.assertEqual(select_best_eligible_for_slot(plan, "FRANJA_A").fixture_id, 1002)
        self.assertTrue(len(plan.ranking_history) >= 1)


if __name__ == "__main__":
    unittest.main()
