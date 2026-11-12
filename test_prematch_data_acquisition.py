"""Offline regressions for observed acquisition defects; never launches a browser."""
import argparse
import json
import tempfile
import unittest
import copy
from pathlib import Path
from unittest.mock import patch
import prematch_phase1 as phase


class AcquisitionTests(unittest.TestCase):
    def test_interruption_keeps_a_durable_attempt_record_without_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);journal=root/'requests.jsonl'
            def interrupted(*args):raise KeyboardInterrupt()
            client=phase.Phase1ApiClient(root/'cache',offline=False,max_new_calls=1,
                key='secret-test-key',transport=interrupted,journal_path=journal)
            with self.assertRaises(KeyboardInterrupt):client.get('teams',id=1)
            rows=[json.loads(row) for row in journal.read_text().splitlines()]
            self.assertEqual(rows[0]['status'],'HTTP_ATTEMPT_RESERVED')
            self.assertEqual(rows[0]['http_attempt_number'],1)
            self.assertNotIn('secret-test-key',journal.read_text())

    def test_expired_cache_is_versioned_and_previous_bytes_remain_reproducible(self):
        from datetime import datetime,timedelta,timezone
        now=[datetime(2026,11,12,tzinfo=timezone.utc)]
        payloads=iter([{'response':[{'value':1}],'errors':[]},{'response':[{'value':2}],'errors':[]}])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            client=phase.Phase1ApiClient(root,offline=False,max_new_calls=2,key='test',clock=lambda:now[0],
                transport=lambda *args:(200,{},next(payloads)),min_interval_seconds=0)
            first=client.get('teams',id=1);original=first.cache_path.read_bytes()
            now[0]+=timedelta(seconds=phase.TTL_SECONDS.get('teams',86400)+1)
            second=client.get('teams',id=1)
            self.assertNotEqual(first.cache_path,second.cache_path)
            self.assertEqual(first.cache_path.read_bytes(),original)
            reader=phase.Phase1ApiClient(root,offline=True,clock=lambda:now[0])
            latest=reader.get('teams',id=1)
            self.assertEqual(latest.response,[{'value':2}]);self.assertEqual(reader.new_calls,0)

    def test_batch_embedded_detail_avoids_four_repeated_http_requests(self):
        from datetime import datetime,timezone
        raw={'fixture':{'id':10,'date':'2026-11-01T12:00:00Z','status':{'short':'FT'}},
             'teams':{'home':{'id':1},'away':{'id':2}},'league':{'id':3,'season':2026},
             'statistics':[],'events':[],'lineups':[],'players':[]}
        calls=[]
        def get(endpoint,**params):
            calls.append((endpoint,params));self.assertEqual(endpoint,'fixtures')
            rows=[raw] if 'team' in params or 'ids' in params else []
            return phase.ApiResult(endpoint,{k:str(v) for k,v in params.items()},rows,'API',None,
                '2026-11-12T02:00:00Z','COMPLETE_RESPONSE',{'response':rows})
        from types import SimpleNamespace
        collector=phase.DeepCollector(SimpleNamespace(get=get),history=20,cutoff=datetime(2026,11,12,3,tzinfo=timezone.utc),core_only=True)
        identity={'api_fixture_id':99,'kickoff_utc':'2026-11-12T18:00:00Z','home_team_id':1,'away_team_id':2,'league_id':3,'season':2026}
        result=collector.collect_fixture(identity)
        self.assertEqual(len(calls),4)
        self.assertEqual(result['historical_detail_status']['10']['fixtures/players']['status'],'EMPTY')
        record=collector.records[phase.endpoint_key('fixtures/players',{'fixture':10})]
        self.assertEqual(record['actual_source_endpoint'],'fixtures')
        self.assertEqual(record['source'],'EMBEDDED_FIXTURES_BATCH')

    def test_preflight_rebuilds_header_clock_instead_of_trusting_status(self):
        from bwin_market_preflight import validate_match
        from datetime import datetime,timezone
        header={'prematch_header':True,'browser_timezone':'Europe/Madrid','date_label':'Hoy','time_label':'18:45',
                'capture_started_at_utc':'2026-11-12T02:00:00Z','captured_at_utc':'2026-11-12T02:00:10Z'}
        match={'fixture_id':1,'bwin_event_id':'1234567','bwin_url':'https://www.bwin.es/es/sports/eventos/a-b-2:1234567',
               'kickoff_madrid':'2026-11-12T18:45:00+01:00','status':'IDENTITY_VERIFIED_AUTOMATICALLY',
               'identity_status':'NAME_AND_ABSOLUTE_TIME_MATCH','inspection':{'title_teams_match':True,
               'event_id_from_url':'1234567','event_start_machine_readable':'2026-11-12T18:45:00+01:00',
               'observed_at_utc':'2026-11-12T02:00:10Z','time_evidence_type':'SCOPED_PREMATCH_HEADER',
               'displayed_time_evidence':header}}
        now=datetime(2026,11,12,2,1,tzinfo=timezone.utc)
        good=validate_match(match,[],now,45,120)
        self.assertEqual(good['status'],'READY_FOR_SCRAPER')
        self.assertEqual(good['time_evidence']['source'],'INSPECTED_EVENT_PREMATCH_HEADER')
        header['browser_timezone']='UTC'
        self.assertEqual(validate_match(match,[],now,45,120)['status'],'BLOCKED')

    def test_scoped_prematch_clock_resolves_today_and_tomorrow(self):
        from bwin_discovery import displayed_header_kickoff
        evidence={'prematch_header':True,'browser_timezone':'Europe/Madrid','date_label':'Hoy','time_label':'18:45',
                  'capture_started_at_utc':'2026-11-12T02:00:00Z','captured_at_utc':'2026-11-12T02:00:10Z'}
        self.assertEqual(displayed_header_kickoff(evidence).isoformat(),'2026-11-12T18:45:00+01:00')
        evidence.update(date_label='Mañana',time_label='01:00')
        self.assertEqual(displayed_header_kickoff(evidence).isoformat(),'2026-11-13T01:00:00+01:00')

    def test_scoped_clock_rejects_ambiguous_or_unanchored_evidence(self):
        from bwin_discovery import displayed_header_kickoff
        good={'prematch_header':True,'browser_timezone':'Europe/Madrid','date_label':'Hoy','time_label':'18:45',
              'capture_started_at_utc':'2026-11-12T02:00:00Z','captured_at_utc':'2026-11-12T02:00:10Z'}
        bad=[{'browser_timezone':'UTC'},{'prematch_header':False},{'date_label':'12/11'},
             {'time_label':'25:45'},{'time_label':'02:00'},
             {'capture_started_at_utc':'2026-11-11T22:59:59Z','captured_at_utc':'2026-11-11T23:00:01Z'},
             {'capture_started_at_utc':'2026-10-25T00:00:00Z','captured_at_utc':'2026-10-25T00:00:10Z','time_label':'02:30'}]
        for change in bad:
            with self.subTest(change=change):
                evidence=copy.deepcopy(good);evidence.update(change)
                self.assertIsNone(displayed_header_kickoff(evidence))

    def test_provider_rate_limit_body_and_retry_after_are_respected(self):
        for status,headers,payload,expected_pause in [(200,{}, {'errors':{'rateLimit':'Too many requests'}},60),
                                                      (429,{'Retry-After':'120'},{},120)]:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as td:
                pauses=[];responses=iter([(status,headers,payload),(200,{}, {'response':[{}],'errors':[]})])
                client=phase.Phase1ApiClient(Path(td),offline=False,max_new_calls=2,key='test',
                    transport=lambda *args:next(responses),sleeper=pauses.append)
                result=client.get('teams',id=1)
                self.assertEqual(result.response,[{}]);self.assertIn(expected_pause,pauses)
                self.assertEqual(client.new_calls,2);self.assertEqual(client.retries,1)

    def test_core_pass_defers_optional_context_explicitly(self):
        from datetime import datetime,timezone
        collector=phase.DeepCollector(None,history=5,cutoff=datetime(2026,11,12,tzinfo=timezone.utc),core_only=True)
        identity={'api_fixture_id':99,'kickoff_utc':'2099-11-12T18:00:00Z',
                  'home_team_id':1,'away_team_id':2,'league_id':3,'season':2099}
        with patch.object(collector,'request',return_value=None) as request:
            result=collector.collect_fixture(identity)
        self.assertEqual({call.args[0] for call in request.call_args_list},{'fixtures'})
        self.assertEqual(result['optional_context_status'],'NOT_REQUESTED_CORE_PASS')
        self.assertNotEqual(result['status'],'COMPLETE')

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
                    '--output-dir',str(out),'--online','--confirm-external','--max-api-calls','2','--discover-bwin','--discovery-only'])
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
