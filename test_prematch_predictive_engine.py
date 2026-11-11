import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import prematch_predictive_engine as engine
from preselect_matches import ApiFootball, PreselectionError


ASOF = datetime.fromisoformat('2026-11-04T10:00:00+01:00')


def row(fid, days, team, opponent, zero=False, missing=False, post=False):
    value = 0 if zero else 1 + fid % 3
    at = ASOF + timedelta(hours=1) if post else ASOF - timedelta(days=days)
    own = {} if missing else {'goals': value, 'corners': value + 2, 'shots': value + 7,
        'on_target': value + 2, 'fouls': value + 10, 'yellow': value, 'offsides': value,
        'blocked_shots': value + 1}
    conceded = {} if missing else {k: max(0, v - 1) for k, v in own.items()}
    return {'fixture_id': fid, 'team_id': team, 'opponent_id': opponent,
        'kickoff_utc': at.astimezone(timezone.utc).isoformat(), 'league_id': 10,
        'season': 2026, 'venue': 'HOME' if fid % 2 else 'AWAY', 'status': 'FT',
        'statistics_requested': True,
        'statistics_request_status': 'REQUESTED_MISSING' if missing else 'VALID',
        'own': own, 'conceded': conceded}


def fixture(fid=100, coach_change=False):
    home = [row(i, 30 - i, 1, 3, zero=i == 1, missing=i == 2, post=i == 29) for i in range(1, 30)]
    away = [row(100 + i, 30 - i, 2, 4, zero=i == 1, missing=i == 2) for i in range(1, 30)]
    return {'fixture_id': fid, 'home': 'Home', 'away': 'Away', 'home_id': 1,
        'away_id': 2, 'league_id': 10, 'league': 'League', 'season': 2026,
        'gender': 'M', 'kickoff_madrid': '2026-11-04T20:00:00+01:00',
        'analysis_cutoff_utc': ASOF.isoformat(), 'details_checked': True,
        'coach_change': coach_change,
        'statistical_observations': {'home': home, 'away': away}}


def source(matches=None):
    matches = matches or [fixture()]
    return {'schema_version': '1.3-preselection', 'generated_at_utc': ASOF.isoformat(),
        'source_filter_sha256': 'synthetic', 'counts': {'input_filter': len(matches),
        'api_errors': 0}, 'api_usage': {'requests_sent': 0, 'cache_hits': 4},
        'methodology': {'no_bwin_odds': True}, 'matches': matches}


