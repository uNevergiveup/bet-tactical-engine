"""Offline regressions for isolated daily-evaluation snapshots."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_prospective_trial as trial


class DailyEvaluationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.window = Path(self.temp.name) / 'outputs/discovery/2026-11-12_1500_0300'
        self.day = date(2026, 11, 12)
        self.args = SimpleNamespace(
            start=time(15), end=time(3), horizon_hours=3,
            evaluation_budget=4, discovery_budget=2,
            max_matches=5, bwin_top=5, history=20, details=8,
            min_lead=45, competition_scope='adult-open', team=[])

    def reference(self, hour):
        return datetime.fromisoformat(f'2026-11-12T{hour:02d}:00:00+01:00')

    def make_resumable(self, folder):
        raw = {'schema_version': '1.0-discovery', 'fixtures': []}
        trial.dump(folder / 'fixtures.json', raw)
        major = {'schema_version': '1.0-major-leagues',
                 'source_sha256': trial.sha(folder / 'fixtures.json'), 'fixtures': []}
        trial.dump(folder / 'fixtures_major.json', major)
        subset = {**major, 'trial_subset': {
            'source_sha256': trial.sha(folder / 'fixtures_major.json'), 'count': 0}}
        trial.dump(folder / 'prospective_trial/fixtures_major_test.json', subset)
        (folder / 'prospective_trial/enrichment_batches').mkdir(parents=True)

    def test_two_completed_evaluations_same_window_different_start_times_are_isolated(self):
        first, mode = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(16))
        self.assertEqual(mode, 'NEW_ISOLATED_EVALUATION')
        trial.dump(first / 'prospective_trial/prospective_test_summary.json', {'stage': 'PREPARED'})
        manifest_before = (first / 'evaluation_manifest.json').read_bytes()

        second, mode = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(18))
        self.assertEqual(mode, 'NEW_ISOLATED_EVALUATION')
        self.assertNotEqual(first, second)
        self.assertEqual((first / 'evaluation_manifest.json').read_bytes(), manifest_before)
        self.assertFalse((second / 'prospective_trial/prospective_test_summary.json').exists())

    def test_horizon_or_budget_change_never_resumes_previous_snapshot(self):
        first, _ = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(16))
        self.make_resumable(first)
        changed = SimpleNamespace(**vars(self.args))
        changed.horizon_hours = 6
        changed.evaluation_budget = 7
        second, mode = trial.resolve_evaluation(self.window, self.day, changed, self.reference(17))
        self.assertEqual(mode, 'NEW_ISOLATED_EVALUATION')
        self.assertNotEqual(first, second)

    def test_same_request_resumes_intact_frozen_snapshot(self):
        first, _ = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(16))
        self.make_resumable(first)
        frozen_hash = trial.sha(first / 'prospective_trial/fixtures_major_test.json')
        resumed, mode = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(17))
        self.assertEqual((resumed, mode), (first, 'RESUME_FROZEN_EVALUATION'))
        self.assertEqual(trial.sha(resumed / 'prospective_trial/fixtures_major_test.json'), frozen_hash)

    def test_prepare_technical_handoff_keeps_intact_snapshot_resumable(self):
        first, _ = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(16))
        self.make_resumable(first)
        trial.dump(first / 'prospective_trial/handoff_prematch.json', {
            'status': 'TECHNICAL_FAILURE',
            'discard_reasons': [{'phase': 'prepare', 'reason': 'INTERRUPTED_BATCH'}]})
        resumed, mode = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(17))
        self.assertEqual((resumed, mode), (first, 'RESUME_FROZEN_EVALUATION'))

    def test_modified_frozen_source_is_rejected_not_replaced(self):
        first, _ = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(16))
        self.make_resumable(first)
        original_manifest = (first / 'evaluation_manifest.json').read_bytes()
        trial.dump(first / 'fixtures.json', {'schema_version': '1.0-discovery', 'fixtures': [{'fixture_id': 9}]})
        with self.assertRaisesRegex(trial.TrialError, 'RESUME_FROZEN_SOURCE_IDENTITY_MISMATCH'):
            trial.resolve_evaluation(self.window, self.day, self.args, self.reference(17))
        self.assertEqual((first / 'evaluation_manifest.json').read_bytes(), original_manifest)
        self.assertEqual(len(list((self.window / 'evaluations').iterdir())), 1)

    def test_latest_evaluation_falls_back_to_legacy_layout(self):
        self.window.mkdir(parents=True)
        self.assertEqual(trial.latest_evaluation(self.window), self.window)
        first, _ = trial.resolve_evaluation(self.window, self.day, self.args, self.reference(16))
        self.assertEqual(trial.latest_evaluation(self.window), first)

    def test_execution_reference_is_captured_once(self):
        args = SimpleNamespace()
        with patch.object(trial, 'now_madrid', side_effect=[self.reference(16), self.reference(17)]) as clock:
            first = trial.execution_reference(args)
            second = trial.execution_reference(args)
        self.assertEqual(first, second)
        self.assertEqual(first, self.reference(16))
        self.assertEqual(clock.call_count, 1)


if __name__ == '__main__':
    unittest.main()
