"""Offline orchestration regressions; fail on acquisition boundaries."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_daily as daily
import prematch_prospective_trial as trial
import test_prematch_market_mapping_v1 as mapping_tests


class V2IntegrationTests(unittest.TestCase):
    def args(self, **kw):
        return SimpleNamespace(market_mapping=True, market_mapping_patterns=None,
                               market_mapping_preflight=[], **kw)

    def test_same_run_predictor_and_original_preflight_used(self):
        td, source, preflight = mapping_tests.MarketMappingTests().make_inputs()
        self.addCleanup(td.cleanup)
        folder = Path(td.name) / 'window'
        base = folder / 'prospective_trial'
        dest = base / 'daily_runs/run/preflight/1/market_preflight.json'
        dest.parent.mkdir(parents=True)
        dest.write_bytes(preflight.read_bytes())
        (dest.parent/'market_selection_review.csv').write_bytes((preflight.parent/'market_selection_review.csv').read_bytes())
        # A comparison audit copy must not create a duplicate fixture.
        copy = base/'daily_runs/run/comparison_audit/preflight/market_preflight.json'
        copy.parent.mkdir(parents=True); copy.write_bytes(preflight.read_bytes())
        predictor = Path(td.name)/'predictor'; predictor.mkdir()
        (predictor/'predictive_patterns.json').write_bytes(source.read_bytes())
        old = base/'market_mapping_v1/market_mapping.json'
        old.parent.mkdir(); old.write_text('frozen old mapping')
        out, counts = daily.run_optional_market_mapping(Path(td.name), folder, self.args(), predictor)
        self.assertEqual(counts['market_matched'], 1)
        doc = trial.read(out/'market_mapping.json')
        self.assertEqual(len(doc['data_sources']['preflights']), 1)
        self.assertEqual(doc['data_sources']['patterns'], str((predictor/'predictive_patterns.json').resolve()))
        self.assertEqual(old.read_text(), 'frozen old mapping')
        before = (out/'market_mapping.json').read_bytes()
        daily.run_optional_market_mapping(Path(td.name), folder, self.args(), predictor)
        self.assertEqual(before, (out/'market_mapping.json').read_bytes())

    def test_no_implicit_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial.dump(root/'prospective_trial/patterns/pattern_engine.json', {'patterns':[]})
            with self.assertRaisesRegex(trial.TrialError, 'EXPLICIT_FROZEN'):
                daily.run_optional_market_mapping(root, root, self.args())

    def test_mapping_only_never_runs_acquisition(self):
        td, patterns, pf = mapping_tests.MarketMappingTests().make_inputs()
        self.addCleanup(td.cleanup)
        with patch.object(trial, 'prepare', side_effect=AssertionError('API')), \
             patch.object(daily, 'scan', side_effect=AssertionError('Bwin')), \
             patch.object(daily, 'run_optional_predictor', side_effect=AssertionError('predictor')):
            self.assertEqual(daily.main(['--root', td.name, '--date','2026-11-09',
                '--market-mapping-only','--market-mapping-patterns',str(patterns),
                '--market-mapping-preflight',str(pf)]), 0)

    def test_daily_mapping_runs_after_scan(self):
        with tempfile.TemporaryDirectory() as td:
            order = []
            with patch.object(daily, 'reusable_preparation', return_value={}), \
                 patch.object(daily, 'run_optional_predictor', side_effect=lambda *a: order.append('predictor')), \
                 patch.object(daily, 'scan', side_effect=lambda *a: (order.append('scan') or {'status':'NO_VALID_CANDIDATE'})), \
                 patch.object(daily, 'run_optional_market_mapping', side_effect=lambda *a: order.append('mapping')), \
                 patch('prematch_daily_csv.export_daily', return_value=[]):
                self.assertEqual(daily.main(['--root',td,'--date','2026-11-09']), 0)
            self.assertEqual(order, ['predictor','scan','mapping'])

    def test_cached_predictor_sha_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); base = root/'prospective_trial'
            trial.dump(base/'enriched/preselection.json', {'matches':[]})
            run = base/'predictive/run'
            trial.dump(run/'predictive_patterns.json', {'patterns':[]})
            trial.dump(run/'run_manifest.json', {
                'source_preselection_sha256':trial.sha(base/'enriched/preselection.json'),
                'artifacts':{'predictive_patterns.json':{'sha256':'incorrect'}}})
            with self.assertRaisesRegex(trial.TrialError, 'SHA_MISMATCH'):
                daily.run_optional_market_mapping(root, root, self.args())

if __name__ == '__main__':
    unittest.main()
