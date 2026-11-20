"""Isolated offline contracts; all provider calls are replaced by local fakes."""
import copy
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bwin_market_preflight as pf
import prematch_opportunity_scanner as scanner
import select_single_match as selector
import prematch_case as case
import preselect_matches as stats
import prematch_pattern_engine as patterns


class ManualFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=datetime.now(timezone.utc)-timedelta(seconds=2)
        self.pre=self.root/'preselection.json';self.discovery=self.root/'bwin_discovery.json'
        self.fixtures=[];events=[];matches=[]
        for fid in (101,102,103):
            eid=str(7000000+fid);url=f'https://www.bwin.es/es/sports/eventos/home{fid}-away{fid}-2:{eid}'
            fixture={'fixture_id':fid,'home':f'Home{fid}','away':f'Away{fid}',
                     'home_id':fid,'away_id':fid+1000,'league_id':9999,'league':'Unknown League',
                     'country':'Unknown','season':2026,'gender':'UNKNOWN','status':'NS',
                     'kickoff_madrid':(self.now+timedelta(hours=4)).isoformat(),
                     'kickoff_utc':(self.now+timedelta(hours=4)).isoformat(),
                     'analysis_cutoff_utc':self.now.isoformat(),'details_checked':True,
                     'data_readiness_score':50+fid-100,'research_priority_score':70+fid-100,'stability_score':55}
            self.fixtures.append(fixture)
            matches.append({**fixture,'status':'EVENT_FOUND_NEEDS_TIME_CONFIRMATION',
                            'identity_status':'NAME_MATCH_TIME_NOT_MACHINE_VERIFIED','candidates_found':1,
                            'bwin_event_id':eid,'bwin_url':url,'inspection':{'title_teams_match':True}})
            events.append({'bwin_event_id':eid,'bwin_url':url,'text':f'Home{fid} Away{fid}',
                           'text_observed_at_utc':self.now.isoformat()})
        self.pre.write_text(json.dumps({'generated_at_utc':self.now.isoformat(),'matches':self.fixtures}))
        self.disc={'generated_at_utc':self.now.isoformat(),'matches':matches,'catalog_observed':events}
        self.discovery.write_text(json.dumps(self.disc));self.out=self.root/'capture'

    def fake_capture(self,root,out,eid,url,**kwargs):
        if eid.endswith('102'):return {'returncode':1,'error':'synthetic per-event timeout'}
        target=out/'captures'/eid/f'cuotas_bwin_{eid}.txt';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(f'Event ID: {eid}\nURL: {url}\nCaptura: {datetime.now(timezone.utc).isoformat()}\n'
                          'Acordeones: 1 | Selecciones: 1\nMERCADO: Resultado del partido | tiempo reglamentario\n'
                          '1 : 1.80\n')
        return {'returncode':0,'fresh_snapshot_path':str(target)}

    def capture(self,budget=0,resume=False):
        args=['--discovery',str(self.discovery),'--preselection',str(self.pre),
              '--root',str(self.root),'--output-dir',str(self.out),'--run-scraper',
              '--confirm-external','--exploration','--refresh-existing','--immutable-capture',
              '--max-captures',str(budget)]
        if resume:args.append('--resume')
        with patch.object(pf,'run_scraper',side_effect=self.fake_capture) as call:
            code=pf.main(args)
        return code,call.call_count,json.loads((self.out/'market_preflight.json').read_text())

    def scan(self):
        out=self.root/'commercial'
        code=scanner.main(['--preselection',str(self.pre),'--preflight',str(self.out/'market_preflight.json'),
                           '--exploration','--output-dir',str(out)])
        self.assertEqual(code,0)
        return out/'opportunity_scan.json'

    def select(self,scan):
        out=self.root/'selection'
        code=selector.main(['--preselection',str(self.pre),'--discovery',str(self.discovery),
                           '--preflight',str(self.out/'market_preflight.json'),'--scanner',str(scan),
                           '--research-only','--output-dir',str(out)])
        self.assertEqual(code,0)
        self.assertFalse((out/'selected_match.json').exists())
        return json.loads((out/'research_selection.json').read_text())

    def test_unknown_metadata_is_explorable_not_strictly_verified(self):
        m=self.disc['matches'][0]
        strict=pf.validate_match(m,self.disc['catalog_observed'],self.now,45,180)
        loose=pf.exploration_check(m,self.disc['catalog_observed'],self.now,45,180)
        self.assertEqual(strict['status'],'BLOCKED')
        self.assertEqual(loose['status'],'READY_FOR_SCRAPER')
        self.assertEqual(set(loose['warnings']),{'TIME_UNCONFIRMED','CATEGORY_UNKNOWN','COMPETITION_UNCONFIRMED'})
        self.assertTrue(loose['final_validation_pending']);self.assertEqual(m['gender'],'UNKNOWN')

    def test_conflicts_and_started_fixture_remain_blocked(self):
        for change in ({'kickoff_madrid':(self.now-timedelta(minutes=1)).isoformat()},
                       {'bwin_url':'https://www.bwin.es/es/sports/eventos/wrong-2:7999999'},
                       {'inspection':{'identity_status':'KICKOFF_MISMATCH_REVIEW'}},
                       {'inspection':{'page_title':'Home101 U19 - Away101 U19'}},
                       {'inspection':{'is_live':True}}, {'candidates_found':2}):
            m={**self.disc['matches'][0],**change}
            self.assertEqual(pf.exploration_check(m,self.disc['catalog_observed'],self.now,45,180)['status'],'BLOCKED',change)

    def test_failed_scrape_does_not_stop_batch_or_discard_failed_candidate(self):
        code,calls,doc=self.capture()
        self.assertEqual(code,0);self.assertEqual(calls,3)
        self.assertEqual([m['status'] for m in doc['matches']],
                         ['MARKET_TRIPLES_EXTRACTED','SCRAPER_FAILED_OR_UNVERIFIED','MARKET_TRIPLES_EXTRACTED'])
        self.assertEqual(doc['batch_summary']['status'],'COMPLETED_WITH_ERRORS')
        report=self.select(self.scan())
        self.assertEqual(report['status'],'SELECTED')
        self.assertEqual(report['comparison']['retryable_capture_failures'],['102'])

    def test_budget_prevents_final_selection_but_keeps_partial_ranking(self):
        self.capture(budget=1)
        report=self.select(self.scan())
        self.assertEqual(report['status'],'INCOMPLETE_COMPARISON');self.assertIsNone(report['selected'])

    def test_budget_and_progressive_resume_preserve_captures_and_market_tables(self):
        _,calls,doc=self.capture(budget=1)
        self.assertEqual(calls,1);self.assertEqual(doc['batch_summary']['pending_fixture_ids'],[102,103])
        original=Path(doc['matches'][0]['snapshot']['snapshot_path']);before=original.read_bytes()
        # An explicit domain exclusion is terminal for this synthetic fixture.
        data=json.loads(self.discovery.read_text());data['matches'][1]['inspection']={'is_live':True}
        self.discovery.write_text(json.dumps(data))
        with self.assertRaises(SystemExit):self.capture(resume=True)
        self.discovery.write_text(json.dumps(self.disc))
        with patch.object(self,'fake_capture',wraps=self.fake_capture):
            _,calls,doc=self.capture(resume=True)
        self.assertEqual(calls,2);self.assertEqual(original.read_bytes(),before)
        self.assertEqual((self.out/'market_selection_review.csv').read_text().count('Home101'),0)
        import csv
        with (self.out/'market_selection_review.csv').open(encoding='utf-8-sig') as fh:
            self.assertEqual(len(list(csv.DictReader(fh))),2)

    def test_completed_research_without_calibrated_model_can_prepare_packet(self):
        # Three healthy synthetic captures, no calls to a provider.
        data=json.loads(self.discovery.read_text());data['matches'][1]['inspection']={'is_live':True}
        self.disc=data;self.discovery.write_text(json.dumps(data))
        self.capture();scan=self.scan();report=self.select(scan)
        self.assertEqual(report['status'],'SELECTED');self.assertEqual(report['selected']['fixture_id'],103)
        self.assertTrue(report['final_validation_pending'])
        self.assertIn('TIME_NOT_CORROBORATED',report['final_validation_reasons'])
        packet=case.prepare_research(self.root/'selection/research_selection.json',self.root/'research')
        self.assertFalse(json.loads(packet.read_text())['actionable_bet'])
        self.assertFalse((self.root/'outputs/matches').exists())

    def test_manual_review_preserves_unknowns_and_does_not_force_verification(self):
        path=self.root/'review.json'
        path.write_text(json.dumps({'source_discovery_sha256':selector.sha256(self.discovery),
            'decisions':[{'fixture_id':101,'decision':'CONFIRM','reviewer':'local test',
                          'reviewed_at':self.now.isoformat(),'evidence':'frozen catalog'}]}))
        reviewed=pf.reviewed_discovery(self.disc,path,selector.sha256(self.discovery))
        self.assertEqual(reviewed['matches'][0]['gender'],'UNKNOWN')
        self.assertEqual(reviewed['matches'][0]['identity_status'],'NAME_MATCH_TIME_NOT_MACHINE_VERIFIED')
        self.assertNotIn('manual_identity_review',self.disc['matches'][0])

    def test_immutable_capture_timeout_preserves_existing_canonical(self):
        target=self.root/'outputs/matches/7000101/inputs/cuotas_bwin_7000101.txt'
        target.parent.mkdir(parents=True);target.write_text('old untouched bytes')
        with patch.object(pf.subprocess,'run',side_effect=subprocess.TimeoutExpired('fake',1)):
            result=pf.run_scraper(self.root,self.out,'7000101',self.disc['matches'][0]['bwin_url'],
                                  refresh=True,immutable=True,timeout_seconds=1)
        self.assertIn('error',result);self.assertEqual(target.read_text(),'old untouched bytes')

    def test_fresh_capture_is_run_owned_even_with_unparseable_old_snapshot(self):
        from types import SimpleNamespace
        target=self.root/'outputs/matches/7000101/inputs/cuotas_bwin_7000101.txt'
        target.parent.mkdir(parents=True);target.write_text('old untouched bytes')
        url=self.disc['matches'][0]['bwin_url']
        def fake_process(command,**kw):
            stage=Path(kw['env']['PREMATCH_EVENT_DATA_DIR']);fresh=stage/target.name
            fresh.write_text(f'Event ID: 7000101\nURL: {url}\nCaptura: {datetime.now(timezone.utc).isoformat()}\n'
                             'Acordeones: 1 | Selecciones: 1\nMERCADO: Total de goles | tiempo reglamentario\nMás de 2,5 : 1.80\n')
            kw['stdout'].write(f'TXT: {fresh}\n')
            return SimpleNamespace(returncode=0)
        with patch.object(pf.subprocess,'run',side_effect=fake_process):
            first=pf.run_scraper(self.root,self.out,'7000101',url,refresh=True,immutable=True)
            second=pf.run_scraper(self.root,self.root/'another_run','7000101',url,refresh=True,immutable=True)
        self.assertIn('fresh_snapshot_path',first)
        self.assertNotEqual(first['fresh_snapshot_path'],second['fresh_snapshot_path'])
        self.assertEqual(target.read_text(),'old untouched bytes')

    def test_statistics_cli_includes_review_without_changing_unknown_metadata(self):
        path=self.root/'fixtures_major.json';pending={**self.fixtures[1],'gender':'UNKNOWN','category_status':'REVIEW'}
        path.write_text(json.dumps({'fixtures':[self.fixtures[0]],'review':[pending],'excluded':[self.fixtures[2]]}))
        report={'counts':{'evaluated':2,'fully_evaluated':0,'api_errors':1},'api_errors':{'one':'missing data'},
                'api_usage':{'requests_sent':0,'cache_hits':0},'matches':[]}
        with patch('config.constants.API_KEY','test'),patch.object(stats,'ApiFootball'), \
             patch.object(stats,'run_evaluator',return_value=report) as evaluate,patch.object(stats,'save_report'):
            code=stats.main(['--input',str(path),'--include-review','--confirm-external','--output-dir',str(self.root/'stats')])
        self.assertEqual(code,0)
        passed=evaluate.call_args.args[0]['fixtures']
        self.assertEqual([f['fixture_id'] for f in passed],[101,102])
        self.assertEqual(passed[1]['gender'],'UNKNOWN')
        self.assertEqual(report['run_status'],'INCOMPLETE')

    def test_auth_error_is_global_and_not_swallowed_as_missing_history(self):
        response=unittest.mock.Mock(status_code=401)
        api=stats.ApiFootball('test','https://example.invalid',self.root)
        with patch.object(api.session,'get',return_value=response):
            with self.assertRaises(stats.GlobalApiError):api.get('fixtures',{'team':1})
        api.close()

    def test_pattern_cli_zero_includes_entire_inventory(self):
        self.pre.write_text(json.dumps({'matches':[{'fixture_id':i} for i in range(25)]}))
        result={'fixtures':[],'patterns':[],'counts':{'fixtures_processed':25,'pattern_rows':0,
                'hypotheses_descriptive':0,'bwin_families_price_evidenced':0,'blocked_candidates':0},
                'asof_madrid':self.now.isoformat()}
        with patch.object(patterns,'analyze',return_value=result) as analyze, \
             patch.object(patterns,'format_report',return_value='synthetic'):
            self.assertEqual(patterns.main(['--preselection',str(self.pre),'--top','0',
                                           '--output-dir',str(self.root/'patterns')]),0)
        self.assertEqual(analyze.call_args.args[3],25)


if __name__=='__main__':unittest.main()
