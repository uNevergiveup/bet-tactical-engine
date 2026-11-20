#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PREMATCH Prospective Trial V0.3. Ventanas configuradas por perfiles, sin picks.

PREPARE: API fixtures -> primeras divisiones -> preselección -> patrones -> discovery Bwin.
SCAN: captura nuevas cuotas por evento -> preflight -> Opportunity Scanner (no calibrado).

Nunca ejecuta un partido pasado, no permite un --asof retrospectivo del usuario
y NO habilita Grok/Gemini/Codex. Internamente congela un único corte por ejecución.
Perfiles persistentes definidos en config/prematch_windows.json.
"""
from __future__ import annotations

import argparse
import hashlib
import copy
import json
import re
import subprocess
import sys
import unicodedata
import shutil
import time as performance_clock
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Madrid')
REGIONS = {'EUROPE':0,'SOUTH_AMERICA':1,'NORTH_CENTRAL_AMERICA':2,'AFRICA':3,'ASIA':4}

class TrialError(Exception):
    pass

def now_madrid():
    return datetime.now(TZ)

DEFAULT_WINDOW_CONFIG = json.loads((Path(__file__).resolve().parent / 'config/prematch_windows.json').read_text(encoding='utf-8'))
CONFIG_RELATIVE_PATH = Path("config/prematch_windows.json")


def parse_date_relative(raw, today=None):
    """Permite fechas ISO o 'hoy'/'mañana' sin consultar relojes dos veces."""
    day = today if today is not None else now_madrid().date()
    token = str(raw).strip().casefold()
    if token == 'hoy':
        return day
    if token in ('mañana', 'manana'):
        return day + timedelta(days=1)
    return parse_date(raw)


def load_window_config(root: Path, user_path: Path | None = None):
    """Si aún no instalaron el JSON, mantiene V0.2 como predeterminado.

    Una ruta --config explícita que no exista siempre causa error.
    """
    path = user_path.expanduser() if user_path is not None else root / CONFIG_RELATIVE_PATH
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        if user_path is not None:
            raise TrialError(f'No existe la configuración indicada: {path}')
        return copy.deepcopy(DEFAULT_WINDOW_CONFIG), False, path
    doc = read(path)
    if not isinstance(doc, dict) or doc.get('schema_version') != '1.0-prematch-windows':
        raise TrialError(f'Esquema inválido en {path}: se espera 1.0-prematch-windows')
    if doc.get('timezone') != 'Europe/Madrid':
        raise TrialError(f'Timezone inválida en {path}: usar Europe/Madrid')
    profiles = doc.get('profiles')
    if not isinstance(profiles, dict) or not profiles:
        raise TrialError(f'No se definieron perfiles en {path}')
    for name, v in profiles.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(v, dict):
            raise TrialError(f'Perfil inválido en {path}: {name!r}')
        for field in ('start', 'end'):
            try:
                parse_clock(v.get(field, ''))
            except (argparse.ArgumentTypeError, TypeError):
                raise TrialError(f'Horario inválido en {path}, perfil {name}: {field}') from None
    if doc.get('default_profile') not in profiles:
        raise TrialError(f'default_profile no existe en profiles de {path}')
    return doc, True, path


def resolve_window(config, selected=None, start_override=None, end_override=None):
    profiles = config['profiles']
    name = selected if selected is not None else config['default_profile']
    if name not in profiles:
        raise TrialError(f'Perfil {name!r} desconocido. Disponibles: {", ".join(profiles)}')
    p = profiles[name]
    start = start_override if start_override is not None else parse_clock(p['start'])
    end = end_override if end_override is not None else parse_clock(p['end'])
    return name, start, end

def parse_date(raw):
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Fecha requerida: YYYY-MM-DD') from exc

def parse_clock(raw):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', raw):
        raise argparse.ArgumentTypeError('Hora requerida: HH:MM, desde 00:00 hasta 23:59')
    return time.fromisoformat(raw)

def local_window(day: date, start: time, end: time):
    begin=datetime.combine(day,start,tzinfo=TZ)
    finish_day=day+timedelta(days=1) if end<=start else day
    finish=datetime.combine(finish_day,end,tzinfo=TZ)
    # Normalize a nonexistent spring clock to the first corresponding real instant.
    begin=begin.astimezone(timezone.utc).astimezone(TZ)
    finish=finish.astimezone(timezone.utc).astimezone(TZ)
    return begin,finish

def norm(raw):
    s=unicodedata.normalize('NFKD',str(raw))
    return ''.join(c for c in s if not unicodedata.combining(c)).casefold().strip()

def dump(path:Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def atomic_dump(path: Path, obj):
    """Publica un JSON completo sin dejar un checkpoint truncado."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if path.is_file() and path.read_bytes() == raw:
        return False
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(raw)
    tmp.replace(path)
    return True

def read(path:Path):
    if not path.is_file():
        raise TrialError(f'Falta archivo requerido: {path}')
    return json.loads(path.read_text(encoding='utf-8-sig'))

def dt(raw):
    try:
        value=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        return value if value.utcoffset() is not None else None
    except (ValueError,TypeError,OverflowError):
        return None

