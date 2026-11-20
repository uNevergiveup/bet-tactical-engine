"""Pruebas focalizadas de auditoría y clasificación explícita de decisiones NO BET / BET_AUTORIZADO."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.context import ObservationStore
from bet_engine.context.lineup_manager import evaluate_fixture_lineup_state
from bet_engine.context.observation_store import HistoricalTeamMatch, MatchObservationBundle
from bet_engine.contracts.multi_slot import (
    MultiMatchExecutionState,
    SlotCandidateRecord,
)
from bet_engine.orchestration import ShadowPipelineRunner
from bet_engine.orchestration.multi_orchestrator import MultiSlotOrchestrator

ROOT = Path(__file__).resolve().parent.parent


def _synthetic_bundle(
    *,
    event_id: str = "9999001",
    fixture_id: int = 8888001,
    kickoff_dt: datetime | None = None,
    cutoff_dt: datetime | None = None,
    n_matches: int = 12,
    confirmed_22_xi: bool = True,
) -> MatchObservationBundle:
    """Construye un MatchObservationBundle sintético aislado (nunca confundir con picks reales)."""
    now_ko = kickoff_dt or datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc)
    now_cut = cutoff_dt or (now_ko - timedelta(minutes=60))

    def _hist(team_id: int, opp_base: int, base_fid: int) -> tuple[HistoricalTeamMatch, ...]:
        rows: list[HistoricalTeamMatch] = []
        for idx in range(n_matches):
            dt = now_ko - timedelta(days=7 * (idx + 1))
            rows.append(
                HistoricalTeamMatch(
                    fixture_id=base_fid + idx,
                    team_id=team_id,
                    opponent_id=opp_base + idx,
                    kickoff_utc=dt.isoformat(),
                    available_at_utc=(dt + timedelta(hours=3)).isoformat(),
                    league_id=71,
                    league_name="Serie A",
                    season=2026,
                    competition_stratum="DOMESTIC_LEAGUE",
                    venue="HOME" if idx % 2 == 0 else "AWAY",
                    status="FT",
                    coach_id=100 + team_id,
                    coach_name=f"Coach {team_id}",
                    red_card_distortion=False,
                    own_metrics={
                        "goals": 1.0,
                        "goals_h1": 0.0,
                        "corners": 5.0,
                        "yellow": 2.0,
                        "red": 0.0,
                        "shots": 12.0,
                        "on_target": 4.0,
                        "fouls": 13.0,
                    },
                    conceded_metrics={
                        "goals": 1.0,
                        "goals_h1": 0.0,
                        "corners": 5.0,
                        "yellow": 2.0,
                        "red": 0.0,
                        "shots": 11.0,
                        "on_target": 4.0,
                        "fouls": 12.0,
                    },
                    has_detailed_stats=True,
                    source="SYNTHETIC_TEST",
                )
            )
        return tuple(rows)

    def _xi(team_id: int, team_name: str, offset: int) -> dict:
        return {
            "team": {"id": team_id, "name": team_name},
            "formation": "4-2-3-1",
            "coach": {"id": 100 + team_id, "name": f"Coach {team_id}"},
            "startXI": [
                {"player": {"id": offset * 100 + i, "name": f"Player {offset}_{i}", "number": i + 1, "pos": "M"}}
                for i in range(11)
            ],
            "substitutes": [],
        }

    home_matches = _hist(101, 500, 50000)
    away_matches = _hist(202, 600, 60000)
    windows = {
        "home": ObservationStore._build_all_windows(
            home_matches,
            target_league_id=71,
            target_season=2026,
            target_venue="HOME",
            current_coach_id=201,
        ),
        "away": ObservationStore._build_all_windows(
            away_matches,
            target_league_id=71,
            target_season=2026,
            target_venue="AWAY",
            current_coach_id=302,
        ),
    }
    api_resp = [_xi(101, "Synthetic Home FC", 1), _xi(202, "Synthetic Away FC", 2)] if confirmed_22_xi else []
    lineup_eval = evaluate_fixture_lineup_state(
        api_lineups_response=api_resp,
        fixture_id=fixture_id,
        event_id=event_id,
        home_id=101,
        away_id=202,
        home_name="Synthetic Home FC",
        away_name="Synthetic Away FC",
        kickoff_utc=now_ko,
        now_utc=now_cut,
        api_acquired_at_utc=now_cut,
    )

    return MatchObservationBundle(
        event_id=event_id,
        fixture_id=fixture_id,
        league_id=71,
        season=2026,
        kickoff_utc=now_ko.isoformat(),
        cutoff_utc=now_cut.isoformat(),
        home_id=101,
        away_id=202,
        home_name="Synthetic Home FC",
        away_name="Synthetic Away FC",
        home_matches=home_matches,
        away_matches=away_matches,
        facts=(),
        contextual_evidence=(),
        rejected_records=(),
        windows=windows,
        source_sha256="synthetic_test_sha256",
        target_lineup_evaluation=lineup_eval,
    )


class TestDecisionClassificationAudit(unittest.TestCase):
    def test_1_calibrated_synthetic_candidate_can_authorize_bet(self) -> None:
        """Un candidato calibrado con n>=10, XI confirmado y EV_low>0 alcanza BET_AUTORIZADO."""
        bundle = _synthetic_bundle(n_matches=12, confirmed_22_xi=True)
        runner = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15)

        report = runner.run_shadow_on_bundle(
            bundle,
            odds_map={"draw": 9.0, "home_win": 1.15, "away_win": 1.15},
            calibration_accredited_families={"MATCH_RESULT_1X2"},
        )

        self.assertEqual(report.decision, "CANDIDATE_IDENTIFIED")
        self.assertEqual(report.decision_taxonomy, "PICK_ACCIONABLE")
        self.assertEqual(report.decision_classification, "BET_AUTORIZADO")
        self.assertGreaterEqual(report.decision_breakdown_counts["PICK_ACCIONABLE"], 1)
        self.assertEqual(
            report.formal_decision_breakdown_counts["BET_AUTORIZADO"],
            report.decision_breakdown_counts["PICK_ACCIONABLE"],
        )
        mdl = report.decision_metrics_summary["match_decision_level"]
        self.assertFalse(mdl["is_statistical_no_bet"])
        self.assertFalse(mdl["is_incomplete_expedient"])
        self.assertEqual(mdl["calibration_accredited_families_count"], 1)

    def test_2_positive_ev_without_calibration_classified_by_uncertainty(self) -> None:
        """EV conservador positivo sin calibración acreditada queda en NO_BET_POR_INCERTIDUMBRE, nunca precio ni BET."""
        bundle = _synthetic_bundle(n_matches=12, confirmed_22_xi=True)
        runner = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15)

        report = runner.run_shadow_on_bundle(
            bundle,
            odds_map={"draw": 9.0, "home_win": 1.15, "away_win": 1.15},
            calibration_accredited_families=None,
        )

        self.assertEqual(report.decision, "NO_BET")
        self.assertEqual(report.decision_taxonomy, "CANDIDATO_CONDICIONAL")
        self.assertEqual(report.decision_classification, "NO_BET_POR_INCERTIDUMBRE")
        self.assertEqual(report.decision_breakdown_counts["PICK_ACCIONABLE"], 0)
        self.assertEqual(report.formal_decision_breakdown_counts["BET_AUTORIZADO"], 0)
        self.assertEqual(report.formal_decision_breakdown_counts["NO_BET_POR_PRECIO"], 0)
        self.assertGreater(report.formal_decision_breakdown_counts["NO_BET_POR_INCERTIDUMBRE"], 0)
        self.assertIn("BLOQUEO_ACCIONABLE_CALIBRACION_NO_ACREDITADA", report.no_bet_reasons)
        self.assertTrue(report.decision_metrics_summary["match_decision_level"]["is_statistical_no_bet"])

    def test_3_calibrated_model_without_value_discarded_by_price(self) -> None:
        """Selecciones sin EV bajo modelo acreditado y XI confirmado se clasifican NO_BET_POR_PRECIO."""
        bundle = _synthetic_bundle(n_matches=12, confirmed_22_xi=True)
        runner = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15)

        report = runner.run_shadow_on_bundle(
            bundle,
            odds_map={"home_win": 1.10, "draw": 1.10, "away_win": 1.10},
            calibration_accredited_families={"MATCH_RESULT_1X2"},
        )

        self.assertEqual(report.decision, "NO_BET")
        self.assertEqual(report.decision_taxonomy, "NO_BET_POR_PRECIO")
        self.assertEqual(report.decision_classification, "NO_BET_POR_PRECIO")
        self.assertEqual(report.formal_decision_breakdown_counts["NO_BET_POR_PRECIO"], 3)
        self.assertEqual(report.formal_decision_breakdown_counts["BET_AUTORIZADO"], 0)
        self.assertTrue(report.decision_metrics_summary["match_decision_level"]["is_statistical_no_bet"])

    def test_4_operational_failure_not_counted_as_statistical_no_bet_and_unmodelled_detected(self) -> None:
        """Fallos operativos (sin XI, sin cuotas, post-kickoff) -> BLOQUEO_OPERATIVO, excluidos de NO BET estadístico."""
        bundle_no_xi = _synthetic_bundle(n_matches=12, confirmed_22_xi=False)
        runner = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15)

        rep_no_xi = runner.run_shadow_on_bundle(
            bundle_no_xi,
            odds_map={"draw": 9.0},
            calibration_accredited_families={"MATCH_RESULT_1X2"},
        )
        self.assertEqual(rep_no_xi.decision_classification, "BLOQUEO_OPERATIVO")
        self.assertFalse(rep_no_xi.decision_metrics_summary["match_decision_level"]["is_statistical_no_bet"])
        self.assertTrue(rep_no_xi.decision_metrics_summary["match_decision_level"]["is_incomplete_expedient"])

        # Solo selecciones no modeladas con XI y cuotas -> MODELO_NO_DISPONIBLE
        bundle_ok = _synthetic_bundle(n_matches=12, confirmed_22_xi=True)
        rep_unmodelled = runner.run_shadow_on_bundle(
            bundle_ok,
            odds_map={"first_goal_none": 8.0},
        )
        self.assertEqual(rep_unmodelled.decision_classification, "MODELO_NO_DISPONIBLE")
        self.assertFalse(rep_unmodelled.decision_metrics_summary["match_decision_level"]["is_statistical_no_bet"])

        # Agregación multi-partido: un bloqueo operativo NO cuenta como NO BET estadístico
        c_stat = SlotCandidateRecord(
            slot_id="S1",
            fixture_id=1,
            event_id="1",
            home_team="A",
            away_team="B",
            league_name="Serie A",
            kickoff_utc="2026-12-01T20:00:00+00:00",
            kickoff_madrid="2026-12-01T21:00:00+01:00",
            execution_state=MultiMatchExecutionState.NO_BET,
            shadow_decision="NO_BET_POR_PRECIO",
            audit_outcome="NO_BET_AUDITED",
        )
        c_op = SlotCandidateRecord(
            slot_id="S1",
            fixture_id=2,
            event_id="2",
            home_team="C",
            away_team="D",
            league_name="Serie A",
            kickoff_utc="2026-12-01T20:00:00+00:00",
            kickoff_madrid="2026-12-01T21:00:00+01:00",
            execution_state=MultiMatchExecutionState.NO_BET,
            shadow_decision="BLOQUEO_TECNICO",
            audit_outcome="BLOCKED_OPERATIONAL",
        )
        c_inc = SlotCandidateRecord(
            slot_id="S1",
            fixture_id=3,
            event_id="3",
            home_team="E",
            away_team="F",
            league_name="Serie A",
            kickoff_utc="2026-12-01T20:00:00+00:00",
            kickoff_madrid="2026-12-01T21:00:00+01:00",
            execution_state=MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
            audit_outcome="REJECTED_NO_CONFIRMED_XI",
        )
        summary = MultiSlotOrchestrator._render_decision_summary([c_stat, c_op, c_inc])
        self.assertEqual(summary["total_candidates"], 3)
        self.assertEqual(summary["statistical_evaluated_matches"], 1)
        self.assertEqual(summary["no_bet_estadistico_total"], 1)
        self.assertEqual(summary["no_bet_por_precio"], 1)
        self.assertEqual(summary["incomplete_or_blocked_excluded_from_no_bet"], 2)

    def test_5_historical_7901472_intact_and_prematch_guard_enforced(self) -> None:
        """Artefactos cerrados 7901472 permanecen intactos y el bloqueo PREMATCH post-kickoff sigue vigente."""
        p_cmp = ROOT / "outputs/matches/7901472/shadow/shadow_comparison_7901472.json"
        if p_cmp.is_file():
            cmp_doc = json.loads(p_cmp.read_text(encoding="utf-8"))
            self.assertEqual(cmp_doc["decision"], "NO_BET")
            self.assertEqual(cmp_doc["decision_taxonomy"], "CANDIDATO_CONDICIONAL")
            self.assertEqual(cmp_doc["decision_breakdown_counts"]["PICK_ACCIONABLE"], 0)
            self.assertEqual(cmp_doc["decision_breakdown_counts"]["CANDIDATO_CONDICIONAL"], 44)
            self.assertEqual(cmp_doc["decision_breakdown_counts"]["NO_BET_POR_PRECIO"], 138)

        # Política PREMATCH: live o lead_mins < min_lead_minutes sigue bloqueado incluso con calibración acreditada
        ko = datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc)
        bundle_late = _synthetic_bundle(
            kickoff_dt=ko,
            cutoff_dt=ko - timedelta(minutes=10),
            confirmed_22_xi=True,
        )
        runner = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15)
        rep_late = runner.run_shadow_on_bundle(
            bundle_late,
            odds_map={"draw": 9.0},
            calibration_accredited_families={"MATCH_RESULT_1X2"},
        )
        self.assertEqual(rep_late.status, "BLOCKED_PREMATCH_GUARD")
        self.assertTrue(rep_late.live_data_blocked)
        self.assertEqual(rep_late.decision_classification, "BLOQUEO_OPERATIVO")


if __name__ == "__main__":
    unittest.main()
