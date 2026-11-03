import json
import tempfile
import unittest
from pathlib import Path

import prematch_selection_audit as audit


ASOF = "2026-11-03T10:00:00+01:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def match(fid, league_id=140):
    return {
        "fixture_id": fid, "home": f"Home {fid}", "away": f"Away {fid}",
        "home_id": fid * 10, "away_id": fid * 10 + 1, "league": "League",
        "league_id": league_id, "season": 2026, "gender": "M", "status": "NS",
        "kickoff_madrid": "2026-11-04T20:00:00+01:00", "details_checked": True,
    }


def signal(fid, side, metric, status="SUPPORTED_DESCRIPTIVE_SIGNAL", market=False,
           own_n=8, opp_n=8, requested=100.0, effective=40.0, mean=2.0):
    series = lambda n: {
        "n": n, "mean": mean, "cv": .4 if n else None, "history_n": 20,
        "expected_n": 8, "history_not_detailed_n": 12,
        "requested_data_coverage_pct": requested,
        "historical_effective_coverage_pct": effective,
        "venue": {"n": 4, "mean": mean}, "recent": {"n": 5, "mean": mean},
        "previous": {"n": 3, "mean": mean},
    }
    return {
        "signal_id": f"{fid}:{side}:{metric}", "metric": metric,
        "family": audit.FAMILY[metric], "team_side": side, "team_id": fid * 10,
        "status": status, "priority_score": 70, "source_mode": "FIXTURE_OBSERVATIONS",
        "own": series(own_n), "opponent_conceded": series(opp_n),
        "contradictions": ["OWN_PRODUCTION_VS_RIVAL_CONCESSION_DISAGREE"] if status == "CONTRADICTORY_SIGNAL" else [],
        "limitations": ["DESCRIPTIVE_NOT_CALIBRATED_NO_EV"],
        "markets": [{"market": "Total", "selection": "Más de 2,5", "price": "1.90"}] if market else [],
    }


