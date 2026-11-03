"""Window/coverage regression. All fixtures created by tests stay in temp folders."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_daily as daily
import prematch_prospective_trial as trial
import prematch_runtime as runtime
from prematch_audit import CandidateLedger
import test_prematch_daily as existing
from test_prematch_daily import NOW

ROOT=Path(__file__).resolve().parent
OBS=datetime.fromisoformat('2026-11-02T01:29:00+01:00')


class WindowBoundaryTests(unittest.TestCase):
    def test_active_window_table_both_profiles(self):
        for end in (time(2),time(3)):
            for clock,expected in [('00:00:00',date(2026,11,1)),('01:29:00',date(2026,11,1)),
                (end.strftime('%H:%M:%S'),date(2026,11,2)),('16:00:00',date(2026,11,2))]:
                with self.subTest(end=end,clock=clock):
                    now=datetime.fromisoformat('2026-11-02T'+clock+'+01:00')
                    self.assertEqual(trial.automatic_day(now,time(16),end),expected)
                    self.assertEqual(daily.automatic_day(now,time(16),end),expected)
        self.assertEqual(trial.automatic_day(datetime.fromisoformat('2026-11-02T02:59:59+01:00'),time(16),time(3)),date(2026,11,1))
    def test_trial_default_uses_active_window(self):
        with patch.object(trial,'now_madrid',return_value=OBS),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trial.main(['--show-config']),0)
        self.assertIn('2026-11-01_1600_0200',output.getvalue())
    def test_daily_manual_date_and_horizon(self):
        with patch.object(trial,'now_madrid',return_value=OBS),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(daily.main(['--date','2026-11-02','--horizon-hours','0','--dry-run']),0)
        self.assertIn('2026-11-02_1600_0200',output.getvalue())
    def test_daily_default_never_promotes_next_window(self):
        with patch.object(trial,'now_madrid',return_value=OBS),contextlib.redirect_stdout(io.StringIO()) as output:
            daily.main(['--dry-run'])
        self.assertIn('2026-11-01_1600_0200',output.getvalue())
    def test_dst_elapsed_minutes_not_wall_clock(self):
        now=datetime(2026,10,25,2,40,tzinfo=trial.TZ,fold=0)
        fixture={'kickoff_madrid':'2026-10-25T02:30:00+01:00'}
        self.assertTrue(trial.kickoff_valid(fixture,now,45))
        self.assertIsNone(trial.scope_reason(fixture,now,45,1))
    def test_dst_spring_missing_clock_normalized(self):
        _,finish=trial.local_window(date(2026,3,28),time(16),time(2))
        self.assertEqual(finish.isoformat(),'2026-03-29T03:00:00+02:00')
    def test_horizon_and_lead_boundaries(self):
        for mins,expected in [(44.99,False),(45,True),(180,True),(180.01,False),(840,False)]:
            f={'status':'NS','kickoff_madrid':(OBS+timedelta(minutes=mins)).isoformat()}
            self.assertEqual(trial.scope_reason(f,OBS,45,3) is None,expected)
        self.assertIsNone(trial.scope_reason({'status':'NS','kickoff_madrid':(OBS+timedelta(hours=14)).isoformat()},OBS,45,0))
    def test_discovery_records_excluded_fixtures_individually(self):
        from discover_fixtures import filter_fixtures
        cache=trial.read(ROOT/'outputs/data/cache/fixtures_discovery/2026-11-02_madrid.json')
        valid=copy.deepcopy(next(r for r in cache['payload']['response'] if r['fixture']['id']==1490590))
        # Synthetic NS boundary case. Live API cache may now mark this FT;
        # real historical discovery is checked separately below.
        valid['fixture'].update(date='2026-11-02T02:30:00+01:00',status={'short':'NS'})
        outside=copy.deepcopy(valid);outside['fixture'].update(id=9990001,date='2026-11-02T06:00:00+01:00')
        past=copy.deepcopy(valid);past['fixture'].update(id=9990002,date='2026-11-02T00:00:00+01:00')
        audit=[];begin,end=trial.local_window(date(2026,11,1),time(16),time(3))
        selected,counts,warnings=filter_fixtures([valid,outside,past],begin,end,OBS.astimezone(timezone.utc),audit)
        self.assertEqual([f['fixture_id'] for f in selected],[1490590])
        self.assertEqual(len(audit),3)
        self.assertEqual(audit[1]['discovery_exclusion'],'OUTSIDE_OPERATIONAL_WINDOW')
        self.assertEqual(audit[2]['discovery_exclusion'],'KICKOFF_ALREADY_PASSED')
    def test_invalid_horizons_and_conflicting_manual_dates(self):
        for args in [['--horizon-hours','nan'],['--horizon-hours','-1'],['--date','2026-11-02','--next-window']]:
            with self.subTest(args=args),self.assertRaises(SystemExit):daily.main(args+['--dry-run'])


class HistoricalEvidenceTests(unittest.TestCase):
    def test_lafc_was_sixth_before_old_top_five_cut(self):
        folder=ROOT/'outputs/discovery/2026-11-01_1600_0300'
        major=trial.read(folder/'fixtures_major.json')
        pre=trial.read(folder/'prospective_trial/enriched/preselection.json')
        at=trial.dt(pre['generated_at_utc'])
        # Replay data selection only; no historical handoff or betting artifact is generated.
        full=trial.select_fixtures(major,[],0,at,45,0)
        self.assertEqual([f['fixture_id'] for f in full].index(1490590),5)
        self.assertNotIn(1490590,[f['fixture_id'] for f in trial.select_fixtures(major,[],5,at,45,0)])
        self.assertNotIn(1490590,[f['fixture_id'] for f in pre['matches']])
    def test_at_0129_near_scope_keeps_lafc(self):
        major=trial.read(ROOT/'outputs/discovery/2026-11-01_1600_0300/fixtures_major.json')
        self.assertIn(1490590,[f['fixture_id'] for f in trial.select_fixtures(major,[],0,OBS,45,3)])
        future=trial.read(ROOT/'outputs/discovery/2026-11-02_1600_0300/fixtures_major.json')
        self.assertNotIn(1509212,[f['fixture_id'] for f in trial.select_fixtures(future,[],0,OBS,45,3)])
    def test_incomplete_legacy_preparation_not_reused(self):
        args=SimpleNamespace(start=time(16),end=time(3),min_lead=45,horizon_hours=3,team=[])
        with patch.object(trial,'now_madrid',return_value=OBS):
            with self.assertRaisesRegex(trial.TrialError,'SCOPE_INCOMPLETE'):
                runtime.reusable_preparation(ROOT,ROOT/'outputs/discovery/2026-11-01_1600_0300',date(2026,11,1),args)
    def test_real_bwin_catalog_link_not_invented(self):
        rows=trial.read(ROOT/'outputs/discovery/2026-11-01_1600_0200/bwin_v04_mls/bwin_discovery.json')['catalog_observed']
        matches=[r for r in rows if 'minnesota' in r.get('text','').lower()]
        self.assertEqual(matches[0]['bwin_event_id'],'7899660')
        self.assertIn('2:7899660',matches[0]['bwin_url'])


class QuotePolicyTests(unittest.TestCase):
    def test_fourteen_hours_early_cannot_pass_final(self):
        with self.assertRaisesRegex(trial.TrialError,'REFRESH_REQUIRED'):
            daily.final_odds_check({'kickoff_madrid':(OBS+timedelta(hours=14)).isoformat(),'capture_madrid':OBS.isoformat()},OBS)
    def test_old_capture_still_rejected_near_kickoff(self):
        with self.assertRaisesRegex(trial.TrialError,'REFRESH_REQUIRED'):
            daily.final_odds_check({'kickoff_madrid':(OBS+timedelta(minutes=30)).isoformat(),'capture_madrid':(OBS-timedelta(hours=13)).isoformat(),'max_odds_age_minutes':1000},OBS)
    def test_fresh_prematch_capture_passes_quote_check(self):
        daily.final_odds_check({'kickoff_madrid':(OBS+timedelta(minutes=30)).isoformat(),'capture_madrid':(OBS-timedelta(minutes=10)).isoformat()},OBS)
    def test_post_kickoff_or_future_capture_blocked(self):
        for ko,cap in [(OBS,OBS-timedelta(minutes=1)),(OBS+timedelta(minutes=30),OBS+timedelta(minutes=1))]:
            with self.assertRaises(trial.TrialError):daily.final_odds_check({'kickoff_madrid':ko.isoformat(),'capture_madrid':cap.isoformat()},OBS)


class BatchedPreparationTests(unittest.TestCase):
    def test_seven_candidates_all_enriched_and_discovered_with_batch_size_two(self):
        from preselect_matches import save_report
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'outputs/discovery/2026-11-01_1600_0300';base=folder/'prospective_trial'
            fixture_template=trial.read(ROOT/'outputs/discovery/2026-11-01_1600_0300/fixtures_major.json')['fixtures'][-1]
            fixtures=[dict(fixture_template,fixture_id=i,home=f'Test Home {i}',away=f'Test Away {i}',
                       kickoff_madrid=(OBS+timedelta(minutes=60+i)).isoformat(),kickoff_utc=(OBS+timedelta(minutes=60+i)).astimezone(timezone.utc).isoformat()) for i in range(1,8)]
            fixture_template=trial.read(ROOT/'outputs/discovery/2026-11-01_1600_0300/prospective_trial/enriched/preselection.json')
            seed=fixture_template['matches'][0];bwin_ids=[];batch_sizes=[]
            def run(root,script,args,logs):
                if script=='discover_fixtures.py':
                    trial.dump(folder/'fixtures.json',{'schema_version':'1.0-discovery','generated_at_utc':OBS.isoformat(),'fixtures':fixtures})
                elif script=='filter_major_leagues.py':
                    trial.dump(folder/'fixtures_major.json',{'schema_version':'1.0-major-leagues','generated_at_utc':OBS.isoformat(),
                        'source_sha256':trial.sha(folder/'fixtures.json'),'fixtures':fixtures,'counts':{}})
                elif script=='preselect_matches.py':
                    inp=Path(args[args.index('--input')+1]);dest=Path(args[args.index('--output-dir')+1]);batch=trial.read(inp)['fixtures'];batch_sizes.append(len(batch))
                    doc=copy.deepcopy(fixture_template);doc['matches']=[]
                    for f in batch:
                        m=copy.deepcopy(seed);m.update({k:f[k] for k in ('fixture_id','home','away','kickoff_madrid')});doc['matches'].append(m)
                    doc['source_filter_sha256']=trial.sha(inp);doc['generated_at_utc']=OBS.isoformat();doc['counts']['evaluated']=len(batch)
                    doc['api_usage']={'requests_sent':0,'cache_hits':0};save_report(doc,dest)
                elif script=='prematch_pattern_engine.py':
                    pre=Path(args[args.index('--preselection')+1]);dest=Path(args[args.index('--output-dir')+1])
                    trial.dump(dest/'pattern_engine.json',{'asof_madrid':OBS.isoformat(),'data_sources':{'preselection_sha256':trial.sha(pre)}})
                elif script=='bwin_discovery.py':
                    ids=[int(args[i+1]) for i,a in enumerate(args) if a=='--fixture-id'];bwin_ids.extend(ids)
                    self.assertLessEqual(len(ids),2)
                    dest=Path(args[args.index('--output-dir')+1]);trial.dump(dest/'bwin_discovery.json',{'matches':[dict(f,status='NOT_FOUND_IN_SCANNED_PAGES') for f in fixtures if f['fixture_id'] in ids]})
                else:raise AssertionError(script)
            args=SimpleNamespace(start=time(16),end=time(3),min_lead=45,horizon_hours=3,team=[],max_matches=2,bwin_top=2,history=20,details=8)
            with patch.object(trial,'now_madrid',return_value=OBS),patch.object(trial,'check_files'),patch.object(trial,'run',side_effect=run):
                trial.prepare(root,folder,date(2026,11,1),args)
                self.assertEqual(len(runtime.reusable_preparation(root,folder,date(2026,11,1),args)['candidate_fixtures']),7)
            self.assertEqual(batch_sizes,[2,2,2,1]);self.assertEqual(set(bwin_ids),set(range(1,8)))
            pre=trial.read(base/'enriched/preselection.json');self.assertEqual(len(pre['matches']),7)
            ledger=trial.read(base/'candidate_ledger.json');self.assertEqual(len(ledger['fixtures']),7)
            self.assertTrue(all(r['state']=='SIN_BWIN' for r in ledger['fixtures']))


class ScanScopeTests(unittest.TestCase):
    setUp=existing.PipelineTests.setUp
    preflight=existing.PipelineTests.preflight
    fake_run=existing.PipelineTests.fake_run
    scan=existing.PipelineTests.scan
    def test_all_verified_candidates_scanned_before_selection_by_default(self):
        self.args.max_scrapes=0
        for i,f in enumerate(self.fixtures):
            f['data_readiness_score']=i*10
            for side in ('home','away'):
                for direction in ('for','against'):f[side+'_evidence']['history']['same_competition_goals_'+direction]['cv']=.3-.1*i
            self.preflight(f)
        trial.dump(self.base/'enriched/preselection.json',{'generated_at_utc':NOW.isoformat(),'matches':self.fixtures})
        # Updating fixture scores changes input SHA: regenerate authentic source references.
        for p in (self.base/'preflight').rglob('market_preflight.json'):
            doc=trial.read(p);doc['source_preselection_sha256']=trial.sha(self.base/'enriched/preselection.json');trial.dump(p,doc)
        doc=self.scan();self.assertEqual(doc['eligible_verified_events'],3);self.assertEqual(doc['fixture_id'],3)
        ledger=trial.read(Path(doc['candidate_ledger']))['fixtures']
        self.assertEqual(sum(any(h['state']=='ANALIZADO' for h in r['history']) for r in ledger),3)
    def test_explicit_capture_budget_is_reported(self):
        for f in self.fixtures:self.preflight(f)
        doc=self.scan();self.assertTrue(doc['comparison_incomplete_due_to_budget'])
        states=[r['state'] for r in trial.read(Path(doc['candidate_ledger']))['fixtures']]
        self.assertEqual(states.count('DIFERIDO_POR_PRESUPUESTO'),2)
    def test_far_future_never_reaches_browser_in_daily_scope(self):
        for f in self.fixtures:f['kickoff_madrid']=(NOW+timedelta(hours=14)).isoformat()
        trial.dump(self.base/'enriched/preselection.json',{'generated_at_utc':NOW.isoformat(),'matches':self.fixtures})
        with patch.object(trial,'run',side_effect=AssertionError('outside horizon')):
            doc=runtime.scan(self.root,self.folder,date(2026,11,1),self.args)
        self.assertEqual(doc['status'],'NO_VALID_CANDIDATE')
        self.assertTrue(all(r['reason']=='OUTSIDE_NEAR_TERM_HORIZON' for r in trial.read(Path(doc['candidate_ledger']))['fixtures']))


if __name__=='__main__':unittest.main()
