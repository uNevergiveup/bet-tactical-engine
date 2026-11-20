import json
import unittest
from pathlib import Path
from unittest.mock import patch

import bwin_discovery as b
from filter_major_leagues import explicit_competition_category


ROOT=Path(__file__).resolve().parents[1]
PRESELECTION=ROOT/'outputs/discovery/2026-11-13_1500_0300/evaluations/20261113T140059021954Z_c0b4ec5b12d1/prospective_trial/enriched/preselection.json'
FROZEN=ROOT/'outputs/pilots/bwin_identity/uefa_nations_20261113/bwin_discovery.json'


class UefaNationsDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PRESELECTION.is_file() or not FROZEN.is_file():
            raise unittest.SkipTest('Frozen UEFA Nations League evidence unavailable')
        matches=json.loads(PRESELECTION.read_text())['matches']
        cls.fixtures={row['fixture_id']:{**row,**(explicit_competition_category(row) or {})}
                      for row in matches if row['fixture_id'] in {1528966,1545659}}
        cls.snapshot=json.loads(FROZEN.read_text())
        cls.events={row['bwin_event_id']:row for row in cls.snapshot['catalog_observed']
                    if row['bwin_event_id'] in {'7905474','7909660'}}

    def test_frozen_shared_catalog_recovers_both_reference_events(self):
        expected={1528966:'7905474',1545659:'7909660'}
        for fixture_id,event_id in expected.items():
            candidates=b.match_catalog(self.fixtures[fixture_id],list(self.events.values()))
            self.assertEqual([row['bwin_event_id'] for row in candidates],[event_id])
            self.assertEqual(candidates[0]['team_order_status'],'HOME_AWAY_ALIAS')
            self.assertEqual(candidates[0]['competition_similarity'],1.0)
            timing=b.countdown_corrob(candidates[0],b.local_kickoff(self.fixtures[fixture_id]['kickoff_madrid']))
            self.assertLessEqual(timing['countdown_difference_minutes'],4.0)

    def test_translations_are_candidate_only_and_unknown_time_stays_unknown(self):
        fixture=self.fixtures[1528966]
        event={**self.events['7905474'],'text_observed_at_utc':None}
        candidate=b.match_catalog(fixture,[event])[0]
        timing=b.countdown_corrob(candidate,b.local_kickoff(fixture['kickoff_madrid']))
        self.assertIsNone(timing['event_start_from_countdown'])
        unknown={**fixture,'league_id':999,'category_status':None,'gender':'UNKNOWN',
                 'category_evidence_status':None,'category_level':None,'category_provenance':None}
        self.assertFalse(b.senior_category_corroborated(unknown,'CATEGORY_UNCONFIRMED',1.0))

    def test_competition_route_prefers_real_dom_link_then_verified_fallback(self):
        fixture=self.fixtures[1528966]
        dom='https://www.bwin.es/es/sports/f%C3%BAtbol-4/apuestas/europa-7/uefa-nations-league-0:99'
        self.assertEqual(b.competition_navigation_url(fixture,{'UEFA Nations League':dom},b.BASE),(dom,'BWIN_DOM_COMPETITION_LINK'))
        url,source=b.competition_navigation_url(fixture,{},b.BASE)
        self.assertIn('uefa-nations-league',url);self.assertEqual(source,'VERIFIED_PUBLIC_COMPETITION_FALLBACK')
        anchors=[{'url':dom,'text':'UEFA Nations League'}]
        self.assertEqual(b.parse_country_nav(anchors,b.BASE)['uefa nations league'],dom)

    def test_batch_navigation_is_competition_only_and_deduplicated(self):
        fixtures=list(self.fixtures.values())
        competition_url='https://www.bwin.es/es/sports/f%C3%BAtbol-4/apuestas/europa-7/uefa-nations-league-0:4'
        events=list(self.events.values())
        with patch.object(b,'read_country_navigation',return_value=({'uefa nations league':competition_url},{'kind':'COUNTRY_NAV_DISCOVERY','status':'OK'})) as nav, \
             patch.object(b,'fetch_catalog',return_value=(events,[{'catalog_page':competition_url,'status':'OK'}])) as fetch:
            catalog,pages,info=b.extend_catalog_for_missing(object(),b.BASE,[],fixtures,4,1,1000)
        self.assertEqual(nav.call_count,1)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(fetch.call_args.args[2],[competition_url])
        self.assertEqual(info['requested_countries'],[])
        self.assertEqual(info['country_pages'],{})
        self.assertEqual({row['bwin_event_id'] for row in catalog},{'7905474','7909660'})
        self.assertTrue(all('africa' not in str(page).lower() and 'alemania' not in str(page).lower()
                            and 'america' not in str(page).lower() for page in pages))


if __name__=='__main__':unittest.main()
