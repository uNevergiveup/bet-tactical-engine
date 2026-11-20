import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_context_delta as d
import prematch_release as release
from prematch_cache_record import acquisition_record
from motor_probabilistico import evidence_for

NOW = datetime(2026, 11, 12, 18, tzinfo=timezone.utc)


def lineup(tid, start):
    return {'team': {'id': tid}, 'formation': '4-4-2', 'coach': {'id': tid + 100},
            'startXI': [{'player': {'id': start + i, 'name': f'P{start+i}', 'pos': 'M', 'grid': str(i)}}
                        for i in range(11)]}


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.event = self.root / 'outputs/matches/7000001'; self.event.mkdir(parents=True)
        self.ctx = {'root': self.root, 'directory': self.event, 'event_id': '7000001', 'fixture_id': 99,
                    'url': 'https://www.bwin.es/es/sports/eventos/home-away-2:7000001',
                    'kickoff': NOW + timedelta(hours=1)}
        self.old = [lineup(10, 1000), lineup(20, 2000)]
        self.node = {'get': 'fixtures/lineups', 'parameters': {'fixture': '99'}, 'response': self.old}
        context = {'generated_at': (NOW - timedelta(hours=2)).isoformat(),
                   'match': {'event_id': 7000001, 'url': self.ctx['url'],
                             'home_name_raw': 'Home', 'away_name_raw': 'Away'},
                   'fixture': {'fixture_id': 99, 'date': self.ctx['kickoff'].isoformat(),
                               'home_id': 10, 'away_id': 20, 'home_name': 'Home', 'away_name': 'Away',
                               'league_id': 3, 'season': 2026},
                   'context': {'lineups': self.node, 'home_history_deep': {'frozen': [1, 2]}}}
        self.manifest = {'status': 'FINALIZADO', 'codex_prompt': 'old', 'inputs': {},
                         'steps': {'validacion': 0, 'motor': 0}}
        for key, name, value in [('context_json', 'context.json', json.dumps(context)),
                                 ('context', 'context.txt', 'HEADER\nLINEUPS DEL FIXTURE OBJETIVO\n=====\n' + json.dumps(self.node) + '\nHISTORY_UNCHANGED')]:
            p = self.event / name; p.write_text(value)
            self.manifest['inputs'][key] = release.reference(p)
        self.new = copy.deepcopy(self.node)
        self.new['response'][0]['startXI'][0]['player']['id'] = 9999
        self.snapshot = self.root / 'acquired.json'
        self.save_snapshot()

    def save_snapshot(self, at=NOW):
        self.snapshot.write_text(json.dumps(acquisition_record(self.new, at)))
        self.digest = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()

    def refresh(self, now=NOW):
        return d.refresh_lineups(self.ctx, self.manifest, self.snapshot, self.digest, now)

    def test_refresh_preserves_old_sources_and_histories_invalidates_final_stages(self):
        before = {r['path']: Path(r['path']).read_bytes() for r in self.manifest['inputs'].values()}
        result, out = self.refresh()
        self.assertEqual(result['status'], 'PREPARADO'); self.assertNotIn('codex_prompt', result)
        self.assertNotIn('motor', result['steps']); self.assertNotIn('validacion', result['steps'])
        updated = json.loads(Path(result['inputs']['context_json']['path']).read_text())
        self.assertEqual(updated['context']['home_history_deep'], {'frozen': [1, 2]})
        self.assertEqual(updated['generated_at'], (NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(updated['differential_refresh']['delta']['changes'][0]['added'], [9999])
        self.assertEqual(updated['differential_refresh']['delta']['status'], 'API_STRUCTURED_NOT_OFFICIAL_CERTIFICATION')
        for p, data in before.items(): self.assertEqual(Path(p).read_bytes(), data)
        self.assertEqual(json.loads((out / 'parent_manifest.json').read_text()), self.manifest)
        self.assertTrue(Path(result['inputs']['context']['path']).read_text().endswith('HISTORY_UNCHANGED'))

    def test_wrong_fixture_duplicate_players_and_partial_lineups_block_without_output(self):
        for mutation in ('fixture', 'duplicate', 'partial', 'team'):
            with self.subTest(mutation=mutation):
                saved = copy.deepcopy(self.new)
                if mutation == 'fixture': self.new['parameters']['fixture'] = '123'
                if mutation == 'duplicate': self.new['response'][0]['startXI'][1] = self.new['response'][0]['startXI'][0]
                if mutation == 'partial': self.new['response'][0]['startXI'].pop()
                if mutation == 'team': self.new['response'][1]['team']['id'] = 21
                self.save_snapshot()
                with self.assertRaises(ValueError): self.refresh()
                self.assertFalse((self.event / 'context_updates').exists())
                self.new = saved

    def test_postkickoff_future_and_stale_records_block(self):
        for at, now in [(NOW + timedelta(seconds=1), NOW),
                        (NOW + timedelta(hours=1), NOW + timedelta(hours=1)),
                        (NOW - timedelta(hours=3), NOW)]:
            self.save_snapshot(at)
            with self.assertRaises(ValueError): self.refresh(now)

    def test_role_formation_coach_changes_are_not_silently_ignored(self):
        new = copy.deepcopy(self.old)
        new[0]['formation'] = '3-5-2'; new[0]['coach']['id'] = 999
        new[0]['startXI'][0]['player']['grid'] = 'different'
        delta = d.compare_lineups(self.old, new, [10, 20])
        self.assertTrue(delta['changes'][0]['formation_changed'])
        self.assertTrue(delta['changes'][0]['coach_changed'])
        self.assertEqual(delta['changes'][0]['roles_changed'], [1000])

    def test_boolean_team_id_is_not_a_numeric_identity(self):
        with self.assertRaisesRegex(ValueError, 'TEAM_IDENTITY'):
            d.lineup_state([lineup(True, 1000)], [1, 20])

    def test_old_binding_cannot_be_reused_after_context_or_odds_refresh(self):
        (self.event / 'manifest.json').write_text(json.dumps(self.manifest))
        old = release.analysis_binding_path(self.ctx, self.manifest)
        updated, _ = self.refresh()
        (self.event / 'manifest.json').write_text(json.dumps(updated))
        self.assertNotEqual(release.analysis_binding_path(self.ctx, updated), old)
        updated.pop('invalidated_by_context_refresh'); updated['invalidated_by_odds_refresh'] = {'at': NOW.isoformat()}
        (self.event / 'manifest.json').write_text(json.dumps(updated))
        self.assertNotEqual(release.analysis_binding_path(self.ctx, updated), old)

    def test_single_command_pins_new_pair_and_preserves_previous_artifacts(self):
        import prematch_single as single
        from match_identity import identity_from_payload
        original = json.loads(Path(self.manifest['inputs']['context_json']['path']).read_text())
        identity = self.event / 'identity.json'; identity.write_text(json.dumps(identity_from_payload(original)))
        self.manifest['inputs']['identity'] = release.reference(identity)
        self.manifest.update(event_id='7000001', url=self.ctx['url'])
        (self.event / 'manifest.json').write_text(json.dumps(self.manifest))
        self.ctx.update(selected_path=self.root / 'selected.json', selected_sha='abc',
                        decision_path=self.root / 'decision.json', decision_sha='def')
        old_output = self.event / 'probabilistic' / 'old.json'
        old_output.parent.mkdir(); old_output.write_text('historical result')
        args = SimpleNamespace(min_refresh_lead_minutes=5, snapshot=self.snapshot, snapshot_sha256=self.digest)
        with patch.object(single, 'local_now', return_value=NOW), patch.object(single, 'run', side_effect=AssertionError('NO_ACQUISITION')):
            self.assertEqual(single.cmd_refresh_context(self.ctx, args), 0)
        updated = single.match_manifest(self.ctx)
        self.assertEqual(updated['status'], 'PREPARADO')
        self.assertEqual(old_output.read_text(), 'historical result')
        run = Path(updated['invalidated_by_context_refresh']['run'])
        self.assertEqual((run / 'previous_outputs/probabilistic/old.json').read_text(), 'historical result')
        self.assertFalse((self.event / 'final_review.json').exists())


class SelectiveEvidenceTests(unittest.TestCase):
    def test_direct_record_is_append_only_even_when_reusable_cache_is_bypassed(self):
        from prematch_cache_record import store_acquisition, unpack_record
        with tempfile.TemporaryDirectory() as td:
            payload = {'get': 'fixtures/lineups', 'parameters': {'fixture': '99'}, 'response': []}
            first = store_acquisition(td, payload, NOW)
            original = first.read_bytes()
            second = store_acquisition(td, payload, NOW + timedelta(seconds=1))
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(unpack_record(json.loads(second.read_text()))[0], payload)

    def test_price_and_neighbor_line_rebuilt_while_evidence_can_be_reused(self):
        from motor_probabilistico import evaluate_selection
        context = {'target': {'home': 'Home', 'away': 'Away'}, 'histories': {}, 'target_players': {}}
        cache = d.EvidenceCache('7000001')
        first = evaluate_selection('Total de goles', 'Más de 2,5', 2.0, context, {}, 5, cache)
        second = evaluate_selection('Total de goles', 'Más de 2,5', 1.5, context, {}, 5, cache)
        self.assertEqual(first['break_even_raw_1_over_odd'], .5)
        self.assertEqual(second['break_even_raw_1_over_odd'], .66667)
        self.assertEqual(second['odd'], 1.5)
        self.assertEqual(cache.reused, 1)
        evaluate_selection('Total de goles', 'Más de 3,5', 2.0, context, {}, 5, cache)
        self.assertEqual(cache.recalculated, 2)
        live = evaluate_selection('Total de goles en vivo', 'Más de 2,5', 2.0, context, {}, 5, cache)
        self.assertEqual(live['evidence_status'], 'MERCADO_EN_VIVO_NO_APTO')

    def test_only_affected_player_recalculates_collective_and_other_player_reuse(self):
        context = {'target': {'home': 'Home', 'away': 'Away'}, 'histories': {},
                   'target_players': {'1': {'name': 'Player One', 'team': 'Home', 'status': 'starter'},
                                      '2': {'name': 'Player Two', 'team': 'Away', 'status': 'starter'}}}
        specs = [{'type': 'total', 'metric': 'total_goals', 'mode': 'over', 'threshold': 2.5},
                 {'type': 'player', 'metric': 'shots', 'mode': 'over', 'threshold': 1.5, 'player_label': 'Player One'},
                 {'type': 'player', 'metric': 'shots', 'mode': 'over', 'threshold': 1.5, 'player_label': 'Player Two'}]
        cache = d.EvidenceCache('7000001')
        for spec in specs: cache.evaluate(spec, context, evidence_for)
        self.assertEqual(cache.recalculated, 3)
        context['target_players']['1']['status'] = 'bench'
        for spec in specs:
            self.assertEqual(cache.evaluate(spec, context, evidence_for), evidence_for(spec, context))
        self.assertEqual(cache.recalculated, 4); self.assertEqual(cache.reused, 2)

    def test_cache_requires_event_hash_and_code_binding(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'cache.json'; cache = d.EvidenceCache('7000001')
            p.write_text(json.dumps(cache.document())); digest = hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertEqual(d.EvidenceCache('7000001', p, digest).items, {})
            with self.assertRaises(ValueError): d.EvidenceCache('7000002', p, digest)
            with self.assertRaises(ValueError): d.EvidenceCache('7000001', p, '0' * 64)


if __name__ == '__main__': unittest.main()
