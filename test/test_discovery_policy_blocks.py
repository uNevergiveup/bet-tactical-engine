import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

import competition_registry as registry
import filter_major_leagues as f
import bwin_discovery as b


class PolicyTests(unittest.TestCase):
    def row(self,**kw):
        return dict(fixture_id=1,league_id=141,league='Segunda División',country='Spain',season=2026,
                    home='Leganes',away='Burgos',status='NS',**kw)

    def test_tiers_and_unknown_names(self):
        self.assertEqual(f.classify(self.row())[0],'ELIGIBLE')
        lower={**self.row(),'league_id':999,'league':'Championship'}
        self.assertEqual(f.classify(lower),('REVIEW','TIER_UNKNOWN'))
        entry={'league_id':999,'country':'Spain','season':2026,'names':['Championship'],
               'type':'LEAGUE','national_tier':3}
        with patch.object(registry,'entries',return_value=[entry]):
            self.assertEqual(f.classify(lower)[0],'EXCLUDED')
        for change in ({'home':'Leganes U19'},{'away':'Burgos II'}):
            self.assertEqual(f.classify({**self.row(),**change})[0],'EXCLUDED')
        self.assertNotEqual(f.classify({**self.row(),'home':'Wick Academy'})[0],'EXCLUDED')

    def test_friendlies_both_genders_and_club_exclusion(self):
        row={**self.row(),'league_id':10,'league':'Friendlies','country':'World'}
        self.assertEqual(f.classify(row)[0],'REVIEW')
        for gender in ('M','F'):
            evidence={'source':'frozen category evidence','gender':gender,'level':'SENIOR'}
            target={**row,'national_team_evidence':evidence}
            self.assertEqual(f.classify(target)[0],'ELIGIBLE')
            self.assertEqual(registry.category(target)['gender'],gender)
        self.assertEqual(f.classify({**row,'league_id':667,'league':'Club Friendlies'})[0],'EXCLUDED')

    def test_cup_evidence_and_season(self):
        row={**self.row(),'league_id':143,'league':'Copa del Rey'}
        self.assertEqual(f.classify(row)[0],'ELIGIBLE')
        evidence={'source':'local participants','stage':'KNOCKOUT','team_tiers':[1,2]}
        self.assertEqual(f.classify({**row,'cup_eligibility_evidence':evidence})[0],'ELIGIBLE')
        self.assertEqual(f.classify({**row,'season':2027})[1],'TIER_UNKNOWN')

    def test_frozen_eight_candidates_and_female_absence(self):
        root=Path(__file__).resolve().parents[1]
        doc=json.loads((root/'outputs/pilots/bwin_identity/ventana_20261114_v1/bwin_discovery.json').read_text())
        expected={1528972:'7905484',1528973:'7909664',1528974:'7905485',1528975:'7909665',
                  1528976:'7909670',1528977:'7909673',1528978:'7909674',1492455:'7901469'}
        for row in doc['matches']:
            if row['fixture_id'] in expected:
                found=b.match_catalog({**row,'status':'NS'},doc['catalog_observed'])
                self.assertIn(expected[row['fixture_id']],{x['bwin_event_id'] for x in found},row['fixture_id'])
            if row['fixture_id']==1581013:
                self.assertEqual(f.classify({**row,'status':'NS'})[0],'ELIGIBLE')
                self.assertEqual(b.match_catalog({**row,'status':'NS'},doc['catalog_observed']),[])

    def test_unknown_category_metadata_warns_but_does_not_block_identity(self):
        fixture={**self.row(),'gender':'M','kickoff_madrid':'2026-11-14T18:00:00+01:00'}
        candidate={'bwin_event_id':'1234567','bwin_url':'https://www.bwin.es/es/sports/eventos/a-2:1234567',
                   'name_score':1,'text':'Leganes Burgos','team_order_status':'HOME_AWAY',
                   'category_compatibility':'COMPATIBLE_OR_UNKNOWN','competition_similarity':1}
        observed={'status':'OPENED','competition_match_score':1,'identity_status':'NAME_AND_ABSOLUTE_TIME_MATCH',
                  'page_category_compatibility':'COMPATIBLE_OR_UNKNOWN','market_headings':{}}
        with patch.object(b,'inspect_event',return_value=observed.copy()):
            result=b.summarize(fixture,[candidate],None,1000,True)
            self.assertEqual(result['status'],'IDENTITY_VERIFIED_AUTOMATICALLY')
            self.assertIn('CLASSIFICATION_PENDING',result['identity_warnings'])
        fixture.update(registry.category(fixture))
        with patch.object(b,'inspect_event',return_value=observed.copy()):
            result=b.summarize(fixture,[candidate],None,1000,True)
            self.assertEqual(result['status'],'IDENTITY_VERIFIED_AUTOMATICALLY')
            self.assertEqual(result['event_identity_status'],'VERIFIED')
        with patch.object(b,'inspect_event',return_value={**observed,'identity_status':'KICKOFF_MISMATCH_REVIEW'}):
            self.assertEqual(b.summarize(fixture,[candidate],None,1000,True)['status'],'EVENT_REQUIRES_REVIEW')

    def test_timeout_dom_recovery_keeps_identity_checks(self):
        fixture={**self.row(),'gender':'M','kickoff_madrid':'2026-11-14T18:00:00+01:00'}
        candidate={'bwin_event_id':'1234567','bwin_url':'https://www.bwin.es/es/sports/eventos/leganes-burgos-2:1234567'}
        page=MagicMock();page.url=candidate['bwin_url']
        page.goto.side_effect=TimeoutError('navigation timeout')
        page.title.return_value='Leganes - Burgos LaLiga 2'
        page.locator.return_value.count.return_value=0
        page.locator.return_value.inner_text.return_value='Leganes Burgos LaLiga 2'
        page.content.return_value='<script type="application/ld+json">{"@type":"SportsEvent","startDate":"2026-11-14T17:00:00Z"}</script>'
        browser=MagicMock();browser.new_page.return_value=page
        with patch.object(b,'browser_collect_anchors',return_value=[]):
            result=b.inspect_event(browser,candidate,fixture,12000)
        self.assertEqual(result['status'],'OPENED')
        self.assertEqual(result['identity_status'],'NAME_AND_ABSOLUTE_TIME_MATCH')
        self.assertIn('navigation_warning',result)
        page.url='https://www.bwin.es/es/sports/eventos/other-2:7654321'
        with patch.object(b,'browser_collect_anchors',return_value=[]):
            self.assertEqual(b.inspect_event(browser,candidate,fixture,12000)['status'],'ERROR')

    def test_retry_is_bounded_and_errors_not_absence(self):
        fixture={**self.row(),'gender':'M'}
        candidate={'bwin_event_id':'1234567','bwin_url':'url','name_score':1,'text':'teams',
                   'team_order_status':'HOME_AWAY','category_compatibility':'COMPATIBLE_OR_UNKNOWN'}
        error={'status':'ERROR','error':'TimeoutError','identity_status':'NOT_VERIFIED','market_headings':{}}
        with patch.object(b,'inspect_event',return_value=error.copy()) as inspect:
            result=b.summarize(fixture,[candidate],None,1000,True,1)
        self.assertEqual(inspect.call_count,2)
        self.assertEqual(result['discovery_diagnostic'],'INSPECTION_FAILED')
        self.assertNotEqual(result['status'],'NOT_FOUND_IN_SCANNED_PAGES')

    def test_pending_labels_use_bounded_navigation_budget(self):
        fixtures=[{**self.row(),'league_id':999,'league':'Unknown','country':country} for country in ('Unknownland','Spain','France')]
        nav={'espana':b.BASE+'/es/sports/f%C3%BAtbol-4/apuestas/espana-1',
             'francia':b.BASE+'/es/sports/f%C3%BAtbol-4/apuestas/francia-2'}
        with patch.object(b,'read_country_navigation',return_value=(nav,{'status':'OK'})), \
             patch.object(b,'fetch_catalog',return_value=([],[])) as fetch:
            _,_,info=b.extend_catalog_for_missing(None,b.BASE,[],fixtures,2,1,1000)
        fetch.assert_called_once()
        self.assertEqual(set(info['requested_countries']),{'unknownland','espana','francia'})

    def test_gender_order_and_absent_event_fail_closed(self):
        fixture={**self.row(),'gender':'M'}
        event={'bwin_event_id':'1234567','bwin_url':'https://www.bwin.es/es/sports/eventos/leganes-burgos-2:1234567',
               'text':'Leganes Women Burgos Women'}
        self.assertEqual(b.match_catalog(fixture,[event]),[])
        self.assertEqual(registry.category({**fixture,'gender':'F'}),{})
        reverse={**event,'text':'Burgos Leganes','bwin_url':'https://www.bwin.es/es/sports/eventos/burgos-leganes-2:1234567'}
        self.assertEqual(b.match_catalog(fixture,[reverse]),[])
        self.assertEqual(b.summarize(fixture,[],None,1000,False)['status'],'NOT_FOUND_IN_SCANNED_PAGES')

    def test_first_division_women_and_official_nationals(self):
        female={**self.row(),'league_id':64,'league':'Feminine Division 1','country':'France',
                'home':'Lyon W','away':'Saint-Malo W'}
        self.assertEqual(registry.category(female)['gender'],'F')
        self.assertEqual(f.classify(female)[0],'ELIGIBLE')
        national={**self.row(),'league_id':5,'league':'UEFA Nations League','country':'World'}
        self.assertEqual(f.classify(national)[0],'ELIGIBLE')
        self.assertEqual(f.classify({**national,'home':'Norway U19'})[0],'EXCLUDED')


if __name__=='__main__':unittest.main()
