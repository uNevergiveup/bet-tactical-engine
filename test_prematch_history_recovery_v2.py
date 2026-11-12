import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import prematch_history_recovery_v2 as r
import test_prematch_phase2_player_integrity as f

class RecoveryTests(unittest.TestCase):
    def test_explicit_scope_dedup_order_and_external_historical_identity(self):
        root,source,fixture=self.setup();target=json.loads(source.read_text())['matches'][0]
        target['statistical_observations']['home']=[]
        source.write_text(json.dumps({'generated_at_utc':f.CUT.isoformat(),'matches':[target]}))
        ids=[15,10,12,11,14,13]
        docs=[]
        for fid in ids:
            record=json.loads(json.dumps(fixture));record['fixture']['id']=fid;docs.append(record)
        path=root/'outputs/data/cache/preselect_api/history.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'retrieved_at_utc':'2026-11-02T00:00:00Z','response':docs}))
        before=(source.read_bytes(),path.read_bytes())
        with patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK')):
            plan,result=r.recover(source,root,root/'out',fixture_ids=ids+[10,15])
        self.assertEqual(plan['explicit_fixture_ids'],ids)
        self.assertEqual([x['fixture_id'] for x in plan['fixtures']],ids)
        self.assertEqual({fid for t in plan['tasks'] for fid in t['fixture_ids']},set(ids))
        self.assertEqual(plan['tasks'][0]['fixture_ids'],ids)
        self.assertEqual(plan['unique_fixtures'],6);self.assertEqual(result['new_api_calls'],0)
        self.assertEqual(before,(source.read_bytes(),path.read_bytes()))

    def test_explicit_unknown_or_conflicting_identity_fails_before_transport(self):
        root,source,fixture=self.setup()
        for ids in ([999],[0],[-1]):
            with self.assertRaises(ValueError):r.recover(source,root,root/str(ids[0]),fixture_ids=ids)
        bad=json.loads(json.dumps(fixture));bad['teams']['away']['id']=999
        path=root/'outputs/data/cache/preselect_api/history.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'retrieved_at_utc':'2026-11-02T00:00:00Z','response':[bad]}))
        client=r.RecoveryApiClient(root/'api',offline=False,max_new_calls=20,key='test',transport=lambda *a: self.fail('UNAUTHORIZED_TRANSPORT'))
        with self.assertRaisesRegex(ValueError,'EXPLICIT_FIXTURE_IDENTITY'):
            r.recover(source,root,root/'conflict',fixture_ids=[10],online=True,max_calls=20,client=client)
        self.assertEqual(client.new_calls,0)

    def test_explicit_retry_requests_never_escape_scope(self):
        root,source,fixture=self.setup();out=root/'retry'
        path=root/'outputs/data/cache/preselect_api/history.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'retrieved_at_utc':'2026-11-02T00:00:00Z','response':[fixture]}))
        calls=[]
        def transport(endpoint,params,*args):
            self.assertEqual(params,{'ids':'10'});calls.append(params);return 503,{},{}
        client=r.RecoveryApiClient(out/'acquired_cache',offline=False,max_new_calls=2,key='test',transport=transport,sleeper=lambda _:None)
        _,result=r.recover(source,root,out,fixture_ids=[10],online=True,max_calls=2,client=client)
        self.assertEqual(len(calls),2);self.assertEqual(result['new_api_calls'],2)
        self.assertTrue(any(x['state']=='BUDGET_EXHAUSTED' for x in result['results']))

    def test_explicit_resume_budget_and_player_context_no_expansion(self):
        root,source,fixture=self.setup();out=root/'out'
        path=root/'outputs/data/cache/preselect_api/history.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'retrieved_at_utc':'2026-11-02T00:00:00Z','response':[fixture]}))
        directed=root/'discovery';directed.mkdir()
        obs=directed/'historical_observations.json';obs.write_text(json.dumps({'observations':[]}))
        (directed/'manifest.json').write_text(json.dumps({'source':{'sha256':r.sha(source)},'artifacts':{'historical_observations.json':r.sha(obs)}}))
        allowed=self.transport(fixture)
        def transport(endpoint,params,*args):
            self.assertEqual(set(str(params.get('ids',params.get('fixture'))).split('-')),{'10'})
            return allowed(endpoint,params,*args)
        def client():return r.RecoveryApiClient(out/'acquired_cache',offline=False,max_new_calls=2,key='test',transport=transport,sleeper=lambda _:None)
        _,a=r.recover(source,root,out,fixture_ids=[10],player_discovery=directed,online=True,max_calls=2,client=client())
        self.assertLessEqual(a['new_api_calls'],2)
        plan,b=r.recover(source,root,out,fixture_ids=[10],player_discovery=directed,online=True,max_calls=2,resume=True,client=client())
        self.assertLessEqual(b['new_api_calls'],2)
        self.assertEqual(plan['explicit_fixture_ids'],[10]);self.assertEqual(b['player_observations_after'],1)

    def test_player_priority_uses_segment_and_older_missing_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);observations=[]
            for i in range(8):
                observations.append({'entity_type':'PLAYER','team_id':1,'player_id':7,'league_id':99,'season':2026,'gender':'M','team_kind':'CLUB','period':'FULL_TIME','fixture_id':100+i,'kickoff_utc':f'2026-10-{10+i}T00:00:00+00:00','metrics':{'player.shots':1}})
            (root/'historical_observations.json').write_text(json.dumps({'observations':observations}))
            rows=[{'fixture_id':10,'team_id':1,'league_id':99,'season':2026,'gender':'M','kickoff_utc':'2026-10-01T00:00:00+00:00'},
                  {'fixture_id':11,'team_id':1,'league_id':88,'season':2026,'gender':'M','kickoff_utc':'2026-10-01T00:00:00+00:00'},
                  {'fixture_id':12,'team_id':1,'league_id':99,'season':2026,'gender':'M','kickoff_utc':'2026-10-20T00:00:00+00:00'}]
            status={x['fixture_id']:{'identity':'PRESENT_VERIFIED_CONTENT','perspectives':1,'endpoints':{'fixtures/players':{'state':'NOT_QUERIED'}}} for x in rows}
            rank,segments=r.player_priority(root,rows,status)
            self.assertEqual([x['fixture_id'] for x in rank],[10])
            self.assertEqual(segments[0]['training_n'],5)
            self.assertEqual(segments[0]['additional_observations_estimated'],1)

    def setup(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);root=Path(td.name)
        target=f.target();target['statistical_observations']['home']=[dict(f.history(),own={'goals':1},conceded={'goals':0})]
        source=root/'pre.json';source.write_text(json.dumps({'generated_at_utc':f.CUT.isoformat(),'matches':[target]}))
        fixture={'fixture':{'id':10,'date':f.history()['kickoff_utc'],'status':{'short':'FT'}},'league':{'id':99,'season':2026},'teams':{'home':{'id':3},'away':{'id':1}}}
        return root,source,fixture

    def transport(self,fixture):
        def call(endpoint,params,headers,timeout):
            if endpoint=='fixtures':response=[fixture]
            elif endpoint=='fixtures/statistics':response=[{'team':{'id':i},'statistics':[{'type':'Total Shots','value':5}]} for i in (1,3)]
            elif endpoint=='fixtures/players':response=[{'team':{'id':i},'players':[f.player(7 if i==1 else 30)]} for i in (1,3)]
            else:response=[{'team':{'id':i},'startXI':[{'player':{'id':pid}} for pid in range(7 if i==1 else 30,18 if i==1 else 41)]} for i in (1,3)]
            return 200,{}, {'response':response,'errors':[]}
        return call

    def test_legacy_identity_found_without_network_or_input_changes(self):
        root,source,fixture=self.setup();path=root/'outputs/data/cache/preselect_api/legacy.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'retrieved_at_utc':'2026-11-02T00:00:00Z','response':[fixture]}));before=source.read_bytes(),path.read_bytes()
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
            plan,result=r.recover(source,root,root/'out')
        self.assertEqual(plan['identity_present'],1);self.assertEqual(plan['identities_missing'],0);self.assertEqual(result['new_api_calls'],0)
        self.assertEqual(before,(source.read_bytes(),path.read_bytes()))

    def test_budget_resume_reuses_positive_and_preserves_raw(self):
        root,source,fixture=self.setup();out=root/'out'
        def client():return r.RecoveryApiClient(out/'acquired_cache',offline=False,max_new_calls=2,key='test',transport=self.transport(fixture),sleeper=lambda _:None)
        _,a=r.recover(source,root,out,online=True,max_calls=2,client=client())
        self.assertEqual(a['new_api_calls'],2)
        raw_before={p:p.read_bytes() for p in (out/'raw').glob('*.json')}
        _,b=r.recover(source,root,out,online=True,max_calls=2,resume=True,client=client())
        self.assertLessEqual(b['new_api_calls'],2)
        self.assertEqual(b['player_observations_after'],1)
        for p,value in raw_before.items():self.assertEqual(p.read_bytes(),value)

    def test_empty_statistics_short_expiry_and_retry_later(self):
        root,_,_=self.setup();now=[datetime(2026,11,9,tzinfo=timezone.utc)];calls=[]
        def transport(*args):
            calls.append(1);return 200,{}, {'response':[] if len(calls)==1 else [{'team':{'id':i},'statistics':[{'type':'Total Shots','value':0}]} for i in (1,3)],'errors':[]}
        client=r.RecoveryApiClient(root/'cache',offline=False,max_new_calls=2,key='test',transport=transport,clock=lambda:now[0])
        client.get('fixtures/statistics',fixture=10);client.get('fixtures/statistics',fixture=10)
        self.assertEqual(len(calls),1)
        now[0]+=timedelta(seconds=301)
        self.assertTrue(r.complete(client.get('fixtures/statistics',fixture=10).response,'fixtures/statistics'))
        self.assertEqual(len(calls),2)

    def test_status_evolution_not_identity_conflict_and_wrong_venue_rejected(self):
        _,_,fixture=self.setup();row=f.history()
        self.assertTrue(r.identity_matches(fixture,row))
        old=json.loads(json.dumps(fixture));old['fixture']['status']['short']='NS'
        self.assertTrue(r.identity_matches(old,row,False));self.assertFalse(r.identity_matches(old,row))
        self.assertFalse(r.identity_matches(fixture,dict(row,venue='HOME')))

    def test_grouped_request_and_cutoff_label(self):
        root,source,fixture=self.setup();out=root/'out'
        client=r.RecoveryApiClient(out/'acquired_cache',offline=False,max_new_calls=4,key='test',transport=self.transport(fixture))
        plan,result=r.recover(source,root,out,online=True,max_calls=4,client=client)
        self.assertEqual(plan['tasks'][0]['params'],{'ids':'10'})
        self.assertEqual(result['player_observations_after'],1)
        self.assertIn('RETROSPECTIVE',result['historical_usage'])

    def test_http_failures_respect_budget_and_remain_pending(self):
        root,source,_=self.setup();out=root/'out'
        client=r.RecoveryApiClient(out/'acquired_cache',offline=False,max_new_calls=2,key='test',transport=lambda *a:(503,{},{}),sleeper=lambda _:None)
        _,result=r.recover(source,root,out,online=True,max_calls=2,client=client)
        self.assertEqual(result['new_api_calls'],2)
        self.assertTrue(any(x['state']=='BUDGET_EXHAUSTED' for x in result['results']))
        self.assertEqual(result['player_observations_after'],0)

if __name__=='__main__':unittest.main()
