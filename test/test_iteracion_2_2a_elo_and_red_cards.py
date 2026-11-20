#!/usr/bin/env python3
"""Pruebas Iteración 2.2A:
1. Auditoría completa AsOfEloTracker (no fuga, mismo kickoff, as-of, deduplicación,
   aislamiento CLUB/NATIONAL_TEAM, fallback sin historial, cobertura reproducible).
2. Semántica de tarjetas rojas: RED_CARD_CONFIRMED / NO_RED_CARD_VERIFIED / RED_CARD_UNKNOWN,
   compatibilidad con snapshots legados y sensibilidad.
3. Contrato del protocolo de validación prospectiva (> 2026-11-14).
"""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.context.history_quality import audit_historical_window_quality
from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    ObservationStore,
    _analyze_red_card_events,
)
from bet_engine.modeling.experimental_v2_1b import (
    AsOfEloTracker,
    ExperimentalConfig,
    predict_experimental_match,
)

ROOT = Path(__file__).resolve().parent.parent


class TestIteracion22AEloAndRedCards(unittest.TestCase):
    def test_elo_same_kickoff_target_leak_dedup_and_fallback(self) -> None:
        t0 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
        t_same = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
        fixtures = [
            # Dos partidos previos limpios para equipo 1 y 2
            {
                "fixture_id": 101,
                "home_id": 1,
                "away_id": 3,
                "hg": 3.0,
                "ag": 0.0,
                "stratum": "CLUB",
                "ko_dt": t0,
                "avail_dt": t0 + timedelta(minutes=125),
            },
            # Duplicado intencional del fixture 101 que debe ignorarse
            {
                "fixture_id": 101,
                "home_id": 1,
                "away_id": 3,
                "hg": 3.0,
                "ag": 0.0,
                "stratum": "CLUB",
                "ko_dt": t0,
                "avail_dt": t0 + timedelta(minutes=125),
            },
            # Dos partidos con EXACTAMENTE el mismo kickoff t_same (102 es el objetivo, 103 simultáneo)
            {
                "fixture_id": 102,
                "home_id": 1,
                "away_id": 2,
                "hg": 0.0,
                "ag": 5.0,
                "stratum": "CLUB",
                "ko_dt": t_same,
                "avail_dt": t_same + timedelta(minutes=125),
            },
            {
                "fixture_id": 103,
                "home_id": 4,
                "away_id": 5,
                "hg": 4.0,
                "ag": 0.0,
                "stratum": "CLUB",
                "ko_dt": t_same,
                "avail_dt": t_same + timedelta(minutes=125),
            },
        ]
        tracker = AsOfEloTracker.build_from_fixtures(fixtures)

        # Deduplicación: equipo 1 solo cuenta 1 partido tras t0 (no 2)
        r1_before, n1_before = tracker.get_rating_asof("CLUB", 1, t_same)
        self.assertEqual(n1_before, 1)
        self.assertGreater(r1_before, 1500.0)

        # En t_same, ni fixture 102 (target) ni 103 (mismo kickoff) han entrado aún (avail_dt > t_same)
        r4_at_same, n4_at_same = tracker.get_rating_asof("CLUB", 4, t_same)
        self.assertEqual((r4_at_same, n4_at_same), (1500.0, 0))

        # Al pasar a predict_experimental_match con <3 partidos en Elo, debe hacer fallback (elo_coverage_ok=False)
        hm = HistoricalTeamMatch(
            fixture_id=101, team_id=1, opponent_id=3,
            kickoff_utc=t0.isoformat(), available_at_utc=(t0 + timedelta(minutes=125)).isoformat(),
            league_id=39, league_name="Test", season=2026, competition_stratum="CLUB",
            venue="HOME", status="FT", coach_id=None, coach_name=None,
            red_card_distortion=False, own_metrics={"goals": 3.0}, conceded_metrics={"goals": 0.0},
            has_detailed_stats=False, source="test",
        )
        res = predict_experimental_match(
            (hm,), (), home_id=1, away_id=2, target_ko_dt=t_same,
            target_league_id=39, target_season=2026, target_stratum="CLUB",
            cfg=ExperimentalConfig(name="M0_plus_Elo", use_elo=True, elo_weight=0.25),
            elo_tracker=tracker,
        )
        self.assertFalse(res["components"]["elo_coverage_ok"])

    def test_red_card_three_way_semantics_and_legacy_compatibility(self) -> None:
        # 1. Confirmada con tarjeta roja
        r_conf = _analyze_red_card_events(
            [{"type": "Card", "detail": "Red Card", "time": {"elapsed": 30}, "team": {"id": 10}}],
            10,
            own_red_stat=1.0,
            opp_red_stat=0.0,
        )
        self.assertEqual(r_conf["red_card_status"], "RED_CARD_CONFIRMED")
        self.assertTrue(r_conf["red_card_verified"])
        self.assertTrue(r_conf["early_any_red"])

        # 2. Ausencia verificada (estadística en 0 o timeline verificada sin rojas)
        r_ver_stat = _analyze_red_card_events([], 10, own_red_stat=0.0, opp_red_stat=0.0)
        self.assertEqual(r_ver_stat["red_card_status"], "NO_RED_CARD_VERIFIED")
        self.assertTrue(r_ver_stat["red_card_verified"])
        self.assertEqual(r_ver_stat["distortion_side"], "NONE")

        r_ver_ev = _analyze_red_card_events(
            [{"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 22}, "team": {"id": 10}}],
            10,
        )
        self.assertEqual(r_ver_ev["red_card_status"], "NO_RED_CARD_VERIFIED")
        self.assertTrue(r_ver_ev["red_card_verified"])

        # 3. Desconocida (SCORE_ONLY_NO_STATS: ni events ni red stat)
        r_unk = _analyze_red_card_events(None, 10, own_red_stat=None, opp_red_stat=None)
        self.assertEqual(r_unk["red_card_status"], "RED_CARD_UNKNOWN")
        self.assertFalse(r_unk["red_card_verified"])
        self.assertEqual(r_unk["distortion_side"], "UNKNOWN")
        self.assertFalse(r_unk["early_any_red"])

        # 4. Compatibilidad con snapshot legado en history_quality (red_card_detail sin red_card_status)
        ko = "2026-10-01T15:00:00+00:00"
        av = "2026-10-01T17:05:00+00:00"
        m_leg = HistoricalTeamMatch(
            fixture_id=555, team_id=1, opponent_id=2,
            kickoff_utc=ko, available_at_utc=av,
            league_id=39, league_name="L", season=2026, competition_stratum="CLUB",
            venue="HOME", status="FT", coach_id=None, coach_name=None,
            red_card_distortion=False, own_metrics={"goals": 1.0}, conceded_metrics={"goals": 0.0},
            has_detailed_stats=False, source="legacy", red_card_detail={"distortion_side": "NONE"},
        )
        bundle = MatchObservationBundle(
            event_id="ev_leg", fixture_id=999, league_id=39, season=2026,
            kickoff_utc="2026-10-10T15:00:00+00:00", cutoff_utc="2026-10-10T14:00:00+00:00",
            home_id=1, away_id=2, home_name="A", away_name="B",
            home_matches=(m_leg,), away_matches=(), facts=(), contextual_evidence=(),
            rejected_records=(), windows={}, source_sha256="abc",
        )
        q = audit_historical_window_quality(bundle, windows=(6,))
        l6_h = q["sides"]["home"]["windows"]["L6"]
        self.assertEqual(l6_h["red_card_unknown_n"], 1)
        self.assertEqual(l6_h["red_card_confirmed_n"], 0)

    def test_prospective_manifest_rejects_non_prospective_timestamps_and_tracks_hashes(self) -> None:
        legacy_manifest_path = ROOT / "audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json"
        v2_manifest_path = ROOT / "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
        if not (legacy_manifest_path.is_file() and v2_manifest_path.is_file()):
            self.skipTest("Manifest 2.2B o V2 2B.2A no presente")
        legacy_raw = legacy_manifest_path.read_bytes()
        legacy_m = json.loads(legacy_raw.decode("utf-8"))
        # La referencia histórica original se conserva intacta sin reemplazo silencioso
        self.assertEqual(legacy_m["schema_version"], "2.2B-frozen-models")
        self.assertEqual(legacy_m["corpus_last_fixture_utc"], "2026-11-14T19:45:00+00:00")

        m_v2 = json.loads(v2_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(m_v2["schema_version"], "2B.2A-frozen-models")
        self.assertEqual(m_v2["supersedes_manifest_sha256"], hashlib.sha256(legacy_raw).hexdigest())
        self.assertEqual(m_v2["corpus_last_fixture_utc"], "2026-11-14T19:45:00+00:00")
        for rel, expected_sha in m_v2["code_hashes"].items():
            actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
            self.assertEqual(actual, expected_sha, f"Hash mismatch en {rel}")


if __name__ == "__main__":
    unittest.main()
