import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

import prematch_sos_v1044 as sos

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
TARGET = datetime(2026, 11, 1, tzinfo=timezone.utc)


def dt(day):
    return (BASE + timedelta(days=day)).isoformat()


def performance_row(fid, *, date=15, tid=1, oid=2, venue='HOME', league=71,
                    shots=10.0, xg=1.2, opp_shots=8.0):
    return {'fixture_id': fid, 'kickoff_utc': dt(date), 'league_id': league,
            'season': 2026, 'status': 'FT', 'venue': venue,
            'opponent': {'id':oid,'name':f'Opponent {oid}'},
            'own': {'shots': shots, 'on_target': 3., 'corners': 4., 'xg': xg},
            'conceded': {'shots': opp_shots, 'on_target': 2., 'corners': 3., 'xg': 0.8},
            'goals_for': 1, 'goals_against': 1}


def perf(rows, away=None):
    return {'event_id_bwin':'123', 'target_fixture_id':999,
            'target_kickoff_utc':TARGET.isoformat(),
            'analysis_cutoff_utc':(TARGET - timedelta(hours=2)).isoformat(),
            'teams': {'home':{'team_id':1, 'team_name':'A', 'historical_fixtures':rows},
                      'away':{'team_id':3, 'team_name':'B','historical_fixtures':away or []}}}


def pool_game(fid, date, home, away, shots1=10., shots2=10., league=71, season=2026):
    stats1 = {'shots':shots1, 'on_target':4., 'corners':5., 'xg':1.5}
    stats2 = {'shots':shots2, 'on_target':3., 'corners':4., 'xg':1.0}
    return {'fixture_id':fid,'kickoff_utc':dt(date),'league_id':league,
            'season':season,'status':'FT',
            'home':{'id':home,'stats':stats1},'away':{'id':away,'stats':stats2}}


def extra(games):
    return {'schema':'prematch-sos-reference-pool-1.0', 'games':games}


