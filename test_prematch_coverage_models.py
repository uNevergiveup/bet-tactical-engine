import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from prematch_predictive_engine import joint_ft_goal_baseline
from prematch_tactical_history import audit_frozen_coverage
import prematch_market_mapping_v1 as m
ROOT=Path(__file__).parent
class BaselineTests(unittest.TestCase):
 def make(self,n=12):
  f=dict(fixture_id=999,home_id=1,away_id=2,league_id=3,season=2026);rows={}
  for side,tid in [('home',1),('away',2)]:rows[side]=[dict(fixture_id=100*tid+i,team_id=tid,opponent_id=50+i,league_id=3,season=2026,status='FT',kickoff_utc=(datetime(2026,10,1,tzinfo=timezone.utc)+timedelta(days=i)).isoformat(),venue='HOME' if i%2 else 'AWAY',own={'goals':i%3},conceded={'goals':(i+1)%3}) for i in range(n)]
  return f,rows,datetime(2026,11,1,tzinfo=timezone.utc)
 def test_joint_baseline_venue_dispersion_and_uncertainty(self):
  f,r,at=self.make();x=joint_ft_goal_baseline(f,r,at,bootstrap_samples=20);self.assertEqual(x['status'],'MODEL_EVALUABLE');self.assertFalse(x['validated']);self.assertEqual(x['series']['home_own']['venue_n'],6);self.assertAlmostEqual(sum(x['poisson_total_distribution']),1,places=7);self.assertAlmostEqual(sum(x['empirical_total_distribution'].values()),1);self.assertFalse(x['parameter_uncertainty']['calibrated_interval'])
 def test_small_sample_not_promoted_and_missing_not_zero(self):
  f,r,at=self.make(3);self.assertEqual(joint_ft_goal_baseline(f,r,at)['status'],'DATA_INSUFFICIENT');r['home'][0]['own']['goals']=None;self.assertEqual(joint_ft_goal_baseline(f,r,at)['series']['home_own']['n'],2)
 def test_target_future_same_block_and_non_ft_excluded(self):
  f,r,at=self.make();future=copy.deepcopy(r['home'][0]);future.update(fixture_id=999,kickoff_utc=at.isoformat());future['own']['goals']=99;r['home'].append(future);x=joint_ft_goal_baseline(f,r,at,bootstrap_samples=0);self.assertNotIn(999,x['series']['home_own']['fixture_ids']);self.assertEqual(x['series']['home_own']['n'],12)
  r['home'][0]['kickoff_utc']=(at-timedelta(hours=2)).isoformat();r['home'][1]['status']='PEN';self.assertEqual(joint_ft_goal_baseline(f,r,at,bootstrap_samples=0)['series']['home_own']['n'],10)
 def test_no_leak_from_future_goals_or_fractional_counts(self):
  f,r,at=self.make();future=copy.deepcopy(r['away'][0]);future.update(fixture_id=800,kickoff_utc=(at+timedelta(days=1)).isoformat());r['away'].append(future);x=joint_ft_goal_baseline(f,r,at,bootstrap_samples=0);future['own']['goals']=999;self.assertEqual(x,joint_ft_goal_baseline(f,r,at,bootstrap_samples=0));r['home'][0]['own']['goals']=1.5;self.assertEqual(joint_ft_goal_baseline(f,r,at,bootstrap_samples=0)['series']['home_own']['n'],11)