class PredictiveEngineTests(unittest.TestCase):
    def test_partial_ledger_five_of_637_never_redefines_universe(self):
        matches = [{'fixture_id': i, 'details_checked': True} for i in range(637)]
        pre = {'counts': {'input_filter': 637}, 'matches': matches,
               'fixture_evaluation_ledger': [{'fixture_id': i, 'status': 'EVALUATED'} for i in range(5)]}
        stages = engine.fixture_stages(pre, [])
        self.assertEqual((stages['source_filtered'], stages['eligible'], stages['basic_evaluated'],
                          stages['deep_evaluated']), (637, 637, 637, 637))
        self.assertEqual(stages['ledger_scope'], 'PARTIAL')
        self.assertEqual((stages['ledger_entries'], stages['ledger_universe']), (5, 637))

    def test_result_ten_minutes_earlier_is_not_available_for_training(self):
        start = ASOF - timedelta(days=10)
        old = [(start + timedelta(days=i), float(i % 3), i) for i in range(6)]
        first = start + timedelta(days=7)
        result = engine.walk_forward(old + [(first, 4.0, 100), (first + timedelta(minutes=10), 2.0, 101)])
        by_id = {p['target_fixture_id']: p for p in result['predictions']}
        self.assertEqual(by_id[101]['training_n'], 6)
        self.assertNotEqual(by_id[101]['training_last_kickoff'], first.isoformat())
        self.assertLessEqual(by_id[101]['training_last_result_available_at'], by_id[101]['target_kickoff'])

    def test_post_kickoff_execution_is_explicit_retrospective_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'; pre.write_text(json.dumps(source()))
            executed = datetime.fromisoformat('2026-11-04T21:00:00+01:00')
            out = engine.run(pre, root / 'out', ASOF, executed_at=executed)[0]
            manifest = engine.read(out / 'run_manifest.json')
            self.assertEqual(manifest['execution_mode'], 'RETROSPECTIVE_REPLAY')
            self.assertFalse(manifest['prospective_claim_allowed'])
            self.assertEqual(manifest['source_cutoff_utc'], ASOF.astimezone(timezone.utc).isoformat())
            self.assertEqual(manifest['run_executed_at_utc'], executed.astimezone(timezone.utc).isoformat())

    def test_changed_existing_artifact_blocks_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'; pre.write_text(json.dumps(source()))
            out = engine.run(pre, root / 'out', ASOF)[0]
            target = out / 'predictive_rankings.json'
            target.write_text(target.read_text() + '\n')
            with self.assertRaisesRegex(engine.PredictiveError, 'EXISTING_ARTIFACT_SHA_MISMATCH'):
                engine.run(pre, root / 'out', ASOF)

    def test_intact_existing_run_is_reused_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'; pre.write_text(json.dumps(source()))
            out = engine.run(pre, root / 'out', ASOF)[0]
            before = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}
            again = engine.run(pre, root / 'out', ASOF)[0]
            after = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}
            self.assertEqual(out, again)
            self.assertEqual(before, after)

    def test_existing_run_from_different_engine_code_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'; pre.write_text(json.dumps(source()))
            out = engine.run(pre, root / 'out', ASOF)[0]
            manifest_path = out / 'run_manifest.json'
            manifest = engine.read(manifest_path)
            manifest['engine_code_sha256'] = '0' * 64
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(engine.PredictiveError, 'EXISTING_RUN_ENGINE_CODE_MISMATCH'):
                engine.run(pre, root / 'out', ASOF)

    def test_manifestless_interrupted_run_is_archived_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'; pre.write_text(json.dumps(source()))
            pre_sha = engine.sha(pre)
            run_id = (f"{ASOF.astimezone(engine.MADRID).strftime('%Y%m%dT%H%M%S%z')}_{pre_sha[:12]}"
                      f"_code-{engine.engine_code_sha()[:12]}")
            partial = root / 'out' / engine.VERSION / run_id
            partial.mkdir(parents=True)
            (partial / 'predictive_coverage.json').write_text('{')
            out = engine.run(pre, root / 'out', ASOF)[0]
            manifest = engine.read(out / 'run_manifest.json')
            archived = Path(manifest['recovered_interrupted_run'])
            self.assertTrue(archived.is_dir())
            self.assertEqual((archived / 'predictive_coverage.json').read_text(), '{')
            self.assertTrue((out / 'predictive_validation.json').is_file())

    def test_compact_fixture_output_is_isolated_and_keeps_global_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'
            pre.write_text(json.dumps(source([fixture(100), fixture(101)])))
            out = engine.run(pre, root / 'out', ASOF, fixture_id=101)[0]
            compact = engine.read(out / 'frozen_predictions_101.json')
            self.assertTrue(compact['records'])
            self.assertTrue(all(r['fixture_id'] == 101 for r in compact['records']))
            self.assertTrue(all('league_reference_rate' not in r for r in compact['records']))
            self.assertEqual({p.name for p in out.iterdir()}, {'frozen_predictions_101.json', 'run_manifest.json'})
            self.assertTrue(engine.read(out / 'run_manifest.json')['configuration']['global_references_preserved'])

    def test_retrospective_evaluation_is_separate_and_handles_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'; pre.write_text(json.dumps(source()))
            run_dir = engine.run(pre, root / 'runs', ASOF)[0]
            results = root / 'results.json'
            results.write_text(json.dumps({'results': [{'fixture_id': 100,
                'observed': {'home': {'goals': 2}, 'away': {'goals': 0}}}]}))
            out = engine.evaluate_run(run_dir, results, root / 'evaluation')
            report = engine.read(out / 'retrospective_evaluation.json')
            self.assertEqual(report['evaluation_mode'], 'RETROSPECTIVE_OFFLINE_SEPARATE_ARTIFACT')
            self.assertEqual(report['counts']['evaluable'], 2)
            self.assertGreater(report['counts']['not_evaluable_missing'], 0)
            self.assertTrue(report['by_market_family'])
            self.assertFalse(report['actionable_bet'])

    def test_daily_optional_integration_disabled_is_noop(self):
        import prematch_daily
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(prematch_daily.run_optional_predictor(
                Path(tmp), Path(tmp) / 'window', SimpleNamespace(predictive_engine=False)))

    def test_daily_optional_integration_enabled_uses_frozen_preselection_offline(self):
        import prematch_daily
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root / 'window'
            source_path = folder / 'prospective_trial/enriched/preselection.json'
            source_path.parent.mkdir(parents=True); source_path.write_text('{}')
            expected = folder / 'prospective_trial/predictive/run'
            with patch('prematch_predictive_engine.run', return_value=(expected, {}, {}, {}, {}, {})) as mocked:
                result = prematch_daily.run_optional_predictor(
                    root, folder, SimpleNamespace(predictive_engine=True, fixture_id=123))
            self.assertEqual(result, expected)
            mocked.assert_called_once_with(source_path, folder / 'prospective_trial/predictive', fixture_id=123)

    def test_zero_is_valid_missing_and_post_cutoff_are_not(self):
        clean = engine.discovery.clean_observations(fixture(), 'home', ASOF)
        values = engine.observed_values(clean, 'own', 'goals')
        self.assertIn(0.0, [x[1] for x in values])
        self.assertNotIn(2, [x[2] for x in values])
        self.assertNotIn(29, [x[2] for x in values])

    def test_duplicate_fixture_and_identity_difference_block(self):
        with self.assertRaisesRegex(engine.PredictiveError, 'DUPLICATE_FIXTURE_ID'):
            engine.validate_source(source([fixture(), fixture()]), ASOF)
        bad = fixture(); bad['away_id'] = bad['home_id']
        with self.assertRaisesRegex(engine.PredictiveError, 'DUPLICATE_TEAM_ID'):
            engine.validate_source(source([bad]), ASOF)

    def test_walk_forward_has_no_future_leakage(self):
        points = [(ASOF - timedelta(days=20-i), float(i % 3), i) for i in range(12)]
        result = engine.walk_forward(points)
        self.assertTrue(result['predictions'])
        for prediction in result['predictions']:
            self.assertLess(prediction['training_last_kickoff'], prediction['target_kickoff'])

    def test_small_sample_and_contradiction_are_explicit(self):
        small = fixture()
        small['statistical_observations'] = {k: v[:3] for k, v in small['statistical_observations'].items()}
        models, _ = engine.fixture_models(source([small]), ASOF, engine.league_reference(source([small]), ASOF))
        self.assertTrue(all(x['pattern_state'] == 'INSUFFICIENT_EVIDENCE' for x in models))
        conflicting = fixture()
        for row_ in conflicting['statistical_observations']['away']:
            if row_.get('conceded'): row_['conceded']['goals'] = 15
        models, _ = engine.fixture_models(source([conflicting]), ASOF, engine.league_reference(source([conflicting]), ASOF))
        goal = next(x for x in models if x['subject'] == 'home' and x['metric'] == 'goals')
        self.assertEqual(goal['pattern_state'], 'CONTRADICTORY')

    def test_coach_change_is_a_risk_not_an_invented_mechanism(self):
        changed = fixture(coach_change=True); pre = source([changed])
        models, _ = engine.fixture_models(pre, ASOF, engine.league_reference(pre, ASOF))
        self.assertTrue(all('KNOWN_COACH_REGIME_CHANGE_REQUIRES_SEGMENTATION' in x['risks'] for x in models))
        self.assertTrue(all('presión' not in x['contrastable_explanation'].casefold() for x in models))

    def test_rankings_are_independent_of_bwin_and_family_diversified(self):
        pre = source(); models, _ = engine.fixture_models(pre, ASOF, engine.league_reference(pre, ASOF))
        ranked = engine.diversified_global(models)
        self.assertTrue(ranked)
        self.assertGreater(len({x['family'] for x in ranked[:7]}), 3)
        self.assertTrue(all(x['ranking_label'] == 'EXPERIMENTAL_RANKING' and not x['actionable_bet'] for x in ranked))
        self.assertTrue(all('bwin_event_id' not in x and 'odd' not in x for x in ranked))
        self.assertTrue(all('NO_BWIN_INPUT' in x['risks'] for x in ranked))

    def test_offsides_remain_experimental_and_never_actionable(self):
        pre=source();models,_=engine.fixture_models(pre,ASOF,engine.league_reference(pre,ASOF))
        offsides=[row for row in models if row['metric']=='offsides']
        self.assertTrue(offsides)
        self.assertTrue(all(row['usage_status']=='EXPERIMENTAL_TACTICAL_ONLY' for row in offsides))
        self.assertTrue(all('OFFSIDES_BWIN_AVAILABILITY_UNVERIFIED_NOT_ACTIONABLE' in row['risks']
                            and not row['actionable_bet'] for row in offsides))

    def test_periods_without_verified_splits_stay_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'
            pre.write_text(json.dumps(source()))
            out = engine.run(pre, root / 'out', ASOF)[0]
            ledger = engine.read(out / 'api_request_ledger.json')
            self.assertEqual(ledger['predictive_engine_network_requests'], 0)
            self.assertEqual(ledger['origin'], 'SOURCE_PRESELECTION_LEDGER_NOT_CURRENT_ENGINE_EXECUTION')
            coverage = engine.read(out / 'predictive_coverage.json')
            periods = [x for x in coverage['rows'] if x['period'] != 'FULL_TIME']
            self.assertTrue(periods)
            self.assertTrue(all(x['availability_status'] == 'INSUFFICIENT_EVIDENCE' for x in periods))
            self.assertTrue(all(x['reason'] == 'PERIOD_SPLIT_NOT_PRESENT_IN_FROZEN_SOURCE' for x in periods))


    def test_parallel_kickoffs_never_enter_each_others_training(self):
        start = ASOF - timedelta(days=20)
        points = [(start + timedelta(days=i), float(i % 3), i) for i in range(6)]
        same_start = start + timedelta(days=6)
        points += [(same_start, 1.0, 100), (same_start, 2.0, 101)]
        result = engine.walk_forward(points)
        self.assertEqual(len(result['predictions']), 2)
        self.assertTrue(all(p['training_last_kickoff'] < p['target_kickoff']
                            for p in result['predictions']))
        self.assertTrue(all(p['training_n'] == 6 for p in result['predictions']))

    def test_operational_source_rejects_asof_at_or_after_kickoff(self):
        pre = source()
        future = datetime.fromisoformat(pre['matches'][0]['kickoff_madrid']) + timedelta(minutes=1)
        with self.assertRaisesRegex(engine.PredictiveError, 'OPERATIONAL_ASOF_NOT_PREKICKOFF'):
            engine.validate_source(pre, future)

    def test_univariate_win_cannot_validate_matchup_model(self):
        pre = source()
        models, validations = engine.fixture_models(pre, ASOF, engine.league_reference(pre, ASOF))
        self.assertTrue(all(m['pattern_state'] != 'TEMPORALLY_VALIDATED' for m in models))
        self.assertTrue(all(v.get('validation_scope') == 'UNIVARIATE_TEAM_HISTORY_NOT_MATCHUP_MODEL'
                            for v in validations if v.get('valid_predictions')))

    def test_pending_fixtures_not_counted_as_basically_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'preselection.json'
            pre = source()
            pre['counts']['input_filter'] = 3
            pre['fixture_evaluation_ledger'] = [
                {'fixture_id': 100, 'status': 'EVALUATED'},
                {'fixture_id': 200, 'status': 'NOT_EVALUATED_MAX_MATCHES_LIMIT'},
                {'fixture_id': 300, 'status': 'NOT_EVALUATED_MAX_MATCHES_LIMIT'},
            ]
            p.write_text(json.dumps(pre))
            out = engine.run(p, Path(tmp) / 'out')[0]
            stage = engine.read(out / 'predictive_rankings.json')['fixture_stages']
            self.assertEqual((stage['eligible'], stage['basic_evaluated'], stage['pending_or_excluded']),
                             (3, 1, 2))

    def test_temporal_exclusions_reduce_eligible_count_but_preserve_source_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'preselection.json'
            pre = source()
            pre['counts']['input_filter'] = 2
            pre['fixture_evaluation_ledger'] = [
                {'fixture_id': 100, 'status': 'EVALUATED'},
                {'fixture_id': 200, 'status': 'EXCLUDED_TEMPORAL'},
            ]
            p.write_text(json.dumps(pre))
            out = engine.run(p, Path(tmp) / 'out')[0]
            stage = engine.read(out / 'predictive_rankings.json')['fixture_stages']
            self.assertEqual((stage['source_filtered'], stage['excluded_temporal'],
                              stage['eligible'], stage['basic_evaluated']), (2, 1, 1, 1))

    def test_log_scores_serialized_to_stable_precision(self):
        pts = [(ASOF - timedelta(days=20 - i), float((i % 4) + 1), i)
               for i in range(16)]
        predictions = engine.walk_forward(pts)['predictions']
        self.assertTrue(predictions)
        for row_ in predictions:
            self.assertEqual(row_['baseline_log_score'], round(row_['baseline_log_score'], 12))
            self.assertEqual(row_['model_log_score'], round(row_['model_log_score'], 12))

    def test_adaptive_unselected_fixture_retains_basic_goal_histories(self):
        from preselect_matches import evaluate_fixture
        base = fixture(998)
        base.update({'kickoff_utc': datetime.fromisoformat(base['kickoff_madrid']).astimezone(timezone.utc).isoformat(),
                     'country': 'Testland', 'region': 'Africa',
                     'region_priority': 4, 'gender': 'M'})
        histories = {}
        for team_id, other_id, offset in ((1, 3, 0), (2, 4, 100)):
            histories[team_id] = [
                {'id': 500 + offset + i,
                 'date_utc': (ASOF - timedelta(days=i + 1)).astimezone(timezone.utc).isoformat(),
                 'status': 'FT', 'league_id': 10, 'season': 2026,
                 'home_id': team_id, 'away_id': other_id,
                 'goals_home': i % 3, 'goals_away': (i + 1) % 3,
                 'team_goals': i % 3, 'opponent_goals': (i + 1) % 3,
                 'venue': 'HOME'} for i in range(12)]
        result = evaluate_fixture(base, histories, {}, {(10, 2026): []}, {},
                                  ASOF, 8, details_selected=False, detail_requested_ids=set())
        self.assertFalse(result['details_checked'])
        self.assertEqual(result['data_readiness_status'], 'PENDIENTE_DETALLE')
        for side in ('home', 'away'):
            observations = result['statistical_observations'][side]
            self.assertEqual(len(observations), 12)
            self.assertTrue(all(x['statistics_request_status'] == 'NOT_REQUESTED' for x in observations))
            self.assertTrue(all('goals' in x['own'] and 'goals' in x['conceded'] for x in observations))
            self.assertTrue(all('corners' not in x['own'] for x in observations))
        doc = source([result])
        models, _ = engine.fixture_models(doc, ASOF, engine.league_reference(doc, ASOF))
        goal = next(x for x in models if x['subject'] == 'home' and x['family'] == 'goals')
        corner = next(x for x in models if x['subject'] == 'home' and x['family'] == 'corners')
        self.assertIsNotNone(goal['predictive_rate'])
        self.assertIsNone(corner['predictive_rate'])

    def test_offline_rehydrate_reads_only_matching_asof_ledger_cache(self):
        from prematch_rehydrate_basic import rebuild
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / 'original.json'
            pre = source()
            pre['matches'][0]['statistical_observations'] = {'home': [], 'away': []}
            pre['matches'][0]['details_checked'] = False
            histories = []
            for team_id, opp_id in ((1, 3), (2, 4)):
                for i in range(10):
                    histories.append({
                        'fixture': {'id': team_id * 100 + i,
                                    'date': (ASOF - timedelta(days=i + 1)).astimezone(timezone.utc).isoformat(),
                                    'status': {'short': 'FT'}},
                        'teams': {'home': {'id': team_id}, 'away': {'id': opp_id}},
                        'goals': {'home': i % 3, 'away': (i + 1) % 3},
                        'league': {'id': 10, 'season': 2026}})
            ledger = []
            cache_dir = root / 'outputs/data/cache/preselect_api'
            cache_dir.mkdir(parents=True)
            for team_id in (1, 2):
                from urllib.parse import urlencode
                query = urlencode(sorted((str(k), str(v)) for k, v in {'team': team_id}.items()))
                key = hashlib.sha256(('fixtures?' + query).encode()).hexdigest()
                response = [x for x in histories if x['teams']['home']['id'] == team_id]
                item = {'retrieved_at_utc': (ASOF - timedelta(minutes=1)).isoformat(),
                        'endpoint': 'fixtures', 'parameters': {'team': team_id}, 'response': response}
                cache = cache_dir / (key + '.json')
                cache.write_text(json.dumps(item))
                ledger.append({'request_id': key, 'endpoint': 'fixtures', 'parameters': {'team': team_id},
                               'status': 'NETWORK_OK', 'cache_sha256': hashlib.sha256(cache.read_bytes()).hexdigest(),
                               'first_observed_at_utc': (ASOF - timedelta(minutes=1)).isoformat()})
            pre['api_request_ledger'] = ledger
            source_path.write_text(json.dumps(pre))
            hydrated, audit = rebuild(source_path, root)
            self.assertEqual(audit['restored_fixture_count'], 1)
            self.assertEqual(audit['restored_team_sides'], 2)
            self.assertEqual(audit['restored_history_rows'], 20)
            self.assertEqual(len(hydrated['matches'][0]['statistical_observations']['home']), 10)
            derived_path = root / 'derived.json'
            derived_path.write_text(json.dumps(hydrated))
            out, _, _, rankings, _, api_ledger = engine.run(derived_path, root / 'derived_runs', ASOF)
            self.assertEqual(rankings['fixture_stages']['statistically_evaluated'], 1)
            self.assertTrue((out / 'run_manifest.json').exists())
            self.assertEqual(api_ledger['predictive_engine_network_requests'], 0)
            # A future cache replacement is rejected even when the filename matches.
            target = cache_dir / (ledger[0]['request_id'] + '.json')
            altered = json.loads(target.read_text())
            altered['retrieved_at_utc'] = (ASOF + timedelta(hours=1)).isoformat()
            target.write_text(json.dumps(altered))
            hydrated2, audit2 = rebuild(source_path, root)
            self.assertEqual(audit2['restored_team_sides'], 1)
            self.assertGreater(audit2['cache_skips_by_reason']['CACHE_SHA_MISMATCH'], 0)

    def test_negative_binomial_requires_enough_stable_overdispersion(self):
        self.assertEqual(engine.choose_count_model([0, 1, 2])['name'], 'POISSON')
        values = [0, 0, 1, 1, 1, 2, 2, 3, 3, 4, 5, 6] * 2
        self.assertEqual(engine.choose_count_model(values)['name'], 'NEGATIVE_BINOMIAL')

    def test_deterministic_offline_artifacts_and_required_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pre = root / 'preselection.json'
            pre.write_text(json.dumps(source(), ensure_ascii=False))
            first = engine.run(pre, root / 'out', ASOF)[0]
            manifest1 = engine.read(first / 'run_manifest.json')
            manifest_bytes1 = (first / 'run_manifest.json').read_bytes()
            second = engine.run(pre, root / 'out', ASOF)[0]
            manifest2 = engine.read(second / 'run_manifest.json')
            self.assertEqual(manifest1['determinism_sha256'], manifest2['determinism_sha256'])
            self.assertEqual(manifest_bytes1, (second / 'run_manifest.json').read_bytes())
            for name in ('predictive_coverage.json', 'predictive_patterns.json', 'predictive_rankings.json',
                         'predictive_validation.json', 'predictive_summary.md', 'api_request_ledger.json'):
                self.assertTrue((first / name).is_file(), name)


class ApiLedgerTests(unittest.TestCase):
    def test_cache_ledger_and_request_budget_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); client = ApiFootball('secret', 'https://example.invalid', root,
                                                    request_budget=2, request_reserve=1)
            route = 'fixtures'; params = {'team': 1, 'season': 2026}
            query = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
            key = hashlib.sha256((route + '?' + query).encode()).hexdigest()
            cache = root / 'outputs/data/cache/preselect_api' / f'{key}.json'
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
                                         'response': [{'fixture': {'id': 1}}]}))
            self.assertEqual(len(client.get(route, params)), 1)
            self.assertEqual(client.request_ledger[-1]['status'], 'CACHE_HIT')
            response = Mock(status_code=200, headers={})
            response.raise_for_status.return_value = None
            response.json.return_value = {'response': [], 'paging': {'total': 1}, 'errors': []}
            client.session.get = Mock(return_value=response)
            client.get('standings', {'league': 10, 'season': 2026})
            with self.assertRaisesRegex(PreselectionError, 'RESERVE'):
                client.get('fixtures', {'team': 2, 'season': 2026})
            self.assertEqual(client.requests_sent, 1)
            client.close()


if __name__ == '__main__':
    unittest.main()
