"""Offline regression tests for operation-specific PREMATCH time windows."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_daily as daily
import prematch_multi as multi
import prematch_runtime as runtime
import prematch_single as single
import prematch_prospective_trial as trial


class TemporalWindowTests(unittest.TestCase):
    def test_preparation_keeps_t45_but_refresh_path_allows_t31(self):
        now = datetime.fromisoformat('2026-11-06T20:29:00+01:00')
        ctx = {'kickoff': datetime.fromisoformat('2026-11-06T21:00:00+01:00'),
               'selection_time': datetime.fromisoformat('2026-11-06T14:00:00+01:00')}
        with patch.object(single, 'local_now', return_value=now):
            with self.assertRaisesRegex(ValueError, 'requieren 45'):
                single.ensure_prematch(ctx, min_lead=45)
            self.assertAlmostEqual(single.ensure_prematch(ctx, min_lead=5), 31.0, places=5)

    def test_t_plus_one_is_blocked_for_every_prematch_operation(self):
        now = datetime.fromisoformat('2026-11-06T21:01:00+01:00')
        ctx = {'kickoff': datetime.fromisoformat('2026-11-06T21:00:00+01:00'),
               'selection_time': datetime.fromisoformat('2026-11-06T14:00:00+01:00')}
        with patch.object(single, 'local_now', return_value=now):
            with self.assertRaisesRegex(ValueError, 'ya empezó'):
                single.ensure_prematch(ctx, min_lead=5)
            with self.assertRaisesRegex(ValueError, 'kickoff pasó'):
                single.freshness(ctx, min_lead=5)

    def test_timezone_age_uses_same_instant_not_labelled_utc(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cuotas_bwin_7902458.txt'
            path.write_text(
                'Event ID: 7902458\n'
                'URL: https://www.bwin.es/es/sports/eventos/sevilla-alaves-2:7902458\n'
                'Captura: 2026-11-06T15:13:21+01:00\n'
                'MERCADO: Resultado del partido | Tiempo reglamentario\n'
                'Sevilla : 1.85\n', encoding='utf-8')
            ctx = {'event_id': '7902458',
                   'kickoff': datetime.fromisoformat('2026-11-06T21:00:00+01:00')}
            now = datetime.fromisoformat('2026-11-06T20:28:00+01:00')
            with patch.object(single, 'local_now', return_value=now):
                snap = single.snapshot_info(path, ctx, max_age=320)
            self.assertAlmostEqual(snap['age_minutes'], 314.65, places=2)
            self.assertLess(snap['age_minutes'], 373)
            with patch.object(single, 'local_now', return_value=now):
                with self.assertRaisesRegex(ValueError, 'Cuotas antiguas'):
                    single.snapshot_info(path, ctx, max_age=300)

    def test_final_odds_t5_allowed_and_after_kickoff_rejected(self):
        doc = {'kickoff_madrid': '2026-11-06T21:00:00+01:00',
               'capture_madrid': '2026-11-06T20:56:00+01:00',
               'final_odds_policy': {'capture_within_kickoff_minutes': 60,
                                     'max_age_minutes': 15}}
        daily.final_odds_check(doc, datetime.fromisoformat('2026-11-06T20:57:00+01:00'))
        with self.assertRaisesRegex(trial.TrialError, 'FINAL_ODDS_REFRESH_REQUIRED'):
            daily.final_odds_check(doc, datetime.fromisoformat('2026-11-06T21:01:00+01:00'))

    def test_review_gate_is_separate_from_original_preparation_gate(self):
        self.assertEqual(daily.review_minimum_lead({'minimum_lead_minutes': 45}), 5)
        self.assertEqual(daily.review_minimum_lead({'minimum_lead_minutes': 45,
                                                   'review_minimum_lead_minutes': 30}), 30)
        self.assertEqual(daily.review_minimum_lead({'review_minimum_lead_minutes': 0}), 5)

    def test_handoff_publishes_operation_windows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(trial, 'now_madrid', return_value=datetime.fromisoformat('2026-11-06T12:00:00+01:00')):
                doc = runtime.write_handoff(root, root / 'run', [], [], min_lead=45, max_odds_age=120)
            self.assertEqual(doc['minimum_lead_minutes'], 45)
            self.assertEqual(doc['review_minimum_lead_minutes'], 5)
            self.assertEqual(doc['refresh_minimum_lead_minutes'], 5)
            self.assertEqual(doc['finish_minimum_lead_minutes'], 5)
            self.assertEqual(doc['operation_windows']['preparation']['minimum_lead_minutes'], 45)
            self.assertEqual(doc['operation_windows']['finish']['minimum_lead_minutes'], 5)

    def test_direct_multi_operations_use_pinned_event_kickoff_and_injected_clock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context = root / 'identity.json'
            context.write_text(json.dumps({'bwin_event_id': '7902458',
                                           'api_fixture_id': 1570450,
                                           'kickoff_utc': '2026-11-06T20:00:00+00:00'}), encoding='utf-8')
            state = {'event_id': '7902458',
                     'url': 'https://www.bwin.es/es/sports/eventos/sevilla-alaves-2:7902458',
                     'inputs': {'identity': {'path': str(context)}}}
            t31 = datetime.fromisoformat('2026-11-06T19:29:00+00:00')
            self.assertAlmostEqual(multi.ensure_event_operation_window(root, '7902458', state,
                                                                        operation='finish', now=t31), 31.0)
            with self.assertRaisesRegex(ValueError, 'AFTER_KICKOFF'):
                multi.ensure_event_operation_window(root, '7902458', state,
                                                    operation='retry-context',
                                                    now=datetime.fromisoformat('2026-11-06T20:01:00+00:00'))
            # A failed retry may have removed manifest references; the
            # event-scoped identity file remains a safe clock source.
            state_without_reference = {'event_id': '7902458', 'inputs': {}}
            event_identity = root / 'outputs' / 'matches' / '7902458' / 'inputs' / 'match_identity.json'
            event_identity.parent.mkdir(parents=True)
            event_identity.write_text(context.read_text(encoding='utf-8'), encoding='utf-8')
            self.assertAlmostEqual(multi.ensure_event_operation_window(
                root, '7902458', state_without_reference, now=t31), 31.0)


if __name__ == '__main__':
    unittest.main()