class CacheAuditTests(unittest.TestCase):
 def setup_cache(self,root,acquired='2026-10-02T00:00:00Z'):
  p=root/'outputs/data/cache/preselect_api';p.mkdir(parents=True)
  raw=dict(fixture={'id':100,'date':'2026-10-01T00:00:00Z','status':{'short':'FT'}},teams={'home':{'id':1},'away':{'id':2}},league={'id':3,'season':2026},goals={'home':1,'away':0},statistics=[{'team':{'id':i},'statistics':[{'type':'Corner Kicks','value':0}]} for i in [1,2]])
  (p/'fixture.json').write_text(json.dumps({'retrieved_at_utc':acquired,'endpoint':'fixtures','parameters':{'ids':'100'},'response':[raw]}))
  row=dict(fixture_id=100,team_id=1,opponent_id=2,league_id=3,season=2026,status='FT',kickoff_utc='2026-10-01T00:00:00Z',venue='HOME',own={'goals':1},conceded={'goals':0},statistics_requested=False)
  return {'matches':[dict(fixture_id=999,home_id=1,away_id=2,league_id=3,season=2026,analysis_cutoff_utc='2026-10-03T00:00:00Z',kickoff_madrid='2026-11-11T21:00:00+01:00',statistical_observations={'home':[row],'away':[]})]}
 def test_recovery_is_metric_specific_preserves_zero_and_original(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);p=self.setup_cache(root);old=copy.deepcopy(p);new,a=audit_frozen_coverage(root,p);self.assertEqual(old,p);self.assertEqual(len(a['recovered']),2);self.assertEqual(new['matches'][0]['statistical_observations']['home'][0]['own']['corners'],0);self.assertTrue(new['matches'][0]['statistical_observations']['home'][0]['statistics_requested'])
 def test_late_cache_not_used_and_no_coverage_denominator_inflation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);p=self.setup_cache(root,'2026-10-04T00:00:00Z');new,a=audit_frozen_coverage(root,p);self.assertFalse(a['recovered']);s=next(s for s in a['series'] if s['side']=='home' and s['kind']=='own' and s['metric']=='goals');self.assertEqual(s['coverage_effective_pct'],100);self.assertEqual(s['eligible_history_n'],1);self.assertEqual(s['target_history_cap'],20)
class RealAuditTests(unittest.TestCase):
 def test_real_goals_regressions_and_direction_separation(self):
  pre=json.loads((ROOT/'outputs/reconciliation_real_20261111/verified_schedule/preselection_reconciled.json').read_text())
  for fid,n in [(1557232,11),(1649465,14),(1600656,16),(1638311,3)]:
   f=next(f for f in pre['matches'] if f['fixture_id']==fid);x=joint_ft_goal_baseline(f,f['statistical_observations'],m.dt(f['analysis_cutoff_utc']),bootstrap_samples=0);self.assertEqual(min(v['n'] for v in x['series'].values()),n);self.assertEqual(x['status'],'DATA_INSUFFICIENT' if fid==1638311 else 'MODEL_EVALUABLE');self.assertFalse(x['validated'])
 def test_real_offline_family_audit_and_juventus_both_directions(self):
  with tempfile.TemporaryDirectory() as tmp,patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')):
   out=Path(tmp);result=m.audit_coverage_models(ROOT/'outputs/reconciliation_real_20261111',out);self.assertEqual(result['counts']['fixtures'],64);self.assertEqual(result['counts']['captured_selections'],1772);self.assertEqual(result['counts']['validated_selections'],0)
   rows=json.loads((out/'market_model_evaluation.json').read_text())['quotes'];corners=[r for r in rows if r['fixture_id']==1638311 and r['metric']=='corners' and r['line']==7.5 and r['period']=='FULL_TIME'];self.assertEqual(len(corners),2);self.assertEqual({r['direction'] for r in corners},{'OVER','UNDER'})
   self.assertTrue(all(r['classification']=='JOINT_MODEL_MISSING' and r['underlying_signal_status']=='UNDERLYING_SIGNAL_CONTRADICTED' and r['market_direction_status'].startswith('NOT_TESTED') for r in corners));self.assertFalse(any(r['predictive_use_authorized'] for r in rows))
   bt=json.loads((out/'temporal_backtest.json').read_text())
   for r in bt['rows']:
    self.assertFalse(r['model_validated']);self.assertNotIn(r['fixture_id'],{i for ids in r['training_fixture_ids'].values() for i in ids})
