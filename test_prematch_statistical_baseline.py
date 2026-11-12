import unittest
from datetime import datetime,timedelta,timezone
from prematch_statistical_baseline import validate_hypothesis,summaries,classify

class BaselineTests(unittest.TestCase):
    def rows(self):
        start=datetime(2020,1,1,tzinfo=timezone.utc)
        return [{'fixture_id':i,'kickoff_utc':(start+timedelta(days=i*7)).isoformat(),
                 'available_at_utc':(start+timedelta(days=i*7,hours=3)).isoformat(),
                 'metrics':{'own.goals':i%3,'derived.btts':i%2},'venue':'HOME','entity_type':'TEAM',
                 'team_id':1,'opponent_id':2,'league_id':1,'season':2020} for i in range(35)]
    def test_walk_forward_never_consumes_target_or_future(self):
        rows=self.rows();r=validate_hypothesis(rows,{'outcome':'own.goals','transform':'recent5'})
        self.assertEqual(r['n'],27)
        for p in r['predictions']:self.assertTrue(all(fid<p['fixture_id'] for fid in p['training_fixture_ids']))
        rows[-1]['metrics']['own.goals']=9999
        after=validate_hypothesis(rows,{'outcome':'own.goals','transform':'recent5'})
        self.assertEqual(r['predictions'][-1]['candidate'],after['predictions'][-1]['candidate'])
    def test_delayed_availability_is_excluded(self):
        rows=self.rows()
        for row in rows:row['available_at_utc']='2099-01-01T00:00:00+00:00'
        self.assertEqual(validate_hypothesis(rows,{'outcome':'own.goals','transform':'recent5'})['n'],0)
    def test_missing_not_zero_and_offsides_not_summarized(self):
        rows=self.rows()[:3];rows[0]['metrics']['own.goals']=None
        for row in rows:row['metrics']['own.offsides']=0
        result=summaries(rows);goals=next(x for x in result if x['metric']=='own.goals')
        self.assertEqual((goals['n'],goals['missing']),(2,1));self.assertFalse(any('offsides' in x['metric'] for x in result))
    def test_matchup_requires_actual_prior_rival_data(self):
        self.assertEqual(validate_hypothesis(self.rows(),{'outcome':'own.goals','transform':'matchup'})['n'],0)
    def test_reversal_and_small_sample_are_distinct(self):
        p={'train':{'counts':[4,4,4,4]},'holdout':{'counts':[3,3,3,3]},'stability':{'train_effect':.5,'holdout_effect':-.5}}
        self.assertEqual(classify(p)[0],'DESCARTADO');p['holdout']['counts']=[1,0,3,3]
        self.assertEqual(classify(p)[0],'INSUFFICIENT_SAMPLE')

if __name__=='__main__':unittest.main()
