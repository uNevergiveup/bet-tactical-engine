import json
import tempfile
import unittest
from pathlib import Path

import prematch_tactical_intelligence_v1050 as mod


class TacticalIntelligenceTests(unittest.TestCase):
    def setUp(self):
        from prematch_test_fixtures import integral_fixture
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = integral_fixture(Path(tmp.name))
    def test_offline_event_without_external_reports(self):
        root = self.root
        out_path, md_path = mod.run("7902458", root)
        self.assertTrue(out_path.exists())
        self.assertTrue(md_path.exists())
        doc = json.loads(out_path.read_text())
        self.assertEqual(doc["target_fixture_id"], 1570450)
        self.assertEqual(doc["qualitative"]["status"], "PARTIAL_NO_EXTERNAL_REPORTS")
        self.assertFalse(doc["odds_enabled"])
        self.assertNotIn("picks", doc)
        self.assertNotIn("probabilities", doc)

    def test_report_adapter_deduplicates_and_excludes_post_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "grok_7902458.json"
            valid.write_text(json.dumps({
                "source": "grok", "event_id_bwin": "7902458", "fixture_id": 1570450,
                "cutoff_utc": "2026-11-06T12:00:00+00:00", "url": "https://example.test/a",
                "facts": [{"kind": "FACT", "label": "CONFIRMADO", "text": "Sevilla tiene una baja."},
                          {"kind": "FACT", "label": "CONFIRMADO", "text": "Sevilla tiene una baja."}]
            }))
            late = root / "gemini_7902458.json"
            late.write_text(json.dumps({"source": "gemini", "event_id_bwin": "7902458", "fixture_id": 1570450,
                                        "cutoff_utc": "2026-11-06T18:00:00+00:00", "facts": ["dato posterior"]}))
            loaded = mod.load_qualitative_reports([valid, late], event_id="7902458", fixture_id=1570450,
                                                   cutoff=mod.utc("2026-11-06T14:49:16+00:00"))
            self.assertEqual(len(loaded["accepted"]), 1)
            self.assertEqual(len(loaded["facts"]), 1)
            self.assertEqual(loaded["excluded"][0]["status"], "POST_CUTOFF_EXCLUDED")

    def test_same_report_copied_to_two_files_counts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"source": "grok", "event_id_bwin": "7902458", "fixture_id": 1570450,
                       "cutoff_utc": "2026-11-06T12:00:00+00:00", "url": "https://example.test/a",
                       "facts": [{"kind": "FACT", "text": "Mismo hecho"}]}
            a, b = root / "grok_a.json", root / "grok_b.json"
            raw = json.dumps(payload); a.write_text(raw); b.write_text(raw)
            loaded = mod.load_qualitative_reports([a, b], event_id="7902458", fixture_id=1570450,
                                                   cutoff=mod.utc("2026-11-06T14:49:16+00:00"))
            self.assertEqual(len(loaded["accepted"]), 2)
            self.assertEqual(len(loaded["facts"]), 1)

    def test_report_identity_mismatch_does_not_block_statistical_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "grok_bad.md"
            bad.write_text("Event ID: 7999999\nFixture ID: 1570450\ncutoff_utc: 2026-11-06T12:00:00+00:00\nFACT: texto")
            root = self.root
            integral = json.loads((root / "outputs/matches/7902458/analysis/deep_v1042/analisis_integral_7902458.json").read_text())
            doc = mod.build(integral, qualitative_reports=[bad])
            self.assertEqual(doc["qualitative"]["status"], "PARTIAL_NO_EXTERNAL_REPORTS")
            self.assertEqual(doc["qualitative"]["excluded"][0]["status"], "IDENTITY_MISMATCH")
            self.assertEqual(doc["teams"]["home"]["name"], "Sevilla")

    def test_duels_keep_unverified_positional_mechanisms_explicit(self):
        root = self.root
        out, _ = mod.run("7902458", root)
        doc = json.loads(out.read_text())
        self.assertTrue(doc["duel_matrix"][0]["statistical_cross"])
        self.assertEqual(doc["duel_matrix"][0]["statistical_cross"][0]["global_L5"]["production"]["mean"], 2.0)
        for duel in doc["duel_matrix"]:
            if duel["duel"] in {"laterales_extremos", "centrales_delanteros", "mediocampo_presion_salida"}:
                self.assertEqual(duel["label"], "NO_VERIFICADO")
                self.assertFalse(duel["players_involved"])

    def test_player_denominators_are_explicit_and_unverifiable_per90_is_suppressed(self):
        root = self.root
        out, _ = mod.run("7902458", root)
        doc = json.loads(out.read_text())
        player = doc["teams"]["home"]["players"][0]
        self.assertTrue(player["metric_denominator"])
        for denominator in player["metric_denominator"]:
            self.assertIn("metric_name", denominator)
            self.assertIn("unit", denominator)
            self.assertIn("observed_matches", denominator)
            self.assertIn("eligible_minutes", denominator)
            self.assertIn("formula", denominator)
            if denominator["eligible_minutes"] is None:
                self.assertIsNone(denominator["per90"])

    def test_l5_trace_and_scenarios_do_not_claim_probable_without_xi(self):
        root = self.root
        out, _ = mod.run("7902458", root)
        doc = json.loads(out.read_text())
        trace = doc["teams"]["home"]["window_trace"]
        self.assertEqual(len(trace["global_L5"]["fixture_ids"]), 5)
        self.assertEqual(len(trace["home_L5"]["fixture_ids"]), 5)
        self.assertNotIn("PROBABLE", {x["label"] for x in doc["scenarios"]})

    def test_research_questions_and_priority_components_are_present(self):
        root = self.root
        out, _ = mod.run("7902458", root)
        doc = json.loads(out.read_text())
        self.assertTrue(doc["research_questions"])
        self.assertTrue(any(q["category"] == "L5_CAMBIO" for q in doc["research_questions"]))
        self.assertTrue(any(q["category"] == "AUSENCIA" for q in doc["research_questions"]))
        for item in doc["priorities"]:
            self.assertIn("magnitude", item)
            self.assertIn("dispersion_cv", item)
            self.assertIn("matchup_relevance", item)
            self.assertIn("contradictions", item)

    def test_halves_use_metric_deltas_not_coverage_only(self):
        root = self.root
        out, _ = mod.run("7902458", root)
        doc = json.loads(out.read_text())
        for side in ("home", "away"):
            period = doc["teams"][side]["period_interpretation"]
            self.assertIn(period["status"], {"INFERIDO_DESCRIPTIVO", "NO_VERIFICADO"})
            self.assertIsNotNone(period["metrics"]["xg"]["difference_second_minus_first"])

    def test_source_cutoff_is_preserved(self):
        root = self.root
        integral = json.loads((root / "outputs/matches/7902458/analysis/deep_v1042/analisis_integral_7902458.json").read_text())
        doc = mod.build(integral)
        self.assertEqual(doc["analysis_cutoff_utc"], integral["analysis_cutoff_utc"])
        self.assertEqual(doc["target_fixture_id"], integral["target_fixture_id"])

    def test_integral_source_hash_mismatch_is_blocked(self):
        root = self.root
        integral = json.loads((root / "outputs/matches/7902458/analysis/deep_v1042/analisis_integral_7902458.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.json"
            source.write_text("changed")
            integral["source_files"] = {"performance": str(source), "performance_sha256": "0" * 64}
            with self.assertRaisesRegex(ValueError, "INTEGRAL_SOURCE_SHA256_MISMATCH"):
                mod.build(integral)


if __name__ == "__main__":
    unittest.main()
