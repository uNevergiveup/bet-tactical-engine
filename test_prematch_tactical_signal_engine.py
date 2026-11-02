"""Tactical engine tests. Services are never called; sample data are synthetic."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

import prematch_prospective_trial as t
from prematch_tactical_signal_engine import (analyze_fixture,filtered_observations,profile_evidence,
    classify_market,market_links,run_engine,selection_rows,diversified_order,stats)
from prematch_tactical_history import LocalHistoryCache
import test_prematch_daily as helpers

NOW=datetime.fromisoformat('2026-11-02T02:03:00+01:00')


def fixture():
    return {'fixture_id':900001,'home':'Home','away':'Away','home_id':1,'away_id':2,
            'league_id':100,'season':2026,'kickoff_madrid':(NOW+timedelta(hours=5)).isoformat(),
            'analysis_cutoff_utc':NOW.isoformat()}


def observations(n=12):
    data={}
    for side,team in [('home',1),('away',2)]:
        data[side]=[{'fixture_id':team*1000+i,'team_id':team,'opponent_id':50+i,
            'kickoff_utc':(NOW-timedelta(days=i+1)).isoformat(),'league_id':100,'season':2026,
            'venue':'HOME' if i%2 else 'AWAY','status':'FT','statistics_requested':True,
            'own':{'goals':2+i%2*.1,'corners':5+i%2*.1,'shots':12+i%2,'on_target':4+i%2*.1,
                   'fouls':12+i%2,'yellow':2+i%2*.1,'offsides':2+i%2*.1,'xg':1.8+i%2*.1,'blocked_shots':3+i%2},
            'conceded':{'goals':2+i%2*.1,'corners':5+i%2*.1,'shots':12+i%2,'on_target':4+i%2*.1,
                   'fouls':12+i%2,'yellow':2+i%2*.1,'offsides':2+i%2*.1,'xg':1.8+i%2*.1}}
            for i in range(n)]
    return data


class SignalTests(unittest.TestCase):
    def test_complete_multiple_market_families(self):
        x=analyze_fixture(fixture(),NOW,observations())
        self.assertEqual(x['status'],'EVALUATED');self.assertEqual(x['signal_count'],14)
        self.assertEqual(set(x['signal_families']),{'goals','corners','shots','cards','fouls','offsides'})
        self.assertEqual(x['signals'][0]['own']['n'],12)
        self.assertGreater(x['priority_score'],80)
    def test_missing_is_not_zero(self):
        data=observations()
        for r in data['home']:r['own']['corners']=None
        x=analyze_fixture(fixture(),NOW,data)
        sig=next(s for s in x['signals'] if s['metric']=='corners' and s['team_side']=='home')
        self.assertEqual(sig['own']['n'],0);self.assertIsNone(sig['own']['mean']);self.assertEqual(sig['own']['missing_n'],12)
        self.assertIn('shots',x['signal_families'])
    def test_small_sample_limited(self):
        x=analyze_fixture(fixture(),NOW,observations(3))
        self.assertEqual(x['signal_count'],0)
        self.assertTrue(all(s['status']=='SMALL_SAMPLE' for s in x['signals']))
    def test_unstable_goals_dont_veto_corners(self):
        data=observations()
        for side in data:
            for i,r in enumerate(data[side]):r['own']['goals']=0 if i%2 else 8;r['conceded']['goals']=0 if i%2 else 8
        x=analyze_fixture(fixture(),NOW,data)
        self.assertNotIn('goals',x['signal_families']);self.assertIn('corners',x['signal_families'])
    def test_contradictory_means(self):
        data=observations()
        for r in data['away']:r['conceded']['corners']=1
        x=analyze_fixture(fixture(),NOW,data)
        sig=next(s for s in x['signals'] if s['metric']=='corners' and s['team_side']=='home')
        self.assertIn('OWN_PRODUCTION_VS_RIVAL_CONCESSION_DISAGREE',sig['contradictions'])
        self.assertNotEqual(sig['status'],'SUPPORTED_DESCRIPTIVE_SIGNAL')
    def test_opposite_trends(self):
        data=observations()
        for i,r in enumerate(data['home']):r['own']['shots']=18 if i<5 else 12
        for i,r in enumerate(data['away']):r['conceded']['shots']=8 if i<5 else 12
        x=analyze_fixture(fixture(),NOW,data)
        self.assertIn('OPPOSITE_RECENT_TRENDS',x['contradictions'])
    def test_no_profiles_is_explicit_and_not_blocking(self):
        x=analyze_fixture(fixture(),NOW,observations())
        self.assertEqual(x['profile_status'],'TACTICAL_PROFILE_UNAVAILABLE');self.assertGreater(x['signal_count'],0)
    def test_post_kickoff_blocked(self):
        f=fixture();f['kickoff_madrid']=NOW.isoformat()
        x=analyze_fixture(f,NOW,observations());self.assertEqual(x['status'],'BLOCKED');self.assertEqual(x['signals'],[])
    def test_post_cutoff_record_and_wrong_team_excluded(self):
        f=fixture();rows=observations()['home'];rows[0]['kickoff_utc']=(NOW+timedelta(minutes=1)).isoformat();rows[1]['team_id']=999
        clean,bad=filtered_observations(rows,f,'home',NOW)
        self.assertEqual(len(clean),10);self.assertEqual({r['reason'] for r in bad},{'POST_CUTOFF_OBSERVATION','OBSERVATION_TEAM_ID_MISMATCH'})
    def test_duplicate_and_other_competition_not_mixed(self):
        f=fixture();rows=observations()['home'];rows.append(copy.deepcopy(rows[0]));rows[1]['league_id']=101;rows[2]['season']=2025
        clean,bad=filtered_observations(rows,f,'home',NOW)
        self.assertEqual(len(clean),10);self.assertEqual(len(bad),3)
    def test_requested_coverage_vs_history_breadth(self):
        data=observations(20)
        for side in data:
            for r in data[side][8:]:r['statistics_requested']=False;r['own']={'goals':2};r['conceded']={'goals':2}
        x=analyze_fixture(fixture(),NOW,data);sig=next(s for s in x['signals'] if s['metric']=='shots')
        self.assertEqual(sig['own']['coverage_pct'],100);self.assertEqual(sig['own']['history_not_detailed_n'],12)
        self.assertEqual(sig['own']['n'],8)
    def test_corner_association_not_causal(self):
        sig=next(s for s in analyze_fixture(fixture(),NOW,observations())['signals'] if s['metric']=='corners')
        self.assertEqual(sig['volume_associations'][0]['paired_n'],12)
        self.assertEqual(sig['volume_associations'][0]['limitation'],'ASSOCIATION_NOT_PRESSING_OR_CAUSATION')
        self.assertIn('NO_CORNER_SCORING_EFFICIENCY_INFERRED',sig['limitations'])
    def test_zeros_observed_are_valid(self):
        self.assertEqual(stats([0,0,0])['n'],3);self.assertEqual(stats([0,0,0])['mean'],0)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.p=Path(self.temp.name)/'profiles.json'
        self.claim={'claim_id':'c1','entity_type':'team','entity_id':1,'team_id':1,'claim_type':'DOCUMENTED_ROUTINE',
            'source':'Synthetic test source','observed_at':(NOW-timedelta(days=2)).isoformat(),
            'valid_from':(NOW-timedelta(days=1)).isoformat(),'valid_until':(NOW+timedelta(days=2)).isoformat(),
            'evidence_level':'CONFIRMADO','markets':['corners'],'claim':'Synthetic documented claim'}
    def evaluate(self,f=None):
        t.dump(self.p,{'schema_version':'1.0-tactical-profiles','version':1,'claims':[self.claim]})
        return profile_evidence(self.p,f or fixture(),NOW)
    def test_valid_profile(self):
        accepted,status,rejected=self.evaluate();self.assertEqual(len(accepted),1);self.assertEqual(status,'TACTICAL_PROFILE_AVAILABLE')
    def test_expired_profile(self):
        self.claim['valid_until']=NOW.isoformat();a,s,r=self.evaluate();self.assertEqual(a,[]);self.assertEqual(r[0]['reason'],'PROFILE_EXPIRED_OR_FUTURE')
    def test_coach_change(self):
        self.claim.update(coach_dependent=True,coach_id=10);f=fixture();f['coach_ids']={'1':11}
        a,s,r=self.evaluate(f);self.assertEqual(a,[]);self.assertEqual(r[0]['reason'],'COACH_CHANGED_PROFILE_INVALIDATED')
    def test_coach_unknown(self):
        self.claim.update(coach_dependent=True,coach_id=10);a,s,r=self.evaluate()
        self.assertEqual(a,[]);self.assertEqual(r[0]['reason'],'COACH_REGIME_UNVERIFIED')
    def test_player_id_unverified(self):
        self.claim.update(entity_type='player',entity_id=999);a,s,r=self.evaluate()
        self.assertEqual(a,[]);self.assertEqual(r[0]['reason'],'PLAYER_ID_OR_EXPOSURE_UNVERIFIED')
    def test_corner_routine_does_not_boost_corner_generation(self):
        self.evaluate();x=analyze_fixture(fixture(),NOW,observations(),self.p)
        self.assertTrue(all(s['components']['profile']==0 for s in x['signals'] if s['metric']=='corners'))
    def test_explicit_generation_claim_can_support_same_metric(self):
        self.claim.update(claim_type='CORNER_GENERATION_VOLUME',target_metric='corners',effect_scope='PRODUCTION')
        self.evaluate();x=analyze_fixture(fixture(),NOW,observations(),self.p)
        self.assertEqual(next(s for s in x['signals'] if s['metric']=='corners' and s['team_side']=='home')['components']['profile'],1)
    def test_inferred_not_confirmed_bonus(self):
        self.claim['evidence_level']='INFERIDO';self.evaluate()
        x=analyze_fixture(fixture(),NOW,observations(),self.p)
        self.assertTrue(all(s['components']['profile']==0 for s in x['signals']))


class MarketTests(unittest.TestCase):
    setUp=helpers.PipelineTests.setUp
    preflight=helpers.PipelineTests.preflight
    def run_link(self,alter=None,age=10):
        f=self.fixtures[0];p,m=self.preflight(f,age)
        if alter:
            d=t.read(p);alter(d);t.dump(p,d)
        x=analyze_fixture(f,helpers.NOW,{},None)
        return x,market_links(f,x['signals'],self.base/'enriched/preselection.json',[p],helpers.NOW,120)
    def test_scanner_failure_produces_safe_handoff_without_selection(self):
        import prematch_runtime as runtime
        from datetime import date
        self.preflight(self.fixtures[0])
        with patch.object(t,'run',side_effect=t.TrialError('scanner failure')):
            result=runtime.scan(self.root,self.folder,date(2026,11,1),self.args)
        self.assertEqual(result['status'],'INSUFFICIENT_EVIDENCE');self.assertNotIn('fixture_id',result)
    def test_real_quote_link_with_literal_selection(self):
        x,a=self.run_link();self.assertEqual(a['verified_preflights'],1)
        markets=[q for s in x['signals'] for q in s['markets']]
        self.assertEqual(markets[0]['odd'],1.5);self.assertEqual(markets[0]['selection'],'Más de 2,5')
        self.assertEqual(markets[0]['fixture_id'],1)
    def test_wrong_fixture_identity_blocks(self):
        x,a=self.run_link(lambda d:d['matches'][0].update(home='Different Team'))
        self.assertEqual(a['verified_preflights'],0);self.assertIn('HOME_MISMATCH',a['block_reasons'])
    def test_stale_snapshot_blocks(self):
        x,a=self.run_link(age=121);self.assertEqual(a['verified_preflights'],0);self.assertIn('STALE_SNAPSHOT',a['block_reasons'])
    def test_no_market_is_pending_not_invented(self):
        x=analyze_fixture(self.fixtures[0],helpers.NOW,{})
        a=market_links(self.fixtures[0],x['signals'],self.base/'enriched/preselection.json',[],helpers.NOW,120)
        self.assertEqual(a['verified_preflights'],0);self.assertTrue(all(s['market_status']=='PENDIENTE_MERCADO' for s in x['signals']))
    def test_live_period_player_and_card_points_rejected(self):
        f=fixture()
        for name in ('Total de goles | Tiempo reglamentario en directo','Total de goles | Tiempo reglamentario | Live','Resto del partido | Tiempo reglamentario','Total de goles - 1ª parte | Tiempo reglamentario',
                     'Total de tarjetas | Tiempo reglamentario','Jugador - Total de tiros | Tiempo reglamentario'):
            self.assertIsNone(classify_market(name,f))
        self.assertEqual(classify_market('Home - Total de córners | Tiempo reglamentario',f),('corners','home'))
    def test_matching_sha_with_wrong_bwin_event_still_blocked(self):
        x,a=self.run_link(lambda d:d['matches'][0].update(bwin_event_id='7999999'))
        self.assertEqual(a['verified_preflights'],0)


class CacheAndIntegrationTests(unittest.TestCase):
    def test_local_history_availability_and_sha(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'outputs/data/cache/preselect_api/example.json'
            raw={'fixture':{'id':100,'date':(NOW-timedelta(days=3)).isoformat(),'status':{'short':'FT'}},
                 'teams':{'home':{'id':1},'away':{'id':3}},'goals':{'home':2,'away':1},'league':{'id':100,'season':2026}}
            t.dump(p,{'retrieved_at_utc':(NOW+timedelta(minutes=1)).isoformat(),'response':[raw]})
            self.assertEqual(LocalHistoryCache(td,NOW).for_team(1,100,NOW),[])
            t.dump(p,{'retrieved_at_utc':(NOW-timedelta(hours=1)).isoformat(),'response':[raw]})
            cache=LocalHistoryCache(td,NOW);rows=cache.for_team(1,100,NOW)
            self.assertEqual(len(rows),1);self.assertEqual(cache.sources[str(p)]['sha256'],t.sha(p))
    def test_slot_budget_is_diverse_and_does_not_drop_ids(self):
        fs=[dict(fixture(),fixture_id=i,kickoff_madrid=f'2026-11-02T{h:02}:00:00+01:00') for i,h in enumerate((15,15,15,18,20))]
        result=diversified_order(fs)
        self.assertEqual([r['fixture_id'] for r in result[:3]],[0,3,4]);self.assertEqual(len(result),5)
    def test_selector_does_not_use_quote_count_or_coverage_alone(self):
        f=fixture();f2=dict(f,fixture_id=900002)
        x=analyze_fixture(f,NOW,observations());x['market_validation']={'verified_preflights':1,'block_reasons':[]}
        for s in x['signals']:s['markets']=[{'status':'VERIFIED_QUOTE_NO_CALIBRATED_MODEL'}]
        bad=copy.deepcopy(x);bad['signals']=[];bad['signal_count']=0;bad['data_coverage']=100;bad['priority_score']=100
        scan={'audits':[{'fixture_id':f['fixture_id'],'status':'VERIFIED'},{'fixture_id':f2['fixture_id'],'status':'VERIFIED'}]}
        ranks=selection_rows([f2,f],{str(f['fixture_id']):x,str(f2['fixture_id']):bad},scan,NOW)
        self.assertEqual(ranks[0]['fixture_id'],f['fixture_id']);self.assertTrue(ranks[0]['eligible']);self.assertFalse(ranks[1]['eligible'])
    def test_json_report_and_csv_preserve_unevaluated_rows(self):
        from prematch_daily_csv import export_daily
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);base=root/'outputs/discovery/2026-11-02_1500_0300/prospective_trial';pre=base/'enriched/preselection.json'
            f=fixture();f['kickoff_madrid']='2026-11-02T18:00:00+01:00';f['statistical_observations']=observations()
            t.dump(pre,{'generated_at_utc':NOW.isoformat(),'matches':[f]})
            other=dict(f,fixture_id=900099);t.dump(base.parent/'fixtures.json',{'fixtures':[f,other]})
            d=run_engine(pre,None,base/'tactical',root,asof=NOW,historical=True,use_cache=False)
            self.assertIsNone(d['selected_fixture_id']);self.assertTrue((base/'tactical/TACTICAL_SELECTION_REPORT.md').is_file())
            self.assertTrue(Path(d['fixtures'][str(f['fixture_id'])]['detail_json']).is_file())
            rows=export_daily(base);self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['tactical_engine_evaluado'],'true');self.assertEqual(rows[1]['tactical_engine_evaluado'],'false')
            self.assertLess(len(rows[0]['detalle_evidencia']),500)
    def test_pattern_hash_mismatch_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'pre.json';pattern=Path(td)/'pattern.json'
            t.dump(p,{'generated_at_utc':NOW.isoformat(),'matches':[]});t.dump(pattern,{'data_sources':{'preselection_sha256':'wrong'}})
            with self.assertRaisesRegex(ValueError,'PATTERN_PRESELECTION_SHA_MISMATCH'):
                run_engine(p,pattern,Path(td)/'out',td,asof=NOW,use_cache=False)
    def test_budget_increase_cannot_reuse_incomplete_subset_silently(self):
        from types import SimpleNamespace
        from datetime import date,time
        from prematch_runtime import reusable_preparation
        # A scope invariant must not depend on today's mutable discovery files.
        with tempfile.TemporaryDirectory() as td,patch.object(t,'now_madrid',return_value=NOW):
            root=Path(td);folder=root/'outputs/discovery/2026-11-02_1500_0300';base=folder/'prospective_trial'
            fs=[dict(fixture(),fixture_id=i,kickoff_madrid='2026-11-02T18:00:00+01:00') for i in (1,2,3)]
            t.dump(folder/'fixtures.json',{'fixtures':fs})
            t.dump(folder/'fixtures_major.json',{'schema_version':'1.0-major-leagues','source_sha256':t.sha(folder/'fixtures.json'),'fixtures':fs})
            t.dump(base/'fixtures_major_test.json',{'fixtures':fs[:2],'trial_subset':{'source_sha256':t.sha(folder/'fixtures_major.json')}})
            pre=base/'enriched/preselection.json'
            t.dump(pre,{'generated_at_utc':NOW.isoformat(),'source_filter_sha256':t.sha(base/'fixtures_major_test.json'),'matches':fs[:2]})
            t.dump(base/'patterns/pattern_engine.json',{'data_sources':{'preselection_sha256':t.sha(pre)}})
            begin,end=t.local_window(date(2026,11,2),time(15),time(3))
            t.dump(base/'prospective_test_summary.json',{'day':'2026-11-02','window_start_madrid':begin.isoformat(),'window_end_madrid':end.isoformat(),
                'evaluation_deferred_ids':[3],'discovery_deferred_ids':[2]})
            args=SimpleNamespace(start=time(15),end=time(3),min_lead=45,horizon_hours=0,team=[],evaluation_budget=2,discovery_budget=1)
            reusable_preparation(root,folder,date(2026,11,2),args)
            args.evaluation_budget=0
            with self.assertRaisesRegex(t.TrialError,'PREPARATION_SCOPE_INCOMPLETE'):
                reusable_preparation(root,folder,date(2026,11,2),args)
            args.evaluation_budget=2;args.discovery_budget=0
            with self.assertRaisesRegex(t.TrialError,'DISCOVERY_SCOPE_INCOMPLETE'):
                reusable_preparation(root,folder,date(2026,11,2),args)
    def test_subprocess_stage_timing_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'stage.py').write_text('print("synthetic test stage")')
            t.run(root,'stage.py',[],root/'logs')
            record=json.loads((root/'logs/pipeline_stage_timings.jsonl').read_text())
            self.assertEqual(record['exit_code'],0);self.assertGreaterEqual(record['elapsed_seconds'],0)
    def test_future_preselection_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'pre.json';t.dump(p,{'generated_at_utc':(NOW+timedelta(minutes=1)).isoformat(),'matches':[]})
            with self.assertRaisesRegex(ValueError,'PRESELECTION_UNDATED_OR_AFTER_ASOF'):
                run_engine(p,None,Path(td)/'out',td,asof=NOW,use_cache=False)

if __name__=='__main__':unittest.main()
