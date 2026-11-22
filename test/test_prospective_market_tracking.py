import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bet_engine.modeling.prospective_market_tracking import attach_closing_odds, evaluate_market_tracking
from bet_engine.modeling.prospective_registry import _canonical_sha256, default_settlement_path


class ProspectiveMarketTrackingTests(unittest.TestCase):
    def test_closing_snapshot_and_calibration_keep_prediction_and_settlement_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            record = folder / "101_7910807.json"
            pre = {
                "fixture_id": 101, "event_id": "7910807",
                "teams": {"home_name": "Troyes", "away_name": "Le Havre"},
                "timing": {"kickoff_utc": "2026-11-20T19:45:00Z", "cutoff_utc": "2026-11-20T18:00:00Z",
                           "generated_utc": "2026-11-20T18:30:00Z", "temporal_integrity_status": "PREMATCH_VERIFIED"},
                "predictions": {"B0": {"outcomes_1x2": {"home_win": .5, "draw": .3, "away_win": .2},
                                       "p_over_2_5": .6, "p_btts_yes": .55}},
                "bwin_odds": {"odds_status": "ODDS_VERIFIED", "quotes": {"home_win": 2.2, "under_2_5_goals": 1.8}},
            }
            pre_sha = _canonical_sha256(pre)
            record.write_text(json.dumps({"pre_kickoff": pre, "pre_kickoff_sha256": pre_sha}))
            original = record.read_bytes()
            settlement = default_settlement_path(record)
            settlement.parent.mkdir()
            settled = {"record_file_sha256": hashlib.sha256(original).hexdigest(), "pre_kickoff_sha256": pre_sha,
                       "settlement": {"status": "SETTLED", "verification_status": "VERIFIED_AUTHORITATIVE",
                                      "creditable": True, "provider_provenance_verified": True,
                                      "match_status": "FT", "hg": 2, "ag": 1}}
            settlement.write_text(json.dumps(settled))
            settlement_bytes = settlement.read_bytes()
            close = folder / "odds" / "close.txt"
            close.parent.mkdir()
            capture = (
                "Event ID: 7910807\nCaptura: 2026-11-20T19:40:00Z\n"
                "MERCADO: Resultado del partido | Tiempo reglamentario\nTroyes : 2.00\nX : 3.00\nLe Havre : 3.30\n"
                "MERCADO: Total de goles | 1ª parte\nMenos de 2,5 : 1.60\n"
            )
            for altered, error in (
                (capture.replace("19:40", "19:45"), "OUTSIDE_WINDOW"),
                (capture.replace("7910807", "7906958"), "EVENT_ID_MISMATCH"),
            ):
                close.write_text(altered)
                with self.assertRaisesRegex(ValueError, error):
                    attach_closing_odds(record, close, expected_sha256=hashlib.sha256(close.read_bytes()).hexdigest())
            close.write_text(capture)
            with self.assertRaisesRegex(ValueError, "SHA_MISMATCH"):
                attach_closing_odds(record, close, expected_sha256="bad")
            sha = hashlib.sha256(close.read_bytes()).hexdigest()
            attached = attach_closing_odds(record, close, expected_sha256=sha)
            self.assertEqual(attached, attach_closing_odds(record, close, expected_sha256=sha))
            self.assertNotIn("under_2_5_goals", attached["quotes"])
            report = evaluate_market_tracking(folder)
            contracts = {e["selection_key"]: e for e in report["by_contract"]}
            self.assertEqual(contracts["home_win"]["mean_raw_clv"], .1)
            self.assertEqual(contracts["over_2_5_goals"]["brier"], .16)
            self.assertEqual(contracts["under_2_5_goals"]["clv_pairs_n"], 0)
            self.assertTrue(all(f["fixtures_n"] == 1 for f in report["by_family"]))
            self.assertEqual(record.read_bytes(), original)
            self.assertEqual(settlement.read_bytes(), settlement_bytes)
            close.write_text(capture + "\n")
            self.assertEqual(evaluate_market_tracking(folder)["counts"]["invalid_closing_snapshot"], 1)
            settled["settlement"]["is_simulation"] = True
            settlement.write_text(json.dumps(settled))
            self.assertFalse(evaluate_market_tracking(folder)["by_contract"])
