"""Objective 7 regressions over frozen 12/11 artifacts; never use network."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bwin_discovery
import prematch_opportunity_scanner as scanner
import prematch_pattern_discovery as discovery
import prematch_prospective_trial as trial
import prematch_runtime as runtime
from prematch_tactical_signal_engine import load_expanded_statistical_dossiers


ROOT=Path(__file__).resolve().parents[1]
RECON=ROOT/'outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json'
BASE=ROOT/'outputs/discovery/2026-11-12_1500_0300/prospective_trial'
RUN=BASE/'daily_runs/20261112T175850794404'
UEFA={1528958,1528959,1528960,1528961,1528962,1528963,1528964,1545613}


class Objective7Tests(unittest.TestCase):
    def test_all_eight_uefa_persisted_bindings_are_exact(self):
        document=trial.read(RECON);fixtures=[r for r in document['fixtures'] if r['fixture_id'] in UEFA]
        bindings,audit=bwin_discovery.verified_identity_candidates(RECON,fixtures)
        self.assertEqual(set(bindings),UEFA);self.assertEqual(audit['usable_bindings'],8)
        self.assertEqual(bindings[1528960]['bwin_event_id'],'7905471')
        self.assertEqual(bindings[1528961]['bwin_event_id'],'7905479')
        self.assertEqual(bindings[1528962]['bwin_event_id'],'7905480')

    def test_objective6_1_dossiers_are_cutoff_compatible_not_commercial(self):
        fixtures=[trial.read(ROOT/f'outputs/control/runs/objective6_STATISTICAL_BASELINE_PREMATCH_v1/{fid}.json')['fixture']
                  for fid in sorted(UEFA)]
        loaded,source=load_expanded_statistical_dossiers(
            ROOT,fixtures,datetime.fromisoformat('2026-11-12T17:00:00+01:00'))
        self.assertEqual(set(map(int,loaded)),UEFA);self.assertEqual(source['fixtures_reused'],8)
        self.assertTrue(all(not row['commercial_use'] and row['qualitative_only'] for row in loaded.values()))
        self.assertTrue(all(row['primary_signal_count']==48 for row in loaded.values()))

    def test_frozen_commercial_replay_preserves_no_false_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=discovery.commercial_link(
                BASE/'patterns_v2/research_queue_sport.json',RUN/'opportunity/opportunity_scan.json',
                BASE/'bwin/bwin_discovery.json',Path(tmp))
        self.assertEqual(sum(result['counts'].values()),464)
        self.assertEqual(sum(x['verified_quote_link'] for x in result['links']),0)
        # The source-hash defect is gone; remaining blocks are semantic/coverage.
        self.assertFalse(any('SHA_MISSING_OR_CONFLICT' in x['commercial_validation_reasons']
                             for x in result['links']))

    def test_scanner_domain_blocks_exit_zero_technical_exception_exits_two(self):
        completed={'mode':'PREMATCH_SNAPSHOT_SCREEN','counts':{},
                   'audits':[{'fixture_id':1,'status':'BLOCKED','reasons':['SHORT_LEAD']}]}
        with patch.object(scanner,'produce',return_value=completed):
            self.assertEqual(scanner.main(['--preselection','x','--preflight','y','--output-dir','z']),0)
        with patch.object(scanner,'produce',side_effect=ValueError('broken')):
            self.assertEqual(scanner.main(['--preselection','x','--preflight','y','--output-dir','z']),2)

    def test_handoff_distinguishes_legitimate_no_bet_and_technical_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run'
            domain=runtime.write_handoff(root,run,[],[{'phase':'coverage','reason':'INSUFFICIENT'}])
            self.assertEqual(domain['status'],'INSUFFICIENT_EVIDENCE')
            self.assertEqual(domain['run_outcome'],'NO_BET_LEGITIMATE')
            technical=runtime.write_handoff(root,root/'run2',[],[
                {'phase':'scanner','failure_class':'TECHNICAL','reason':'EXIT_2'}])
            self.assertEqual(technical['status'],'TECHNICAL_FAILURE')


if __name__=='__main__':unittest.main()
