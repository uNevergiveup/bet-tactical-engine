import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import bwin_discovery as b
import competition_registry as registry
import filter_major_leagues as filters


ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'outputs/discovery/2026-11-14_1500_0300/manual_api/fixtures.json'
V2=ROOT/'outputs/pilots/bwin_identity/ventana_20261114_v2/bwin_discovery.json'
V1=ROOT/'outputs/pilots/bwin_identity/ventana_20261114_v1/bwin_discovery.json'


class DiscoveryV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v2=json.loads(V2.read_text())
        cls.v1=json.loads(V1.read_text())
        cls.fixture_rows={row['fixture_id']:row for row in cls.v2['matches']}

    def test_v3_budget_classifies_before_external_work(self):
        ready,skipped=b.read_candidates(FIXTURES,[],0,True,
            datetime(2026,11,14,tzinfo=ZoneInfo('Europe/Madrid')))
        eligible=[row for row in ready if row['matching_eligibility']=='ELIGIBLE']
        pending=[row for row in ready if row['matching_eligibility']=='PENDING_CLASSIFICATION']
        self.assertEqual((len(eligible),len(pending),len(skipped)),(151,298,108))
        self.assertTrue(all(row['discovery_resource_policy']=='FULL_DISCOVERY' for row in eligible))
        self.assertTrue(all(row['discovery_resource_policy']=='FULL_DISCOVERY_CLASSIFICATION_WARNING' for row in pending))
        self.assertTrue(all(row['discovery_resource_policy']=='NO_SEARCH' for row in skipped))

    def test_pending_without_candidate_can_create_bounded_navigation_demand(self):
        pending={**self.fixture_rows[1647953],'matching_eligibility':'PENDING_CLASSIFICATION'}
        with patch.object(b,'read_country_navigation',return_value=({}, {'status':'EMPTY'})) as nav, \
             patch.object(b,'fetch_catalog') as fetch:
            _,pages,info=b.extend_catalog_for_missing(None,b.BASE,[],[pending],8,1,1000)
        self.assertTrue(nav.called);fetch.assert_not_called();self.assertEqual(len(pages),2)
        self.assertIn('francia',info['requested_countries'])

    @staticmethod
    def verified_observation(fixture,candidate,*,category=None,identity='NAME_AND_ABSOLUTE_TIME_MATCH'):
        return {'status':'OPENED','identity_status':identity,'event_id_from_url':candidate['bwin_event_id'],
                'event_start_machine_readable':fixture['kickoff_madrid'],'kickoff_difference_minutes':0.0,
                'title_teams_match':True,'competition_match_score':0.0,
                'page_category_compatibility':category or candidate['category_compatibility'],
                'market_headings':{},'duration_seconds':0.01}

    def test_pending_candidate_is_inspected_and_identity_can_close(self):
        cup={**self.fixture_rows[1647953],'status':'NS','matching_eligibility':'PENDING_CLASSIFICATION',
             'matching_eligibility_reason':'CUP_PARTICIPANTS_UNKNOWN'}
        possible=b.match_catalog(cup,self.v2['catalog_observed']);observed=self.verified_observation(cup,possible[0])
        with patch.object(b,'inspect_event',return_value=observed):
            result=b.summarize(cup,possible,None,1000,True)
        self.assertEqual(result['status'],'IDENTITY_VERIFIED_AUTOMATICALLY')
        self.assertEqual(result['event_identity_status'],'VERIFIED')
        self.assertEqual(result['prematch_authorization'],'NOT_GRANTED')
        self.assertIn('CUP_PARTICIPANTS_UNKNOWN',result['identity_warnings'])

    def test_all_four_cup_candidates_remain_visible_without_authorization(self):
        expected={1647953:'7911524',1647930:'7911792',1647949:'7911531',1648008:'7911799'}
        for fixture_id,event_id in expected.items():
            row={**self.fixture_rows[fixture_id],'status':'NS',
                 'matching_eligibility':'PENDING_CLASSIFICATION'}
            possible=b.match_catalog(row,self.v2['catalog_observed'])
            with patch.object(b,'inspect_event',return_value=self.verified_observation(row,possible[0])):
                result=b.summarize(row,possible,None,1000,True)
            self.assertEqual(result['status'],'IDENTITY_VERIFIED_AUTOMATICALLY',fixture_id)
            self.assertEqual(result['bwin_event_id'],event_id,fixture_id)
            self.assertEqual(result['prematch_authorization'],'NOT_GRANTED',fixture_id)

    def test_four_new_and_eight_prior_alias_regressions(self):
        expected={1556896:'7910905',1556900:'7910903',1580955:'7911213',1520974:'7910957',
                  1528972:'7905484',1528973:'7909664',1528974:'7905485',1528975:'7909665',
                  1528976:'7909670',1528977:'7909673',1528978:'7909674',1492455:'7901469'}
        for fid,event_id in expected.items():
            rows=b.match_catalog({**self.fixture_rows[fid],'status':'NS'},self.v2['catalog_observed'])
            self.assertIn(event_id,{row['bwin_event_id'] for row in rows},fid)

    def test_aliases_never_bypass_gender_order_or_time(self):
        fixture={**self.fixture_rows[1580955],'status':'NS'}
        event=next(row for row in self.v2['catalog_observed'] if row['bwin_event_id']=='7911213')
        self.assertTrue(b.match_catalog(fixture,[event]))
        self.assertEqual(b.match_catalog({**fixture,'gender':'M'},[event]),[])
        reversed_event={**event,'text':'FC Fleury F Montpellier HSC F',
                        'bwin_url':'https://www.bwin.es/es/sports/eventos/fc-fleury-f-montpellier-hsc-f-2:7911213'}
        self.assertEqual(b.match_catalog(fixture,[reversed_event]),[])
        candidate={**b.match_catalog(fixture,[event])[0]}
        observed={'status':'OPENED','identity_status':'KICKOFF_MISMATCH_REVIEW',
                  'competition_match_score':1,'page_category_compatibility':'FEMALE_CORROBORATED',
                  'market_headings':{}}
        with patch.object(b,'inspect_event',return_value=observed):
            self.assertEqual(b.summarize(fixture,[candidate],None,1000,True)['status'],'EVENT_REQUIRES_REVIEW')

    def test_timeout_recovery_methods_and_circuit_breaker(self):
        fixture={**self.fixture_rows[1556896],'status':'NS','category_evidence_status':'CORROBORATED',
                 'category_level':'SENIOR','category_provenance':{'source':'test'}}
        candidate=b.match_catalog(fixture,self.v2['catalog_observed'])[0]
        error={'status':'ERROR','error':'TimeoutError: Page.goto','identity_status':'NOT_VERIFIED',
               'market_headings':{},'duration_seconds':1.0}
        control={'threshold':2,'probe_interval':3}
        with patch.object(b,'inspect_event',return_value=error.copy()) as inspect:
            first=b.summarize(fixture,[candidate],None,1000,True,1,control)
            second=b.summarize(fixture,[candidate],None,1000,True,0,control)
            deferred=b.summarize(fixture,[candidate],None,1000,True,0,control)
        self.assertEqual([call.args[4] for call in inspect.call_args_list],
                         ['DOMCONTENTLOADED','COMMIT_THEN_VALIDATE','DOMCONTENTLOADED'])
        self.assertEqual(first['discovery_diagnostic'],'INSPECTION_FAILED')
        self.assertEqual(second['discovery_diagnostic'],'INSPECTION_FAILED')
        self.assertEqual(deferred['discovery_diagnostic'],'INSPECTION_DEFERRED_CIRCUIT_OPEN')
        self.assertNotEqual(deferred['status'],'NOT_FOUND_IN_SCANNED_PAGES')

    def test_ninety_five_persistent_timeouts_are_budgeted_offline(self):
        def timed_out(row):return 'TimeoutError' in str((row.get('inspection') or {}).get('error',''))
        persistent=({row['fixture_id'] for row in self.v1['matches'] if timed_out(row)} &
                    {row['fixture_id'] for row in self.v2['matches'] if timed_out(row)})
        ready,skipped=b.read_candidates(FIXTURES,[],0,True,
            datetime(2026,11,14,tzinfo=ZoneInfo('Europe/Madrid')))
        state={row['fixture_id']:row['matching_eligibility'] for row in ready+skipped}
        counts={key:sum(state[fid]==key for fid in persistent)
                for key in ('ELIGIBLE','PENDING_CLASSIFICATION','EXCLUDED')}
        self.assertEqual(len(persistent),95)
        self.assertEqual(counts,{'ELIGIBLE':11,'PENDING_CLASSIFICATION':76,'EXCLUDED':8})

    def test_category_competition_and_cup_participants_are_separate(self):
        cup={**self.fixture_rows[1647953],'status':'NS'}
        category=registry.category(cup)
        self.assertEqual(category['category_status'],'ELIGIBLE')
        self.assertEqual(category['competition_identity_status'],'CORROBORATED')
        self.assertEqual(category['competition_eligibility'],'ELIGIBLE')
        self.assertEqual(category['participant_eligibility'],'NOT_REQUIRED')
        positive={**cup,'cup_eligibility_evidence':{'source':'local registry','stage':'KNOCKOUT','team_tiers':[1,2]}}
        negative={**cup,'cup_eligibility_evidence':{'source':'local registry','stage':'KNOCKOUT','team_tiers':[1,3]}}
        self.assertEqual(filters.matching_eligibility(positive)[0],'ELIGIBLE')
        self.assertEqual(filters.matching_eligibility(negative)[0],'ELIGIBLE')

    def test_friendlies_require_positive_senior_evidence_for_both_genders(self):
        base={'fixture_id':1,'league_id':10,'league':'Friendlies','country':'World','season':2026,
              'home':'A','away':'B','status':'NS'}
        self.assertEqual(filters.matching_eligibility(base)[0],'PENDING_CLASSIFICATION')
        for gender in ('M','F'):
            row={**base,'national_team_evidence':{'source':'local team category','level':'SENIOR','gender':gender}}
            self.assertEqual(filters.matching_eligibility(row)[0],'ELIGIBLE')
            self.assertEqual(registry.category(row)['gender'],gender)
        self.assertEqual(filters.classify({**base,'home':'A U18'})[0],'EXCLUDED')

    def test_lower_tiers_and_unknown_inventory(self):
        tier3={'fixture_id':1,'league_id':435,'league':'Primera División RFEF - Group 1',
               'country':'Spain','season':2026,'home':'A','away':'B','status':'NS'}
        self.assertEqual(filters.classify(tier3),('EXCLUDED','NATIONAL_TIER_NOT_ADMITTED'))
        unknown={**tier3,'fixture_id':2,'league_id':9999,'league':'Championship'}
        rows=registry.unknown_inventory([unknown,unknown,{**unknown,'fixture_id':3,'league_id':9998,'league':'Other'}])
        self.assertEqual(rows[0]['fixtures'],2)
        self.assertEqual(rows[0]['reason'],'TIER_UNKNOWN')
        self.assertEqual(filters.matching_eligibility({**tier3,'league_id':141,
            'league':'Segunda División','status':'1H'})[0],'EXCLUDED')

    def test_max_scrolls_is_partial_not_absence(self):
        fixture=self.fixture_rows[1556896]
        url='https://www.bwin.es/es/sports/futbol-4/apuestas/escocia-26'
        pages=[{'catalog_page':url,'status':'OK','collection_trace':{'termination':'MAX_SCROLLS'}}]
        info={'country_pages':{'escocia':url},'competition_pages':{},'unresolved_contexts':[]}
        result=b.coverage_for_fixture(fixture,pages,info,candidate_visible=False)
        self.assertEqual(result['status'],'CATALOG_PARTIAL')
        self.assertFalse(result['exhaustive'])
        event=next(row for row in self.v2['catalog_observed'] if row['bwin_event_id']=='7910905')
        with patch.object(b,'VERIFIED_CANDIDATE_ALIASES',()):
            near=b.visible_without_matching({**fixture,'status':'NS'},[event])
        self.assertEqual(near[0]['event_id'],'7910905')
        visible=b.coverage_for_fixture(fixture,pages,info,candidate_visible=False,unmatched_visible=near)
        self.assertEqual(visible['status'],'EVENT_VISIBLE_WITHOUT_MATCHING')


if __name__=='__main__':unittest.main()
