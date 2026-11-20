#!/usr/bin/env python3
"""Pruebas offline del empaquetador de IA PREMATCH. No usa red ni outputs reales."""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import prematch_ai_packager as packager


EVENT = '7909230'
OTHER = '7909231'
NOW = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc)
SENTINEL_INSTRUCTIONS = 'SENTINEL_INSTRUCCION_NO_COPIAR'
SENTINEL_ORDER = 'ORDEN_SECRETA_NO_COPIAR'
SENTINEL_ODDS = 'CUOTA_SECRETA_NO_COPIAR'
SENTINEL_REPORT = 'SELECCION_SECRETA_NO_COPIAR'
SENTINEL_LOG = 'LOG_SECRETO_NO_INVENTARIAR'
REPO = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def stamp(path: Path, when: datetime) -> None:
    moment = when.timestamp()
    os.utime(path, (moment, moment))


class PackagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        write(self.project / 'AGENTS.md', SENTINEL_INSTRUCTIONS + '\n')
        write(self.project / 'instructions' / 'analysis_instructions.txt', SENTINEL_INSTRUCTIONS + '\n')
        write(
            self.project / 'config' / 'tactical_profiles' / 'profiles.json',
            json.dumps({'schema_version': '1.0-tactical-profiles', 'version': 7, 'claims': []}) + '\n',
        )
        self.matches = self.root / 'outputs' / 'matches'

    def tearDown(self):
        self.tmp.cleanup()

    def package(self, expediente: Path, **kwargs):
        kwargs.setdefault('project_root', self.project)
        kwargs.setdefault('now', NOW)
        return packager.package_event(EVENT, expediente, **kwargs)

    def test_expediente_completo_no_copia_ni_valida(self):
        event = self._complete_event()
        sibling = self._sibling()
        before = self._snapshot(event)
        sibling_before = digest(sibling / 'inputs' / f'cuotas_bwin_{OTHER}.txt')
        manifest_mtime = (event / 'manifest.json').stat().st_mtime_ns
        result = self.package(event)
        inventory = json.loads(result.inventory_path.read_text(encoding='utf-8'))

        self.assertEqual(result.coverage, 'CANONICA_COMPLETA')
        self.assertEqual(result.missing, [])
        self.assertEqual(inventory['resumen']['validados'], 0)
        self.assertFalse(inventory['manifiesto_operativo']['modificado_por_el_empaquetador'])
        self.assertEqual(inventory['manifiesto_operativo']['sha256'], digest(event / 'manifest.json'))
        self.assertEqual((event / 'manifest.json').stat().st_mtime_ns, manifest_mtime)
        self.assertEqual(before, self._snapshot(event))
        self.assertEqual(digest(sibling / 'inputs' / f'cuotas_bwin_{OTHER}.txt'), sibling_before)
        self.assertFalse((sibling / 'ai_packages').exists())
        self.assertFalse(any(path.name == 'manifest.json' for path in result.output_dir.rglob('*')))

        odds = (event / 'inputs' / f'cuotas_bwin_{EVENT}.txt').read_bytes()
        self.assertTrue(odds)
        self.assertFalse(any(path.is_file() and path.read_bytes() == odds for path in result.output_dir.rglob('*')))
        source = Path(packager.__file__).read_text(encoding='utf-8')
        self.assertNotIn('shutil', source)
        self.assertNotIn('Granada', source)
        self.assertNotIn('Las Palmas', source)

        prompts = self._prompts(result)
        self.assertEqual(set(prompts), set(packager.PROVIDERS))
        blob = '\n'.join(prompts.values())
        for sentinel in (
            SENTINEL_INSTRUCTIONS, SENTINEL_ORDER, SENTINEL_ODDS, SENTINEL_REPORT, SENTINEL_LOG,
            'NO BET de prueba', 'FIXTURE_AND_BWIN_TIME_VERIFIED',
        ):
            self.assertNotIn(sentinel, blob)
        self.assertIn(str((self.project / 'AGENTS.md').resolve()), prompts['codex'])
        self.assertIn(str((self.project / 'instructions' / 'analysis_instructions.txt').resolve()), prompts['gemini'])
        self.assertNotIn('cuotas_bwin', prompts['grok'])
        self.assertNotIn('match_engine', prompts['grok'])
        self.assertIn('match_engine', prompts['gemini'])
        self.assertNotIn('cuotas_bwin', prompts['gemini'])
        self.assertIn('cuotas_bwin', prompts['codex'])
        self.assertIn('PRECIO_PENDIENTE_DE_VERIFICACION', prompts['codex'])
        self.assertIn('validado=false', prompts['codex'])
        self.assertIn('ACTUAL_REFERENCIADO', prompts['codex'])
        self.assertNotIn('archives/', prompts['codex'])
        self.assertNotIn(f'cuotas_bwin_{OTHER}', blob)
        self.assertNotIn(str((event / 'research' / 'external' / f'fusion_{EVENT}.md').resolve()), prompts['argon'])
        self.assertIn(str((event / 'research' / 'external' / f'grok_{EVENT}.md').resolve()), prompts['argon'])
        self.assertIn('version=1.0-tactical-profiles / 7', prompts['gemini'])
        self.assertNotIn('profiles.json', prompts['grok'])
        self.assertIn('Local: Granada CF [CONCORDANTE_NO_VERIFICADO]', prompts['perplexity'])
        self.assertIn('NO_VERIFICADO', prompts['perplexity'])

        by_path = {item['ruta_relativa']: item for item in inventory['archivos']}
        odds_row = by_path[f'inputs/cuotas_bwin_{EVENT}.txt']
        self.assertEqual(odds_row['clase_temporal'], 'ACTUAL_REFERENCIADO')
        self.assertFalse(odds_row['validado'])
        self.assertEqual(odds_row['precio'], 'PRECIO_PENDIENTE_DE_VERIFICACION')
        self.assertEqual(odds_row['relacion_manifiesto'], 'COINCIDE')
        self.assertFalse(odds_row['copiado'])
        foreign = by_path[f'inputs/cuotas_bwin_{OTHER}.txt']
        self.assertEqual(foreign['vinculo_evento'], 'EVENTO_AJENO')
        archive = next(item for item in inventory['archivos'] if item['ruta_relativa'].startswith('archives/'))
        self.assertEqual(archive['clase_temporal'], 'SNAPSHOT_HISTORICO')
        self.assertNotIn(SENTINEL_LOG, json.dumps(inventory))
        self.assertIn('logs', inventory['resumen']['directorios_omitidos'])
        for provider in inventory['proveedores'].values():
            for attached in provider['adjuntar']:
                self.assertFalse(attached['validado'])
                self.assertNotEqual(attached['vinculo_evento'], 'EVENTO_AJENO')
                self.assertNotEqual(attached['clase_temporal'], 'SNAPSHOT_HISTORICO')

    def test_expediente_incompleto_continua(self):
        event = self.matches / EVENT
        write(event / 'inputs' / 'match_identity.json', self._identity())
        result = self.package(event)
        inventory = json.loads(result.inventory_path.read_text(encoding='utf-8'))
        self.assertEqual(result.coverage, 'INCOMPLETA')
        self.assertIn(f'inputs/cuotas_bwin_{EVENT}.txt', result.missing)
        self.assertIn(f'inputs/match_engine_{EVENT}.txt', result.missing)
        self.assertIn(f'research/external/perplexity_{EVENT}.md', result.missing)
        self.assertTrue(all(path is not None for path in result.prompts.values()))
        self.assertIn('Ausentes para este encargo', (result.prompts['argon']).read_text(encoding='utf-8'))
        self.assertIn(f'research/external/grok_{EVENT}.md', inventory['proveedores']['argon']['ausentes'])
        self.assertEqual(inventory['archivos'][0]['relacion_manifiesto'], 'SIN_MANIFIESTO')
        self.assertFalse((event / 'manifest.json').exists())

    def test_versiones_timestamps_y_hash_distinto(self):
        event = self.matches / EVENT
        current = write(event / 'inputs' / f'match_engine_{EVENT}.json', json.dumps({
            'schema_version': 'context-test-3',
            'generated_at': '2026-11-15T09:30:00+00:00',
            'match': {'event_id': EVENT, 'home_name_raw': 'Granada CF', 'away_name_raw': 'UD Las Palmas'},
            'fixture': {'fixture_id': 900001, 'date': '2026-11-15T20:00:00+00:00'},
        }))
        archived = write(
            event / 'archives' / 'force_prepare_20261101T090000000000Z' / 'inputs' / f'match_engine_{EVENT}.json',
            json.dumps({
                'schema_version': 'context-test-1',
                'generated_at': '2026-11-01T09:30:00+00:00',
                'match': {'event_id': EVENT},
            }),
        )
        odds = write(event / 'inputs' / f'cuotas_bwin_{EVENT}.txt', f'Event ID: {EVENT}\nCaptura: 2026-11-15T18:00:00+01:00\n')
        late = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
        early = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        stamp(current, late)
        stamp(archived, early)
        write(event / 'manifest.json', json.dumps({
            'event_id': EVENT,
            'inputs': {
                'context_json': {'path': str(current.resolve()), 'sha256': digest(current)},
                'odds': {'path': str(odds.resolve()), 'sha256': '0' * 64},
            },
        }))
        result = self.package(event)
        rows = {item['ruta_relativa']: item for item in json.loads(result.inventory_path.read_text(encoding='utf-8'))['archivos']}
        current_row = rows[f'inputs/match_engine_{EVENT}.json']
        archive_row = rows[f'archives/force_prepare_20261101T090000000000Z/inputs/match_engine_{EVENT}.json']
        odds_row = rows[f'inputs/cuotas_bwin_{EVENT}.txt']
        self.assertEqual(current_row['version_declarada'], 'context-test-3')
        self.assertEqual(archive_row['version_declarada'], 'context-test-1')
        self.assertEqual(current_row['marca_temporal_declarada'], '2026-11-15T09:30:00+00:00')
        self.assertEqual(archive_row['marca_temporal_declarada'], '2026-11-01T09:30:00+00:00')
        self.assertEqual(current_row['mtime_filesystem_utc'], late.isoformat())
        self.assertEqual(archive_row['mtime_filesystem_utc'], early.isoformat())
        self.assertNotEqual(current_row['sha256'], archive_row['sha256'])
        self.assertEqual(current_row['clase_temporal'], 'ACTUAL_REFERENCIADO')
        self.assertEqual(archive_row['clase_temporal'], 'SNAPSHOT_HISTORICO')
        self.assertEqual(odds_row['clase_temporal'], 'VIGENCIA_NO_VERIFICADA')
        self.assertEqual(odds_row['relacion_manifiesto'], 'HASH_DISTINTO')
        self.assertFalse(odds_row['validado'])
        self.assertEqual(odds_row['marca_temporal_declarada'], '2026-11-15T18:00:00+01:00')
        self.assertEqual(odds_row['marca_temporal_fiabilidad'], 'AUTODECLARADA_NO_VERIFICADA')
        codex = result.prompts['codex'].read_text(encoding='utf-8')
        self.assertIn('manifiesto=HASH_DISTINTO', codex)
        self.assertNotIn('archives/', codex)

    def test_reejecucion_sin_duplicar(self):
        event = self._complete_event()
        first = self.package(event)
        first_files = self._package_snapshot(first.output_dir)
        prompt_mtime = first.prompts['gemini'].stat().st_mtime_ns
        self.package(event)
        self.assertEqual(first_files, self._package_snapshot(first.output_dir))
        self.assertEqual(list(first.output_dir.rglob('inventory.json')).__len__(), 1)
        self.assertFalse((first.output_dir / 'ai_packages').exists())
        later = self.package(event, now=NOW + timedelta(hours=1))
        self.assertEqual(later.prompts['gemini'].stat().st_mtime_ns, prompt_mtime)
        self.assertEqual(later.prompts['gemini'].read_bytes(), first.prompts['gemini'].read_bytes())
        updated = json.loads(later.inventory_path.read_text(encoding='utf-8'))
        self.assertNotEqual(updated['generado_utc'], NOW.isoformat())
        self.assertEqual(len(list(later.output_dir.rglob('inventory.json'))), 1)
        names = sorted(path.name for path in later.output_dir.rglob('*') if path.is_file())
        self.assertEqual(len(names), len(set(names)) + names.count('LEEME.md') - 1)

    def test_ia_no_disponible_y_prompt_ajeno(self):
        event = self._complete_event()
        foreign = write(event / 'ai_packages' / 'prompts' / f'COPIAR_GROK_{EVENT}.md', 'EDITADO_POR_USUARIO\n')
        external = event / 'research' / 'external' / f'COPIAR_GROK_{EVENT}.md'
        external_hash = digest(external)
        result = self.package(event)
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'EDITADO_POR_USUARIO\n')
        alternate = event / 'ai_packages' / 'prompts' / f'COPIAR_GROK_{EVENT}.prematch_ai_packager.md'
        self.assertTrue(alternate.is_file())
        self.assertEqual(result.prompts['grok'], alternate.resolve())
        self.package(event)
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'EDITADO_POR_USUARIO\n')
        self.assertEqual(len(list((event / 'ai_packages' / 'prompts').glob('COPIAR_GROK*'))), 2)
        gemini_bytes = result.prompts['gemini'].read_bytes()
        gemini_mtime = result.prompts['gemini'].stat().st_mtime_ns
        second = self.package(event, unavailable=['grok', 'deepseek'], now=NOW + timedelta(minutes=5))
        self.assertIsNone(second.prompts['grok'])
        self.assertIsNone(second.prompts['deepseek'])
        self.assertFalse(alternate.exists())
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'EDITADO_POR_USUARIO\n')
        self.assertEqual(result.prompts['gemini'].read_bytes(), gemini_bytes)
        self.assertEqual(result.prompts['gemini'].stat().st_mtime_ns, gemini_mtime)
        self.assertTrue(second.prompts['codex'].is_file())
        self.assertEqual(digest(external), external_hash)
        self.assertIn('NO_DISPONIBLE', second.adjuntos_path.read_text(encoding='utf-8'))
        restored = self.package(event)
        self.assertTrue(restored.prompts['deepseek'].is_file())
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'EDITADO_POR_USUARIO\n')

    def test_separacion_estricta_y_salida_protegida(self):
        other = self.matches / OTHER
        write(other / 'manifest.json', json.dumps({'event_id': OTHER}))
        with self.assertRaises(packager.PackagerError) as other_dir:
            self.package(other)
        self.assertIn('EXPEDIENTE_DE_OTRO_EVENTO', str(other_dir.exception))
        event = self.matches / EVENT
        write(event / 'manifest.json', json.dumps({
            'event_id': OTHER,
            'url': f'https://www.bwin.es/es/sports/eventos/otro-2:{OTHER}',
        }))
        before = (event / 'manifest.json').read_bytes()
        with self.assertRaises(packager.PackagerError) as bad_manifest:
            self.package(event)
        self.assertIn('MANIFIESTO_DE_OTRO_EVENTO', str(bad_manifest.exception))
        self.assertEqual((event / 'manifest.json').read_bytes(), before)
        with self.assertRaises(packager.PackagerError) as same_dir:
            self.package(event, output_dir=event)
        self.assertIn('OUTPUT_ES_EL_EXPEDIENTE', str(same_dir.exception))
        with self.assertRaises(packager.PackagerError) as outside:
            self.package(event, output_dir=other)
        self.assertIn('OUTPUT_FUERA_DEL_EXPEDIENTE', str(outside.exception))
        self.assertFalse((event / 'ai_packages').exists())
        self.assertFalse((other / 'ai_packages').exists())

    def test_identidad_no_conciliada_y_ambiguos(self):
        event = self.matches / EVENT
        write(event / 'inputs' / 'match_identity.json', self._identity())
        write(
            event / 'inputs' / f'match_engine_{EVENT}.txt',
            f'Event ID: {EVENT}\nHome Bwin: Equipo Alfa\nAway Bwin: UD Las Palmas\n'
            'Fixture ID: 900001\nGenerado: 2026-11-15T09:30:00+00:00\n',
        )
        first = write(event / 'extra' / 'a' / f'cuotas_bwin_{EVENT}.txt', f'Event ID: {EVENT}\n')
        second = write(event / 'extra' / 'b' / f'cuotas_bwin_{EVENT}.txt', f'Event ID: {EVENT}\n')
        result = self.package(event)
        card = json.loads((result.output_dir / 'identidad_declarada.json').read_text(encoding='utf-8'))
        self.assertEqual(card['campos']['home']['estado'], 'NO_CONCILIADO')
        self.assertFalse(card['validado'])
        prompt = result.prompts['codex'].read_text(encoding='utf-8')
        self.assertIn('Local: NO_CONCILIADO', prompt)
        self.assertNotIn('Local: Granada CF', prompt)
        self.assertNotIn('Local: Equipo Alfa', prompt)
        self.assertNotIn(str(first.resolve()), prompt)
        self.assertNotIn(str(second.resolve()), prompt)
        rows = json.loads(result.inventory_path.read_text(encoding='utf-8'))['archivos']
        self.assertEqual(sum(item['ruta_relativa'].endswith(f'cuotas_bwin_{EVENT}.txt') for item in rows), 2)

    def test_archivo_grande_no_se_copia(self):
        event = self.matches / EVENT
        header = f'Event ID: {EVENT}\nCaptura: 2026-11-15T18:00:00+01:00\nPartido: Granada CF vs UD Las Palmas\n'
        payload = header + (SENTINEL_ODDS + ' 1.91\n') + ('x' * 2_000_000)
        odds = write(event / 'inputs' / f'cuotas_bwin_{EVENT}.txt', payload)
        write(event / 'inputs' / 'match_identity.json', self._identity())
        write(event / 'manifest.json', json.dumps({
            'event_id': EVENT,
            'inputs': {'odds': {'path': str(odds.resolve()), 'sha256': digest(odds)}},
        }))
        result = self.package(event)
        package_bytes = sum(path.stat().st_size for path in result.output_dir.rglob('*') if path.is_file())
        self.assertLess(package_bytes, odds.stat().st_size)
        blob = '\n'.join(path.read_text(encoding='utf-8') for path in result.prompts.values() if path)
        self.assertNotIn(SENTINEL_ODDS, blob)
        self.assertIn(str(odds.resolve()), result.prompts['codex'].read_text(encoding='utf-8'))
        self.assertNotIn(str(odds.resolve()), result.prompts['deepseek'].read_text(encoding='utf-8'))

    def test_instrucciones_reales_no_se_duplican(self):
        event = self.matches / EVENT
        write(event / 'inputs' / 'match_identity.json', self._identity())
        agents = REPO / 'AGENTS.md'
        instructions = REPO / 'instructions' / 'analysis_instructions.txt'
        agents_mtime = agents.stat().st_mtime_ns
        result = self.package(event, project_root=REPO)
        prompt = result.prompts['codex'].read_text(encoding='utf-8')
        self.assertIn(str(agents.resolve()), prompt)
        self.assertIn(str(instructions.resolve()), prompt)
        self.assertNotIn('Una única ejecución analítica de Codex', prompt)
        self.assertNotIn('Nunca deducir', prompt)
        self.assertEqual(agents.stat().st_mtime_ns, agents_mtime)
        self.assertFalse((REPO / 'ai_packages').exists())

    def test_cli(self):
        event = self.matches / EVENT
        write(event / 'inputs' / 'match_identity.json', self._identity())
        with contextlib.redirect_stdout(io.StringIO()):
            code = packager.main([
                '--event-id', EVENT, '--expediente', str(event),
                '--project-root', str(self.project), '--unavailable', 'perplexity',
            ])
        self.assertEqual(code, 0)
        self.assertFalse((event / 'ai_packages' / 'prompts' / f'COPIAR_PERPLEXITY_{EVENT}.md').exists())
        self.assertTrue((event / 'ai_packages' / 'prompts' / f'COPIAR_CODEX_{EVENT}.md').is_file())
        fresh = self.matches / '7000001'
        fresh.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = packager.main(['--event-id', '7000001', '--expediente', str(fresh), '--unavailable', 'claude'])
        self.assertEqual(code, 2)
        self.assertFalse((fresh / 'ai_packages').exists())

    def _identity(self) -> str:
        return json.dumps({
            'schema_version': '1.0-test',
            'bwin_event_id': EVENT,
            'api_fixture_id': 900001,
            'league_name': 'Segunda División',
            'kickoff_utc': '2026-11-15T20:00:00+00:00',
            'identity_status': 'FIXTURE_AND_BWIN_TIME_VERIFIED',
            'home': {'api_name': 'Granada CF', 'bwin_name': 'Granada CF'},
            'away': {'api_name': 'UD Las Palmas', 'bwin_name': 'UD Las Palmas'},
        })

    def _complete_event(self) -> Path:
        event = self.matches / EVENT
        files = {
            f'inputs/cuotas_bwin_{EVENT}.txt': (
                f'Event ID: {EVENT}\nPartido: Granada CF vs UD Las Palmas\n'
                'Captura: 2026-11-15T18:00:00+01:00\n' + SENTINEL_ODDS + '\n'
            ),
            f'inputs/match_engine_{EVENT}.txt': (
                f'Generado: 2026-11-15T09:30:00+00:00\nEvent ID: {EVENT}\n'
                'Home Bwin: Granada CF\nAway Bwin: UD Las Palmas\nFixture ID: 900001\n'
                'League: Segunda División\nDate UTC: 2026-11-15T20:00:00+00:00\n'
            ),
            f'inputs/match_engine_{EVENT}.json': json.dumps({
                'schema_version': 'context-test-3',
                'generated_at': '2026-11-15T09:30:00+00:00',
                'match': {
                    'event_id': EVENT,
                    'home_name_raw': 'Granada CF',
                    'away_name_raw': 'UD Las Palmas',
                    'league_name': 'Segunda División',
                },
                'fixture': {
                    'fixture_id': 900001,
                    'date': '2026-11-15T20:00:00+00:00',
                    'home_name': 'Granada CF',
                    'away_name': 'UD Las Palmas',
                    'league_name': 'Segunda División',
                },
            }),
            'inputs/match_identity.json': self._identity(),
            f'inputs/reporte_cualitativo_{EVENT}.md': f'Event ID: {EVENT}\nInforme de prueba.\n',
            f'validation/validacion_{EVENT}.json': json.dumps({'event_id': EVENT, 'status': 'REVISAR'}),
            f'validation/validacion_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'probabilistic/matriz_evidencia_{EVENT}.json': json.dumps({'event_id': EVENT}),
            f'probabilistic/ranking_probabilistico_{EVENT}.json': json.dumps({'event_id': EVENT}),
            f'probabilistic/resumen_codex_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'probabilistic/mapa_tactico_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'probabilistic/hipotesis_cruzadas_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'probabilistic/patrones_jugadores_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'probabilistic/contexto_competitivo_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'probabilistic/contexto_competitivo_{EVENT}.json': json.dumps({'event_id': EVENT}),
            f'analysis/analisis_{EVENT}.md': 'DECISIÓN\nNO BET de prueba\n',
            f'CODEX_{EVENT}.md': f'# Orden\n{SENTINEL_ORDER}\n',
            f'research/external/gemini_{EVENT}.md': f'Event ID: {EVENT}\n{SENTINEL_REPORT}\n',
            f'research/external/grok_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'research/external/fusion_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'research/external/perplexity_{EVENT}.md': f'Event ID: {EVENT}\n',
            f'research/external/COPIAR_GROK_{EVENT}.md': 'NO TOCAR\n',
            f'analysis/deep_v1042/rendimiento_profundo_{EVENT}.json': json.dumps({
                'schema_version': 'deep-1', 'event_id_bwin': EVENT,
            }),
            f'analysis/deep_v1042/contexto_ampliado_{EVENT}.json': json.dumps({
                'schema_version': 'deep-1', 'match': {'event_id': EVENT},
            }),
            f'inputs/cuotas_bwin_{OTHER}.txt': f'Event ID: {OTHER}\n',
            'logs/scraper.log': SENTINEL_LOG + '\n',
            'archives/force_prepare_20261101T090000000000Z/inputs/cuotas_bwin_' + EVENT + '.txt': (
                f'Event ID: {EVENT}\nCaptura: 2026-11-01T10:00:00+01:00\nHISTORICO\n'
            ),
        }
        written = {relative: write(event / relative, content) for relative, content in files.items()}
        referenced = ('inputs/cuotas_bwin_' + EVENT + '.txt', 'inputs/match_engine_' + EVENT + '.txt',
                      'inputs/match_engine_' + EVENT + '.json', 'inputs/match_identity.json',
                      'inputs/reporte_cualitativo_' + EVENT + '.md')
        write(event / 'manifest.json', json.dumps({
            'event_id': EVENT,
            'status': 'FINALIZADO',
            'url': f'https://www.bwin.es/es/sports/eventos/granada-las-palmas-2:{EVENT}',
            'inputs': {
                'odds': {'path': str(written[referenced[0]].resolve()), 'sha256': digest(written[referenced[0]])},
                'context': {'path': str(written[referenced[1]].resolve()), 'sha256': digest(written[referenced[1]])},
                'context_json': {'path': str(written[referenced[2]].resolve()), 'sha256': digest(written[referenced[2]])},
                'identity': {'path': str(written[referenced[3]].resolve()), 'sha256': digest(written[referenced[3]])},
                'report': {'path': str(written[referenced[4]].resolve()), 'sha256': digest(written[referenced[4]])},
            },
        }))
        return event

    def _sibling(self) -> Path:
        other = self.matches / OTHER
        write(other / 'inputs' / f'cuotas_bwin_{OTHER}.txt', f'Event ID: {OTHER}\n')
        return other

    def _snapshot(self, event: Path) -> dict[str, str]:
        found = {}
        for path in event.rglob('*'):
            if not path.is_file() or 'ai_packages' in path.parts:
                continue
            found[path.relative_to(event).as_posix()] = digest(path)
        return found

    def _package_snapshot(self, output: Path) -> dict[str, tuple[str, int]]:
        found = {}
        for path in output.rglob('*'):
            if path.is_file():
                found[path.relative_to(output).as_posix()] = (digest(path), path.stat().st_mtime_ns)
        return found

    def _prompts(self, result) -> dict[str, str]:
        return {name: path.read_text(encoding='utf-8') for name, path in result.prompts.items()}


if __name__ == '__main__':
    unittest.main()
