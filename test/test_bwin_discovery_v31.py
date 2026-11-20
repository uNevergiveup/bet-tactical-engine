import json
import unittest
from pathlib import Path
from unittest.mock import patch

import bwin_discovery as b
import prematch_phase1 as phase1


ROOT=Path(__file__).resolve().parents[1]
SNAPSHOT=ROOT/'outputs/pilots/bwin_identity/ventana_20261114_v2/bwin_discovery.json'


class DiscoveryV31Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document=json.loads(SNAPSHOT.read_text())
        cls.fixtures={row['fixture_id']:row for row in cls.document['matches']}
        cls.catalog=cls.document['catalog_observed']

    def close_identity(self,fixture_id,*,page_category=None,identity='NAME_AND_ABSOLUTE_TIME_MATCH'):
        fixture={**self.fixtures[fixture_id],'status':'NS','matching_eligibility':'PENDING_CLASSIFICATION',
                 'matching_eligibility_reason':'CLASSIFICATION_PENDING'}
        possible=b.match_catalog(fixture,self.catalog);self.assertEqual(len(possible),1)
        candidate=possible[0]
        observed={'status':'OPENED','identity_status':identity,
                  'event_id_from_url':candidate['bwin_event_id'],'title_teams_match':True,
                  'event_start_machine_readable':fixture['kickoff_madrid'],'kickoff_difference_minutes':0.0,
                  'competition_match_score':0.0,
                  'page_category_compatibility':page_category or candidate['category_compatibility'],
                  'market_headings':{},'duration_seconds':0.01}
        with patch.object(b,'inspect_event',return_value=observed):
            return b.summarize(fixture,possible,None,1000,True)

    def test_wolfsburg_and_tottenham_close_identity_despite_pending_classification(self):
        for fixture_id,event_id in ((1593176,'7911356'),(1607730,'7911158')):
            result=self.close_identity(fixture_id)
            self.assertEqual(result['status'],'IDENTITY_VERIFIED_AUTOMATICALLY')
            self.assertEqual(result['bwin_event_id'],event_id)
            self.assertEqual(result['event_identity_status'],'VERIFIED')
            self.assertEqual(result['prematch_authorization'],'NOT_GRANTED')
            self.assertIn('CLASSIFICATION_PENDING',result['identity_warnings'])

    def test_unknown_league_can_close_identity_without_filling_unknown_fields(self):
        fixture={**self.fixtures[1607730],'league':'Unknown League','league_id':999999,
                 'gender':'UNKNOWN','category_status':None,'category_evidence_status':None}
        self.fixtures[999999]=fixture
        result=self.close_identity(999999,page_category='FEMALE_CORROBORATED')
        self.assertEqual(result['status'],'IDENTITY_VERIFIED_AUTOMATICALLY')
        self.assertEqual(result['gender'],'UNKNOWN')
        self.assertIsNone(result['category_status'])

    def test_real_category_time_and_event_conflicts_remain_blocked(self):
        self.assertEqual(self.close_identity(1607730,page_category='GENDER_CONFLICT')['status'],
                         'EVENT_REQUIRES_REVIEW')
        self.assertEqual(self.close_identity(1607730,identity='KICKOFF_MISMATCH_REVIEW')['status'],
                         'EVENT_REQUIRES_REVIEW')
        fixture={**self.fixtures[1607730],'status':'NS','matching_eligibility':'PENDING_CLASSIFICATION'}
        possible=b.match_catalog(fixture,self.catalog)
        observed={'status':'OPENED','identity_status':'EVENT_ID_CHANGED_REVIEW','event_id_from_url':'7999999',
                  'title_teams_match':True,'competition_match_score':1.0,
                  'page_category_compatibility':'FEMALE_CORROBORATED','market_headings':{}}
        with patch.object(b,'inspect_event',return_value=observed):
            result=b.summarize(fixture,possible,None,1000,True)
        self.assertEqual(result['status'],'EVENT_REQUIRES_REVIEW')

    def test_phase1_accepts_verified_identity_with_classification_warning(self):
        fixture={**self.fixtures[1607730],'category_status':'REVIEW','status':'NS'}
        observed=self.close_identity(1607730)
        document=phase1.identity_map({'generated_at_utc':'2026-11-14T03:00:00+00:00','fixtures':[fixture]},
                                     {'generated_at_utc':'2026-11-14T03:01:00+00:00','matches':[observed],
                                      'events':[],'pages':[]})
        self.assertEqual(document['counts']['verified'],1)
        self.assertIn('CLASSIFICATION_PENDING',document['verified'][0]['identity_warnings'])


if __name__=='__main__':unittest.main()
