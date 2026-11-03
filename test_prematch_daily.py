"""Offline integration tests: actual TXT parsing/hashes, mocked network boundary only."""
import copy
import csv
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_daily as daily
import prematch_runtime as runtime
import prematch_prospective_trial as trial
from test_preflight_timing_v0_3_1 import records, T0

NOW = datetime.fromisoformat('2026-11-02T00:00:00+01:00')


class CalendarTests(unittest.TestCase):
    def test_midnight(self):
        self.assertEqual(daily.automatic_day(NOW,time(16),time(3)),date(2026,11,1))
    def test_window_end(self):
        self.assertEqual(daily.automatic_day(NOW.replace(hour=3),time(16),time(3)),date(2026,11,2))
    def test_before_and_during_today_window(self):
        for hour in (10,16,23):
            self.assertEqual(daily.automatic_day(NOW.replace(hour=hour),time(16),time(3)),date(2026,11,2))
    def test_spring_dst(self):
        before=datetime.fromisoformat('2026-03-29T01:30:00+01:00')
        after=datetime.fromisoformat('2026-03-29T03:30:00+02:00')
        self.assertEqual(daily.automatic_day(before,time(16),time(3)),date(2026,3,28))
        self.assertEqual(daily.automatic_day(after,time(16),time(3)),date(2026,3,29))
    def test_autumn_both_folds(self):
        for offset in ('+02:00','+01:00'):
            now=datetime.fromisoformat('2026-10-25T02:30:00'+offset)
            self.assertEqual(daily.automatic_day(now,time(16),time(3)),date(2026,10,24))
    def test_dry_run_no_api(self):
        with patch.object(trial,'prepare',side_effect=AssertionError('API')), patch.object(daily,'scan',side_effect=AssertionError('browser')):
            self.assertEqual(daily.main(['--dry-run']),0)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'window';self.base=self.folder/'prospective_trial'
        self.base.mkdir(parents=True)
        self.args=SimpleNamespace(start=time(16),end=time(3),min_lead=45,max_scrapes=1,
                                  max_odds_age=120,discovery_retries=2)
        self.fixtures=[{'fixture_id':f,'home':f'Home {f}','away':f'Away {f}',
                        'kickoff_madrid':'2026-11-02T03:00:00+01:00','analysis_cutoff_utc':NOW.isoformat(),
                        'region':'EUROPE'} for f in (1,2,3)]
        # Synthetic, well-covered goal series support real tactical checks; other families remain missing.
        for f in self.fixtures:
            for side in ('home','away'):
                f[side+'_evidence']={'history':{'same_competition_total':12,
                    'same_competition_goals_for':{'n':12,'mean':2,'cv':.2},
                    'same_competition_goals_against':{'n':12,'mean':2,'cv':.2}},'details':{}}
        trial.dump(self.base/'enriched/preselection.json',{'generated_at_utc':NOW.isoformat(),'matches':self.fixtures})
        self.clock=patch.object(trial,'now_madrid',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
        self.prep=patch.object(runtime,'reusable_preparation',return_value={});self.prep.start();self.addCleanup(self.prep.stop)

    def preflight(self, fixture, age=10):
        eid=str(7000000+fixture['fixture_id']);capture=NOW-timedelta(minutes=age)
        url=f'https://www.bwin.es/es/sports/eventos/home-away-2:{eid}'
        event={**fixture,'bwin_event_id':eid,'bwin_url':url,'status':'EVENT_FOUND_NEEDS_TIME_CONFIRMATION',
               'identity_status':'NAME_MATCH_TIME_NOT_MACHINE_VERIFIED','inspection':{'title_teams_match':True}}
        discovery=self.base/'bwin'/f'retry_{fixture["fixture_id"]}'/'bwin_discovery.json'
        trial.dump(discovery,{'generated_at_utc':NOW.isoformat(),'matches':[event], 'catalog_observed':[
            {'bwin_event_id':eid,'bwin_url':url,'text':'Hoy / 03:00', 'text_observed_at_utc':NOW.isoformat()}]})
        txt=self.root/'outputs/matches'/eid/'inputs'/f'cuotas_bwin_{eid}.txt';txt.parent.mkdir(parents=True)
        txt.write_text(f'Event ID: {eid}\nCaptura: {capture.isoformat()}\nAcordeones: 1 | Selecciones: 1\nMERCADO: Total de goles | Tiempo reglamentario\nMás de 2,5 : 1.50\n')
        match={**fixture,'bwin_event_id':eid,'bwin_url':url,'status':'MARKET_TRIPLES_EXTRACTED',
          'time_evidence':{'status':'DISPLAY_TIME_CORROBORATED'},
          'snapshot':{'snapshot_path':str(txt),'sha256':trial.sha(txt),'event_id_verified_in_txt':True,
                      'capture_declared_madrid':capture.isoformat(),'selection_triplets_verified':1},
          'quality_gate':{'join_status':'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF','researchable_after_data_gate':1,
                          'data_status_counts':{'DATA_READY_FOR_RESEARCH':1},'market_kinds':{'total_goals':{'researchable':1}}}}
        path=self.base/'preflight'/str(fixture['fixture_id'])/'market_preflight.json'
        trial.dump(path,{'generated_at_utc':NOW.isoformat(),'source_discovery':str(discovery),'source_preselection':str(self.base/'enriched/preselection.json'),
                        'source_sha256':trial.sha(discovery),'source_preselection_sha256':trial.sha(self.base/'enriched/preselection.json'),
                        'matches':[match]})
        for name in ('market_selection_review.csv','market_research_shortlist.csv'):
            with (path.parent/name).open('w') as handle:
                writer=csv.DictWriter(handle,fieldnames=['fixture_id','bwin_event_id','market','selection','odd','market_kind','data_status']);writer.writeheader()
                writer.writerow({'fixture_id':fixture['fixture_id'],'bwin_event_id':eid,'market':'Total de goles | Tiempo reglamentario',
                                 'selection':'Más de 2,5','odd':'1.50','market_kind':'total_goals','data_status':'DATA_READY_FOR_RESEARCH'})
        return path,match

    def fake_run(self,root,script,args,logs):
        if script!='prematch_opportunity_scanner.py':
            raise trial.TrialError('Playwright timeout')
        paths=[Path(args[i+1]) for i,x in enumerate(args) if x=='--preflight']
        audits=[{'fixture_id':trial.read(p)['matches'][0]['fixture_id'],'status':'VERIFIED','reasons':[]} for p in paths]
        out=Path(args[args.index('--output-dir')+1]);trial.dump(out/'opportunity_scan.json',{'audits':audits,'sources':{'preselection_sha256':trial.sha(self.base/'enriched/preselection.json'),'preflights':[str(p) for p in paths]},'asof_madrid':NOW.isoformat(),'mode':'PREMATCH_SNAPSHOT_SCREEN'})

    def scan(self):
        with patch.object(trial,'run',side_effect=self.fake_run):
            return runtime.scan(self.root,self.folder,date(2026,11,1),self.args)

    def test_one_verified_reused_no_browser(self):
        self.preflight(self.fixtures[0]);doc=self.scan()
        self.assertEqual(doc['status'],'READY_FOR_MANUAL_RESEARCH');self.assertEqual(doc['fixture_id'],1)
        self.assertIn('Prioridad conjunta',doc['routing_reason']);self.assertFalse(doc['actionable_bet'])
        self.assertTrue(Path(doc['context_paths'][0]).is_file())
    def test_two_verified_success_limit(self):
        self.preflight(self.fixtures[0]);self.preflight(self.fixtures[1]);self.args.max_scrapes=2
        doc=self.scan();self.assertEqual(doc['eligible_verified_events'],2);self.assertEqual(doc['fixture_id'],1)
    def test_zero_browser_errors_exhaust_candidates(self):
        doc=self.scan();self.assertEqual(doc['status'],'INSUFFICIENT_EVIDENCE')
        self.assertEqual({x['fixture_id'] for x in doc['discard_reasons']},{1,2,3})
    def test_stale_not_overwritten_next_candidate(self):
        old,_=self.preflight(self.fixtures[0],age=121);self.preflight(self.fixtures[1])
        before=trial.sha(Path(trial.read(old)['matches'][0]['snapshot']['snapshot_path']))
        doc=self.scan();self.assertEqual(doc['fixture_id'],2)
        self.assertTrue(any('STALE_SNAPSHOT' in x['reason'] for x in doc['discard_reasons']))
        self.assertEqual(before,trial.sha(Path(trial.read(old)['matches'][0]['snapshot']['snapshot_path'])))
    def test_changed_sha_rejected(self):
        p,m=self.preflight(self.fixtures[0]);Path(m['snapshot']['snapshot_path']).write_text('corrupted')
        with self.assertRaises(trial.TrialError):
            runtime.verified_preflight(self.root,p,self.fixtures[0],self.base/'enriched/preselection.json',self.args)
    def test_discovery_event_id_conflict(self):
        p,m=self.preflight(self.fixtures[0]);doc=trial.read(p);doc['matches'][0]['bwin_event_id']='7999999';trial.dump(p,doc)
        with self.assertRaisesRegex(trial.TrialError,'EVENT_CONFLICT'):
            runtime.verified_preflight(self.root,p,self.fixtures[0],self.base/'enriched/preselection.json',self.args)
    def test_fixture_conflict(self):
        p,m=self.preflight(self.fixtures[0]);fixture=dict(self.fixtures[0],away='Wrong')
        with self.assertRaisesRegex(trial.TrialError,'IDENTITY'):
            runtime.verified_preflight(self.root,p,fixture,self.base/'enriched/preselection.json',self.args)
    def test_started_never_reaches_browser(self):
        for f in self.fixtures:f['kickoff_madrid']=NOW.isoformat()
        trial.dump(self.base/'enriched/preselection.json',{'generated_at_utc':NOW.isoformat(),'matches':self.fixtures})
        with patch.object(trial,'run',side_effect=AssertionError('No browser')):
            doc=runtime.scan(self.root,self.folder,date(2026,11,1),self.args)
        self.assertEqual(doc['status'],'NO_VALID_CANDIDATE')
    def test_generic_title_retry_only_requested_fixture(self):
        good_path,match=self.preflight(self.fixtures[0]);source=Path(trial.read(good_path)['source_discovery']);good=trial.read(source)
        bad=copy.deepcopy(good);bad['matches'][0]['inspection']['title_teams_match']=False;trial.dump(source,bad)
        seen=[]
        def run(root,script,args,logs):
            seen.append((script,args));trial.dump(Path(args[args.index('--output-dir')+1])/'bwin_discovery.json',good)
        with patch.object(trial,'run',side_effect=run):
            path,event=runtime.discovery_for(self.root,self.base,self.fixtures[0],self.args,[])
        self.assertEqual(len(seen),1);self.assertEqual(seen[0][1][seen[0][1].index('--fixture-id')+1],'1')
        self.assertTrue(event['inspection']['title_teams_match'])
    def test_countdown_scanner_bridge_replays_source(self):
        p,m=self.preflight(self.fixtures[0]);pf=trial.read(p);source=Path(pf['source_discovery']);disc=trial.read(source)
        e=disc['matches'][0];e['identity_status']='NAME_AND_RELATIVE_TIME_CORROBORATED'
        e['inspection'].update(event_id_from_url=e['bwin_event_id'],time_evidence_type='RELATIVE_CATALOG_COUNTDOWN',
            countdown_observed_at_utc=NOW.isoformat(),event_start_from_countdown=e['kickoff_madrid'],countdown_minutes=180)
        disc['catalog_observed'][0]['text']='Home Away 3h 00m restante';trial.dump(source,disc)
        pf['source_sha256']=trial.sha(source);pf['matches'][0]['time_evidence']['status']='COUNTDOWN_TIME_CORROBORATED';trial.dump(p,pf)
        runtime.verified_preflight(self.root,p,self.fixtures[0],self.base/'enriched/preselection.json',self.args)
        disc['matches'][0]['inspection']['event_id_from_url']='7999999';trial.dump(source,disc)
        with self.assertRaises(trial.TrialError):
            runtime.verified_preflight(self.root,p,self.fixtures[0],self.base/'enriched/preselection.json',self.args)
    def test_review_revalidates_identity(self):
        self.preflight(self.fixtures[0]);doc=self.scan();path=self.base/'handoff_prematch.json'
        doc['bwin_event_id']='7999999';trial.dump(path,doc)
        with self.assertRaisesRegex(trial.TrialError,'IDENTITY'):
            daily.review(path,self.root)
    def test_handoff_review_expiry(self):
        self.preflight(self.fixtures[0]);doc=self.scan();path=self.base/'handoff_prematch.json'
        with patch.object(trial,'now_madrid',return_value=NOW+timedelta(hours=4)):
            with self.assertRaisesRegex(trial.TrialError,'EXPIRED'):daily.review(path)
    def test_configured_final_margin(self):
        p,m=self.preflight(self.fixtures[0]);f=dict(self.fixtures[0],kickoff_madrid=(NOW+timedelta(minutes=50)).isoformat())
        doc=runtime.write_handoff(self.root,self.base/'daily_runs/test',[(f,m,p)],[],min_lead=60)
        self.assertEqual(doc['status'],'NO_VALID_CANDIDATE')


if __name__=='__main__':unittest.main()
