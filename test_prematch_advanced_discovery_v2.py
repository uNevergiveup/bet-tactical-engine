import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import prematch_advanced_discovery_v2 as engine
import test_prematch_phase2_player_integrity as fixtures


def observations(n=100):
    rows=[]
    for i in range(n):
        high=i%2
        outcome=high if i%13 else 1-high
        rows.append({'fixture_id':1000+i,'team_id':1,'opponent_id':2,'player_id':None,
            'entity_type':'TEAM','league_id':99,'season':2025,'gender':'M','team_kind':'CLUB','period':'FULL_TIME',
            'kickoff_utc':(datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(days=i)).isoformat(),
            'metrics':{'own.new_alpha':high,'own.new_beta':outcome,'conceded.some_metric':high},
            'conditions':{'venue':'HOME' if high else 'AWAY'}})
    return rows


class AdvancedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.network=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.network.start();self.addCleanup(self.network.stop)

    def test_exact_test_and_multiplicity(self):
        self.assertAlmostEqual(engine.fisher(4,0,0,4),2/70)
        self.assertEqual(engine.bh([1,1,1]),[1,1,1])
        self.assertTrue(all(x>=y for x,y in zip(engine.by_adjust([.01,.2,1]),engine.bh([.01,.2,1]))))

    def test_new_relationships_three_classes_counterexamples_and_holdout(self):
        patterns,_=engine.discover(observations(),[])
        self.assertEqual({p['category'] for p in patterns},{'OWN','CROSSED','CONDITIONAL'})
        for pattern in patterns:
            self.assertFalse(pattern['predictive_use']['authorized'])
            if pattern['category']=='CROSSED':
                self.assertFalse(pattern['predictive_use']['feature_available_prematch'])
                self.assertEqual(pattern['predictive_use']['reason'],'SAME_MATCH_POST_KICKOFF_VARIABLE')
        conditional=[p for p in patterns if p['category']=='CONDITIONAL' and p['variables']['outcome']=='own.new_beta'][0]
        self.assertTrue(conditional['evidence_fixture_ids'])
        self.assertTrue(conditional['counterevidence_fixture_ids'])
        self.assertFalse(set(conditional['train']['fixture_ids']) & set(conditional['holdout']['fixture_ids']))
        self.assertEqual(conditional['validation_status'],'TEMPORALLY_VALIDATED_ASSOCIATION')

    def test_training_threshold_is_not_fitted_to_future(self):
        a,_=engine.discover(observations(),[])
        changed=observations()
        for row in changed[70:]:row['metrics']['own.new_beta']=1000
        b,_=engine.discover(changed,[])
        byid={p['pattern_id']:p for p in b}
        for p in a:
            self.assertEqual(p['variables'],byid[p['pattern_id']]['variables'])
            self.assertEqual(p['train'],byid[p['pattern_id']]['train'])

    def test_competitions_categories_and_players_stay_separate(self):
        rows=observations(20)
        for i,row in enumerate(rows):
            row['league_id']=1 if i<10 else 2
            row['entity_type']='PLAYER';row['player_id']=7 if i%2 else 8
            row['team_kind']='NATIONAL_TEAM' if i%3 else 'CLUB'
        patterns,_=engine.discover(rows,[])
        self.assertEqual(patterns,[])  # no pooled sample can manufacture support

    def test_no_variation_does_not_create_spurious_associations(self):
        rows=observations()
        for row in rows:row['metrics']={'own.a':0,'own.b':None}
        self.assertEqual(engine.discover(rows,[])[0],[])

    def test_seeded_noise_is_not_promoted_and_test_budget_is_audited(self):
        import random
        rng=random.Random(240)
        rows=observations()
        for row in rows:row['metrics']={f'own.noise_{k}':rng.randrange(10) for k in range(6)}
        patterns,budget=engine.discover(rows,[],max_tests=12)
        self.assertEqual(len(patterns),12)
        self.assertGreater(budget[0]['untested_due_to_budget'],0)
        self.assertTrue(all(p['validation_status']=='ASSOCIATION_DISCOVERED' for p in patterns))

    def test_embedded_fixture_detail_reused_with_original_sha(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);fixture={'fixture':{'id':10},'events':[{'type':'Goal'}]}
            path=fixtures.write_cache(root,'fixtures','id=10',[fixture],fetched='2026-11-01T18:00:00Z')
            cache=engine.EnrichedCache(root)
            response,ref=cache.get('fixtures/events',10,fixtures.CUT)
            self.assertEqual(response,[{'type':'Goal'}])
            self.assertEqual(ref['embedded_field'],'events')
            self.assertEqual(ref['sha256'],engine.hashlib.sha256(path.read_bytes()).hexdigest())

    def test_later_identity_version_cannot_change_an_earlier_frozen_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=fixtures.target();fixtures.setup_history(root,target)
            path=next((root/'fixtures').glob('*.json'));raw=json.loads(path.read_text())['response'][0]
            raw['score']={'fulltime':{'home':0,'away':2},'halftime':{'home':0,'away':1}}
            fixtures.write_cache(root,'fixtures','id=10',[raw],fetched='2026-11-06T12:00:00Z')
            later=copy.deepcopy(raw);later['league']['id']=1234
            fixtures.write_cache(root,'fixtures','id=10',[later],fetched='2026-11-07T12:00:00Z',suffix='later')
            pre={'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}
            rows,_,_=engine.normalize(pre,{'fetched_at_utc':fixtures.CUT.isoformat()},engine.EnrichedCache(root))
            team=next(r for r in rows if r['entity_type']=='TEAM')
            self.assertEqual(team['metrics']['own.goals_h1'],1)
            self.assertEqual(team['conditions']['halftime_score_state'],'LEADING')
            self.assertEqual(team['metrics']['conceded.goals_h2'],0)

    def test_real_half_query_remains_separate_from_ft_and_coach_is_historical(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=fixtures.target();fixtures.setup_history(root,target)
            when='2026-11-06T12:00:00Z'
            fixture_doc=json.loads(next((root/'fixtures').glob('*.json')).read_text())
            fixtures.write_cache(root,'fixtures','id=10',fixture_doc['response'],fetched=when)
            halves=[{'team':{'id':tid},'statistics':[{'type':'Corner Kicks','value':99}],
                     'statistics_1h':[{'type':'Corner Kicks','value':value}],
                     'statistics_2h':[{'type':'Corner Kicks','value':None}]} for tid,value in ((1,0),(3,2))]
            fixtures.write_cache(root,'fixtures/statistics','fixture=10&half=true',halves,fetched=when)
            lineup=[{'team':{'id':1},'formation':'4-3-3','coach':{'id':77},'startXI':[]}]
            fixtures.write_cache(root,'fixtures/lineups','fixture=10',lineup,fetched=when,suffix='coach')
            pre={'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}
            cache=engine.EnrichedCache(root)
            observations,_,_=engine.normalize(pre,{'path':'synthetic','sha256':'synthetic','fetched_at_utc':fixtures.CUT.isoformat()},cache)
            team=next(r for r in observations if r['entity_type']=='TEAM')
            self.assertEqual(team['metrics']['own.corners_h1'],0)
            self.assertEqual(team['metrics']['conceded.corners_h1'],2)
            self.assertIsNone(team['metrics']['own.corners_h2'])
            self.assertNotEqual(team['metrics'].get('own.corners'),99)
            self.assertEqual(team['conditions']['coach_id'],77)
            self.assertTrue(any('half=true' in str(r.get('query')) for r in team['provenance']))

    def test_half_corners_are_never_relabelled_as_goals(self):
        rows=observations(100)
        for row in rows:
            row['metrics']['own.corners_h1']=row['metrics'].pop('own.new_beta')
        patterns,_=engine.discover(rows,[])
        corners=[p for p in patterns if p['variables']['outcome']=='own.corners_h1']
        self.assertTrue(corners)
        self.assertTrue(all(p['period']=='FIRST_HALF' and p['commercial_adapter']['metric']=='corners' for p in corners))

    def test_dedup_missing_zero_and_temporal_identity_filters(self):
        target=fixtures.target();row=fixtures.history()
        row.update(own={'goals':0,'shots':None},conceded={'goals':1})
        target['statistical_observations']['home']=[row,copy.deepcopy(row),dict(row,team_id=888),dict(row,fixture_id=100),dict(row,fixture_id=11,kickoff_utc='2026-11-07T00:00:00Z')]
        pre={'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}
        obs,_,audit=engine.normalize(pre,{'path':'synthetic','sha256':'synthetic'})
        self.assertEqual(len(obs),1)
        self.assertEqual(obs[0]['metrics']['own.goals'],0)
        self.assertIsNone(obs[0]['metrics']['own.shots'])
        self.assertEqual({r['reason'] for r in audit['rejected']},{'TEAM_ID_CONFLICT','TARGET_IN_HISTORY','NOT_FINAL_BEFORE_CUTOFF'})

    def test_player_cache_identity_and_reconstruction_label(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=fixtures.target()
            fixtures.setup_history(root,target)
            pre={'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}
            cache=engine.players.PlayerCache(root)
            obs,_,_=engine.normalize(pre,{'path':'synthetic','sha256':'synthetic'},cache,False)
            self.assertFalse(any(r['entity_type']=='PLAYER' for r in obs))
            obs,_,_=engine.normalize(pre,{'path':'synthetic','sha256':'synthetic'},cache,True)
            actual=next(r for r in obs if r['entity_type']=='PLAYER')
            self.assertEqual((actual['player_id'],actual['team_id']),(7,1))
            self.assertEqual(actual['acquisition_status'],'RETROSPECTIVE_RECONSTRUCTION')
            self.assertEqual(actual['per90']['player.shots'],2)
            self.assertLess(engine.dt(actual['available_at_utc']),fixtures.CUT)

    def test_extra_time_player_totals_not_pooled_with_regulation_time(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);target=fixtures.target();row=fixtures.history();row['status']='AET'
            fixtures.setup_history(root,target,row=row)
            path=next((root/'fixtures').glob('*.json'));doc=json.loads(path.read_text())
            doc['response'][0]['fixture']['status']['short']='AET';path.write_text(json.dumps(doc))
            pre={'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}
            observations,_,audit=engine.normalize(pre,{},engine.EnrichedCache(root),True)
            self.assertFalse(any(r['entity_type']=='PLAYER' for r in observations))
            self.assertTrue(any(r['reason']=='PLAYER_NON_REGULATION_TIME_NOT_POOLED_WITH_FT' for r in audit['rejected']))

    def test_completed_opponent_reference_is_prior_to_each_game(self):
        target=fixtures.target();rows=[]
        for i in range(6):
            row=fixtures.history(fid=30+i,tid=2,venue='HOME',kickoff=f'2026-10-{10+i:02}T14:00:00Z')
            row.update(own={'goals':2},conceded={'goals':0});rows.append(row)
        current=fixtures.history();current.update(opponent_id=2,own={'goals':1},conceded={'goals':0})
        target['statistical_observations']={'home':[current],'away':rows}
        obs,_,_=engine.normalize({'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}, {})
        actual=next(r for r in obs if r['team_id']==1)
        self.assertEqual(actual['conditions']['opponent_strength'],'STRONG')
        self.assertEqual(len(actual['opponent_reference_fixture_ids']),6)
        earliest=min((r for r in obs if r['team_id']==2),key=lambda r:r['kickoff_utc'])
        self.assertIsNone(earliest['conditions']['opponent_strength'])

    def test_offline_write_once_idempotence_and_original_v1_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'preselection.json'
            target=fixtures.target();target['statistical_observations']['home']=[dict(fixtures.history(),own={'goals':1},conceded={'goals':0})]
            source.write_text(json.dumps({'generated_at_utc':fixtures.CUT.isoformat(),'matches':[target]}));before=source.read_bytes()
            one,_=engine.run(source,root/'out');two,_=engine.run(source,root/'out')
            self.assertEqual(one,two);self.assertEqual(source.read_bytes(),before)
            from prematch_predictive_engine import available_observations
            self.assertEqual(len(available_observations(target,'home',fixtures.CUT)),1)
            with self.assertRaisesRegex(ValueError,'OUTPUT_EXISTS_CONFLICT'):engine.run(source,root/'out',max_tests=2)

if __name__=='__main__':unittest.main()
