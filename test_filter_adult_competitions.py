"""Regression tests for expanded adult competitions (offline, zero API requests)."""
import json
import unittest

from filter_major_leagues import VERSION, classify, filter_document, gender_for


def row(league, *, lid=10001, country='World', home='Arsenal', away='Benfica', status='NS'):
    return dict(fixture_id=lid, country=country, league=league, league_id=lid,
                home=home, away=away, status=status, kickoff_madrid='2026-11-04T21:00:00+01:00')


class AdultCompetitionsTest(unittest.TestCase):
    def test_adult_champions_cups_and_lower_divisions(self):
        for label in ('UEFA Champions League', 'Europa League', 'Copa del Rey',
                      'FA Cup', 'Primera Nacional', 'Segunda División',
                      'National 2', 'Club Friendlies', 'Africa Cup of Nations'):
            with self.subTest(label=label):
                self.assertEqual(classify(row(label))[0], 'ELIGIBLE')

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
        self.assertEqual(classify(row('Argentine Primera División',home='Boca Juniors'))[0], 'ELIGIBLE')
        self.assertEqual(classify(row('Swiss Super League',home='Young Boys'))[0], 'ELIGIBLE')

    def test_legacy_strict_remains_available(self):
        self.assertEqual(classify(row('UEFA Champions League'),policy='domestic-top-tier')[0], 'EXCLUDED')
        self.assertEqual(classify(row('Premier League',lid=39,country='England'),policy='domestic-top-tier')[0], 'ELIGIBLE')

    def test_non_prematch_and_missing_identity(self):
        self.assertEqual(classify(row('UEFA Champions League',status='FT'))[0], 'EXCLUDED')
        self.assertEqual(classify(row('UEFA Champions League',country=''))[0], 'REVIEW')
        self.assertEqual(classify(row('UEFA Champions League',home=''))[0], 'REVIEW')

    def test_gender_unknown_is_not_assumed_male(self):
        self.assertEqual(gender_for(row("Women's Champions League")), 'F')
        self.assertEqual(gender_for(row('UEFA Champions League')), 'UNKNOWN')
        self.assertEqual(gender_for(row('Premier League',lid=39,country='England')), 'M')

    def test_output_schema_compatible_and_audited(self):
        data={'schema_version':'1.0-discovery','fixtures':[
            row('UEFA Champions League',lid=10001),
            row('UEFA Youth League',lid=10002),
            row('Premier League',lid=10003,home='Boca Juniors',away='Young Boys'),
        ]}
        raw=json.dumps(data).encode()
        result=filter_document(data,raw)
        self.assertEqual(result['schema_version'],VERSION)
        self.assertEqual(result['policy']['competition_tier'],'ADULT_OPEN')
        self.assertEqual(result['counts']['eligible'],2)
        self.assertEqual(result['counts']['excluded'],1)
        self.assertEqual(result['counts']['eligible_gender_unknown'],2)
        self.assertEqual(result['source_sha256'], __import__('hashlib').sha256(raw).hexdigest())

    def test_no_duplicate_fixture_ids(self):
        one=row('Premier League')
        with self.assertRaisesRegex(ValueError,'duplicado'):
            filter_document({'schema_version':'1.0-discovery','fixtures':[one,one]},b'{}')


if __name__=='__main__':
    unittest.main()