class Tests(unittest.TestCase):
    def test_no_reference_no_fabricated_adjustment(self):
        out=sos.analyze(perf([performance_row(11)]))
        q=out['teams']['home']['windows']['shots_for']['L5']
        self.assertEqual(q['raw_mean'], 10)
        self.assertIsNone(q['adjusted_descriptive_mean'])
        self.assertEqual(q['adjusted_metric_n'], 0)

    def test_l20_incomplete_window_not_accepted(self):
        out=sos.analyze(perf([performance_row(11)]))
        q=out['teams']['home']['windows']['shots_for']['L20']
        self.assertEqual(q['status'], 'INCOMPLETE_WINDOW')
        self.assertFalse(q['window_complete'])

    def test_missing_is_not_zero_but_real_zero_is(self):
        rows=[performance_row(11, shots=0),performance_row(12, shots=None)]
        out=sos.analyze(perf(rows))
        q=out['teams']['home']['windows']['shots_for']['L5']
        self.assertEqual(q['raw_metric_n'], 1)
        self.assertEqual(q['raw_mean'], 0)

    def test_strict_asof_excludes_future_opponent_games(self):
        g = [pool_game(100+i, date=i, home=2, away=100+i, shots2=9.) for i in (1,2,3)]
        g += [pool_game(200, date=16, home=2, away=199, shots2=200.)]
        target=performance_row(11, date=15, oid=2)
        p=sos.analyze(perf([target]), extra_pool=extra(g))
        evidence=p['teams']['home']['evidence']['shots_for'][0]['reference_evidence']
        self.assertEqual(evidence['opponent_prior_n'],3)
        self.assertNotIn(200, evidence['opponent_prior_fixture_ids'])

    def test_adjustment_with_sufficient_pre_event_venue_data(self):
        # 12+ same-league prior fixtures from 8+ teams, opponent #2 visited multiple grounds.
        games=[pool_game(100+i, date=i+1, home=20+i, away=2 if i<4 else 60+i,
                         shots1=20 if i<4 else 10,shots2=8) for i in range(13)]
        # Five matches in L5; at least three have references available.
        rows=[performance_row(800+i, date=15+i, tid=1, oid=2, venue='HOME', shots=16.)
              for i in range(5)]
        out=sos.analyze(perf(rows),extra_pool=extra(games))
        q=out['teams']['home']['windows']['shots_for']['L5']
        self.assertGreaterEqual(q['adjusted_metric_n'],3)
        self.assertEqual(q['status'],'DESCRIPTIVE_PARTIAL_REFERENCE')
        self.assertIsNotNone(q['adjusted_descriptive_mean'])
        self.assertNotEqual(q['adjusted_descriptive_mean'],q['raw_matched_mean'])
        self.assertGreater(q['raw_matched_mean'],q['adjusted_descriptive_mean'])

    def test_separate_competitions(self):
        games=[pool_game(100+i,date=i+1,home=20+i,away=2,league=2) for i in range(15)]
        out=sos.analyze(perf([performance_row(11,league=71)]),extra_pool=extra(games))
        e=out['teams']['home']['evidence']['shots_for'][0]['reference_evidence']
        self.assertEqual(e['opponent_prior_n'],0)

    def test_dedup_fixture_between_team_histories(self):
        a=performance_row(11,tid=1,oid=3,venue='HOME')
        b=performance_row(11,tid=3,oid=1,venue='AWAY',shots=8,opp_shots=10)
        out=sos.analyze(perf([a],away=[b]))
        self.assertEqual(out['reference']['unique_fixtures'],1)

    def test_conflicting_duplicate_fixture_rejected(self):
        a=performance_row(11,tid=1,oid=3,venue='HOME')
        b=performance_row(11,tid=3,oid=8,venue='AWAY')
        with self.assertRaisesRegex(ValueError, 'Conflicto'):
            sos.analyze(perf([a],away=[b]))

    def test_after_cutoff_rejected(self):
        row=performance_row(11)
        row['kickoff_utc']=(TARGET + timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'corte temporal'):
            sos.analyze(perf([row]))

    def test_target_fixture_rejected(self):
        with self.assertRaisesRegex(ValueError,'corte temporal'):
            sos.analyze(perf([performance_row(999)]))

    def test_critical_quality_rejected(self):
        with self.assertRaisesRegex(ValueError,'QUALITY_AUDIT_BLOCKED'):
            sos.analyze(perf([performance_row(11)]),quality={
                'event_id_bwin':'123','target_fixture_id':999,
                'quality_status':'BLOCKED','issues':[{'severity':'CRITICAL'}]})

    def test_provisional_identity_no_odds(self):
        out=sos.analyze(perf([performance_row(11)]),identity={
            'bwin_event_id':'123','api_fixture_id':999,
            'kickoff_utc':TARGET.isoformat(),
            'identity_status':'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN'})
        self.assertFalse(out['odds_enabled'])
        self.assertEqual(out['identity_status'],'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN')

    def test_conflicting_identity_rejected(self):
        with self.assertRaisesRegex(ValueError,'identidad'):
            sos.analyze(perf([performance_row(11)]),identity={
                'bwin_event_id':'123','api_fixture_id':12345,
                'kickoff_utc':TARGET.isoformat()})

    def test_external_pool_future_rejected(self):
        g=pool_game(101,65,2,20)
        with self.assertRaisesRegex(ValueError,'post-cutoff'):
            sos.analyze(perf([performance_row(11)]),extra_pool=extra([g]))

    def test_shrink_cap(self):
        games=[pool_game(100+i, date=i+1, home=20+i, away=2 if i<4 else 60+i,
                         shots1=300 if i<4 else 10, shots2=8) for i in range(13)]
        out=sos.analyze(perf([performance_row(11)]),extra_pool=extra(games))
        e=out['teams']['home']['evidence']['shots_for'][0]
        self.assertLessEqual(e['reference_factor'],1.25)
        self.assertGreaterEqual(e['reference_factor'],0.75)

    def test_no_write_if_quality_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            folder=root/'outputs/matches/123/analysis/deep_v1042'
            identity=root/'outputs/matches/123/inputs/match_identity.json'
            folder.mkdir(parents=True)
            identity.parent.mkdir(parents=True)
            (folder/'rendimiento_profundo_123.json').write_text(json.dumps(perf([performance_row(11)])))
            identity.write_text(json.dumps({'bwin_event_id':'123', 'api_fixture_id':999,
                              'kickoff_utc':TARGET.isoformat()}))
            (folder/'auditoria_calidad_123.json').write_text(json.dumps({
                'event_id_bwin':'123','target_fixture_id':999,'quality_status':'BLOCKED'}))
            with self.assertRaisesRegex(ValueError,'QUALITY_AUDIT_BLOCKED'):
                sos.run('123',root)
            self.assertFalse((folder/'ajuste_rivales_123.json').exists())

if __name__=='__main__':
    unittest.main()
