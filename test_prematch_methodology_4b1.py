import copy,json,unittest
from pathlib import Path
import prematch_advanced_discovery_v2 as e

BASE=Path('outputs/history_recovery_v2/player_directed_20261109/discovery_new_version')
class MethodologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patterns=json.loads((BASE/'advanced_patterns.json').read_text())['patterns']
        cls.obs=json.loads((BASE/'historical_observations.json').read_text())['observations']
    def pattern(self,prefix):return copy.deepcopy(next(p for p in self.patterns if p['pattern_id'].startswith(prefix)))
    def test_period_correction_does_not_refit(self):
        p=self.pattern('a3243f3d80b2');before=copy.deepcopy(p)
        e.methodological_review(p,self.obs)
        self.assertEqual(p['period'],'FIRST_HALF');self.assertEqual(p['commercial_adapter']['metric'],'goals')
        for k in ('train','holdout','uncertainty','variables'):self.assertEqual(p[k],before[k])
        self.assertEqual(p['holdout_assessment']['status'],'CONTRARY_EFFECT')
    def test_missing_reference_and_variation_are_not_confirmation(self):
        for prefix,status in [('138f4a156317','NO_VALID_OBSERVATIONS'),('8ec3557a7001','NO_COMPARABLE_REFERENCE_GROUP'),('b8888b686d42','NO_OUTCOME_VARIATION')]:
            p=e.methodological_review(self.pattern(prefix),self.obs)
            self.assertEqual(p['holdout_assessment']['status'],status)
            self.assertFalse(p['holdout_assessment']['predictive_validation'])
    def test_denominators_and_low_exposure_sensitivity(self):
        p=e.methodological_review(self.pattern('b8888b686d42'),self.obs)
        self.assertEqual(p['per90_audit']['train']['eligible_minutes'],606)
        self.assertTrue(all(r['formula_verified'] for r in p['per90_audit']['train']['records']))
        self.assertEqual(sum(r['low_exposure'] for r in p['per90_audit']['train']['records']),1)
        self.assertFalse(p['predictive_use']['authorized'])
        self.assertTrue(any('313064' in q for q in e.tactical_questions(p)))
    def test_invalid_denominator_is_blocked(self):
        obs=copy.deepcopy(self.obs);p=self.pattern('b8888b686d42')
        row=next(r for r in obs if r.get('player_id')==313064 and r['fixture_id']==1519381)
        row['minutes']=0
        e.methodological_review(p,obs)
        self.assertEqual(p['predictive_use']['reason'],'PER90_DENOMINATOR_UNVERIFIED')
        self.assertEqual(p['per90_audit']['train']['valid_matches'],7)
    def test_consistent_effect_does_not_authorize_prediction(self):
        p=self.pattern('a3243f3d80b2');p['holdout']['counts']=[3,0,0,3];p['holdout']['risk_difference']=1
        e.methodological_review(p,self.obs)
        self.assertEqual(p['holdout_assessment']['status'],'CONSISTENT_EFFECT')
        self.assertFalse(p['predictive_use']['authorized'])

if __name__=='__main__':unittest.main()
