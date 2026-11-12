"""Real offline validator/motor cycle in a temporary synthetic event. No acquisition."""
import argparse
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import motor_probabilistico as motor
import prematch_multi as multi
import prematch_single as single
import prematch_release as release
import prematch_prospective_trial as trial
import validate_prematch as validator
from prematch_cache_record import acquisition_record
from test_prematch_context_delta import lineup

START = datetime(2026, 11, 12, 18, tzinfo=timezone.utc)


class FinalCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.event = self.root / 'outputs/matches/7000001'
        inputs = self.event / 'inputs'; inputs.mkdir(parents=True)
        self.now = START; self.kickoff = START + timedelta(minutes=40)
        self.url = 'https://www.bwin.es/es/sports/eventos/synthetic-home-away-2:7000001'
        self.node = {'get': 'fixtures/lineups', 'parameters': {'fixture': '99'},
                     'response': [lineup(10, 1000), lineup(20, 2000)]}
        self.payload = {'generated_at': (START - timedelta(hours=1)).isoformat(),
            'match': {'event_id': 7000001, 'url': self.url,
                      'home_name_raw': 'Synthetic Home', 'away_name_raw': 'Synthetic Away'},
            'fixture': {'fixture_id': 99, 'home_id': 10, 'away_id': 20,
                        'home_name': 'Synthetic Home', 'away_name': 'Synthetic Away',
                        'league_id': 3, 'league_name': 'Synthetic League', 'season': 2026,
                        'date': self.kickoff.isoformat()}, 'context': {'lineups': self.node}}
        js = inputs / 'match_engine_7000001.json'; trial.dump(js, self.payload)
        txt = inputs / 'match_engine_7000001.txt'
        txt.write_text(f'Generado: {self.payload["generated_at"]}\nEvent ID: 7000001\nBwin URL: {self.url}\n'
            'Home Bwin: Synthetic Home\nAway Bwin: Synthetic Away\n'
            'FIXTURE RESUELTO\n=====\nFixture ID: 99\nHome: Synthetic Home (10)\nAway: Synthetic Away (20)\n'
            f'Date UTC: {self.kickoff.isoformat()}\nStatus: NS\nLeague: Synthetic League\nSeason: 2026\n'
            'Identity status: FIXTURE_VERIFIED_BY_GENERATOR\n=====\n'
            'LINEUPS DEL FIXTURE OBJETIVO\n=====\n' + json.dumps(self.node))
        text = txt.read_text()
        history = ''
        for i, name in enumerate(('Synthetic Home', 'Synthetic Away')):
            history += (f'PARTIDO HISTÓRICO P-1 — {name}\n====================\nFixture ID: {800+i}\n'
                        f'Fecha: {(START-timedelta(days=5)).isoformat()}\nCondición: HOME\nCompetición: Synthetic League\n'
                        'Resultado: 2 - 1\n====================\n')
        txt.write_text(text.replace('LINEUPS DEL FIXTURE OBJETIVO', history + 'LINEUPS DEL FIXTURE OBJETIVO'))
        from match_identity import write_identity
        identity = write_identity(js)
        self.odds = inputs / 'cuotas_bwin_7000001.txt'; self.write_odds(2.0, START - timedelta(minutes=1))
        self.report = self.root / 'research/reporte_cualitativo_7000001.md'
        self.report.parent.mkdir()
        body = (f'Event ID: 7000001\nFixture ID: 99\nkickoff_utc: {self.kickoff.isoformat()}\n'
                f'analysis_cutoff_utc: {START.isoformat()}\ncutoff_utc: {START.isoformat()}\n'
                'SYNTHETIC TEST ONLY. No real football facts or official lineup claims.\n' * 4)
        self.report.write_text(body)
        external = self.event / 'research/external'; external.mkdir(parents=True)
        for name in ('grok', 'gemini', 'fusion'): (external / f'{name}_7000001.md').write_text(body)
        manifest = {'event_id': '7000001', 'url': self.url, 'status': 'PREPARADO', 'steps': {},
                    'inputs': {k: release.reference(p) for k, p in
                               [('context', txt), ('context_json', js), ('identity', identity), ('odds', self.odds)]}}
        multi.save_manifest(self.root, '7000001', manifest)
        selected = {'schema_version': '1.0-selected-single-fixture', 'status': 'SELECTED_FOR_DEEP_RESEARCH',
            'fixture_id': 99, 'bwin_event_id': '7000001', 'bwin_url': self.url,
            'home': 'Synthetic Home', 'away': 'Synthetic Away', 'kickoff_madrid': self.kickoff.astimezone(single.TZ).isoformat(),
            'selection_asof_madrid': START.astimezone(single.TZ).isoformat(), 'market_families_ready_for_research': []}
        self.selected = self.event / 'selection/run/selected_match.json'
        trial.dump(self.selected, selected)
        trial.dump(self.selected.with_name('selection_decision.json'),
                   {'status': 'SELECTED', 'provisional_or_selected': dict(selected, market_families_ready=[])})
        self.ctx = single.load_context(argparse.Namespace(root=self.root, selected=self.selected, decision=None))
        # Only placeholder existence checks; these files can never be executed here.
        for name in ('prematch_multi.py', 'scraper_bwin.py', 'generate_context.py', 'config/constants.py'):
            p = self.root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('# test placeholder\n')
        self.executed = []
        owner = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.now.astimezone(tz) if tz else owner.now.replace(tzinfo=None)
        for module, name, value in [(validator, 'datetime', Clock), (motor, 'datetime', Clock),
                                    (multi, 'utc_now_dt', lambda: self.now),
                                    (single, 'local_now', lambda: self.now), (trial, 'now_madrid', lambda: self.now)]:
            p = patch.object(module, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(multi, 'execute', side_effect=self.execute); p.start(); self.addCleanup(p.stop)
        p = patch.object(single, 'run', side_effect=self.dispatch); p.start(); self.addCleanup(p.stop)

    def write_odds(self, odd, captured):
        self.odds.write_text(f'Event ID: 7000001\nURL: {self.url}\nCaptura: {captured.isoformat()}\n'
                             'Acordeones: 2 | Selecciones: 2\nMERCADO: Total de goles\nMás de 2,5 : ' + str(odd) +
                             '\nMERCADO: Jugador - Total de tiros (1+)\nP1000 : 2.50\n')

    def execute(self, root, event, stage, command):
        self.executed.append(stage)
        if stage == 'validacion':
            with patch.object(sys, 'argv', command[2:]), contextlib.redirect_stdout(io.StringIO()):
                return validator.main()
        if stage == 'motor':
            with patch.object(sys, 'argv', command[2:]), contextlib.redirect_stdout(io.StringIO()):
                motor.main()
            return 0
        raise AssertionError('UNAUTHORIZED_STAGE:' + stage)

    def dispatch(self, command, root, env=None, log=None):
        if 'finish' in command:
            return 0 if multi.finish_one(root, '7000001', sys.executable, str(self.report)) else 1
        if 'scraper_bwin.py' in command:
            self.write_odds(1.5, self.now)
            log.parent.mkdir(parents=True, exist_ok=True); log.write_text('TXT: ' + str(self.odds) + '\n')
            return 0
        raise AssertionError('UNAUTHORIZED_COMMAND:' + str(command))

    def finish(self):
        args = argparse.Namespace(min_finish_lead_minutes=5, historical=False, multi=Path('prematch_multi.py'),
                                  max_odds_age_minutes=15, report=self.report)
        return single.cmd_finish(self.ctx, args)

    def review_no_bet(self):
        binding = release.analysis_input(self.root, self.selected)
        self.assertTrue(binding.is_file())
        analysis = self.event / 'analysis/analisis_7000001.md'
        analysis.parent.mkdir(exist_ok=True); analysis.write_text('DECISIÓN: NO BET\nSynthetic integration, no recommendations.\n')
        self.assertEqual(release.final_review(self.root, self.selected, analysis)['status'], 'NO_BET')

    def test_real_finish_context_delta_odds_refresh_and_final_review(self):
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.executed, ['validacion', 'motor'])
        self.review_no_bet()
        before = multi.load_manifest(self.root, '7000001')
        old_binding = release.analysis_binding_path(self.ctx, before)
        self.now += timedelta(minutes=1)
        node = copy.deepcopy(self.node); node['response'][0]['startXI'][0]['player']['id'] = 9999
        snapshot = self.root / 'acquisition.json'; trial.dump(snapshot, acquisition_record(node, self.now))
        single.cmd_refresh_context(self.ctx, argparse.Namespace(min_refresh_lead_minutes=5,
            snapshot=snapshot, snapshot_sha256=trial.sha(snapshot)))
        with self.assertRaisesRegex(ValueError, 'complete'):
            release.analysis_input(self.root, self.selected)
        self.assertTrue(old_binding.is_file())
        self.assertEqual(self.finish(), 0)
        cache_after_xi = json.loads((self.event / 'probabilistic/evidence_cache_7000001.json').read_text())
        self.assertEqual(cache_after_xi['reused'], 1)
        self.assertEqual(cache_after_xi['recalculated'], 1)
        analysis = self.event / 'analysis/analisis_7000001.md'
        with self.assertRaises((OSError, ValueError, trial.TrialError)):
            release.final_review(self.root, self.selected, analysis)
        self.review_no_bet()
        self.now += timedelta(minutes=1)
        single.cmd_refresh(self.ctx, argparse.Namespace(min_refresh_lead_minutes=5,
            multi=Path('prematch_multi.py'), max_odds_age_minutes=15))
        with self.assertRaisesRegex(ValueError, 'invalid or incomplete'):
            release.final_review(self.root, self.selected, analysis)
        self.assertEqual(self.finish(), 0)
        self.review_no_bet()
        result = json.loads((self.event / 'probabilistic/ranking_probabilistico_7000001.json').read_text())
        self.assertEqual(result['ranking'][0]['odd'], 1.5)
        self.assertEqual(result['ranking'][0]['break_even_raw_1_over_odd'], .66667)
        cache = json.loads((self.event / 'probabilistic/evidence_cache_7000001.json').read_text())
        self.assertGreater(cache['reused'], 0)
        self.assertEqual(cache['recalculated'], 0)
        self.assertEqual(self.executed, ['validacion', 'motor'] * 3)

    def test_report_inbox_and_previous_pin_remain_intact(self):
        inbox = self.root / 'instructions/reporte_cualitativo_7000001.md'
        inbox.parent.mkdir(exist_ok=True); inbox.write_text(self.report.read_text())
        self.report = inbox
        self.assertEqual(self.finish(), 0)
        canonical = self.event / 'inputs/reporte_cualitativo_7000001.md'
        original = canonical.read_bytes()
        self.assertTrue(inbox.is_file())
        inbox.write_text(inbox.read_text() + '\nSynthetic report revision; no real claims.\n')
        state = multi.load_manifest(self.root, '7000001'); state['status'] = 'PREPARADO'
        multi.save_manifest(self.root, '7000001', state)
        self.assertEqual(self.finish(), 0)
        pin = Path(multi.load_manifest(self.root, '7000001')['inputs']['report']['path'])
        self.assertNotEqual(pin, canonical)
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(pin.read_bytes(), inbox.read_bytes())

    def test_postkickoff_stops_before_validator_or_motor(self):
        # Test an early preflight/late finish clock, never use a historical override.
        self.now = self.kickoff + timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, 'empezó'):
            self.finish()
        self.assertEqual(self.executed, [])

    def test_real_validator_rejects_late_snapshot_and_does_not_run_motor(self):
        state = multi.load_manifest(self.root, '7000001')
        self.write_odds(2.0, self.kickoff + timedelta(seconds=1))
        state['inputs']['odds'] = release.reference(self.odds)
        multi.save_manifest(self.root, '7000001', state)
        self.assertFalse(multi.finish_one(self.root, '7000001', sys.executable, str(self.report)))
        self.assertEqual(self.executed, ['validacion'])
        result = json.loads((self.event / 'validation/validacion_7000001.json').read_text())
        self.assertTrue(any(i['code'] == 'ODDS_CAPTURE_LATE' and i['severity'] == 'BLOCK' for i in result['issues']))

    def test_new_capture_uses_acquisition_header_never_restoration_mtime(self):
        import bwin_market_preflight as preflight
        for captured, mtime, accepted in [(START-timedelta(days=1), START, False),
                                          (START, START-timedelta(days=10), True)]:
            with self.subTest(accepted=accepted):
                self.odds.unlink()  # Synthetic temporary input only, never repository outputs.
                def scraper(command, **kwargs):
                    self.write_odds(2.0, captured)
                    os.utime(self.odds, (mtime.timestamp(), mtime.timestamp()))
                    kwargs['stdout'].write('TXT: '+str(self.odds)+'\n'); kwargs['stdout'].flush()
                    return argparse.Namespace(returncode=0)
                with patch.object(preflight, 'utcnow', return_value=START), patch.object(preflight.subprocess, 'run', side_effect=scraper):
                    result = preflight.run_scraper(self.root, self.root/'preflight', '7000001', self.url)
                self.assertEqual('fresh_snapshot_path' in result, accepted)


if __name__ == '__main__': unittest.main()
