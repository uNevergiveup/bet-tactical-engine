#!/usr/bin/env python3
"""Pruebas locales sin red ni dependencias externas."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import match_performance_engine as perf
import team_form_engine as form

ROOT = Path(__file__).parent


def sample():
    path = ROOT / "fixtures" / "match_engine_7896590.json"
    if not path.exists():
        return None
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


class EngineTests(unittest.TestCase):
    def test_iso_requires_offset(self):
        with self.assertRaises(ValueError):
            perf.aware("2026-11-04T20:00:00", "foo")

    def test_missing_not_zero(self):
        self.assertIsNone(perf.as_number(None))
        self.assertIsNone(perf.as_number("N/A"))
        self.assertEqual(perf.as_number("65%"), 65.0)
        stats, warnings = perf.one_team_statistics({"statistics": []}, 10)
        self.assertIsNone(stats["shots"])
        self.assertIn("TEAM_STATISTICS_ROWS_0", warnings)

    def test_internal_stat_consistency(self):
        own = {"shots": 5, "on_target": 8, "possession_pct": 120}
        rival = {"shots": 7, "on_target": 2, "possession_pct": 30}
        row = {"score": {"fulltime": {"home": 1, "away": 0}},
               "goals": {"home": 2, "away": 0}}
        q = perf.audit_historical_statistics(row, own, rival)
        self.assertEqual(q["status"], "ERROR")
        self.assertIn("OWN_ON_TARGET_EXCEEDS_TOTAL_SHOTS",
                      [i["code"] for i in q["issues"]])
        self.assertIn("FULLTIME_GOALS_DISAGREE_HOME", [i["code"] for i in q["issues"]])

    def test_domination_not_causation(self):
        own = {"shots": 21, "on_target": 7, "xg": 2.4, "corners": 9,
               "possession_pct": 68}
        rival = {"shots": 7, "on_target": 2, "xg": 0.6, "corners": 2,
                 "possession_pct": 32}
        result = perf.advantage(own, rival, "D")
        self.assertEqual(result["label"], "SUPERIORIDAD_ESTADISTICA_MULTIDIMENSIONAL")
        self.assertEqual(result["result_performance_gap"], "RESULTADO_INFERIOR_A_PRODUCCION_OBSERVADA")

    def test_no_trend_with_five_games(self):
        rows = [{"goals_for": 1} for _ in range(5)]
        self.assertEqual(form.compare_periods(rows)["status"], "HISTORIAL_INSUFICIENTE_PARA_COMPARAR")

    def test_recent_decline(self):
        # Orden reciente -> antiguo, con 10 observaciones.
        rows = []
        for i in range(10):
            high = i >= 5
            rows.append({"goals_for": 1,
                         "own": {"shots": 18 if high else 8,
                                  "on_target": 7 if high else 2,
                                  "xg": 2.2 if high else 0.7}})
        trend = form.compare_periods(rows)
        self.assertEqual(trend["direction"], "POSIBLE_DESACELERACION_OFENSIVA")
        self.assertEqual(trend["recent_n"], 5)
        self.assertEqual(trend["comparisons"]["xg_for"]["status"], "DESCENSO_DESCRIPTIVO")

    def test_coach_id_zero_vs_id_with_same_name(self):
        rows = [{"fixture_id": 2, "kickoff_utc": "2026-10-02T00:00:00+00:00",
                 "league_id": 1, "season": 2026,
                 "lineup": {"coach_id": 999, "coach_name": "El Mismo", "formation": "4-4-2", "xi_complete": False}},
                {"fixture_id": 1, "kickoff_utc": "2026-10-01T00:00:00+00:00",
                 "league_id": 1, "season": 2026,
                 "lineup": {"coach_id": None, "coach_name": "El Mismo", "formation": "4-4-2", "xi_complete": False}}]
        self.assertEqual(form.lineup_and_coach_regimes(rows)["confirmed_coach_changes_in_observed_history"], [])

    def test_coach_accent_is_not_change(self):
        rows = [{"fixture_id": 2, "kickoff_utc": "2026-10-02T00:00:00+00:00",
                 "league_id": 2, "season": 2026,
                 "lineup": {"coach_id": 21528, "coach_name": "Cesc Fàbregas", "formation": "4-2-3-1"}},
                {"fixture_id": 1, "kickoff_utc": "2026-10-01T00:00:00+00:00",
                 "league_id": 2, "season": 2026,
                 "lineup": {"coach_id": 21528, "coach_name": "Cesc Fabregas", "formation": "4-2-3-1"}}]
        self.assertEqual(form.lineup_and_coach_regimes(rows)["confirmed_coach_changes_in_observed_history"], [])

    @unittest.skipIf(sample() is None, "Fixture real no disponible: prueba de integración opcional")
    def test_real_fixture(self):
        raw, sha = sample()
        out = perf.build(raw, input_sha256=sha)
        self.assertEqual(out["target_fixture_id"], 1635681)
        self.assertEqual(out["event_id_bwin"], "7896590")
        self.assertEqual(out["teams"]["home"]["valid_count"], 6)
        self.assertEqual(out["teams"]["away"]["valid_count"], 6)
        self.assertTrue(all(r["fixture_id"] != 1635681 for s in ("home", "away")
                            for r in out["teams"][s]["historical_fixtures"]))
        lens_sporting = next(r for r in out["teams"]["home"]["historical_fixtures"]
                             if r["fixture_id"] == 1635679)
        self.assertEqual(lens_sporting["statistical_advantage"]["result_performance_gap"],
                         "RESULTADO_INFERIOR_A_PRODUCCION_OBSERVADA")
        como_feyenoord = next(r for r in out["teams"]["away"]["historical_fixtures"]
                              if r["fixture_id"] == 1635665)
        self.assertEqual(como_feyenoord["xg_scoreline_gap"], "NO_GANO_PESE_A_XG_SUPERIOR")
        trend = form.build(out, source_sha256="test")
        self.assertEqual(trend["teams"]["home"]["trend_comparison"]["status"], "EXPLORATORIO_N3_VS_N3")
        self.assertFalse(trend["teams"]["home"]["window_summaries"]["L10"]["complete_window"])

    @unittest.skipIf(sample() is None, "Fixture real no disponible: prueba de integración opcional")
    def test_target_fixture_cannot_leak(self):
        raw, sha = sample()
        extra = copy.deepcopy(raw["context"]["home_history_deep"]["detailed_fixtures"][0])
        extra["fixture"]["id"] = 1635681
        raw["context"]["home_history_deep"]["detailed_fixtures"].append(extra)
        result = perf.build(raw, input_sha256=sha)
        self.assertTrue(any(s["reason"] == "TARGET_FIXTURE_EXCLUDED"
                            for s in result["teams"]["home"]["skipped"]))
        self.assertEqual(result["teams"]["home"]["valid_count"], 6)

    @unittest.skipIf(sample() is None, "Fixture real no disponible: prueba de integración opcional")
    def test_after_kickoff_snapshot_fails(self):
        raw, sha = sample()
        raw["generated_at"] = "2026-11-04T20:01:00+00:00"
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_AT_OR_AFTER_TARGET_KICKOFF"):
            perf.build(raw, input_sha256=sha)

    @unittest.skipIf(sample() is None, "Fixture real no disponible: prueba de integración opcional")
    def test_output_is_deterministic(self):
        raw, sha = sample()
        a = perf.build(raw, input_sha256=sha)
        b = perf.build(raw, input_sha256=sha)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
