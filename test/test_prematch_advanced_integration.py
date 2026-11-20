import unittest
from prematch_market_mapping_v1 import integrate_advanced

class IntegrationTest(unittest.TestCase):
    def test_all_fixtures_retained_without_exploratory_rank_boost(self):
        ranking=[{'fixture_id':1,'rank':1},{'fixture_id':2,'rank':2}]
        explanations={'fixtures':[{'fixture_id':1},{'fixture_id':2}]}
        result=integrate_advanced(ranking,{'patterns':[]},{'mappings':[]},explanations,{'matches':[{'fixture_id':1,'status':'NOT_FOUND_IN_SCANNED_PAGES'}]})
        self.assertEqual([r['rank'] for r in result['ranking']],[1,2])
        self.assertEqual(len(result['fixture_capture_audit']),2)
        self.assertEqual(result['fixture_capture_audit'][1]['capture_status'],'NOT_IN_LEGACY_DISCOVERY_COHORT')
        self.assertFalse(result['ready'])
        self.assertNotIn('advanced_evidence',ranking[0])

    def test_evidence_and_counterevidence_survive_in_both_outputs(self):
        p={'pattern_id':'a','entity_type':'TEAM','team_id':3,'period':'FIRST_HALF',
           'commercial_adapter':{'metric':'corners'},'variables':{'condition':{'field':'venue'}},
           'train':{'n':8},'holdout':{'n':4},'uncertainty':{'multiple_testing_q':1},
           'validation_status':'ASSOCIATION_DISCOVERED','evidence_fixture_ids':[7],
           'counterevidence_fixture_ids':[8],'limitations':['NO_CALIBRATION'],
           'predictive_use':{'authorized':False},'next_match_applicability':[{'fixture_id':1}]}
        r=integrate_advanced([{'fixture_id':1,'rank':1}],{'patterns':[p]},
           {'mappings':[{'fixture_id':1,'reason':'NO_VERIFIED_PREFLIGHT'}]},
           {'fixtures':[{'fixture_id':1}]},{'matches':[]})
        evidence=r['ranking'][0]['advanced_evidence'][0]
        self.assertEqual(evidence['counterevidence_fixture_ids'],[8])
        self.assertEqual(evidence['uncertainty']['multiple_testing_q'],1)
        self.assertEqual(r['explanations']['fixtures'][0]['advanced_evidence'][0],evidence)
        self.assertFalse(evidence['predictive_use']['authorized'])
