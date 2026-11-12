"""Offline regressions for observed acquisition defects; never launches a browser."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import prematch_phase1 as phase


class AcquisitionTests(unittest.TestCase):
    def test_all_competitions_data_gate_does_not_relax_commercial_gate(self):
        fixture={'fixture_id':99,'home_id':1,'away_id':2,'home':'Youth A','away':'Youth B',
                 'league_id':3,'season':2099,'kickoff_utc':'2099-11-12T18:00:00Z',
                 'category_status':'EXCLUDED','gender':'UNKNOWN','status':'NS'}
        self.assertNotEqual(phase.api_fixture_verification(fixture)[0],'API_FIXTURE_VERIFIED')
        rows=phase.all_data_candidates({'fixtures':[fixture]})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['category_status'],'EXCLUDED')
        self.assertEqual(rows[0]['gender'],'UNKNOWN')
        self.assertEqual(rows[0]['bwin_binding_status'],'UNVERIFIED')
        fixture['status']='LIVE'
        self.assertEqual(phase.all_data_candidates({'fixtures':[fixture]}),[])

    def test_bwin_failure_retains_api_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source.json';source.write_text('{}')
            out=root/'run'
            client=argparse.Namespace(ledger=[{'status':'API_OK','endpoint':'fixtures'}],
                                      statistics=lambda:{'new_http_calls':2})
            with patch.object(phase,'Phase1ApiClient',return_value=client),\
                 patch.object(phase,'canonical_fixtures',return_value={'fixtures':[]}),\
                 patch.object(phase,'run_bwin_once',side_effect=phase.Phase1Error('browser failed')):
                status=phase.main(['--root',str(root),'--date','2026-11-12','--fixtures-input',str(source),
                    '--output-dir',str(out),'--online','--max-api-calls','2','--discover-bwin','--discovery-only'])
            self.assertEqual(status,2)
            report=json.loads((out/'api_request_ledger_error.json').read_text())
            self.assertEqual(report['api_usage']['new_http_calls'],2)
            self.assertEqual(len(report['operations']),1)

    def test_explicit_browser_forwarded_without_changing_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); executable=root/'chromium'; executable.write_text('test placeholder')
            out=root/'bwin';out.mkdir();(out/'bwin_discovery.json').write_text('{}')
            args=argparse.Namespace(bwin_headless=True,bwin_max_country_pages=30,bwin_timeout=25000,
                                    bwin_browser_executable=executable)
            with patch.object(phase.subprocess,'run',return_value=argparse.Namespace(returncode=0)) as run:
                phase.run_bwin_once(root,root/'fixtures.json',out,args)
            command=run.call_args.args[0]
            self.assertEqual(command[command.index('--browser-executable')+1],str(executable.resolve()))
            self.assertEqual(command[command.index('--top')+1],'0')


if __name__ == '__main__':unittest.main()
