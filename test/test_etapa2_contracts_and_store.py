"""Pruebas offline de la Etapa 2: Contratos epistémicos, planificación T-24h/proximidad,
caché unificada, cobertura TRUE/FALSE/UNKNOWN y ObservationStore sobre snapshots reales.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bet_engine.acquisition import (
    CacheMissOfflineError,
    CoveragePolicyEvaluator,
    UnifiedApiClient,
    to_coverage_flag,
)
from bet_engine.context import ObservationStore
from bet_engine.contracts import (
    AcceptedModelFeature,
    ContextualEvidence,
    CoverageFlag,
    EpistemicContractError,
    MatchScheduleCandidate,
    ObservedFact,
    PatternScope,
    PreAnalysisT24Snapshot,
    PrematchAvailability,
    ProximityPlanState,
    StatisticalPattern,
    TacticalHypothesis,
    TemporalLeakageError,
    ValidatedPattern,
    VerificationStatus,
    assert_prematch_cutoff,
    select_next_proximity_candidate,
    validate_epistemic_promotion,
)


ROOT = Path(__file__).resolve().parent.parent


class TestEpistemicContracts(unittest.TestCase):
    def test_null_not_zero_enforced(self) -> None:
        with self.assertRaises(EpistemicContractError):
            ObservedFact(
                fact_id="f1",
                entity_type="TEAM",
                entity_id=1,
                fixture_id=10,
                kickoff_utc="2026-11-10T18:00:00Z",
                available_at_utc="2026-11-10T20:05:00Z",
                competition_id=140,
                season=2026,
                competition_stratum="DOMESTIC_LEAGUE",
                period="FT",
                metric_key="own.xg",
                value=0.0,
                is_covered=False,
            )

        ok = ObservedFact(
            fact_id="f2",
            entity_type="TEAM",
            entity_id=1,
            fixture_id=10,
            kickoff_utc="2026-11-10T18:00:00Z",
            available_at_utc="2026-11-10T20:05:00Z",
            competition_id=140,
            season=2026,
            competition_stratum="DOMESTIC_LEAGUE",
            period="FT",
            metric_key="own.xg",
            value=None,
            is_covered=False,
        )
        self.assertIsNone(ok.value)

    def test_temporal_cutoff_blocks_leakage(self) -> None:
        with self.assertRaises(TemporalLeakageError):
            assert_prematch_cutoff(
                "2026-11-10T20:05:00Z",
                "2026-11-10T18:00:00Z",
                label="future_fact",
            )

    def test_dsl_blocks_arbitrary_python_code(self) -> None:
        with self.assertRaises(EpistemicContractError):
            TacticalHypothesis(
                hypothesis_id="h1",
                origin="LLM_INVESTIGATOR_CODEX",
                prematch_availability=PrematchAvailability.PREMATCH_OBSERVABLE,
                causal_chain={"local_plan": "test"},
                target_market_families=("CORNERS",),
                python_test={"code": "import os; os.system('rm -rf /')"},
            )

    def test_promotion_barriers(self) -> None:
        with self.assertRaises(EpistemicContractError):
            ValidatedPattern(
                validated_id="v1",
                source_pattern_id="p1",
                source_hypothesis_id=None,
                prematch_availability=PrematchAvailability.PREMATCH_OBSERVABLE,
                validation_protocol="WALK_FORWARD_OUT_OF_SAMPLE",
                train_metrics={"n": 10},
                holdout_metrics={"n": 6},
                regime_check={"coach_consistent": True},
                authorized_for_candidate_ranking=True,
                authorized_for_probabilistic_adjustment=True,
            )

        vp_postmatch = ValidatedPattern(
            validated_id="v2",
            source_pattern_id="p2",
            source_hypothesis_id=None,
            prematch_availability=PrematchAvailability.POSTMATCH_ONLY,
            validation_protocol="TEMPORAL_SPLIT_BY_FDR",
            train_metrics={"n": 10},
            holdout_metrics={"n": 6},
            regime_check={"coach_consistent": True},
            authorized_for_candidate_ranking=False,
        )
        with self.assertRaises(EpistemicContractError):
            validate_epistemic_promotion(vp_postmatch, "AcceptedModelFeature")


class TestPlanningContracts(unittest.TestCase):
    def test_t24_cannot_emit_final_picks(self) -> None:
        with self.assertRaises(EpistemicContractError):
            PreAnalysisT24Snapshot(
                snapshot_id="t24_1",
                target_date_local="2026-11-16",
                generated_at_utc="2026-11-15T18:00:00Z",
                candidates=(),
                final_picks_generated=True,
            )

    def test_proximity_scheduler_respects_1300_quality_and_reservation(self) -> None:
        c_low_qual = MatchScheduleCandidate(
            event_id="ev_low",
            fixture_id=101,
            league_id=140,
            season=2026,
            home_name="A",
            away_name="B",
            kickoff_utc="2026-11-16T14:00:00Z",
            coverage_tier="C_LOW",
            analytical_quality_score=40.0,
            min_quality_passed=False,
        )
        c_early_ok = MatchScheduleCandidate(
            event_id="ev_early",
            fixture_id=102,
            league_id=140,
            season=2026,
            home_name="C",
            away_name="D",
            kickoff_utc="2026-11-16T15:15:00Z",
            coverage_tier="B_PARTIAL",
            analytical_quality_score=65.0,
            min_quality_passed=True,
            estimated_duration_minutes=40,
        )
        c_later_superior = MatchScheduleCandidate(
            event_id="ev_sup",
            fixture_id=103,
            league_id=39,
            season=2026,
            home_name="E",
            away_name="F",
            kickoff_utc="2026-11-16T15:25:00Z",
            coverage_tier="A_FULL",
            analytical_quality_score=90.0,
            min_quality_passed=True,
            min_lead_minutes=45,
            safety_margin_minutes=10,
        )
        plan = ProximityPlanState(
            plan_id="plan_1",
            target_date_local="2026-11-16",
            available_from_local="13:00",
            timezone_name="Europe/Madrid",
            min_quality_score=55.0,
            superior_quality_delta=15.0,
            candidates=(c_low_qual, c_early_ok, c_later_superior),
        )

        # Antes de las 13:00 Europe/Madrid (12:00Z en invierno CET)
        early_check = select_next_proximity_candidate(plan, "2026-11-16T11:30:00Z")
        self.assertEqual(early_check["status"], "WAITING_AVAILABLE_TIME")

        # A las 14:15Z (15:15 CET): ejecutar ev_early (40m -> 14:55Z) invadiría latest_start de ev_sup (14:30Z)
        # => debe reservar capacidad para ev_sup y rechazar ev_low por calidad
        res = select_next_proximity_candidate(plan, "2026-11-16T14:15:00Z")
        self.assertEqual(res["status"], "RESERVED_FOR_SUPERIOR_MATCH")
        self.assertEqual(res["selected_event_id"], "ev_sup")
        self.assertEqual(res["deferred_event_id"], "ev_early")
        self.assertEqual(res["rejected"], [{"event_id": "ev_low", "reason": "REJECTED_QUALITY"}])


class TestCoverageAndOfflineCache(unittest.TestCase):
    def test_coverage_true_false_unknown(self) -> None:
        self.assertEqual(to_coverage_flag(True), CoverageFlag.TRUE)
        self.assertEqual(to_coverage_flag(False), CoverageFlag.FALSE)
        self.assertEqual(to_coverage_flag(None), CoverageFlag.UNKNOWN)

        ev = CoveragePolicyEvaluator()
        audit = ev.audit_observations(
            league_id=140,
            season=2026,
            league_coverage={"fixtures": {"statistics_fixtures": True, "events": None}, "players": False},
            total_matches=20,
            detailed_matches_requested=8,
            metric_valid_counts={"corners": 8, "xg": 5},
        )
        self.assertEqual(audit.declared_statistics_fixtures, CoverageFlag.TRUE)
        self.assertEqual(audit.declared_events, CoverageFlag.UNKNOWN)
        self.assertEqual(audit.declared_players, CoverageFlag.FALSE)
        self.assertEqual(audit.not_requested_count, 12)
        self.assertEqual(audit.observed_by_metric["xg"]["missing_within_requested"], 3)

    def test_unified_client_offline_blocks_uncached(self) -> None:
        client = UnifiedApiClient(ROOT, offline_only=True, max_new_calls=0)
        with self.assertRaises(CacheMissOfflineError):
            client.get("fixtures", id=999999999)
        self.assertEqual(client.telemetry.cache_misses, 1)
        self.assertEqual(client.telemetry.api_calls_effective, 0)

    def test_unified_client_reuses_real_phase1_cache_offline(self) -> None:
        cache_file = ROOT / "outputs" / ".api_football_history_cache" / "fixtures" / "0046fc550d807b3c27cc03af.json"
        if not cache_file.is_file():
            self.skipTest("Caché real de fixtures no presente")
        client = UnifiedApiClient(ROOT, offline_only=True, cutoff_utc="2026-11-12T03:00:00Z")
        res = client.get("fixtures", last="5", team="4310")
        self.assertEqual(res["source"], "cache")
        self.assertIsInstance(res["response"], list)
        self.assertEqual(client.telemetry.cache_hits, 1)
        self.assertEqual(client.telemetry.api_calls_effective, 0)


class TestObservationStoreRealSnapshot(unittest.TestCase):
    def test_real_match_engine_7903872_snapshot_and_comparison(self) -> None:
        snap_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.json"
        if not snap_path.is_file():
            self.skipTest("Snapshot match_engine_7903872.json no presente")

        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)

        self.assertEqual(bundle.event_id, "7903872")
        self.assertEqual(len(bundle.home_matches), 6)
        self.assertEqual(len(bundle.away_matches), 5)
        self.assertGreater(len(bundle.facts), 100)

        # Comparación exacta frente al parser legacy P-1..P-6 de motor_probabilistico.py
        txt_path = ROOT / "outputs" / "matches" / "7903872" / "inputs" / "match_engine_7903872.txt"
        if txt_path.is_file():
            from motor_probabilistico import parse_context
            parsed_txt = parse_context(txt_path)
            legacy_home = parsed_txt["histories"][bundle.home_name.lower()]
            legacy_away = parsed_txt["histories"][bundle.away_name.lower()]
            self.assertEqual(len(bundle.home_matches), len(legacy_home))
            self.assertEqual(len(bundle.away_matches), len(legacy_away))
            for metric_new, metric_legacy in (
                ("corners", "corner_kicks"),
                ("shots", "total_shots"),
                ("fouls", "fouls"),
                ("yellow", "yellow_cards"),
            ):
                leg_vals = [m["metrics"][metric_legacy] for m in legacy_home if m["metrics"].get(metric_legacy) is not None]
                new_vals = [m.own_metrics[metric_new] for m in bundle.home_matches if m.own_metrics.get(metric_new) is not None]
                self.assertEqual(len(new_vals), len(leg_vals))
                self.assertAlmostEqual(sum(new_vals) / len(new_vals), sum(leg_vals) / len(leg_vals), places=4)

        # Verifica que referee_profile, schedule_fatigue y coach_regime se extraen como ContextualEvidence
        cats = {c.category for c in bundle.contextual_evidence}
        self.assertIn("SCHEDULE_FATIGUE", cats)
        self.assertIn("COACH_REGIME", cats)
        self.assertIn("REFEREE_PROFILE", cats)

        # Verifica ventanas múltiples y separación disponibles vs válidos
        l5_home = bundle.windows["home"]["L5_all_competitions"]
        self.assertEqual(l5_home.total_matches_available, 5)
        self.assertIn("own.corners", l5_home.metrics)
        self.assertIsNotNone(l5_home.metrics["own.corners"]["mean"])

        # Comparación con cálculo heredado de motor_probabilistico.py (si existe salida previa)
        legacy_motor_path = ROOT / "outputs" / "matches" / "7903872" / "motor_probabilistico_7903872.json"
        if legacy_motor_path.is_file():
            legacy = json.loads(legacy_motor_path.read_text(encoding="utf-8"))
            self.assertEqual(str(legacy.get("event_id") or bundle.event_id), "7903872")

    def test_real_20_match_preselection_snapshot_and_edge_cases(self) -> None:
        snap_20 = (
            ROOT
            / "outputs"
            / "discovery"
            / "2026-11-15_1500_0300"
            / "evaluations"
            / "20261115T195445143219Z_3987d11f552b"
            / "prospective_trial"
            / "daily_runs"
            / "20261115T205609404284"
            / "comparison_audit"
            / "sources"
            / "preselection.json"
        )
        if not snap_20.is_file():
            self.skipTest("Snapshot real de 20 partidos no presente")

        data = json.loads(snap_20.read_text(encoding="utf-8"))
        target = data["matches"][0]
        cutoff = data["generated_at_utc"]

        # Inyectamos en copia en memoria 1 duplicado y 1 partido futuro post-cutoff para validar rechazo
        import copy
        modified = copy.deepcopy(target)
        dup = copy.deepcopy(modified["statistical_observations"]["home"][0])
        future = copy.deepcopy(modified["statistical_observations"]["home"][0])
        future["fixture_id"] = 999999991
        future["kickoff_utc"] = "2099-01-01T18:00:00+00:00"
        modified["statistical_observations"]["home"].extend([dup, future])

        bundle = ObservationStore.from_preselection_match(modified, cutoff_utc=cutoff)

        # Verifica 20 partidos procesados y separación entre disponibles y válidos detallados
        self.assertEqual(len(bundle.home_matches), 20)
        self.assertEqual(len(bundle.away_matches), 20)
        rejected_reasons = {r["reason"] for r in bundle.rejected_records}
        self.assertIn("INVALID_OR_DUP_FIXTURE", rejected_reasons)
        self.assertIn("AFTER_CUTOFF", rejected_reasons)

        l5 = bundle.windows["home"]["L5_all_competitions"]
        l10 = bundle.windows["home"]["L10_all_competitions"]
        l20 = bundle.windows["home"]["L20_all_competitions"]
        l20_exact = bundle.windows["home"]["L20_exact_competition"]

        self.assertEqual(l5.total_matches_available, 5)
        self.assertEqual(l10.total_matches_available, 10)
        self.assertEqual(l20.total_matches_available, 20)
        self.assertEqual(l20.detailed_matches_valid, 5)  # Solo 5 de 20 tienen detalle
        self.assertEqual(l20.metrics["own.goals"]["valid_n"], 20)
        self.assertEqual(l20.metrics["own.corners"]["valid_n"], 5)
        self.assertEqual(l20.metrics["own.corners"]["missing_n"], 15)  # None != 0 preservado
        self.assertEqual(l20_exact.total_matches_available, 16)
        self.assertEqual(l20.competition_mix["exact_competition_pct"], 80.0)
        self.assertEqual(l20.competition_mix["other_competitions_pct"], 20.0)
        self.assertTrue(all(f.coverage_declared == CoverageFlag.UNKNOWN for f in bundle.facts))
        self.assertIn("SCHEDULE_FATIGUE", {c.category for c in bundle.contextual_evidence})


if __name__ == "__main__":
    unittest.main()
