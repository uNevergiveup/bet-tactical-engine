import unittest
from datetime import datetime, timezone

from generate_context import (
    APITeam,
    BwinParser,
    BwinMatch,
    ContextualTeamResolver,
    resolve_full_pair_from_competition_fixture,
    contextual_name_variants,
    bwin_fixture_names_match,
    fixture_in_event_window,
    temporal_identity_check,
    fixture_temporal_eligibility,
)
from match_identity import identity_from_payload, validate_identity


def fixture_row(*, fixture_id=1636223, home_id=168, away_id=81,
                home='Bayer Leverkusen', away='Marseille',
                league_id=3, season=2026,
                date='2026-11-05T20:00:00+00:00'):
    return {
        'fixture': {'id': fixture_id, 'date': date, 'status': {'short': 'NS'}},
        'league': {'id': league_id, 'name': 'UEFA Europa League',
                   'country': 'World', 'season': season},
        'teams': {
            'home': {'id': home_id, 'name': home},
            'away': {'id': away_id, 'name': away},
        },
    }


class FakeApi:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        if endpoint != 'fixtures':
            return {'response': []}
        if 'id' in params:
            return {'response': [r for r in self.rows
                                 if r['fixture']['id'] == params['id']]}
        if 'date' in params:
            return {'response': [r for r in self.rows
                                 if r['fixture']['date'].startswith(params['date'])]}
        if 'team' in params:
            return {'response': [r for r in self.rows
                                 if params['team'] in (r['teams']['home']['id'], r['teams']['away']['id'])]}
        return {'response': []}


class EmptyResolver:
    def candidates_for_pair(self, name, gender, max_candidates=4):
        return []


def match():
    return BwinMatch(
        url='https://www.bwin.es/es/sports/eventos/bayer-leverkusen-vs-o-marsella-2:7883704',
        event_id=7883704,
        home_name_raw='Bayer Leverkusen', away_name_raw='O. Marsella',
        home_name='Bayer Leverkusen', away_name='O. Marsella',
        home_gender='male', away_gender='male',
        tournament_id=3, competition_display='Europa League',
        event_date='2026-11-05T20:00:00+00:00',
    )


def palmeiras_match():
    return BwinMatch(
        url='https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
        event_id=7892146,
        home_name_raw='Palmeiras', away_name_raw='Clube do Remo',
        # Tras la resolución nominal, API-Football usa la forma canónica Remo.
        home_name='Palmeiras', away_name='Remo',
        home_gender='male', away_gender='male',
        tournament_id=71, competition_display='Brazil Serie A',
        event_date='2026-11-05T23:30:00+01:00',
    )


