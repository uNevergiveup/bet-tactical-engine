import unittest

from prematch_retrospective_audit import audit_signal, extract_observed, validate_result


class RetrospectiveAuditTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "fixture_id": 10, "kickoff_madrid": "2026-11-07T18:00:00+01:00",
            "home_id": 1, "away_id": 2,
        }
        self.result = {
            "fixture": {"id": 10, "date": "2026-11-07T17:00:00+00:00"},
            "teams": {"home": {"id": 1}, "away": {"id": 2}},
            "goals": {"home": 2, "away": 1},
        }

    def test_identity_and_timezone_are_verified(self):
        validate_result(self.source, self.result)
        wrong = {**self.result, "teams": {"home": {"id": 2}, "away": {"id": 1}}}
        with self.assertRaisesRegex(ValueError, "HOME_ID_CONFLICT"):
            validate_result(self.source, wrong)

    def test_statistics_are_mapped_by_team_id_and_null_is_not_zero(self):
        stats = [
            {"team": {"id": 2}, "statistics": [{"type": "Corner Kicks", "value": None}]},
            {"team": {"id": 1}, "statistics": [{"type": "Corner Kicks", "value": 7}]},
        ]
        observed, issues = extract_observed(self.source, self.result, stats)
        self.assertEqual([], issues)
        self.assertEqual(7.0, observed["home"]["corners"])
        self.assertIsNone(observed["away"]["corners"])

    def test_descriptive_signal_is_never_retrofitted_as_success(self):
        signal = {
            "signal_id": "10:home:corners", "team_side": "home", "team_id": 1,
            "metric": "corners", "family": "corners", "status": "SUPPORTED_DESCRIPTIVE_SIGNAL",
            "own": {"n": 5, "mean": 6.0, "fixture_ids": [1, 2, 3, 4, 5]},
            "opponent_conceded": {"n": 5, "mean": 5.0, "fixture_ids": [6, 7, 8, 9, 11]},
        }
        row = audit_signal(signal, {"home": {"corners": 9.0}}, "FT")
        self.assertEqual("NO_EVALUABLE_NO_PREDECLARED_THRESHOLD", row["evaluation_status"])
        self.assertIsNone(row["threshold"])
        self.assertIsNone(row["fulfilled"])

    def test_nonfinal_match_never_uses_live_result(self):
        signal = {"signal_id": "x", "team_side": "home", "team_id": 1, "metric": "goals", "family": "goals", "own": {}, "opponent_conceded": {}}
        row = audit_signal(signal, {"home": {"goals": 3.0}}, "2H")
        self.assertEqual("PENDING_MATCH_NOT_FINAL", row["evaluation_status"])


if __name__ == "__main__":
    unittest.main()
