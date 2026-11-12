import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import prematch_sos_reference_collector_v1045 as collector
from test_prematch_sos_v1044 import performance_row, perf, BASE, TARGET


def raw_fixture(fid, day, *, home=2, away=50, league=71, season=2026,
                status='FT', shots_home=0, shots_away=7):
    return {
        'fixture': {'id': fid, 'date': (BASE + timedelta(days=day)).isoformat(),
                    'status': {'short': status}},
        'league': {'id': league, 'name': 'Brazil Serie A', 'country': 'Brazil', 'season': season},
        'teams': {'home': {'id': home, 'name': f'Team {home}'},
                  'away': {'id': away, 'name': f'Team {away}'}},
        'goals': {'home': 1, 'away': 0},
    }


class FakeClient:
    def __init__(self, history, stats):
        self.history = history
        self.stats_rows = stats
        self.calls = []
        self.stat = type('Stats', (), {'calls': 0, 'hits': 0, 'max_calls': 20})()

    def get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        self.stat.calls += 1
        if endpoint == 'fixtures':
            return list(self.history)
        if endpoint == 'fixtures/statistics':
            fid = params['fixture']
            return self.stats_rows.get(fid, [])
        raise AssertionError(endpoint)


class BudgetClient(FakeClient):
    def get(self, endpoint, **params):
        self.stat.calls += 1
        raise collector.BudgetExhausted('BUDGET_MAX_CALLS_1')


class SequenceStatsClient(FakeClient):
    def __init__(self, history, responses):
        super().__init__(history, {})
        self.responses = {fid: list(values) for fid, values in responses.items()}

    def get(self, endpoint, **params):
        if endpoint == 'fixtures/statistics':
            self.calls.append((endpoint, params))
            self.stat.calls += 1
            queue = self.responses[params['fixture']]
            return queue.pop(0) if queue else []
        return super().get(endpoint, **params)


def write_target(root: Path):
    folder = root / 'outputs/matches/123/analysis/deep_v1042'
    folder.mkdir(parents=True)
    data = perf([performance_row(11, date=15, tid=1, oid=2)],
                away=[performance_row(12, date=14, tid=3, oid=4, venue='AWAY')])
    data['event_id_bwin'] = '123'
    data['target_fixture_id'] = 999
    data['target_kickoff_utc'] = TARGET.isoformat()
    data['analysis_cutoff_utc'] = (TARGET - timedelta(hours=2)).isoformat()
    (folder / 'rendimiento_profundo_123.json').write_text(json.dumps(data))
    adjustment = {'teams': {'home': {'reference_gaps_shots_for': [
        {'opponent_id': 2, 'league_id': 71, 'season': 2026,
         'affected_fixture_ids': [11], 'team_venue': 'HOME'}]},
        'away': {'reference_gaps_shots_for': []}}}
    (folder / 'ajuste_rivales_123.json').write_text(json.dumps(adjustment))
    return folder


