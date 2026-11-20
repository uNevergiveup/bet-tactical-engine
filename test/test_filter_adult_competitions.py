"""Regression tests for expanded adult competitions (offline, zero API requests)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import bwin_discovery
import discover_fixtures
import preselect_matches
from filter_major_leagues import (VERSION, classify, explicit_competition_category,
                                  filter_document, gender_for)


def row(league, *, lid=10001, country='World', season=2026, home='Arsenal', away='Benfica', status='NS'):
    return dict(fixture_id=lid, country=country, league=league, league_id=lid, season=season,
                home=home, away=away, status=status, kickoff_madrid='2026-11-04T21:00:00+01:00')


class AdultCompetitionsTest(unittest.TestCase):
    def test_adult_champions_cups_and_lower_divisions(self):
        curated = [
            ('UEFA Champions League Women', 525, 'World'),
            ('UEFA Nations League', 5, 'World'),
            ('Copa del Rey', 143, 'Spain'),
            ('Segunda División', 141, 'Spain'),
            ('Serie A Women', 139, 'Italy'),
            ('Feminine Division 1', 64, 'France'),
        ]
        for label, lid, country in curated:
            with self.subTest(label=label):
                self.assertEqual(classify(row(label, lid=lid, country=country))[0], 'ELIGIBLE')
        # Categorías desconocidas sin evidencia verificable quedan en REVIEW, no auto-promovidas.
        self.assertEqual(classify(row('Unknown Cup 10001', lid=10001))[0], 'REVIEW')
        # top-tier-and-senior-cups admite Champions femenina y Copa del Rey pero excluye tier 2.
        self.assertEqual(classify(row('UEFA Champions League Women', lid=525, country='World'), policy='top-tier-and-senior-cups')[0], 'ELIGIBLE')
        self.assertEqual(classify(row('Copa del Rey', lid=143, country='Spain'), policy='top-tier-and-senior-cups')[0], 'ELIGIBLE')
        self.assertEqual(classify(row('Segunda División', lid=141, country='Spain'), policy='top-tier-and-senior-cups'), ('EXCLUDED', 'LOWER_DIVISION'))

    def test_youth_competitions_excluded(self):
        for label in ('UEFA Youth League', 'U19 Champions League', 'U-21 Premier League',
                      'Under 20 World Cup', 'Sub-23 Championship', 'Campionato Primavera'):
            with self.subTest(label=label):
                self.assertEqual(classify(row(label))[0], 'EXCLUDED')

    def test_youth_and_reserve_teams_excluded(self):
        for name in ('Manchester City U21', 'Barcelona B', 'Real Madrid II',
                     'Liverpool Reserves', 'Inter Primavera'):
            with self.subTest(name=name):
                self.assertEqual(classify(row('Premier League',away=name))[0], 'EXCLUDED')

    def test_normal_club_names_not_false_positive(self):
        for name in ('Boca Juniors', 'Young Boys'):
            status, reason = classify(row('La Liga', lid=140, country='Spain', home=name))
            self.assertNotIn('YOUTH', reason)
            self.assertNotIn('RESERVE', reason)
            self.assertEqual(status, 'ELIGIBLE')

    def test_legacy_strict_remains_available(self):
        status, reason = classify(row('UEFA Champions League Women', lid=525, country='World'), policy='domestic-top-tier')
        self.assertEqual(status, 'EXCLUDED')
        self.assertEqual(reason, 'CUP_OR_INTERNATIONAL_EXCLUDED_BY_DOMESTIC_TOP_TIER')
        self.assertEqual(classify(row('Premier League',lid=39,country='England'),policy='domestic-top-tier')[0], 'ELIGIBLE')

    def test_non_prematch_and_missing_identity(self):
        self.assertEqual(classify(row('UEFA Champions League',status='FT'))[0], 'EXCLUDED')
        self.assertEqual(classify(row('UEFA Champions League',country=''))[0], 'REVIEW')
        self.assertEqual(classify(row('UEFA Champions League',home=''))[0], 'REVIEW')

    def test_gender_unknown_is_not_assumed_male(self):
        self.assertEqual(gender_for(row("Women's Champions League")), 'F')
        self.assertEqual(gender_for(row('UEFA Champions League')), 'UNKNOWN')
        self.assertEqual(gender_for(row('Premier League',lid=39,country='England')), 'M')

    def test_leganes_category_propagates_through_real_contracts(self):
        api={
          'fixture':{'id':1570019,'date':'2026-11-13T19:30:00+00:00','status':{'short':'NS'},
                     'venue':{'name':'Municipal de Butarque','city':'Leganes'}},
          'league':{'id':141,'name':'Segunda División','country':'Spain','season':2026,
                    'round':'Regular Season - 14'},
          'teams':{'home':{'id':537,'name':'Leganes'},'away':{'id':9580,'name':'Burgos'}},
        }
        discovered=discover_fixtures.parse_fixture(api)
        self.assertEqual((discovered['gender'],discovered['category_status']),('M','ELIGIBLE'))
        provenance=discovered['category_provenance']
        filtered=filter_document({'schema_version':'1.0-discovery','fixtures':[discovered]},b'frozen')
        fixture=filtered['fixtures'][0]
        self.assertEqual(fixture['category_provenance'],provenance)
        selected=preselect_matches.evaluate_fixture(fixture,{}, {}, {}, {},
            datetime(2026,11,13,14,tzinfo=timezone.utc),8,details_selected=False)
        self.assertEqual((selected['gender'],selected['category_status']),('M','ELIGIBLE'))
        self.assertEqual(selected['category_provenance'],provenance)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'preselection.json'
            path.write_text(json.dumps({'matches':[selected]}),encoding='utf-8')
            ready,_=bwin_discovery.read_candidates(path,[1570019],0,True,
                datetime(2026,11,13,14,tzinfo=timezone.utc))
        self.assertEqual((ready[0]['gender'],ready[0]['category_status']),('M','ELIGIBLE'))
        self.assertEqual(ready[0]['category_provenance'],provenance)

    def test_explicit_category_requires_all_identity_fields(self):
        valid={'league_id':141,'league':'Segunda División','country':'Spain','season':2026}
        self.assertIsNotNone(explicit_competition_category(valid))
        for change in ({'country':'Mexico'},{'league':'Segunda División Femenina'},
                       {'league_id':999},{'season':None}):
            with self.subTest(change=change):
                self.assertIsNone(explicit_competition_category({**valid,**change}))

    def test_output_schema_compatible_and_audited(self):
        data={'schema_version':'1.0-discovery','fixtures':[
            row('UEFA Champions League Women',lid=525,country='World'),
            row('UEFA Youth League',lid=10002),
            row('La Liga',lid=140,country='Spain',home='Boca Juniors',away='Young Boys'),
            row('Unknown League',lid=10004,country='World'),
        ]}
        raw=json.dumps(data).encode()
        result=filter_document(data,raw)
        self.assertEqual(result['schema_version'],VERSION)
        self.assertEqual(result['policy']['competition_tier'],'ADULT_OPEN')
        self.assertEqual(result['counts']['eligible'],2)
        self.assertEqual(result['counts']['excluded'],1)
        self.assertEqual(result['counts']['review'],1)
        self.assertEqual(result['source_sha256'], __import__('hashlib').sha256(raw).hexdigest())

    def test_no_duplicate_fixture_ids(self):
        one=row('Premier League')
        with self.assertRaisesRegex(ValueError,'duplicado'):
            filter_document({'schema_version':'1.0-discovery','fixtures':[one,one]},b'{}')


if __name__=='__main__':
    unittest.main()