class SelectionAuditTests(unittest.TestCase):
    def make_run(self, fixtures, signals_by_fixture=None, review=None, omit=(), summary=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = Path(temp.name) / "2026-11-03_1500_0300"
        trial = run / "prospective_trial"
        discovered = {"schema_version": "1.0-discovery", "generated_at_utc": "2026-11-03T08:00:00+00:00", "fixtures": fixtures}
        major = {"schema_version": "1.0-major-leagues", "generated_at_utc": "2026-11-03T08:01:00+00:00",
                 "fixtures": fixtures, "review": review or [], "excluded": []}
        pre = {"schema_version": "1.3-preselection", "generated_at_utc": "2026-11-03T08:05:00+00:00", "matches": fixtures}
        pattern_rows = []
        for fid, sigs in (signals_by_fixture or {}).items():
            for s in sigs:
                pattern_rows.append({"fixture_id": fid, "team_side": s["team_side"], "metric": s["metric"],
                                     "status": "MUESTRA_DESCRIPTIVA_OK", "pattern_flag": "PATRON_A_CONFIRMAR"})
        pattern = {"schema_version": "0.1-pattern-engine", "asof_madrid": ASOF,
                   "fixtures": [match(fid) for fid in (signals_by_fixture or {})], "patterns": pattern_rows}
        tactical_fixtures = {
            str(fid): {**match(fid), "status": "EVALUATED", "signals": sigs, "priority_score": 70}
            for fid, sigs in (signals_by_fixture or {}).items()
        }
        tactical = {"schema_version": "2.0-descriptive-tactical-signals", "asof_madrid": ASOF,
                    "fixtures": tactical_fixtures, "ranking": [], "sources": []}
        ledger = {"schema_version": "2.0-candidate-ledger", "scope": {"max_matches_batch_size": 5},
                  "fixtures": [{**x, "state": "PENDIENTE", "reason": "TEST", "stage": "test"} for x in fixtures]}
        docs = {
            "fixtures.json": discovered, "fixtures_major.json": major,
            "prospective_trial/fixtures_major_test.json": major,
            "prospective_trial/enriched/preselection.json": pre,
            "prospective_trial/patterns/pattern_engine.json": pattern,
            "prospective_trial/tactical/tactical_signal_engine.json": tactical,
            "prospective_trial/candidate_ledger.json": ledger,
            "prospective_trial/prospective_test_summary.json": summary or {"evaluation_deferred_ids": [], "discovery_deferred_ids": []},
            "prospective_trial/budget_assignment.json": {"evaluation_fixture_ids": [x["fixture_id"] for x in fixtures], "bwin_order_fixture_ids": []},
        }
        for rel, doc in docs.items():
            if rel not in omit:
                write(run / rel, doc)
        return run

    def test_20_history_8_details_is_100_requested_40_effective(self):
        run = self.make_run([match(1)], {1: [signal(1, "home", "corners")]})
        _, catalog, _ = audit.run_audit(run)
        row = catalog["patterns"][0]
        self.assertEqual(row["coverage_requested_pct"]["own"], 100.0)
        self.assertEqual(row["coverage_effective_pct"]["own"], 40.0)
        self.assertEqual(row["history_not_detailed_n"]["own"], 12)

    def test_contradictory_goals_does_not_remove_supported_corners(self):
        sigs = [signal(1, "home", "goals", "CONTRADICTORY_SIGNAL"), signal(1, "home", "corners")]
        run = self.make_run([match(1)], {1: sigs})
        _, catalog, _ = audit.run_audit(run)
        states = {x["metric"]: x["evidence_status"] for x in catalog["patterns"]}
        self.assertEqual(states, {"goals": "CONTRADICTORY_SIGNAL", "corners": "SUPPORTED_DESCRIPTIVE_SIGNAL"})

    def test_multiple_metrics_and_fixture_ids_never_mix(self):
        run = self.make_run([match(1), match(2)], {1: [signal(1, "home", "goals"), signal(1, "home", "corners")],
                                                   2: [signal(2, "away", "shots")]})
        _, catalog, _ = audit.run_audit(run)
        keys = {(x["fixture_id"], x["team_side"], x["metric"]) for x in catalog["patterns"]}
        self.assertEqual(keys, {(1, "home", "goals"), (1, "home", "corners"), (2, "away", "shots")})

    def test_review_fixture_is_recorded_without_invented_metrics(self):
        review = match(9, 999)
        run = self.make_run([match(1)], {1: [signal(1, "home", "goals")]}, review=[review])
        selection, catalog, _ = audit.run_audit(run)
        self.assertTrue(any(x["fixture_id"] == 9 and x["reason"] == "COMPETITION_REVIEW_NOT_EVALUATED"
                            for x in catalog["fixtures_not_evaluated"]))
        self.assertFalse(any(x["fixture_id"] == 9 for x in catalog["patterns"]))
        self.assertEqual(next(x for x in selection["stage_summary"] if x["stage"] == "competition_review")["numerator"], 1)

    def test_signal_without_bwin_is_preserved_not_bet(self):
        run = self.make_run([match(1)], {1: [signal(1, "home", "corners", market=False)]})
        _, catalog, _ = audit.run_audit(run)
        self.assertEqual(catalog["patterns"][0]["market_status"], "PATTERN_DETECTED_NO_MARKET")
        self.assertEqual(catalog["patterns"][0]["line_status"], "LINE_NOT_VERIFIED")
        self.assertFalse(catalog["actionable_bet"])

    def test_budget_deferred_is_separate_from_excluded(self):
        summary = {"evaluation_deferred_ids": [1], "discovery_deferred_ids": [2]}
        run = self.make_run([match(1), match(2)], {1: [signal(1, "home", "goals")], 2: [signal(2, "home", "goals")]}, summary=summary)
        selection, catalog, _ = audit.run_audit(run)
        self.assertTrue(selection["budget"]["deferred_is_not_excluded"])
        self.assertEqual({x["market_status"] for x in catalog["patterns"]}, {"DEFERRED"})

    def test_post_asof_artifact_is_flagged(self):
        run = self.make_run([match(1)], {1: [signal(1, "home", "goals")]})
        doc = json.loads((run / "fixtures.json").read_text())
        doc["generated_at_utc"] = "2026-11-04T12:00:00+00:00"
        write(run / "fixtures.json", doc)
        selection, _, _ = audit.run_audit(run)
        self.assertEqual(selection["artifacts"]["fixtures"]["temporal_status"], "POST_ASOF")
        # Ledger outcome is retained retrospectively but is not backdated to asof.
        ledger_path=run / "prospective_trial/candidate_ledger.json"
        ledger=json.loads(ledger_path.read_text());ledger["generated_madrid"]="2026-11-04T12:00:00+01:00";write(ledger_path,ledger)
        selection, _, _ = audit.run_audit(run)
        self.assertEqual(selection["fixtures"][0]["classification_asof"], "NO_VERIFICABLE_AS_OF")

    def test_null_is_not_converted_to_zero_and_no_duplicates(self):
        row = signal(1, "home", "xg", "INSUFFICIENT_DATA", own_n=0, opp_n=0, mean=None)
        row["own"]["mean"] = None
        row["opponent_conceded"]["mean"] = None
        run = self.make_run([match(1)], {1: [row, dict(row)]})
        _, catalog, _ = audit.run_audit(run)
        self.assertEqual(len(catalog["patterns"]), 1)
        self.assertIsNone(catalog["patterns"][0]["mean_own"])

    def test_missing_artifacts_produce_partial_audit(self):
        run = self.make_run([match(1)], {}, omit=("prospective_trial/enriched/preselection.json",
                                                  "prospective_trial/patterns/pattern_engine.json",
                                                  "prospective_trial/tactical/tactical_signal_engine.json"))
        selection, catalog, destination = audit.run_audit(run)
        self.assertIn("preselection", selection["missing_artifacts"])
        self.assertEqual(catalog["counts"]["rows"], 0)
        self.assertTrue((destination / "selection_audit.md").is_file())

    def test_players_periods_and_unobserved_tactics_are_never_supported(self):
        run = self.make_run([match(1)], {1: [signal(1, "home", "goals")]})
        _, catalog, _ = audit.run_audit(run)
        limits = catalog["patterns"][0]["limitations"]
        self.assertTrue(any(x.startswith("PLAYER_METRICS_NOT_IMPLEMENTED") for x in limits))
        self.assertTrue(any(x.startswith("PERIOD_METRICS_NOT_IMPLEMENTED") for x in limits))
        self.assertTrue(any(x.startswith("UNOBSERVED_TACTICS_NOT_SUPPORTED") for x in limits))

    def test_verified_market_never_becomes_calibrated_model(self):
        run = self.make_run([match(1)], {1: [signal(1, "home", "goals", market=True)]})
        _, catalog, _ = audit.run_audit(run)
        row = catalog["patterns"][0]
        self.assertEqual(row["market_status"], "MARKET_VERIFIED_NO_CALIBRATED_MODEL")
        self.assertNotIn("EV", row)


if __name__ == "__main__":
    unittest.main()