class CollectorTests(unittest.TestCase):
    def test_dedup_temporal_filter_and_missing_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            history = [
                raw_fixture(100, 1, home=50, away=2, shots_home=0),  # rival AWAY: observed zero
                raw_fixture(100, 1, home=50, away=2),  # duplicate ID
                raw_fixture(101, 10, home=60, away=2),  # before target, inverted locality
                raw_fixture(999, 14, home=2, away=1),  # objetivo: exclude
                raw_fixture(102, 20, home=2, away=70),  # before target cutoff, after target historical row
                raw_fixture(103, 80, home=2, away=71),  # after cutoff: exclude
                raw_fixture(104, 2, home=2, away=72, status='AET'),  # 120 min: exclude
            ]
            stats = {
                    100: [{'team': {'id': 2}, 'statistics': [
                    {'type': 'Total Shots', 'value': 0},
                    {'type': 'Shots on Goal', 'value': 0},
                    {'type': 'Corner Kicks', 'value': 3},
                    # xG intentionally absent -> null
                ]}, {'team': {'id': 50}, 'statistics': [
                    {'type': 'Total Shots', 'value': 8}]}],
                101: [{'team': {'id': 2}, 'statistics': []},
                      {'team': {'id': 60}, 'statistics': []}],
                102: [{'team': {'id': 2}, 'statistics': []},
                      {'team': {'id': 70}, 'statistics': []}],
            }
            result = collector.collect(root=root, event_id='123', client=FakeClient(history, stats),
                                       max_api_calls=10)
            ids = [g['fixture_id'] for g in result['pool']['games']]
            self.assertIn(100, ids)
            self.assertNotIn(999, ids)
            self.assertNotIn(103, ids)
            self.assertNotIn(104, ids)
            self.assertEqual(ids.count(100), 1)
            game = next(g for g in result['pool']['games'] if g['fixture_id'] == 100)
            self.assertEqual(game['own']['shots'], 0.0)
            self.assertIsNone(game['own']['xg'])
            self.assertEqual(game['venue'], 'AWAY')
            self.assertEqual(game['home']['stats']['goals'], 1.0)
            self.assertEqual(game['away']['stats']['goals'], 0.0)
            self.assertTrue(any(x['fixture_id'] == 104 for x in result['manifest']['period_exclusions']))
            self.assertNotIn(102, ids)  # posterior al histórico afectado: no se selecciona
            analysis = __import__('prematch_sos_v1044').analyze(
                json.loads((root / 'outputs/matches/123/analysis/deep_v1042/rendimiento_profundo_123.json').read_text()),
                extra_pool=result['pool'])
            evidence_ids = analysis['teams']['home']['evidence']['shots_for'][0]['reference_evidence']['opponent_prior_fixture_ids']
            self.assertNotIn(102, evidence_ids)  # posterior al partido histórico de referencia
            self.assertEqual(result['manifest']['original_cutoff_utc'],
                             '2026-10-31T22:00:00+00:00')

    def test_pilot_is_dry_run_and_does_not_require_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            result = collector.collect(root=root, event_id='123', dry_run=True,
                                       pilot=True, max_api_calls=0)
            self.assertTrue(result['manifest']['usage']['dry_run'])
            self.assertEqual(result['manifest']['usage']['api_calls'], 0)
            self.assertEqual(result['manifest']['rival_selection']['requested_rivals'], 1)

    def test_dry_run_never_replaces_existing_enriched_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = write_target(root)
            definitive = folder / 'pool_referencias_v1045_123.json'
            definitive.write_text(json.dumps({'schema': 'prematch-sos-reference-pool-1.0',
                                              'games': [{'fixture_id': 777}]}))
            collector.collect(root=root, event_id='123', dry_run=True, max_api_calls=0)
            self.assertEqual(json.loads(definitive.read_text())['games'][0]['fixture_id'], 777)
            self.assertTrue((folder / 'pool_referencias_v1045_123_dryrun.json').is_file())

    def test_enriched_pool_is_reused_without_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            raw = raw_fixture(100, 1, home=50, away=2)
            stats = {100: [{'team': {'id': 2}, 'statistics': [{'type': 'Total Shots', 'value': 9}]},
                           {'team': {'id': 50}, 'statistics': [{'type': 'Total Shots', 'value': 8}]}]}
            collector.collect(root=root, event_id='123', client=FakeClient([raw], stats), max_api_calls=10)
            second = FakeClient([], {})
            result = collector.collect(root=root, event_id='123', client=second,
                                       max_api_calls=10, resume=False)
            self.assertTrue(result['manifest']['pool_reused_without_resume'])
            self.assertIn(100, [g['fixture_id'] for g in result['pool']['games']])
            self.assertFalse(any(endpoint == 'fixtures/statistics' for endpoint, _ in second.calls))

    def test_empty_statistics_remains_pending_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            raw = raw_fixture(100, 1, home=50, away=2)
            client = SequenceStatsClient([raw], {100: [[], [], [
                {'team': {'id': 2}, 'statistics': [{'type': 'Total Shots', 'value': 5}]},
                {'team': {'id': 50}, 'statistics': [{'type': 'Total Shots', 'value': 4}]},
            ]]})
            first = collector.collect(root=root, event_id='123', client=client, max_api_calls=10)
            self.assertTrue(any(x['reason'] == 'STATISTICS_EMPTY_OR_UNUSABLE'
                                for x in first['manifest']['pending']))
            # The fixture was not promoted to the pool until a later run.
            self.assertNotIn(100, [g['fixture_id'] for g in first['pool']['games']])
            second_client = SequenceStatsClient([raw], {100: [[
                {'team': {'id': 2}, 'statistics': [{'type': 'Total Shots', 'value': 5}]},
                {'team': {'id': 50}, 'statistics': [{'type': 'Total Shots', 'value': 4}]},
            ]]})
            second = collector.collect(root=root, event_id='123', client=second_client,
                                       max_api_calls=10, resume=True)
            self.assertIn(100, [g['fixture_id'] for g in second['pool']['games']])

    def test_reference_selection_requires_scope_and_individual_asof(self):
        affected = BASE + timedelta(days=20)
        performance = {'teams': {'home': {'team_id': 1, 'historical_fixtures': [{
            'fixture_id': 500, 'kickoff_utc': affected.isoformat(), 'league_id': 71,
            'season': 2026, 'venue': 'HOME', 'opponent': {'id': 2}}]}, 'away': {}}}
        rival = {'opponent_id': 2, 'scopes': [{'league_id': 71, 'season': 2026,
                                               'team_venue': 'HOME', 'affected_fixture_ids': [500]}]}
        history = [raw_fixture(1, 25, home=9, away=2),   # after affected: reject
                   raw_fixture(2, 19, home=9, away=2),   # valid closest prior
                   raw_fixture(3, 18, home=2, away=9),   # wrong rival venue
                   raw_fixture(4, 17, home=9, away=2, league=13),  # wrong competition
                   raw_fixture(5, 16, home=9, away=2)]
        selected = collector._select_reference_rows(history, rival,
                                                     collector._affected_history_index(performance), 20)
        self.assertEqual([r['fixture']['id'] for r in selected], [2, 5])
        self.assertTrue(all(r['_matched_scopes'][0]['affected_fixture_id'] == 500 for r in selected))

    def test_missing_adjustment_is_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = write_target(root)
            (folder / 'ajuste_rivales_123.json').unlink()
            with self.assertRaisesRegex(RuntimeError, 'SOS_INITIAL_REQUIRED'):
                collector.collect(root=root, event_id='123', dry_run=True)

    def test_budget_exhaustion_is_recorded_and_resume_reuses_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            first = collector.collect(root=root, event_id='123', client=BudgetClient([], {}),
                                      max_api_calls=1)
            self.assertTrue(first['manifest']['usage']['budget_exhausted'])
            second = collector.collect(root=root, event_id='123', client=FakeClient([], {}),
                                       resume=True, dry_run=True)
            self.assertTrue(Path(second['pool_path']).is_file())

    def test_resume_does_not_repeat_completed_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            raw = raw_fixture(100, 1, home=2, away=50)
            stats = {100: [{'team': {'id': 2}, 'statistics': [{'type': 'Total Shots', 'value': 4}]},
                           {'team': {'id': 50}, 'statistics': [{'type': 'Total Shots', 'value': 3}]}]}
            first_client = FakeClient([raw], stats)
            collector.collect(root=root, event_id='123', client=first_client, max_api_calls=10)
            second_client = FakeClient([raw], stats)
            collector.collect(root=root, event_id='123', client=second_client,
                              max_api_calls=10, resume=True)
            self.assertFalse(any(endpoint == 'fixtures/statistics'
                                 for endpoint, _ in second_client.calls))

    def test_online_without_credential_is_explicit_error_not_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_target(root)
            if collector.CONFIGURED_FOOTBALL_API_KEY:
                self.skipTest('credential configured in this environment')
            with self.assertRaises(ValueError):
                collector.collect(root=root, event_id='123', dry_run=False,
                                  max_api_calls=1)

    def test_same_rival_in_two_competitions_is_one_query_with_two_scopes(self):
        performance = perf([performance_row(11, date=15, tid=1, oid=2)], away=[])
        adjustment = {'teams': {'home': {'reference_gaps_shots_for': [
            {'opponent_id': 2, 'league_id': 71, 'season': 2026,
             'affected_fixture_ids': [11], 'team_venue': 'HOME'},
            {'opponent_id': 2, 'league_id': 13, 'season': 2026,
             'affected_fixture_ids': [12], 'team_venue': 'AWAY'}]},
            'away': {'reference_gaps_shots_for': []}}}
        rivals = collector._gap_rivals(adjustment, performance, pilot=False)
        self.assertEqual(len(rivals), 1)
        self.assertEqual(len(rivals[0]['scopes']), 2)

    def test_pilot_uses_kickoff_and_separate_home_away_last_five(self):
        rows = []
        # Fixture IDs intentionally contradict chronology.
        for i, (fid, day, venue, oid) in enumerate([
                (900, 30, 'HOME', 2), (100, 20, 'HOME', 3),
                (800, 10, 'HOME', 4), (700, 9, 'AWAY', 11),
                (600, 8, 'AWAY', 6), (500, 7, 'AWAY', 7),
                (400, 6, 'AWAY', 8), (300, 5, 'AWAY', 12),
                (200, 4, 'AWAY', 10), (50, 3, 'AWAY', 11)]):
            rows.append({'fixture_id': fid, 'kickoff_utc': (BASE + timedelta(days=day)).isoformat(),
                         'venue': venue, 'opponent': {'id': oid}})
        performance = {'teams': {'home': {'team_id': 1, 'historical_fixtures': rows},
                                 'away': {'team_id': 9, 'historical_fixtures': []}}}
        gaps = {'teams': {'home': {'reference_gaps_shots_for': [
            {'opponent_id': 2, 'league_id': 71, 'season': 2026, 'affected_fixture_ids': [900], 'team_venue': 'AWAY'},
            {'opponent_id': 11, 'league_id': 71, 'season': 2026, 'affected_fixture_ids': [700], 'team_venue': 'AWAY'},
            {'opponent_id': 12, 'league_id': 71, 'season': 2026, 'affected_fixture_ids': [300], 'team_venue': 'AWAY'}]},
            'away': {}}}
        rivals = collector._gap_rivals(gaps, performance, pilot=True)
        self.assertEqual([x['opponent_id'] for x in rivals], [2, 11])
        self.assertGreater(rivals[0]['priority']['L5'], 0)
        self.assertIn({'team_id': 1, 'venue': 'HOME'}, rivals[0]['priority']['venues'])

    def test_candidate_covering_more_affected_games_beats_newer_single_match(self):
        performance = {'teams': {'home': {'team_id': 1, 'historical_fixtures': [
            {'fixture_id': 500, 'kickoff_utc': (BASE + timedelta(days=20)).isoformat(),
             'league_id': 71, 'season': 2026, 'venue': 'HOME', 'opponent': {'id': 2}},
            {'fixture_id': 501, 'kickoff_utc': (BASE + timedelta(days=21)).isoformat(),
             'league_id': 71, 'season': 2026, 'venue': 'HOME', 'opponent': {'id': 2}}]}, 'away': {}}}
        rival = {'opponent_id': 2, 'scopes': [{'league_id': 71, 'season': 2026,
                                               'team_venue': 'HOME',
                                               'affected_fixture_ids': [500, 501]}]}
        history = [raw_fixture(10, 20, home=9, away=2),
                   raw_fixture(11, 18, home=9, away=2)]
        selected = collector._select_reference_rows(history, rival,
                                                     collector._affected_history_index(performance), 2)
        self.assertEqual([r['fixture']['id'] for r in selected], [11, 10])
        self.assertEqual(len(selected[0]['_matched_scopes']), 2)

    def test_budget_distribution_persists_pending_rivals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = write_target(root)
            adjustment = {'teams': {'home': {'reference_gaps_shots_for': [
                {'opponent_id': i, 'league_id': 71, 'season': 2026,
                 'affected_fixture_ids': [11], 'team_venue': 'HOME'} for i in (2, 3, 4, 5)]},
                'away': {'reference_gaps_shots_for': []}}}
            (folder / 'ajuste_rivales_123.json').write_text(json.dumps(adjustment))
            result = collector.collect(root=root, event_id='123', client=FakeClient([], {}),
                                       max_api_calls=2)
            pending = result['manifest']['pending']
            self.assertTrue(any(x['stage'] == 'fixtures' for x in pending))
            self.assertTrue(Path(result['pending_path']).is_file())

    def test_recalculate_uses_pool_without_api_and_preserves_not_estimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = write_target(root)
            # Add the identity/quality inputs required by the SOS integration.
            (root / 'outputs/matches/123/inputs').mkdir(parents=True)
            (root / 'outputs/matches/123/inputs/match_identity.json').write_text(json.dumps({
                'bwin_event_id': '123', 'api_fixture_id': 999,
                'kickoff_utc': TARGET.isoformat(),
                'identity_status': 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN'}))
            (folder / 'auditoria_calidad_123.json').write_text(json.dumps({
                'event_id_bwin': '123', 'target_fixture_id': 999,
                'quality_status': 'REVIEW_REQUIRED', 'issues': []}))
            result = collector.collect(root=root, event_id='123', dry_run=True)
            jpath, _, report = collector.recalculate_without_api(
                root=root, event_id='123', pool_path=Path(result['pool_path']))
            self.assertTrue(jpath.is_file())
            self.assertEqual(report['teams']['home']['windows']['shots_for']['L5']['status'],
                             'INCOMPLETE_WINDOW')


if __name__ == '__main__':
    unittest.main()
