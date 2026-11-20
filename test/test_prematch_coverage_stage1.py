"""Stage 1 contracts: all responses synthetic and all transport mocked."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import preselect_matches as p
import prematch_phase1 as phase
from prematch_cache_record import historical_available, result_available_at

NOW=datetime(2026,11,15,20,tzinfo=timezone.utc)

def contract(stats=True,standings=True,kind='League'):
    return p.competition_coverage([{'league':{'id':5,'type':kind},'seasons':[{'year':2026,'coverage':{
        'fixtures':{'statistics_fixtures':stats,'events':True,'lineups':True,'statistics_players':True},
        'standings':standings,'players':True}}]}],5,2026)

def candidate(fid=1,score=50,coverage=None,n=6):
    rows=[{'fixture_id':100+i,'team_id':2,'kickoff_utc':(NOW-timedelta(days=i+1)).isoformat(),
           'league_id':5,'season':2026,'status':'FT','own':{'goals':1,'shots':5,'corners':None},'conceded':{'goals':0,'shots':6}}
          for i in range(n)]
    return {'fixture_id':fid,'league_id':5,'season':2026,'research_priority_score':score,
        'data_readiness_score':50,'preliminary_coverage_score':50,'region_priority':1,
        'kickoff_madrid':(NOW+timedelta(hours=4)).isoformat(),'analysis_cutoff_utc':NOW.isoformat(),
        'competition_coverage':coverage or contract(),
        'statistical_observations':{'home':copy.deepcopy(rows),'away':copy.deepcopy(rows)}}

class CoverageTests(unittest.TestCase):
    def test_complete(self):
        m=candidate();p.apply_coverage_priority(m)
        self.assertEqual(m['statistical_priority']['category'],'ADVANCED')
        self.assertEqual(m['research_priority_score'],57)
        self.assertEqual(m['statistical_priority']['families']['corners']['minimum_series_n'],0)
        self.assertEqual(m['statistical_priority']['families']['players']['status'],'EXPOSURE_NOT_VERIFIED')
    def test_false_retains_secondary(self):
        m=candidate(coverage=contract(False));p.apply_coverage_priority(m)
        self.assertEqual(m['research_priority_score'],50)
        self.assertEqual(m['statistical_priority']['category'],'SECONDARY')
    def test_unknown(self):
        m=candidate(coverage=p.competition_coverage([],5,2026));p.apply_coverage_priority(m)
        self.assertEqual(m['competition_coverage']['capabilities']['statistics_fixtures'],'UNKNOWN')
        self.assertEqual(m['research_priority_score'],50)
    def test_wrong_identity_and_duplicate_contract_unknown(self):
        self.assertEqual(p.competition_coverage([{'league':{'id':7},'seasons':[]}],5,2026)['status'],'UNKNOWN')
    def test_cup_without_standings(self):
        m=candidate(coverage=contract(True,False,'Cup'));p.apply_coverage_priority(m)
        self.assertEqual(m['statistical_priority']['category'],'ADVANCED')
        self.assertEqual(m['statistical_priority']['exception_reason'],'PROVIDER_CUP_FORMAT')
    def test_league_no_table_requires_explicit_exception(self):
        m=candidate(coverage=contract(True,False));p.apply_coverage_priority(m)
        self.assertEqual(m['statistical_priority']['category'],'SECONDARY')
        m['coverage_exception_reason']='PLAYOFF_NO_TABLE';p.apply_coverage_priority(m)
        self.assertEqual(m['statistical_priority']['category'],'ADVANCED')
    def test_true_empty_is_not_advanced(self):
        m=candidate()
        for rows in m['statistical_observations'].values():
            for row in rows:
                row['own'].pop('shots');row['conceded'].pop('shots')
        p.apply_coverage_priority(m)
        self.assertEqual(m['research_priority_score'],50)
        self.assertEqual(m['statistical_priority']['category'],'SECONDARY')
    def test_comparison_and_disabled_rules(self):
        a=candidate(1,50);b=candidate(2,54,p.competition_coverage([],5,2026))
        p.assign_ranks([a,b]);self.assertEqual(b['data_readiness_rank'],1)
        for m in (a,b):p.apply_coverage_priority(m)
        p.assign_ranks([a,b]);self.assertEqual(a['data_readiness_rank'],1)
        for m in (a,b):p.apply_coverage_priority(m,{'enabled':False})
        p.assign_ranks([a,b]);self.assertEqual(b['data_readiness_rank'],1)
    def test_invalid_rules(self):
        for rules in ({'advanced_bonus':float('nan')},{'minimum_sample':0},{'enabled':'false'}):
            with self.assertRaises(p.PreselectionError):p.validate_coverage_rules(rules)
    def test_delayed_result(self):
        row={'kickoff_utc':(NOW-timedelta(hours=1)).isoformat()}
        self.assertFalse(historical_available(row,NOW))
        row['completed_at_utc']=(NOW-timedelta(minutes=5)).isoformat()
        self.assertTrue(historical_available(row,NOW))
        row['completed_at_utc']=(NOW+timedelta(minutes=5)).isoformat()
        self.assertFalse(historical_available(row,NOW))
    def test_frozen_preselection_cannot_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            api=p.ApiFootball('test','https://invalid',Path(td),asof=NOW)
            with patch.object(api.session,'get') as request:
                with self.assertRaises(p.PreselectionError):api.get('leagues',{'id':5,'season':2026})
                request.assert_not_called()
            api.close()
    def test_phase1_after_cutoff_preserved_but_excluded(self):
        result=phase.ApiResult('fixtures',{'id':'1'},[{'ok':True}],'test',None,
            (NOW+timedelta(seconds=1)).isoformat(),'COMPLETE_RESPONSE',{})
        client=SimpleNamespace(get=Mock(return_value=result))
        collector=phase.DeepCollector(client,history=5,cutoff=NOW)
        self.assertIsNone(collector.request('fixtures',{'id':1},scope={'target_kickoff_utc':(NOW+timedelta(hours=4)).isoformat()}))
        self.assertEqual(next(iter(collector.records.values()))['status'],'AFTER_ANALYSIS_CUTOFF')
    def test_phase1_result_not_available_at_cutoff(self):
        row={'fixture':{'id':1,'date':(NOW-timedelta(hours=1)).isoformat(),'status':{'short':'FT'}},
            'teams':{'home':{'id':2},'away':{'id':3}}}
        self.assertEqual(phase.final_historical_rows([row],NOW,99,2,5),[])
    def test_deep_history_does_not_fallback_to_nonfinal(self):
        from generate_context import DeepHistoryCollector, APITeam
        row={'fixture':{'id':1,'date':(NOW-timedelta(days=1)).isoformat(),'status':{'short':'NS'}}}
        api=SimpleNamespace(get=Mock(return_value={'response':[row],'_acquired_at_utc':NOW.isoformat()}))
        result=DeepHistoryCollector(api).collect_team_history(APITeam(2,'Team',None,None,None,False,'M',None,None),5,NOW)
        self.assertEqual(result['fixture_ids'],[])
        self.assertEqual(api.get.call_count,1)

class PipelineTests(unittest.TestCase):
    def evaluate(self,stats,unknown=False,probe=None):
        fixture={'fixture_id':99,'league_id':5,'season':2026,'status':'NS','home_id':2,'away_id':3,
            'kickoff_utc':(NOW+timedelta(hours=5)).isoformat(),'kickoff_madrid':(NOW+timedelta(hours=5)).isoformat(),
            'home':'Home','away':'Away','country':'Test','league':'Test','gender':'M','region':'EUROPE','region_priority':1}
        if probe:fixture['coverage_probe_reason']=probe
        calls=[]
        class Client:
            def get(self,route,params):
                calls.append((route,params))
                if route=='leagues':
                    if unknown:return []
                    return [{'league':{'id':5,'type':'League'},'seasons':[{'year':2026,'coverage':{
                        'fixtures':{'statistics_fixtures':stats},'standings':True}}]}]
                if route=='standings':return []
                if 'team' in params:
                    return [{'fixture':{'id':100+i,'date':(NOW-timedelta(days=i+1)).isoformat(),'status':{'short':'FT'}},
                        'teams':{'home':{'id':2},'away':{'id':3}},'league':{'id':5,'season':2026},'goals':{'home':1,'away':0}}
                        for i in range(6)]
                if 'ids' in params:return []
                raise AssertionError((route,params))
        result=p.run_evaluator({'schema_version':'1.0-major-leagues','fixtures':[fixture]},Client(),NOW)
        return calls,result
    def test_coverage_before_details_and_false_saves_calls(self):
        calls,result=self.evaluate(False)
        self.assertEqual(calls[0][0],'leagues')
        self.assertFalse(any('ids' in params for _,params in calls))
        self.assertEqual(len(result['matches']),1)
        self.assertEqual(result['matches'][0]['statistical_priority']['category'],'SECONDARY')
    def test_unknown_probes_and_empty_remains_secondary(self):
        calls,result=self.evaluate(True,unknown=True)
        self.assertTrue(any('ids' in params for _,params in calls))
        self.assertEqual(result['matches'][0]['statistical_priority']['category'],'SECONDARY')
    def test_false_explicit_probe_allowed(self):
        calls,result=self.evaluate(False,probe='DASHBOARD_RETURNED_STATISTICS')
        self.assertTrue(any('ids' in params for _,params in calls))
    def test_true_empty_keeps_goals_without_detail_validity(self):
        calls,result=self.evaluate(True)
        m=result['matches'][0]
        self.assertEqual(m['statistical_priority']['category'],'SECONDARY')
        self.assertEqual(m['statistical_priority']['families']['goals']['minimum_series_n'],6)
        self.assertEqual(m['statistical_priority']['families']['shots']['minimum_series_n'],0)

class FrozenCacheTests(unittest.TestCase):
    def test_context_cache_observed_after_cutoff_cannot_fallback_to_network(self):
        from generate_context import JSONCache, APIFootballClient
        from prematch_cache_record import acquisition_record
        with tempfile.TemporaryDirectory() as td:
            cache=JSONCache(Path(td));path=cache._path('fixtures',{'id':1});path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(acquisition_record({'response':[{}]},NOW+timedelta(seconds=1))))
            original=path.read_bytes()
            api=APIFootballClient('test',asof=NOW)
            with patch('generate_context.CACHE',cache), patch.object(api.session,'get') as request:
                with self.assertRaises(ValueError):api.get('fixtures',{'id':1})
                request.assert_not_called()
            self.assertEqual(path.read_bytes(),original)
            api.session.close()
    def test_preselection_cache_after_cutoff_excluded(self):
        import hashlib
        from urllib.parse import urlencode
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);params={'id':5,'season':2026}
            key=hashlib.sha256(('leagues?'+urlencode(sorted((str(k),str(v)) for k,v in params.items()))).encode()).hexdigest()
            path=root/'outputs/data/cache/preselect_api'/f'{key}.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'retrieved_at_utc':(NOW+timedelta(seconds=1)).isoformat(),'endpoint':'leagues','parameters':params,'response':[{}]}))
            api=p.ApiFootball('test','https://invalid',root,asof=NOW)
            with patch.object(api.session,'get') as request:
                with self.assertRaises(p.PreselectionError):api.get('leagues',params)
                request.assert_not_called()
            api.close()
    def test_phase1_live_cut_advances_and_never_crosses_kickoff(self):
        acquired=NOW+timedelta(minutes=1)
        result=phase.ApiResult('fixtures',{'id':'1'},[{}],'test',None,acquired.isoformat(),'COMPLETE_RESPONSE',{})
        client=SimpleNamespace(get=Mock(return_value=result),clock=lambda:acquired)
        collector=phase.DeepCollector(client,history=5,cutoff=NOW,live=True)
        self.assertIsNotNone(collector.request('fixtures',{'id':1},scope={'target_kickoff_utc':(NOW+timedelta(hours=1)).isoformat()}))
        self.assertEqual(collector.cutoff,acquired)
        self.assertIsNone(collector.request('fixtures',{'id':2},scope={'target_kickoff_utc':acquired.isoformat()}))

class ProvenanceTests(unittest.TestCase):
    def test_historical_rescore_rejects_known_late_acquisition(self):
        report={'schema_version':'1.3-preselection','generated_at_utc':NOW.isoformat(),
                'api_request_ledger':[{'first_observed_at_utc':(NOW+timedelta(seconds=1)).isoformat()}]}
        with self.assertRaisesRegex(p.PreselectionError,'AFTER_CUTOFF'):p.rescore_existing(report)
    def test_duplicated_observations_do_not_inflate_family_sample(self):
        m=candidate(n=1)
        for side in ('home','away'):m['statistical_observations'][side]*=6
        p.apply_coverage_priority(m)
        self.assertEqual(m['statistical_priority']['families']['shots']['minimum_series_n'],1)
        self.assertEqual(m['statistical_priority']['category'],'SECONDARY')


class CacheVersionTests(unittest.TestCase):
    def test_refresh_preserves_original_bytes(self):
        from prematch_cache_record import preserve_cache_version
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'cache.json';old=b'{"source":"original"}';path.write_bytes(old)
            preserve_cache_version(path);path.write_bytes(b'{"source":"new"}')
            versions=list((path.parent/'versions').glob('*.json'))
            self.assertEqual(len(versions),1);self.assertEqual(versions[0].read_bytes(),old)