def sha(path:Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execution_reference(args):
    """One immutable eligibility clock per command invocation."""
    value = getattr(args, 'execution_reference', None)
    if value is None:
        value = now_madrid()
        args.execution_reference = value
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TrialError('EXECUTION_REFERENCE_REQUIRES_TIMEZONE')
    return value.astimezone(TZ)


def _normalized_team_filter(args) -> list[str]:
    raw = getattr(args, 'team', None)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    return sorted(str(x) for x in raw if x is not None and str(x).strip())


def _intact_pre_enrichment_snapshot(folder: Path, base: Path) -> bool:
    raw_path = Path(folder) / 'fixtures.json'
    major_path = Path(folder) / 'fixtures_major.json'
    trial_input = Path(base) / 'fixtures_major_test.json'
    if not (raw_path.is_file() and major_path.is_file() and trial_input.is_file() and (Path(base) / 'candidate_ledger.json').is_file()):
        return False
    try:
        from filter_major_leagues import FILTER_IMPLEMENTATION
        major = read(major_path)
        subset = read(trial_input)
        return (
            major.get('filter_implementation') == FILTER_IMPLEMENTATION
            and major.get('source_sha256') == sha(raw_path)
            and subset.get('trial_subset', {}).get('source_sha256') == sha(major_path)
        )
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        return False


def _evaluation_request(day, args):
    from filter_major_leagues import FILTER_IMPLEMENTATION
    begin, finish = local_window(day, args.start, args.end)
    return {
        'day': day.isoformat(), 'window_start_madrid': begin.isoformat(),
        'window_end_madrid': finish.isoformat(),
        'horizon_hours': float(getattr(args, 'horizon_hours', 0) or 0),
        'evaluation_budget': int(getattr(args, 'evaluation_budget', 0) or 0),
        'discovery_budget': int(getattr(args, 'discovery_budget', 0) or 0),
        'max_matches_batch_size': int(args.max_matches),
        'bwin_top_batch_size': int(args.bwin_top),
        'history_limit': int(args.history), 'detail_limit': int(args.details),
        'min_lead_minutes': int(args.min_lead),
        'competition_scope': str(getattr(args, 'competition_scope', None) or 'adult-open'),
        'filter_implementation': FILTER_IMPLEMENTATION,
        'coverage_rules_sha256': sha(Path(args.coverage_rules)) if getattr(args,'coverage_rules',None) else None,
        'team_filter': _normalized_team_filter(args),
    }


def _canonical_hash(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def evaluation_manifest(folder):
    path = Path(folder) / 'evaluation_manifest.json'
    document = read(path)
    request = document.get('request')
    if (document.get('schema_version') != '1.0-daily-evaluation' or
            not isinstance(request, dict) or
            document.get('request_sha256') != _canonical_hash(request) or
            Path(document.get('evaluation_path', '')).resolve() != Path(folder).resolve()):
        raise TrialError(f'EVALUATION_MANIFEST_IDENTITY_MISMATCH:{path}')
    return document


def resolve_evaluation(window_folder, day, args, reference=None):
    """Choose an isolated evaluation before any frozen input is written.

    Only an unfinished, content-addressed snapshot with enrichment checkpoints
    is resumable. Completed/terminal or merely partial attempts remain untouched
    and a new evaluation directory is allocated automatically.
    """
    window_folder = Path(window_folder)
    reference = (reference or execution_reference(args)).astimezone(TZ)
    request = _evaluation_request(day, args)
    request_hash = _canonical_hash(request)
    evaluations = window_folder / 'evaluations'
    if evaluations.is_dir():
        candidates = []
        for manifest_path in evaluations.glob('*/evaluation_manifest.json'):
            try:
                document = evaluation_manifest(manifest_path.parent)
            except (TrialError, OSError, ValueError, json.JSONDecodeError):
                continue
            base = manifest_path.parent / 'prospective_trial'
            handoff_path = base / 'handoff_prematch.json'
            handoff = read(handoff_path) if handoff_path.is_file() else None
            resumable_handoff = (handoff is None or
                (handoff.get('status') == 'TECHNICAL_FAILURE' and
                 handoff.get('discard_reasons') and
                 all(row.get('phase') == 'prepare' for row in handoff['discard_reasons'])))
            if (document.get('request_sha256') == request_hash and
                    not (base / 'prospective_test_summary.json').exists() and
                    resumable_handoff and
                    (base / 'fixtures_major_test.json').is_file() and
                    (base / 'enrichment_batches').is_dir()):
                # Validate the complete frozen source chain before selecting it.
                subset = read(base / 'fixtures_major_test.json')
                major = manifest_path.parent / 'fixtures_major.json'
                raw = manifest_path.parent / 'fixtures.json'
                if not major.is_file() or not raw.is_file():
                    raise TrialError('RESUME_FROZEN_SOURCE_MISSING')
                if (read(major).get('source_sha256') != sha(raw) or
                        subset.get('trial_subset', {}).get('source_sha256') != sha(major)):
                    raise TrialError('RESUME_FROZEN_SOURCE_IDENTITY_MISMATCH')
                candidates.append((document.get('reference_madrid', ''), manifest_path.parent))
        if candidates:
            return sorted(candidates)[-1][1], 'RESUME_FROZEN_EVALUATION'
    evaluations.mkdir(parents=True, exist_ok=True)
    prefix = reference.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stem = f'{prefix}_{request_hash[:12]}'
    target = evaluations / stem
    suffix = 1
    while target.exists():
        suffix += 1
        target = evaluations / f'{stem}_{suffix}'
    target.mkdir()
    manifest = {
        'schema_version': '1.0-daily-evaluation', 'evaluation_id': target.name,
        'evaluation_path': str(target.resolve()), 'window_path': str(window_folder.resolve()),
        'reference_madrid': reference.isoformat(),
        'reference_utc': reference.astimezone(timezone.utc).isoformat(),
        'request': request, 'request_sha256': request_hash,
        'storage_policy': 'ISOLATED_WRITE_ONCE_INPUTS',
        'legacy_read_compatibility': True,
    }
    atomic_dump(target / 'evaluation_manifest.json', manifest)
    return target, 'NEW_ISOLATED_EVALUATION'


def latest_evaluation(window_folder):
    """Read helper: newest isolated evaluation, otherwise the legacy layout."""
    window_folder = Path(window_folder)
    manifests = sorted((window_folder / 'evaluations').glob('*/evaluation_manifest.json'))
    valid = []
    for path in manifests:
        try:
            document = evaluation_manifest(path.parent)
            valid.append((document.get('reference_madrid', ''), path.parent))
        except (TrialError, OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(valid)[-1][1] if valid else window_folder


def _batch_configuration(args):
    return {'max_matches_batch_size': int(args.max_matches),
            'history_limit': int(args.history), 'detail_limit': int(args.details),
            'team_filter': _normalized_team_filter(args),
            'evaluation_budget': int(getattr(args, 'evaluation_budget', 0) or 0),
            'horizon_hours': float(getattr(args, 'horizon_hours', 0) or 0),
            'min_lead_minutes': int(args.min_lead),
            'coverage_rules_sha256': sha(Path(args.coverage_rules)) if getattr(args,'coverage_rules',None) else None}


def _validate_batch_result(path, input_path, fixture_ids, config):
    try:
        doc = read(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrialError(f'BATCH_RESULT_JSON_INVALID:{path.parent.name}') from exc
    if doc.get('schema_version') not in ('1.2-preselection', '1.3-preselection'):
        raise TrialError(f'BATCH_RESULT_SCHEMA_INVALID:{path}')
    if doc.get('source_filter_sha256') != sha(input_path):
        raise TrialError(f'BATCH_SOURCE_SHA_MISMATCH:{path.parent.name}')
    rows = doc.get('matches') or []
    ids = [str(row.get('fixture_id')) for row in rows]
    expected = [str(fid) for fid in fixture_ids]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise TrialError(f'BATCH_FIXTURE_IDENTITY_MISMATCH:{path.parent.name}')
    methodology = doc.get('methodology') or {}
    if (methodology.get('sample_history_cap') != config['history_limit'] or
            methodology.get('sample_details_cap') != config['detail_limit'] or
            (doc.get('counts') or {}).get('input_filter') != len(expected)):
        raise TrialError(f'BATCH_PARAMETERS_MISMATCH:{path.parent.name}')
    if dt(doc.get('generated_at_utc')) is None:
        raise TrialError(f'BATCH_RESULT_UNDATED:{path.parent.name}')
    for record in doc.get('api_request_ledger',[]):
        acquired=dt(record.get('first_observed_at_utc') or record.get('fetched_at_utc'))
        if acquired and acquired>dt(doc['generated_at_utc']):
            raise TrialError('BATCH_ACQUISITION_AFTER_CUTOFF:'+path.parent.name)
    return doc


def enrichment_resume_plan(base: Path, subset: dict, args):
    """Crea o revalida el plan inmutable de lotes y devuelve sólo pendientes.

    Permite adoptar ejecuciones antiguas sin checkpoint siempre que cada input
    y resultado coincidan exactamente con el snapshot congelado y parámetros.
    """
    base = Path(base)
    source = base / 'fixtures_major_test.json'
    if not source.is_file() or read(source) != subset:
        raise TrialError('ENRICHMENT_SOURCE_SNAPSHOT_MISMATCH')
    source_sha = sha(source)
    fixtures = subset.get('fixtures') or []
    ids = [str(row.get('fixture_id')) for row in fixtures]
    if len(ids) != len(set(ids)):
        raise TrialError('ENRICHMENT_SOURCE_DUPLICATE_FIXTURE')
    config = _batch_configuration(args)
    checkpoint_path = base / 'enrichment_resume.json'
    try:
        previous = read(checkpoint_path) if checkpoint_path.is_file() else None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrialError('ENRICHMENT_RESUME_CHECKPOINT_INVALID') from exc
    legacy_material = any((base / 'enrichment_batches').glob('*/fixtures.json'))
    if not previous and legacy_material and (config['team_filter'] or config['evaluation_budget']):
        raise TrialError('LEGACY_RESUME_SCOPE_PARAMETERS_UNVERIFIABLE')
    if previous:
        if previous.get('source_sha256') != source_sha or previous.get('configuration') != config:
            raise TrialError('ENRICHMENT_RESUME_SOURCE_OR_PARAMETERS_MISMATCH')
        if previous.get('planned_fixture_ids') != ids:
            raise TrialError('ENRICHMENT_RESUME_FIXTURE_ORDER_MISMATCH')
    entries, completed_docs, completed_sources, pending = [], [], [], []
    prior_by_index = {int(row['index']): row for row in (previous or {}).get('batches', [])}
    for offset in range(0, len(fixtures), config['max_matches_batch_size']):
        index = offset // config['max_matches_batch_size'] + 1
        batch = fixtures[offset:offset + config['max_matches_batch_size']]
        batch_ids = [row['fixture_id'] for row in batch]
        dest = base / 'enrichment_batches' / str(index)
        input_path = dest / 'fixtures.json'
        expected_input = dict(subset, fixtures=batch)
        if input_path.exists():
            try:
                actual_input = read(input_path)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise TrialError(f'BATCH_INPUT_JSON_INVALID:{index}') from exc
            if actual_input != expected_input:
                raise TrialError(f'BATCH_INPUT_IDENTITY_OR_PARAMETERS_MISMATCH:{index}')
        else:
            atomic_dump(input_path, expected_input)
        input_sha = sha(input_path)
        prior = prior_by_index.get(index)
        if prior and prior.get('input_sha256') != input_sha:
            raise TrialError(f'BATCH_INPUT_SHA_MISMATCH:{index}')
        result_path = dest / 'preselection.json'
        entry = {'index': index, 'fixture_ids': batch_ids, 'input': str(input_path.resolve()),
                 'input_sha256': input_sha, 'result': str(result_path.resolve())}
        if prior and prior.get('interrupted_artifacts'):
            entry['interrupted_artifacts'] = list(prior['interrupted_artifacts'])
        if result_path.exists():
            result_sha = sha(result_path)
            if prior and prior.get('result_sha256') and prior['result_sha256'] != result_sha:
                raise TrialError(f'BATCH_RESULT_SHA_MISMATCH:{index}')
            try:
                doc = _validate_batch_result(result_path, input_path, batch_ids, config)
            except TrialError as exc:
                if not str(exc).startswith('BATCH_RESULT_JSON_INVALID:'):
                    raise
                # Un JSON truncado de un lote que nunca llegó a completarse se
                # conserva como evidencia y el lote vuelve a PENDING. Un lote
                # previamente completado nunca entra aquí: su SHA ya se validó.
                archive = dest / 'interrupted_artifacts'
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / f'preselection.{result_sha[:16]}.invalid.json'
                suffix = 1
                while target.exists():
                    suffix += 1
                    target = archive / f'preselection.{result_sha[:16]}.{suffix}.invalid.json'
                result_path.replace(target)
                interrupted = entry.setdefault('interrupted_artifacts', [])
                interrupted.append({'path': str(target.resolve()), 'sha256': result_sha,
                                    'reason': 'INTERRUPTED_OR_TRUNCATED_JSON'})
                entry.update(status='PENDING', result_sha256=None, api_errors=None)
                pending.append({'index': index, 'fixtures': batch, 'dest': dest,
                                'input': input_path, 'fixture_ids': batch_ids})
            else:
                status = 'COMPLETED_WITH_API_ERRORS' if doc.get('api_errors') else 'COMPLETED'
                entry.update(status=status, result_sha256=result_sha,
                             api_errors=len(doc.get('api_errors') or {}))
                completed_docs.append(doc)
                completed_sources.append({'path': str(result_path.resolve()), 'sha256': result_sha,
                                          'input': str(input_path.resolve()), 'input_sha256': input_sha})
        else:
            entry.update(status='PENDING', result_sha256=None, api_errors=None)
            pending.append({'index': index, 'fixtures': batch, 'dest': dest,
                            'input': input_path, 'fixture_ids': batch_ids})
        entries.append(entry)
    checkpoint = {'schema_version': '1.0-enrichment-resume',
                  'source': str(source.resolve()), 'source_sha256': source_sha,
                  'configuration': config, 'planned_fixture_ids': ids,
                  'counts': {'fixtures': len(fixtures), 'batches': len(entries),
                             'completed_batches': len(completed_docs),
                             'completed_fixtures': sum(len(row['fixture_ids']) for row in entries if row['status'].startswith('COMPLETED')),
                             'pending_batches': len(pending),
                             'pending_fixtures': sum(len(row['fixture_ids']) for row in entries if row['status'] == 'PENDING')},
                  'batches': entries}
    atomic_dump(checkpoint_path, checkpoint)
    return {'checkpoint': checkpoint, 'checkpoint_path': checkpoint_path,
            'completed_docs': completed_docs, 'completed_sources': completed_sources,
            'pending': pending}


def merge_enrichment_batches(batch_docs, batch_sources, trial_input: Path):
    if not batch_docs:
        raise TrialError('NO_COMPLETED_ENRICHMENT_BATCHES')
    from preselect_matches import assign_ranks
    pre = copy.deepcopy(batch_docs[0])
    pre['matches'] = [fixture for doc in batch_docs for fixture in doc['matches']]
    ids = [str(fixture['fixture_id']) for fixture in pre['matches']]
    expected_ids = [str(fixture['fixture_id']) for fixture in read(trial_input)['fixtures']]
    if len(ids) != len(set(ids)):
        raise TrialError('DUPLICATE_BATCH_FIXTURE')
    if set(ids) != set(expected_ids):
        raise TrialError('INCOMPLETE_ENRICHMENT_BATCHES')
    batch_cutoffs=[dt(doc.get('generated_at_utc')) for doc in batch_docs]
    if any(cut is None for cut in batch_cutoffs):raise TrialError('BATCH_RESULT_UNDATED')
    pre['generated_at_utc'] = max(batch_cutoffs).astimezone(timezone.utc).isoformat()
    pre['api_request_ledger']=[entry for doc in batch_docs for entry in doc.get('api_request_ledger',[])]
    pre['source_filter_sha256'] = sha(trial_input)
    pre['source_batches'] = batch_sources
    for field in ('counts', 'api_usage'):
        pre[field] = {key: sum(doc.get(field, {}).get(key, 0) or 0 for doc in batch_docs)
                      for key in {key for doc in batch_docs for key in doc.get(field, {})}}
    pre['api_errors'] = {f'batch_{i}_{key}': value for i, doc in enumerate(batch_docs, 1)
                         for key, value in doc.get('api_errors', {}).items()}
    pre['history_seasons_used'] = {key: value for doc in batch_docs
                                   for key, value in doc.get('history_seasons_used', {}).items()}
    assign_ranks(pre['matches'])
    return pre

def window_path(root:Path, day:date, start:time=time(15,0), end:time=time(3,0)):
    begin,finish=local_window(day,start,end)
    return root/'outputs'/'discovery'/f'{begin.date():%Y-%m-%d}_{begin:%H%M}_{finish:%H%M}'

def allowed_window(day, current, start:time=time(15,0), end:time=time(3,0)):
    _begin,finish=local_window(day,start,end)
    if current.timestamp()>=finish.timestamp():
        raise TrialError(f'La ventana {day} {start:%H:%M} → {finish:%Y-%m-%d %H:%M} terminó. No se reabre un PREMATCH pasado.')

def automatic_day(current, start, end):
    local = current.astimezone(TZ)
    yesterday = local.date() - timedelta(days=1)
    begin, finish = local_window(yesterday, start, end)
    if begin.timestamp() <= current.timestamp() < finish.timestamp():
        return yesterday
    return local.date()


def kickoff_valid(obj,current,min_lead):
    start=dt(obj.get('kickoff_madrid'))
    return bool(start and start.timestamp() >= current.timestamp() + min_lead*60)


def scope_reason(obj, current, min_lead, horizon_hours=0):
    if obj.get('status', 'NS') != 'NS' or not kickoff_valid(obj,current,min_lead):
        return 'STARTED_OR_INSUFFICIENT_PREMATCH_MARGIN'
    if horizon_hours and dt(obj['kickoff_madrid']).timestamp() > current.timestamp() + horizon_hours*3600:
        return 'OUTSIDE_NEAR_TERM_HORIZON'
    return None

def run(root:Path,script:str,args:list[str],logs:Path):
    target=root/script
    if not target.is_file():
        raise TrialError(f'Falta {script} en {root}. Debe estar instalado en el proyecto.')
    logs.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f')
    logfile=logs/f'{target.stem}_{stamp}.log'
    command=[sys.executable,str(target),*map(str,args)]
    print('\n$ '+' '.join(command),flush=True)
    started=performance_clock.perf_counter()
    with logfile.open('w',encoding='utf-8') as f:
        with subprocess.Popen(command,cwd=str(root),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                              text=True,encoding='utf-8',errors='replace',bufsize=1) as process:
            assert process.stdout is not None
            for line in process.stdout:
                print(line,end='',flush=True)
                f.write(line)
            code=process.wait()
    with (logs/'pipeline_stage_timings.jsonl').open('a',encoding='utf-8') as timing:
        timing.write(json.dumps({'script':script,'exit_code':code,'elapsed_seconds':round(performance_clock.perf_counter()-started,4),
            'finished_madrid':now_madrid().isoformat(),'log':str(logfile)},ensure_ascii=False)+'\n')
    if code:
        raise TrialError(f'{script} terminó con código {code}; consulta {logfile}')
    return logfile

def select_fixtures(doc, keywords, limit, current,min_lead,horizon_hours=0):
    if doc.get('schema_version')!='1.0-major-leagues':
        raise TrialError('fixtures_major.json no tiene el esquema esperado')
    rows=doc.get('fixtures') or []
    valid=[x for x in rows if scope_reason(x,current,min_lead,horizon_hours) is None]
    if keywords:
        terms=[norm(t) for t in keywords]
        valid=[x for x in valid if any(t in norm(x.get('home','')) or t in norm(x.get('away','')) for t in terms)]
    valid.sort(key=lambda x:(REGIONS.get(x.get('region'),99),x.get('kickoff_madrid',''),str(x.get('fixture_id',''))))
    return valid[:limit] if limit else valid

def check_files(root, scripts):
    missing=[x for x in scripts if not (root/x).is_file()]
    if missing:
        raise TrialError('Scripts necesarios no instalados: '+', '.join(missing))

def prepare(root,folder,day,args):
    from prematch_audit import CandidateLedger
    from preselect_matches import save_report
    check_files(root,['discover_fixtures.py','filter_major_leagues.py','preselect_matches.py',
                      'prematch_pattern_engine.py','prematch_pattern_discovery.py','bwin_discovery.py'])
    reference = execution_reference(args)
    if ((folder/'prospective_trial/enriched/preselection.json').exists() and
            (folder/'prospective_trial/prospective_test_summary.json').exists()):
        archive=root/'outputs/discovery/archives'/f'{folder.name}_{now_madrid():%Y%m%dT%H%M%S%f}'
        shutil.copytree(folder,archive)
    base=folder/'prospective_trial'; logs=base/'logs'
    trial_input=base/'fixtures_major_test.json'
    resume_existing = (not (base/'prospective_test_summary.json').exists() and trial_input.is_file()
                       and ((base/'enrichment_batches').is_dir() or _intact_pre_enrichment_snapshot(folder, base)))
    from prematch_tactical_signal_engine import diversified_order,run_engine
    if resume_existing:
        subset=read(trial_input)
        major_path=folder/'fixtures_major.json'
        begin,finish=local_window(day,args.start,args.end)
        if (subset.get('schema_version')!='1.0-major-leagues' or
                subset.get('trial_subset',{}).get('source_sha256')!=sha(major_path)):
            raise TrialError('RESUME_FROZEN_SOURCE_IDENTITY_MISMATCH')
        selection=subset.get('fixtures') or []
        if any(not (dt(row.get('kickoff_madrid')) and begin <= dt(row['kickoff_madrid']) < finish)
               for row in selection):
            raise TrialError('RESUME_FIXTURE_OUTSIDE_FROZEN_WINDOW')
        all_selection=list(selection);major=read(major_path)
        ledger=CandidateLedger.load(base/'candidate_ledger.json')
        print(f'Reanudación detectada: snapshot congelado {sha(trial_input)[:12]}, {len(selection)} fixtures.',flush=True)
    else:
        run(root,'discover_fixtures.py',['--date',day.isoformat(),'--start',args.start.strftime('%H:%M'),
                                       '--end',args.end.strftime('%H:%M'),'--show','12',
                                       '--output-dir',str(folder),'--asof',reference.astimezone(timezone.utc).isoformat()],logs)
        discovered=read(folder/'fixtures.json')
        ledger=CandidateLedger(base/'candidate_ledger.json', {
            'horizon_hours':getattr(args,'horizon_hours',3),'min_lead_minutes':args.min_lead,
            'max_matches_batch_size':args.max_matches,'bwin_top_batch_size':args.bwin_top})
        for f in discovered.get('fixture_audit', []):
            ledger.record(f,'DESCUBIERTO','discovery','API_RECORD',str(folder/'fixtures.json'))
            if f.get('discovery_exclusion'):
                ledger.record(f,'EXCLUIDO_POR_HORARIO','discovery',f['discovery_exclusion'])
        for f in discovered['fixtures']:
            if str(f['fixture_id']) not in ledger.rows:
                ledger.record(f,'DESCUBIERTO','discovery','API_PREMATCH',str(folder/'fixtures.json'))
        ledger.write()
        run(root,'filter_major_leagues.py',['--input',str(folder/'fixtures.json'),
                                            '--output-dir',str(folder)],logs)
        major=read(folder/'fixtures_major.json')
        for f in major.get('excluded',[])+major.get('review',[]):
            ledger.record(f,'FILTRADO_POR_LIGA','league_filter',f.get('filter_reason'))
        for f in major['fixtures']:
            reason=scope_reason(f,reference,args.min_lead,getattr(args,'horizon_hours',3))
            if reason:ledger.record(f,'EXCLUIDO_POR_HORARIO','scope',reason)
        selection=select_fixtures(major,_normalized_team_filter(args),0,reference,args.min_lead,getattr(args,'horizon_hours',3) or 0)
        if getattr(args,'evaluation_budget',0):selection=diversified_order(selection)
        all_selection=list(selection);budget=getattr(args,'evaluation_budget',0)
        if budget:
            selection=selection[:budget]
            for f in all_selection[budget:]:ledger.record(f,'DIFERIDO_POR_PRESUPUESTO','preselection','EVALUATION_BUDGET_NOT_ANALYZED')
        all_ids={str(x['fixture_id']) for x in all_selection}
        for f in major['fixtures']:
            if str(f['fixture_id']) not in all_ids and not scope_reason(f,reference,args.min_lead,getattr(args,'horizon_hours',3)):
                ledger.record(f,'EXCLUIDO_POR_FILTRO_MANUAL','scope','TEAM_FILTER')
        ledger.write()
        if not selection:
            raise TrialError('No hay partidos futuros y elegibles en el horizonte/ventana solicitado. No se consultó Bwin.')
        subset=dict(major,fixtures=selection)
        subset['counts']=dict(major.get('counts',{}),eligible=len(selection))
        subset['trial_subset']={'source_sha256':sha(folder/'fixtures_major.json'),'count':len(selection),
                                'reference_madrid':reference.isoformat(),
                                'purpose':'ALL_ELIGIBLE_IN_SCOPE_NO_CHRONOLOGICAL_TRUNCATION'}
        if trial_input.exists():
            if read(trial_input) != subset:
                raise TrialError('FROZEN_TRIAL_INPUT_WRITE_ONCE_CONFLICT')
        else:
            atomic_dump(trial_input,subset)
    selected_ids={str(f['fixture_id']) for f in selection}
    plan=enrichment_resume_plan(base,subset,args)
    if plan['checkpoint']['counts']['completed_batches']:
        print(f"Lotes enriquecidos reutilizados: {plan['checkpoint']['counts']['completed_batches']} "
              f"({plan['checkpoint']['counts']['completed_fixtures']} fixtures).",flush=True)
    for pending in plan['pending']:
        cmd_pre = [
            '--input', str(pending['input']), '--output-dir', str(pending['dest']),
            '--max-matches', str(len(pending['fixtures'])), '--history-limit', str(args.history),
            '--detail-limit', str(args.details),
        ]
        if getattr(args, 'coverage_rules', None):
            cmd_pre += ['--coverage-rules', str(args.coverage_rules)]
        if getattr(args, 'confirm_external', False):
            cmd_pre.append('--confirm-external')
        run(root, 'preselect_matches.py', cmd_pre, logs)
        plan=enrichment_resume_plan(base,subset,args)
    if plan['checkpoint']['counts']['pending_batches']:
        raise TrialError('ENRICHMENT_RESUME_INCOMPLETE')
    pre=merge_enrichment_batches(plan['completed_docs'],plan['completed_sources'],trial_input)
    # Selection's frozen start remains immutable. Acquisition has its own
    # effective cutoff: never backdate newly acquired evidence to the start.
    selection_reference = reference
    batch_cuts=[dt(doc.get('generated_at_utc')) for doc in plan['completed_docs']]
    reference=max([reference]+[cut for cut in batch_cuts if cut is not None])
    pre['selection_reference_utc']=selection_reference.astimezone(timezone.utc).isoformat()
    pre['generated_at_utc']=reference.astimezone(timezone.utc).isoformat()
    pre['temporal_policy']='ACQUISITION_COMPLETED_BEFORE_ANALYSIS_CUTOFF'
    if {str(f['fixture_id']) for f in pre['matches']}!=selected_ids:raise TrialError('INCOMPLETE_ENRICHMENT_BATCHES')
    outpre=base/'enriched';prepath=outpre/'preselection.json'
    if prepath.exists():
        existing=read(prepath)
        required=[outpre/'preselection.csv',outpre/'preselection.md']
        if (existing.get('source_filter_sha256')!=sha(trial_input) or
                {str(row['fixture_id']) for row in existing.get('matches',[])}!=selected_ids or
                any(not path.is_file() for path in required)):
            raise TrialError('PUBLISHED_ENRICHMENT_INCOMPLETE_OR_IDENTITY_MISMATCH')
        for source in existing.get('source_batches',[]):
            if sha(Path(source['path']))!=source['sha256'] or sha(Path(source['input']))!=source['input_sha256']:
                raise TrialError('PUBLISHED_ENRICHMENT_BATCH_SHA_MISMATCH')
        pre=existing
        print('Enriquecimiento consolidado existente: reutilizado sin sobrescritura.',flush=True)
    else:
        staging=base/'enrichment_publications'/now_madrid().strftime('%Y%m%dT%H%M%S%f')
        save_report(pre,staging)
        atomic_dump(staging/'MANIFEST.json',{'schema_version':'1.0-enrichment-publication',
            'source_filter_sha256':sha(trial_input),'fixture_ids':sorted(selected_ids),
            'artifacts':{path.name:sha(path) for path in staging.iterdir() if path.is_file()}})
        if outpre.exists():raise TrialError('ENRICHMENT_PUBLICATION_TARGET_ALREADY_EXISTS')
        staging.replace(outpre)
    # Sidecar deportivo V2: determinista, offline y anterior a cualquier dato Bwin.
    run(root,'prematch_pattern_discovery.py',['--preselection',str(prepath),
        '--output-dir',str(base/'patterns_v2'),'--asof',reference.isoformat()],logs)
    for f in pre['matches']:
        ready=f.get('data_readiness_score') is not None and f.get('details_checked')
        ledger.record(f,'PENDIENTE_BWIN' if ready else 'SIN_COBERTURA','preselection',f.get('data_readiness_status'))
    patternfolder=base/'patterns'
    run(root,'prematch_pattern_engine.py',['--preselection',str(prepath),'--top',str(len(selection)),
        '--min-lead-minutes',str(args.min_lead),'--output-dir',str(patternfolder),
        '--asof',reference.isoformat()],logs)
    # Tactical evidence is computed locally before spending the Bwin budget.
    tactical=run_engine(prepath,patternfolder/'pattern_engine.json',base/'tactical',root,
        profiles=root/'config/tactical_profiles/profiles.json',asof=reference)
    tactical_scores={fid:x['priority_score'] for fid,x in tactical['fixtures'].items()}
    ordered=diversified_order(pre['matches'],tactical_scores)
    atomic_dump(base/'budget_assignment.json',{'strategy':'ROUND_ROBIN_TWO_HOUR_SLOTS_TACTICAL_QUALITY_WITHIN_SLOT',
        'evaluation_fixture_ids':[f['fixture_id'] for f in selection],
        'bwin_order_fixture_ids':[f['fixture_id'] for f in ordered],
        'tactical_scores':tactical_scores,'warning':'NO_EV_NO_CALIBRATED_PROBABILITY'})
    discovery_deferred=[]
    budget=getattr(args,'discovery_budget',0)
    if budget:
        discovery_deferred=ordered[budget:];ordered=ordered[:budget]
        for f in discovery_deferred:ledger.record(f,'DIFERIDO_POR_PRESUPUESTO','bwin_discovery','DISCOVERY_BUDGET_NOT_ANALYZED')
    ledger.write()
    # One shared acquisition per evaluation.  The previous implementation ran
    # the same 5-8 catalog pages once per fixture batch and later once per
    # missing fixture.  Candidate matching is local, so batching browser
    # navigation is both slower and weaker (different fixtures saw different
    # catalog instants). ``bwin_top`` remains in the frozen request for legacy
    # read compatibility but is no longer a browser batch size.
    bwinfolder=base/'bwin';dest=bwinfolder/'shared'
    cmd=['--input',str(prepath),'--headless','--output-dir',str(dest)]
    if getattr(args, 'confirm_external', False):
        cmd.append('--confirm-external')
    for f in ordered:cmd+=['--fixture-id',str(f['fixture_id'])]
    run(root,'bwin_discovery.py',cmd,logs)
    shared=read(dest/'bwin_discovery.json')
    from bwin_identity_audit import catalog_identity_fields
    by_event={str(row['bwin_event_id']):{**row,**catalog_identity_fields(row)} for row in shared.get('catalog_observed',[])
              if str(row.get('bwin_event_id') or '').isdigit()}
    bwin={**shared,'schema_version':'3.0-shared-catalog-discovery',
          'source_input':str(prepath),'source_input_sha256':sha(prepath),'prematch_only':True,
          'catalog_observed':list(by_event.values()),
          'source_batches':[str(dest/'bwin_discovery.json')],
          'acquisition_strategy':'ONE_SHARED_CATALOG_LOCAL_MATCHING',
          'catalog_snapshot':{'source_path':str((dest/'bwin_discovery.json').resolve()),
                              'source_sha256':sha(dest/'bwin_discovery.json'),
                              'captured_at_utc':shared.get('generated_at_utc'),
                              'scope':'DAILY_OPERATIONAL_WINDOW_PLUS_BOUNDED_COUNTRY_COMPLEMENT'},
          'legacy_bwin_top_ignored_for_navigation':args.bwin_top}
    atomic_dump(bwinfolder/'bwin_discovery.json',bwin)
    # Publish a bidirectional, all-fixture audit without additional network.
    from bwin_identity_audit import (audit as identity_audit,write_reports as write_identity_reports,
                                     merge_catalog,catalog_scope_complete)
    canonical_path=bwinfolder/'bwin_discovery.json'
    audit_catalog,audit_pages,_=merge_catalog([canonical_path])
    catalog_complete=catalog_scope_complete(audit_pages)
    identity_doc=identity_audit(pre['matches'],audit_catalog,audit_pages,
        [{'path':str((bwinfolder/'bwin_discovery.json').resolve()),'sha256':sha(bwinfolder/'bwin_discovery.json'),
          'generated_at_utc':bwin.get('generated_at_utc'),'rows':len(audit_catalog)}],
        catalog_complete=catalog_complete)
    write_identity_reports(bwinfolder/'identity_audit',identity_doc)
    for f in bwin['matches']:
        status=f.get('status','')
        if not f.get('bwin_event_id'):ledger.record(f,'SIN_BWIN','bwin_discovery',status)
        elif 'REVIEW' in status:ledger.record(f,'IDENTIDAD_INCORRECTA','bwin_discovery',status)
        else:ledger.record(f,'PENDIENTE_CUOTAS','bwin_discovery',status)
    ledger.write();begin,finish=local_window(day,args.start,args.end)
    summary={'schema_version':'2.0-prospective-trial','stage':'PREPARED','day':day.isoformat(),
        'generated_madrid':now_madrid().isoformat(),'execution_reference_madrid':reference.isoformat(),
        'window_start_madrid':begin.isoformat(),
        'window_end_madrid':finish.isoformat(),'fixtures_considered':len(selection),
        'candidate_fixtures':all_selection,'scope':ledger.scope,
        'evaluation_deferred_ids':[f['fixture_id'] for f in all_selection if str(f['fixture_id']) not in selected_ids],
        'discovery_deferred_ids':[f['fixture_id'] for f in discovery_deferred],
        'sources':{'preselection':str(prepath),'pattern':str(patternfolder/'pattern_engine.json'),
                   'pattern_discovery_v2':str(base/'patterns_v2/pattern_discovery.json'),
                   'research_queue_sport':str(base/'patterns_v2/research_queue_sport.json'),
                   'discovery':str(bwinfolder/'bwin_discovery.json')}}
    atomic_dump(base/'prospective_test_summary.json',summary)
    return summary

def scan(root,folder,day,args):
    # Shared production integration: bounded event retries and validated reuse.
    import fcntl
    from prematch_runtime import scan as production_scan
    lock_path = root / 'outputs/.prematch_daily.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TrialError('PREMATCH_ALREADY_RUNNING') from None
        return production_scan(root,folder,day,args)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evaluation-budget',type=int,default=0)
    p.add_argument('--discovery-budget',type=int,default=0)
    p.add_argument('--date',help='Forzar fecha de inicio Madrid; por defecto ventana operativa actual')
    p.add_argument('--window',help='Perfil de config/prematch_windows.json: normal, ampliada u otro personalizado')
    p.add_argument('--config',type=Path,help='Ruta opcional a JSON de perfiles (por defecto config/prematch_windows.json)')
    p.add_argument('--start',type=parse_clock,help='Sobrescribe inicio HH:MM del perfil sin modificar JSON')
    p.add_argument('--end',type=parse_clock,help='Sobrescribe fin HH:MM del perfil sin modificar JSON')
    p.add_argument('--horizon-hours',type=float,default=3,help='Próximas N horas; 0 = toda la ventana')
    p.add_argument('--show-config',action='store_true',help='Mostrar fecha, perfil, ventana y carpeta sin llamar a API/Bwin')
    p.add_argument('--stage',choices=['prepare','scan','all'],default='prepare')
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--team',action='append',default=[],help='Equipo a priorizar (subcadena de nombre); repetible')
    p.add_argument('--max-matches',type=int,default=5,help='Tamaño del lote API; no recorta candidatos')
    p.add_argument('--bwin-top',type=int,default=5,help='Tamaño del lote Bwin; no recorta candidatos')
    p.add_argument('--max-scrapes',type=int,default=0,help='Presupuesto explícito; 0 = todos los candidatos en el horizonte')
    p.add_argument('--discovery-retries',type=int,default=2)
    p.add_argument('--max-odds-age',type=int,default=120)
    p.add_argument('--history',type=int,default=20)
    p.add_argument('--coverage-rules',type=Path)
    p.add_argument('--details',type=int,default=8)
    p.add_argument('--min-lead',type=int,default=60)
    p.add_argument('--confirm-external',action='store_true')
    args=p.parse_args(argv)
    if args.evaluation_budget<0 or args.discovery_budget<0 or not __import__("math").isfinite(args.horizon_hours) or args.horizon_hours<0 or args.discovery_retries<0 or args.max_odds_age<1 or args.max_matches<1 or args.bwin_top<1 or args.max_scrapes<0 or not 4<=args.history<=50 or args.details<1 or args.details>args.history or args.min_lead<45:
        p.error('Límites inválidos: max-matches/bwin-top >0, max-scrapes >=0, history 4..50, 1<=details<=history y min-lead>=45')
    if args.date is not None:
        try:
            day = parse_date_relative(args.date)
        except (argparse.ArgumentTypeError, ValueError) as exc:
            p.error(str(exc))
    else:
        day = None
    root=args.root.expanduser().resolve()
    try:
        cfg, from_file, cfg_path=load_window_config(root,args.config)
        window_name, args.start, args.end=resolve_window(cfg,args.window,args.start,args.end)
        reference = now_madrid()
        args.execution_reference = reference
        day = day or automatic_day(reference,args.start,args.end)
        begin,finish=local_window(day,args.start,args.end)
        window_folder=window_path(root,day,args.start,args.end)
        print(f'CONFIG: {cfg_path} ({"JSON" if from_file else "integrada; instala el JSON para editarla"})',flush=True)
        print(f'PERFIL: {window_name} | VENTANA: {begin.isoformat()} -> {finish.isoformat()}',flush=True)
        print(f'VENTANA RAÍZ: {window_folder}',flush=True)
        print(f'HORIZONTE: {args.horizon_hours} h (0=toda la ventana)',flush=True)
        if args.show_config:
            return 0
        allowed_window(day,reference,args.start,args.end)
        if args.stage in ('prepare','all'):
            folder, mode = resolve_evaluation(window_folder,day,args,reference)
            if mode == 'RESUME_FROZEN_EVALUATION':
                args.execution_reference = dt(evaluation_manifest(folder).get('reference_madrid'))
            print(f'EVALUACIÓN {mode}: {folder}',flush=True)
        else:
            folder = latest_evaluation(window_folder)
            print(f'EVALUACIÓN DE LECTURA: {folder}',flush=True)
        if args.stage in ('prepare','all'):
            prepare(root,folder,day,args)
        if args.stage in ('scan','all'):
            scan(root,folder,day,args)
        return 0
    except (TrialError,OSError,ValueError,KeyError,TypeError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