class IdentityResolutionTests(unittest.TestCase):
    def test_bwin_parser_normalizes_madrid_kickoff_to_utc(self):
        parsed = BwinParser().parse(
            'https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
            '<title>Palmeiras - Clube do Remo | Bwin</title>'
            '<div>7892146 Palmeiras Clube do Remo 5/11/26 23:30</div>',
        )
        self.assertEqual(parsed.event_date, '2026-11-05T23:30:00+01:00')

    def test_bwin_parser_rejects_multiple_event_dates_but_ignores_distant_other_event(self):
        ambiguous = BwinParser().parse(
            'https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
            '<title>Palmeiras - Clube do Remo | Bwin</title>'
            '<div>7892146 5/11/26 23:30 y 6/11/26 00:30</div>',
        )
        self.assertIsNone(ambiguous.event_date)
        distant = BwinParser().parse(
            'https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
            '<title>Palmeiras - Clube do Remo | Bwin</title>'
            '<div>Otro evento 01/01/26 12:00</div>'
            + ('x' * 1200) + '<div>7892146 5/11/26 23:30</div>',
        )
        self.assertEqual(distant.event_date, '2026-11-05T23:30:00+01:00')

    def test_bwin_parser_marks_absent_kickoff_unknown(self):
        parsed = BwinParser().parse(
            'https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
            '<title>Palmeiras - Clube do Remo | Bwin</title><div>7892146</div>',
        )
        self.assertIsNone(parsed.event_date)

    def test_structured_event_metadata_has_priority_over_html_update_timestamp(self):
        parsed = BwinParser().parse(
            'https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
            '<title>Palmeiras - Clube do Remo | Bwin</title>'
            '<script type="application/json">'
            '{"eventId":"7892146","startTime":"2026-11-05T22:30:00Z",'
            '"date":"2026-11-02T20:32:53Z","updatedAt":"2026-11-02T20:32:53Z"}</script>'
            '<div>7892146 1/1/26 12:00</div>',
        )
        self.assertEqual(parsed.event_date, '2026-11-05T23:30:00+01:00')

    def test_temporal_identity_handles_europe_midnight_and_cet(self):
        current = match()
        current.event_date = '2026-11-06T00:15:00+01:00'
        row = fixture_row(date='2026-11-05T23:15:00+00:00')
        ok, reason, diagnostic = temporal_identity_check(current, row)
        self.assertTrue(ok)
        self.assertEqual(reason, 'OK')
        self.assertEqual(diagnostic['difference_minutes'], 0.0)
        self.assertTrue(fixture_in_event_window(current, row))

    def test_temporal_identity_handles_cest_and_unverified_time(self):
        current = match()
        current.event_date = '2026-07-05T23:30:00+02:00'
        row = fixture_row(date='2026-07-05T21:30:00+00:00')
        ok, reason, diagnostic = temporal_identity_check(current, row)
        self.assertTrue(ok)
        self.assertEqual(diagnostic['bwin_offset_minutes'], 120)
        current.event_date = None
        ok, reason, diagnostic = temporal_identity_check(current, row)
        self.assertFalse(ok)
        self.assertEqual(reason, 'BWIN_KICKOFF_UNVERIFIED')
        self.assertIsNone(diagnostic['bwin_kickoff_utc'])

    def test_future_api_fixture_without_bwin_time_is_provisional_only(self):
        current = palmeiras_match()
        current.event_date = None
        row = fixture_row(
            fixture_id=1492447, home_id=121, away_id=1198,
            home='Palmeiras', away='Remo', league_id=71,
            date='2099-11-05T22:30:00+00:00',
        )
        row['league'].update(name='Brazil Serie A', country='Brazil')
        ok, status, reason, _ = fixture_temporal_eligibility(
            current, row, reference_now=datetime(2099, 11, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(ok)
        self.assertEqual(status, 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN')
        self.assertEqual(reason, 'BWIN_KICKOFF_UNVERIFIED')

    def test_identity_bridge_preserves_provisional_status(self):
        payload = {
            'match': {
                'event_id': 7892146,
                'url': 'https://www.bwin.es/es/sports/eventos/palmeiras-clube-do-remo-2:7892146',
                'identity_status': 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN',
                'home_name_raw': 'Palmeiras', 'away_name_raw': 'Clube do Remo',
                'home_gender': 'male', 'away_gender': 'male',
            },
            'fixture': {
                'fixture_id': 1492447, 'date': '2026-11-05T22:30:00+00:00',
                'home_id': 121, 'home_name': 'Palmeiras',
                'away_id': 1198, 'away_name': 'Remo',
                'league_id': 71, 'season': 2026, 'league_name': 'Brazil Serie A',
            },
        }
        identity = identity_from_payload(payload)
        self.assertEqual(identity['identity_status'], 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN')
        self.assertTrue(validate_identity(identity, payload, '7892146', payload['match']['url']))

    def test_alias_is_contextual_not_global(self):
        self.assertNotIn('marseille', contextual_name_variants('O. Marsella'))
        self.assertIn(
            'marseille',
            contextual_name_variants('O. Marsella', counterpart='Bayer Leverkusen'),
        )

    def test_calendar_recovery_works_without_team_search_candidates(self):
        api = FakeApi([fixture_row()])
        home, away = resolve_full_pair_from_competition_fixture(
            match(), api, EmptyResolver(), None, None,
            reference_now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
        )
        self.assertEqual((home.id, away.id), (168, 81))
        self.assertEqual(match().verified_fixture_id, None)
        # Re-run against the actual match object to assert the verified ID.
        current = match()
        home, away = resolve_full_pair_from_competition_fixture(
            current, api, EmptyResolver(), None, None,
            reference_now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
        )
        self.assertEqual((home.id, away.id, current.verified_fixture_id), (168, 81, 1636223))

    def test_palmeiras_remo_fixture_uses_utc_kickoff_without_forcing_id(self):
        current = palmeiras_match()
        row = fixture_row(
            fixture_id=1492447, home_id=121, away_id=1198,
            home='Palmeiras', away='Remo', league_id=71,
            date='2026-11-05T22:30:00+00:00',
        )
        row['league'].update(name='Brazil Serie A', country='Brazil')
        home, away = resolve_full_pair_from_competition_fixture(
            current, FakeApi([row]), EmptyResolver(), None, None,
            reference_now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
        )
        self.assertEqual((home.id, away.id), (121, 1198))
        self.assertEqual(current.verified_fixture_id, 1492447)

    def test_exact_resolved_ids_allow_clube_do_remo_name_difference(self):
        current = palmeiras_match()
        current.away_name = 'Clube do Remo'
        row = fixture_row(
            fixture_id=1492447, home_id=121, away_id=1198,
            home='Palmeiras', away='Remo', league_id=71,
            date='2026-11-05T22:30:00+00:00',
        )
        row['league'].update(name='Brazil Serie A', country='Brazil')
        home = APITeam(121, 'Palmeiras', None, 'Brazil', None, False, 'male', None, None)
        away = APITeam(1198, 'Clube do Remo', None, 'Brazil', None, False, 'male', None, None)
        self.assertTrue(bwin_fixture_names_match(current, row))
        self.assertTrue(bwin_fixture_names_match(
            current, row, expected_home_id=121, expected_away_id=1198))
        resolved_home, resolved_away = resolve_full_pair_from_competition_fixture(
            current, FakeApi([row]), EmptyResolver(), home, away,
            reference_now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
        )
        self.assertEqual((resolved_home.id, resolved_away.id), (121, 1198))
        self.assertEqual(current.verified_fixture_id, 1492447)

    def test_homonym_resolved_id_does_not_pass_initial_pair_check(self):
        current = palmeiras_match()
        row = fixture_row(
            fixture_id=1492447, home_id=9999, away_id=1198,
            home='Palmeiras', away='Remo', league_id=71,
            date='2026-11-05T22:30:00+00:00',
        )
        row['league'].update(name='Brazil Serie A', country='Brazil')
        self.assertFalse(bwin_fixture_names_match(
            current, row, expected_home_id=121, expected_away_id=1198))

    def test_reserve_women_inverted_locality_and_other_competition_are_rejected(self):
        current = palmeiras_match()
        rows = [
            fixture_row(fixture_id=1492450, home_id=121, away_id=1198,
                        home='Palmeiras U20', away='Remo', league_id=71,
                        date='2026-11-05T22:30:00+00:00'),
            fixture_row(fixture_id=1492451, home_id=121, away_id=1198,
                        home='Palmeiras Women', away='Remo Women', league_id=71,
                        date='2026-11-05T22:30:00+00:00'),
            fixture_row(fixture_id=1492452, home_id=1198, away_id=121,
                        home='Remo', away='Palmeiras', league_id=71,
                        date='2026-11-05T22:30:00+00:00'),
            fixture_row(fixture_id=1492453, home_id=121, away_id=1198,
                        home='Palmeiras', away='Remo', league_id=72,
                        date='2026-11-05T22:30:00+00:00'),
        ]
        current.event_date = None
        current.competition_display = 'Brazil Serie A'
        home, away = resolve_full_pair_from_competition_fixture(
            current, FakeApi(rows), EmptyResolver(),
            APITeam(121, 'Palmeiras', None, 'Brazil', None, False, 'male', None, None),
            APITeam(1198, 'Remo', None, 'Brazil', None, False, 'male', None, None),
            reference_now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
        )
        self.assertEqual((home.id, away.id), (121, 1198))
        self.assertIsNone(current.verified_fixture_id)

    def test_contextual_resolver_recovers_marseille_from_bayer_anchor(self):
        api = FakeApi([fixture_row()])
        resolved = ContextualTeamResolver(api).resolve_from_opponent_fixtures(
            'O. Marsella', 'male',
            APITeam(168, 'Bayer Leverkusen', None, 'Germany', None, False,
                    'male', None, None),
        )
        self.assertIsNotNone(resolved)
        self.assertEqual((resolved.id, resolved.name), (81, 'Marseille'))

    def test_wrong_league_season_or_locality_never_resolves(self):
        rows = [
            fixture_row(league_id=2),
            fixture_row(fixture_id=1636224, season=2025),
            fixture_row(fixture_id=1636225, home_id=81, away_id=168,
                        home='Marseille', away='Bayer Leverkusen'),
        ]
        current = match()
        home, away = resolve_full_pair_from_competition_fixture(
            current, FakeApi(rows), EmptyResolver(), None, None,
            reference_now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
        )
        self.assertIsNone(home)
        self.assertIsNone(away)
        self.assertIsNone(current.verified_fixture_id)

    def test_fixture_name_check_accepts_alias_only_in_pair_context(self):
        self.assertTrue(bwin_fixture_names_match(match(), fixture_row()))


if __name__ == '__main__':
    unittest.main()
