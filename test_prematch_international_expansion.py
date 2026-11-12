import unittest
from datetime import datetime,timedelta,timezone
from prematch_international_expansion import fit,evaluate,classification

class InternationalTests(unittest.TestCase):
    def rows(self):
        start=datetime(2024,1,1,tzinfo=timezone.utc)
        return [{'fixture_id':i,'team_id':1,'league_id':32 if i<10 else 5,'season':2024 if i<10 else 2026,
            'venue':'HOME' if i%2 else 'AWAY','kickoff_utc':(start+timedelta(days=i*30)).isoformat(),
            'available_at_utc':(start+timedelta(days=i*30,hours=3)).isoformat(),
            'metrics':{'own.goals':i%4}} for i in range(20)]
    def test_future_and_target_excluded_from_walk_forward(self):
        rows=self.rows();result=evaluate(rows,'own.goals','hierarchical')
        self.assertEqual(result['n'],14)
        for p in result['predictions']:self.assertTrue(all(i<p['fixture_id'] for i in p['training_ids']))
        rows[-1]['metrics']['own.goals']=1000
        self.assertEqual(result['predictions'][-1]['candidate'],evaluate(rows,'own.goals','hierarchical')['predictions'][-1]['candidate'])
    def test_weights_and_ess(self):
        rows=self.rows();at=datetime(2026,1,1,tzinfo=timezone.utc)
        result=fit(rows,'own.goals',at,5,2026,'HOME','weighted')
        self.assertLess(result['effective_n'],result['n']);self.assertLess(result['weights'][0],result['weights'][-1])
    def test_missing_is_not_zero(self):
        rows=self.rows();rows[0]['metrics']['own.goals']=None
        result=fit(rows,'own.goals',datetime(2026,1,1,tzinfo=timezone.utc),5,2026,'HOME','baseline')
        self.assertEqual(result['n'],19)
    def test_current_stratum_shrinkage_not_implicit_pooling(self):
        rows=self.rows()
        for r in rows:r['metrics']['own.goals']=0 if r['league_id']==32 else 4
        estimate=fit(rows,'own.goals',datetime(2026,1,1,tzinfo=timezone.utc),5,2026,'HOME','hierarchical')
        self.assertGreater(estimate['mean'],2);self.assertLess(estimate['mean'],4)
    def test_no_validation_promotion_for_small_or_unstable_samples(self):
        est={'n':15,'effective_n':12};ev={'n':7}
        self.assertEqual(classification(est,ev)[0],'FRAGIL')
        ev={'n':10,'mean_loss_difference':-.1,'first_half_difference':-.3,'second_half_difference':.1}
        self.assertEqual(classification(est,ev)[0],'FRAGIL')
        ev['mean_loss_difference']=.1;self.assertEqual(classification(est,ev)[0],'DESCARTADO')

if __name__=='__main__':unittest.main()
