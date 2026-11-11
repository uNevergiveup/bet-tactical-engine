#!/usr/bin/env python3
"""PREMATCH multiplexado: preparar hasta 3 eventos y validar/motorizar por Event ID.

No ejecuta Perplexity ni Codex. Nunca modifica config/constants.py.
Uso: python prematch_multi.py --help
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from match_identity import write_identity, validate_identity

PRINT_LOCK = threading.Lock()
UTC = timezone.utc
FINISH_MIN_LEAD_MINUTES = 5


def say(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def identifier(url: str) -> str:
    pieces = urlsplit(url.strip())
    if (pieces.scheme != 'https' or pieces.hostname not in ('bwin.es', 'www.bwin.es')
            or '/sports/eventos/' not in pieces.path):
        raise ValueError('URL inválida: se espera evento HTTPS de bwin.es')
    found = re.search(r'-2:(\d+)$', unquote(pieces.path.rstrip('/')))
    if not found:
        raise ValueError('La URL no contiene -2:<Event ID>: '+url)
    return found.group(1)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_dt() -> datetime:
    """Injectable clock seam for operation-window checks and offline tests."""
    return datetime.now(UTC)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event_dir(root: Path, event: str) -> Path:
    return root / 'outputs' / 'matches' / event


def manifest_path(root: Path, event: str) -> Path:
    return event_dir(root, event) / 'manifest.json'


def save_manifest(root: Path, event: str, data: dict) -> None:
    target = manifest_path(root, event)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f'manifest.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(target)


def load_manifest(root: Path, event: str) -> dict:
    path = manifest_path(root, event)
    if not path.is_file():
        raise ValueError(f'No se preparó el evento {event}: falta {path}')
    obj = json.loads(path.read_text(encoding='utf-8'))
    if obj.get('event_id') != event or identifier(obj.get('url', '')) != event:
        raise ValueError(f'Manifiesto del evento {event} inconsistente')
    return obj


def _kickoff_from_manifest(root: Path, event: str, state: dict) -> datetime:
    """Resolve kickoff only from this Event ID's pinned identity/context.

    Direct ``prematch_multi.py`` invocations retain the same temporal safety as
    ``prematch_single.py``. If no canonical kickoff is available, fail closed
    instead of guessing from a filename or another event.
    """
    inputs = state.get('inputs') or {}
    candidates = []
    for key in ('identity', 'context_json'):
        entry = inputs.get(key) or {}
        path = Path(entry.get('path') or '')
        if not path.is_file():
            continue
        try:
            obj = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, TypeError):
            continue
        candidates.extend([
            obj.get('kickoff_utc'),
            (obj.get('fixture') or {}).get('date'),
            (obj.get('match') or {}).get('kickoff_utc'),
        ])
    # A failed retry deliberately removes manifest references to the context
    # before regenerating it, but the event-scoped files remain intact.  Use
    # those same-event files as a read-only clock source so a later retry is
    # still possible; never search globally or borrow another Event ID.
    if not candidates:
        for path in (event_dir(root, event) / 'inputs' / 'match_identity.json',
                     event_dir(root, event) / 'inputs' / f'match_engine_{event}.json'):
            if not path.is_file():
                continue
            try:
                obj = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError, TypeError):
                continue
            candidates.extend([
                obj.get('kickoff_utc'),
                (obj.get('fixture') or {}).get('date'),
                (obj.get('match') or {}).get('kickoff_utc'),
            ])
    parsed = []
    for raw in candidates:
        try:
            value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if value.tzinfo is not None and value.utcoffset() is not None:
                parsed.append(value.astimezone(UTC))
        except (TypeError, ValueError, OverflowError):
            continue
    if not parsed:
        raise ValueError(f'{event}: FINISH_KICKOFF_UNVERIFIED; falta kickoff canónico del propio evento')
    if any(abs((value - parsed[0]).total_seconds()) > 60 for value in parsed[1:]):
        raise ValueError(f'{event}: KICKOFF_IDENTITY_CONFLICT; no se ejecuta la operación')
    return parsed[0]


def ensure_event_operation_window(root: Path, event: str, state: dict,
                                  *, min_lead: int = FINISH_MIN_LEAD_MINUTES,
                                  operation: str = 'operation', now: datetime | None = None) -> float:
    """Allow an operation only while the fixture is future and >= T-min_lead."""
    kickoff = _kickoff_from_manifest(root, event, state)
    remaining = (kickoff - (now or utc_now_dt()).astimezone(UTC)).total_seconds() / 60
    if remaining <= 0:
        raise ValueError(f'{event}: {operation.upper()}_AFTER_KICKOFF; PREMATCH bloqueado')
    if remaining < min_lead:
        raise ValueError(f'{event}: {operation.upper()}_WINDOW_EXPIRED; quedan {remaining:.1f} min, se requieren {min_lead}')
    return remaining


def match_header(path: Path, event: str) -> bool:
    if not path.is_file():
        return False
    with path.open('r', encoding='utf-8-sig', errors='replace') as f:
        head = ''.join(f.readline() for _ in range(90))
    return re.search(r'(?m)^Event ID:\s*'+re.escape(event)+r'\s*$',head) is not None


def find_one(root: Path, event: str, kind: str) -> Path | None:
    if kind == 'odds':
        directory = event_dir(root, event) / 'inputs'
        prefix = 'cuotas_bwin'
    else:
        directory = root / 'outputs' / 'data'
        prefix = 'match_engine'
    if not directory.is_dir():
        return None
    paths = [p for p in directory.glob(prefix + '*.txt') if match_header(p, event)]
    # No usar un contexto antiguo por casualidad en una re-ejecución.
    if len(paths) != 1:
        raise ValueError(f'{event}: archivos {kind} encontrados={len(paths)}. '
                         f'Elige/retira capturas duplicadas antes de preparar: {[p.name for p in paths[:5]]}')
    return paths[0]


def stage_file_from_log(root: Path, event: str, kind: str) -> Path:
    """El fichero recién producido en ESTE intento (nunca otro snapshot)."""
    name='scraper' if kind=='odds' else 'contexto'
    log=event_dir(root,event)/'logs'/(name+'.log')
    text=log.read_text(encoding='utf-8',errors='replace')
    if kind=='odds':
        found=re.findall(r'(?m)^TXT:\s*(.+?\.txt)\s*$',text)
    else:
        found=re.findall(r'(?m)^\[OK\]\s+TXT generado:\s*(.+?\.txt)\s*$',text)
    if not found:
        # Nunca recuperar un TXT previo solo porque comparte Event ID:
        # exigir la ruta emitida por ESTA ejecución en el log actual.
        raise ValueError(f'La ejecución actual no declaró un TXT {kind} en {log}; '
                         'no se reutilizan snapshots anteriores.')
    path=Path(found[-1].strip())
    if not path.is_absolute():
        path=root/path
    if not match_header(path,event):
        raise ValueError(f'Log {name} apunta a fichero de otro evento o ausente: {path}')
    if kind == 'odds':
        expected = event_dir(root,event) / 'inputs' / f'cuotas_bwin_{event}.txt'
        if path.resolve() != expected.resolve():
            raise ValueError(f'Scraper escribió fuera de la carpeta del evento: {path}; '
                             f'se esperaba {expected}. Actualiza scraper_bwin.py.')
    return path


def pin_input(origin: Path, dest: Path, event: str, *, remove_source: bool = True) -> None:
    """Verifica el snapshot antes de retirar su versión temporal.

    El .tmp y la comparación de hash evitan borrar una fuente que no quedó
    correctamente persistida. Sólo modifica rutas específicas del evento.
    """
    if not origin.is_file():
        raise ValueError(f'Fuente no encontrada: {origin}')
    if not match_header(origin, event):
        raise ValueError(f'Event ID no coincide en {origin}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    if origin.resolve() == dest.resolve():
        return
    old_hash = sha(origin)
    temporary = dest.with_name(dest.name + f'.tmp-{os.getpid()}-{threading.get_ident()}')
    try:
        shutil.copy2(origin, temporary)
        if sha(temporary) != old_hash or not match_header(temporary, event):
            raise ValueError(f'Error al verificar copia: {origin}')
        temporary.replace(dest)
        if remove_source and sha(dest) == old_hash and sha(origin) == old_hash:
            origin.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def execute(root: Path, event: str, name: str, command: list[str], env: dict | None = None) -> int:
    logs = event_dir(root,event) / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    logpath = logs / (name+'.log')
    say(f'[{event}] INICIO {name}')
    with logpath.open('w', encoding='utf-8') as log:
        log.write(f'# {utc_now()} | {command[1]} | Event ID {event}\n')
        log.flush()
        try:
            result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, check=False)
            code = result.returncode
        except OSError as exc:
            log.write(f'ERROR iniciando programa: {exc}\n')
            code = 127
    say(f'[{event}] {"OK" if code == 0 else "ERROR"} {name} (código {code}) · {logpath.relative_to(root)}')
    return code


def prepare_one(root: Path, url: str, python: str, shared_file: Path,
                api_minute: int, api_second: int, reuse_odds: bool = False) -> bool:
    event = identifier(url)
    if reuse_odds:
        from prematch_single import load_context,freshness,validate_reuse_odds
        from types import SimpleNamespace
        before=load_context(SimpleNamespace(root=root,selected=Path(reuse_odds),decision=None))
        if before['event_id']!=event or before['url']!=url:raise ValueError('REUSE_ODDS_SELECTION_IDENTITY_MISMATCH')
        freshness(before,min_lead=45)
        validate_reuse_odds(before,max_age=120)
    location = event_dir(root,event)
    location.mkdir(parents=True, exist_ok=True)
    # Archive stale identity/context before preparation; never allow a failed API
    # call to reuse a previous JSON, and retain the original bytes for audit.
    previous=[location/'inputs'/f'match_engine_{event}.json',location/'inputs'/'match_identity.json']
    if any(p.exists() for p in previous):
        archive=location/'archives'/('before_context_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        archive.mkdir(parents=True,exist_ok=False)
        for p in previous:
            if p.is_file():p.replace(archive/p.name)
    state = {'event_id':event, 'url':url, 'prepared_at':utc_now(), 'status':'PREPARANDO',
             'inputs':{}, 'steps':{}}
    save_manifest(root,event,state)
    env = os.environ.copy()
    env['PREMATCH_BATCH_MODE'] = '1'
    env['PREMATCH_SHARED_BUDGET_FILE'] = str(shared_file)
    env['PREMATCH_API_MAX_PER_MINUTE'] = str(api_minute)
    env['PREMATCH_API_MAX_PER_SECOND'] = str(api_second)
    env['PREMATCH_EVENT_ID'] = event
    env['PREMATCH_EVENT_DATA_DIR'] = str((location/'inputs').resolve())
    if reuse_odds:
        from prematch_single import snapshot_info,load_context,freshness,read_json,validate_reuse_odds
        from types import SimpleNamespace
        ctx=load_context(SimpleNamespace(root=root,selected=Path(reuse_odds),decision=None))
        if ctx['event_id']!=event or ctx['url']!=url:raise ValueError('REUSE_ODDS_SELECTION_IDENTITY_MISMATCH')
        freshness(ctx,min_lead=45)
        snap=validate_reuse_odds(ctx,max_age=120)
        env['PREMATCH_TEMPORAL_IDENTITY_SELECTED']=str(ctx['selected_path'])
        odds_code=0
        state['odds_reused']=True
    else:
        odds_code = execute(root,event,'scraper', [python,'-u','scraper_bwin.py','--url',url],env)
    state['steps']['scraper'] = odds_code
    # El contexto puede obtenerse aun si Bwin no devuelve cuotas.
    api_code = execute(root,event,'contexto', [python,'-u','generate_context.py',url],env)
    state['steps']['contexto'] = api_code
    state['status'] = ('PREPARADO' if odds_code == 0 and api_code == 0 else
                       'PARCIAL_SIN_CUOTAS' if api_code == 0 else 'ERROR_CONTEXTO')
    if odds_code == 0 or api_code == 0:
        try:
            for kind in ('odds','context'):
                if (kind == 'odds' and odds_code != 0) or (kind == 'context' and api_code != 0):
                    continue
                origin = location/'inputs'/f'cuotas_bwin_{event}.txt' if kind=='odds' and reuse_odds else stage_file_from_log(root,event,kind)
                if origin is None:
                    raise ValueError(f'No se encuentra fichero {kind} para Event ID {event}')
                dest = location/'inputs'/('cuotas_bwin_'+event+'.txt' if kind=='odds' else
                                          'match_engine_'+event+'.txt')
                # Ambos productores escriben directamente en la carpeta individual.
                # pin_input valida Event ID y conserva hashes/compatibilidad.
                if kind == 'context':
                    json_origin = origin.with_suffix('.json')
                    json_payload = None
                    if json_origin.is_file():
                        try:
                            extra = json.loads(json_origin.read_text(encoding='utf-8-sig'))
                            ident = str((extra.get('match') or {}).get('event_id'))
                            json_fixture = str((extra.get('fixture') or {}).get('fixture_id'))
                            with origin.open('r', encoding='utf-8-sig', errors='replace') as fh:
                                txt_head = ''.join(fh.readline() for _ in range(130))
                            m = re.search(r'(?m)^Fixture ID:\s*(\d+)\s*$', txt_head)
                            g = re.search(r'(?m)^Generado:\s*(\S+)', txt_head)
                            t_json = datetime.fromisoformat(str(extra.get('generated_at', '')).replace('Z', '+00:00'))
                            t_txt = datetime.fromisoformat(g.group(1).replace('Z', '+00:00')) if g else None
                            if not (ident == event and m is not None and json_fixture == m.group(1) and
                                    t_txt is not None and t_json.tzinfo is not None and t_txt.tzinfo is not None and
                                    abs((t_json-t_txt).total_seconds()) <= 30):
                                raise ValueError('JSON API distinto del TXT actual: ID/fixture/fecha')
                            json_payload = json_origin
                        except (ValueError, TypeError, OSError, json.JSONDecodeError, OverflowError) as err:
                            state.setdefault('warnings', []).append(f'JSON complementario omitido: {err}')
                            say(f'[{event}] WARN contexto JSON: {err}')
                    else:
                        state.setdefault('warnings', []).append('JSON API complementario no disponible')
                pin_input(origin, dest, event, remove_source=True)
                state['inputs'][kind] = {'path':str(dest.resolve()), 'sha256':sha(dest),
                                         'generated_from':str(origin.resolve())}
                if kind == 'context' and json_payload is not None:
                    json_dest = location/'inputs'/f'match_engine_{event}.json'
                    if json_payload.resolve() != json_dest.resolve():
                        # El par JSON/TXT fue validado antes del movimiento.
                        json_dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = json_dest.with_name(json_dest.name+f'.tmp-{os.getpid()}')
                        try:
                            shutil.copy2(json_payload,tmp)
                            if sha(tmp) != sha(json_payload):
                                raise ValueError('JSON API no coincide tras copiar')
                            tmp.replace(json_dest)
                            if sha(json_dest) == sha(json_payload):
                                json_payload.unlink()
                        finally:
                            tmp.unlink(missing_ok=True)
                    state['inputs']['context_json'] = {
                        'path': str(json_dest.resolve()), 'sha256': sha(json_dest),
                        'generated_from': str(json_payload.resolve())}
        except (OSError,ValueError) as exc:
            state['status'] = 'ERROR_ARCHIVOS'
            state['error'] = str(exc)
    if api_code == 0 and state['status'] != 'ERROR_ARCHIVOS':
        try:
            identity_status = bind_identity(root,event,state)
            if identity_status == 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN':
                state['status'] = 'PREPARADO_PROVISIONAL'
                state.setdefault('warnings', []).append('BWIN_KICKOFF_UNVERIFIED: contexto aislado sin vínculo temporal Bwin verificado')
        except (OSError,ValueError,KeyError) as exc:
            state['status']='ERROR_ARCHIVOS'
            state['error']='Identidad: '+str(exc)
    state['finished_at'] = utc_now()
    save_manifest(root,event,state)
    say(f'[{event}] PREPARACIÓN: {state["status"]}')
    return state['status'] in {'PREPARADO', 'PREPARADO_PROVISIONAL'}


def bind_identity(root: Path, event: str, state: dict) -> str:
    """Vincula identidad comprobando el JSON de contexto y el snapshot SHA."""
    entry=(state.get('inputs') or {}).get('context_json')
    if not entry:
        raise ValueError('No hay JSON de contexto para generar identidad canónica')
    j=Path(entry['path'])
    if not j.is_file() or sha(j)!=entry['sha256']:
        raise ValueError('JSON del contexto no coincide con su hash del manifiesto')
    dst=event_dir(root,event)/'inputs'/'match_identity.json'
    # Reconstituye desde el JSON verificado: no acepta identidad de ejecuciones anteriores.
    write_identity(j,dst)
    obj=json.loads(dst.read_text(encoding='utf-8'))
    validate_identity(obj,json.loads(j.read_text(encoding='utf-8')),event,state['url'])
    state['inputs']['identity']={'path':str(dst.resolve()),'sha256':sha(dst),
                                 'generated_from':str(j.resolve())}
    state['identity_status'] = obj.get('identity_status')
    return str(obj.get('identity_status'))


def finish_one(root: Path, event: str, python: str, override_report: str | None = None,
               allow_no_odds: bool = False, historical: bool = False) -> bool:
    state = load_manifest(root,event)
    if not historical:
        ensure_event_operation_window(root, event, state,
                                      min_lead=FINISH_MIN_LEAD_MINUTES, operation='finish')
    if state.get('status') == 'PREPARADO_PROVISIONAL':
        raise ValueError('API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN: contexto provisional; no procede finish ni picks finales')
    if state.get('status') not in {'PREPARADO','PARCIAL_SIN_CUOTAS','VALIDADO','FINALIZADO',
                                   'BLOQUEADO_VALIDACION','ERROR_VALIDACION','ERROR_MOTOR','AUDITORIA_HISTORICA'}:
        raise ValueError(f'Evento {event}: preparación incompleta ({state.get("status")})')
    if state['status']=='PARCIAL_SIN_CUOTAS' and not allow_no_odds:
        raise ValueError(f'Evento {event}: faltan cuotas Bwin; usa --allow-no-odds para solo estadística')
    incoming = root/'instructions'/f'reporte_cualitativo_{event}.md'
    pinned = event_dir(root,event)/'inputs'/f'reporte_cualitativo_{event}.md'
    report = Path(override_report) if override_report else (incoming if incoming.is_file() else pinned)
    if not report.is_absolute():
        report = root/report
    if not report.is_file() or not report.stat().st_size:
        raise ValueError(f'Reporte cualitativo del evento {event} ausente: {report}. '
                         f'Colócalo en instructions/ o en {pinned}')
    # No aceptar un informe explícitamente etiquetado con otro Event ID.
    body=report.read_text(encoding='utf-8-sig',errors='replace')
    label=re.search(r'(?im)\b(?:event[_ ]id(?: bwin)?|evento bwin)\s*[:|]\s*(\d{6,})\b',body[:10000])
    if label and label.group(1) != event:
        raise ValueError(f'Reporte {report.name} declara Event ID {label.group(1)} en vez de {event}')
    existing_meta = (state.get('inputs') or {}).get('report') or {}
    unchanged = (report.resolve() == pinned.resolve() and existing_meta.get('sha256') == sha(report))
    if not unchanged:
        source_mtime = datetime.fromtimestamp(report.stat().st_mtime,timezone.utc).isoformat()
        is_inbox = report.resolve() == incoming.resolve()
        # Sólo mover el archivo de entrada que pertenece al proyecto.
        # Un --report externo se conserva en su ubicación de origen.
        pinned.parent.mkdir(parents=True, exist_ok=True)
        if report.resolve() != pinned.resolve():
            temporary = pinned.with_name(pinned.name+f'.tmp-{os.getpid()}-{threading.get_ident()}')
            try:
                shutil.copy2(report,temporary)
                if sha(temporary) != sha(report):
                    raise ValueError('Falló la importación verificada del informe')
                temporary.replace(pinned)
                if is_inbox and sha(pinned) == sha(report):
                    report.unlink()
            finally:
                temporary.unlink(missing_ok=True)
        state['inputs']['report']={'path':str(pinned.resolve()),'sha256':sha(pinned),
                                   'imported_from':str(report.resolve()),
                                   'imported_at_utc':utc_now(), 'source_mtime_utc':source_mtime,
                                   'temporal_note':'Importación y mtime no prueban hora real de generación.'}
    else:
        state['inputs']['report'] = existing_meta
    state['status']='VALIDANDO'
    save_manifest(root,event,state)
    inputs = state['inputs']
    for kind in ('odds','context','context_json','identity'):
        if kind not in inputs:
            continue
        p=Path(inputs[kind]['path'])
        if not p.is_file() or sha(p)!=inputs[kind]['sha256']:
            raise ValueError(f'Snapshot {kind} distinto del manifest de {event}')
        if kind in ('odds','context'):
            correct=match_header(p,event)
        elif kind=='context_json':
            correct=str((json.loads(p.read_text(encoding='utf-8-sig')).get('match') or {}).get('event_id'))==event
        else:
            obj=json.loads(p.read_text(encoding='utf-8-sig'))
            context_json=inputs.get('context_json')
            payload=json.loads(Path(context_json['path']).read_text(encoding='utf-8-sig')) if context_json else None
            correct=validate_identity(obj,payload,event,state['url'])
        if not correct:
            raise ValueError(f'Identidad de snapshot {kind} no coincide con {event}')
    # Compatibilidad: preparar identidad para eventos anteriores con JSON válido.
    if 'identity' not in inputs and 'context_json' in inputs:
        bind_identity(root,event,state)
        save_manifest(root,event,state)
    if 'context' not in inputs:
        raise ValueError('Falta contexto API para el evento '+event)
    valdir = event_dir(root,event)/'validation'
    args=[python,'-u','validate_prematch.py','--root',str(root),'--url',state['url'],
          '--api',inputs['context']['path'],'--report',str(pinned),'--out',str(valdir)]
    if 'identity' in inputs:
        args.extend(['--identity',inputs['identity']['path']])
    if 'odds' in inputs:
        args.extend(['--odds',inputs['odds']['path']])
    else:
        args.append('--no-odds')
    if historical:
        args.append('--historical')
    # Sin cuotas se valida la integridad restante, SIN picks ni precios.
    valcode=execute(root,event,'validacion',args)
    state['steps']['validacion']=valcode
    valpath=valdir/f'validacion_{event}.json'
    if not valpath.exists():
        state['status']='ERROR_VALIDACION';save_manifest(root,event,state)
        return False
    validated=json.loads(valpath.read_text(encoding='utf-8'))
    state['validation_state']=validated.get('status')
    if valcode not in (0,1) or validated.get('status')=='BLOQUEADO_PREPARTIDO':
        state['status']='BLOQUEADO_VALIDACION';save_manifest(root,event,state)
        blocks = [item for item in validated.get('issues', []) if item.get('severity') == 'BLOCK']
        for item in blocks:
            say(f'[{event}] BLOCK {item.get("code", "SIN_CODIGO")}: {item.get("detail", "")[:300]}')
        say(f'[{event}] Validación bloqueada: {valdir / ("validacion_" + event + ".md")}')
        say(f'[{event}] Corrige la causa y reintenta finish SIN repetir prepare; no se omitirán los BLOCK.')
        return False
    state['status']='VALIDADO';save_manifest(root,event,state)
    probdir=event_dir(root,event)/'probabilistic'
    engine=[python,'-u','motor_probabilistico.py','--context',inputs['context']['path'],
            '--event-id',event,'--validation',str(valpath),'--report',str(pinned),
            '--output-dir',str(probdir)]
    if 'context_json' in inputs:
        engine.extend(['--context-json',inputs['context_json']['path']])
    if 'odds' in inputs:
        engine.extend(['--odds',inputs['odds']['path']])
    else:
        engine.append('--no-odds')
    code=execute(root,event,'motor',engine)
    state['steps']['motor']=code
    state['status']='FINALIZADO' if code==0 else 'ERROR_MOTOR'
    state['finish_completed_at'] = utc_now()
    if code == 0 and historical:
        state['status']='AUDITORIA_HISTORICA'
        state['historical']=True
        say(f'[{event}] AUDITORÍA HISTÓRICA: motor ejecutado sobre snapshots; NO ejecutar apuestas ni análisis live')
    if code == 0 and not historical:
        prompt = event_dir(root,event)/f'CODEX_{event}.md'
        prompt.write_text(f'''# Ejecución Codex: SOLO evento Bwin {event}

Analiza SOLO este evento. No utilices `BWIN_EVENT_URL` para elegir el partido:
el parámetro explícito Event ID {event} y estas rutas prevalecen para ESTA sesión.
Sigue `AGENTS.md` y `instructions/analysis_instructions.txt` para el resto.
No reejecutes scraper, generador, validador ni motor. No uses web.

- Validación: `{valpath.resolve()}` (estado: {validated.get('status')})
- API-Football TXT: `{inputs['context']['path']}`
- API-Football JSON de la misma captura: `{inputs.get('context_json',{}).get('path','NO DISPONIBLE')}`
- Bwin: `{inputs.get('odds',{}).get('path','NO DISPONIBLE')}`
- Informe cualitativo (Gemini/Perplexity): `{pinned.resolve()}`
- Matriz: `{(probdir/f'matriz_evidencia_{event}.json').resolve()}`
- Ranking: `{(probdir/f'ranking_probabilistico_{event}.json').resolve()}`
- Resumen: `{(probdir/f'resumen_codex_{event}.md').resolve()}`
- Mapa táctico: `{(probdir/f'mapa_tactico_{event}.md').resolve()}`
- Hipótesis: `{(probdir/f'hipotesis_cruzadas_{event}.md').resolve()}`
- Jugadores: `{(probdir/f'patrones_jugadores_{event}.md').resolve()}`
- Contexto competitivo: `{(probdir/f'contexto_competitivo_{event}.md').resolve()}`
- Contexto competitivo JSON: `{(probdir/f'contexto_competitivo_{event}.json').resolve()}`

Comprueba identities, timestamps, XI y mercado literal. Si el kickoff ya ocurrió,
solo auditoría retrospectiva basada en snapshots prepartido; NO apuestas live.
Produce `outputs/matches/{event}/analysis/analisis_{event}.md` exclusivamente para este evento.
''',encoding='utf-8')
        state['codex_prompt']=str(prompt.resolve())
        say(f'[{event}] LISTO PARA CODEX: {prompt.relative_to(root)}')
    save_manifest(root,event,state)
    return code==0



def retry_context_one(root: Path, event: str, python: str, shared_file: Path,
                      api_minute: int, api_second: int, force: bool = False,
                      temporal_identity_selected: Path | None = None) -> bool:
    """Reintenta SOLO API-Football; conserva snapshot Bwin y su hash original.

    Nunca ejecuta scraper_bwin ni usa capturas de otro Event ID. Un fallo de
    reintento conserva el snapshot Bwin y permite reintentar de nuevo.
    """
    state = load_manifest(root, event)
    ensure_event_operation_window(root, event, state,
                                  min_lead=FINISH_MIN_LEAD_MINUTES, operation='retry-context')
    if state.get('status') not in {'ERROR_CONTEXTO', 'ERROR_ARCHIVOS', 'PREPARADO_PROVISIONAL'}:
        raise ValueError(f'{event}: estado={state.get("status")}. '
                         'retry-context es solo para fallos previos del contexto.')
    if state.get('steps', {}).get('scraper') != 0:
        raise ValueError(f'{event}: el scraper no finalizó correctamente; no se reutilizan cuotas no válidas.')
    odds = state.get('inputs', {}).get('odds') or {}
    old = Path(odds.get('path', ''))
    if not (odds.get('sha256') and old.is_file() and match_header(old, event)
            and sha(old) == odds['sha256']):
        raise ValueError(f'{event}: falta snapshot de cuotas intacto (ID/SHA256); '
                         'no se permite retry-context.')

    state['status'] = 'REINTENTANDO_CONTEXTO'
    state['inputs'].pop('context', None)
    state['inputs'].pop('context_json', None)
    state['inputs'].pop('identity', None)
    state['retry_context_started_at'] = utc_now()
    save_manifest(root, event, state)

    env = os.environ.copy()
    env['PREMATCH_BATCH_MODE'] = '1'
    env['PREMATCH_SHARED_BUDGET_FILE'] = str(shared_file)
    env['PREMATCH_API_MAX_PER_MINUTE'] = str(api_minute)
    env['PREMATCH_API_MAX_PER_SECOND'] = str(api_second)
    env['PREMATCH_EVENT_ID'] = event
    env['PREMATCH_EVENT_DATA_DIR'] = str((event_dir(root,event)/'inputs').resolve())
    if temporal_identity_selected:
        env['PREMATCH_TEMPORAL_IDENTITY_SELECTED']=str(temporal_identity_selected.resolve())
    command = [python, '-u', 'generate_context.py', state['url']]
    if force:
        command.append('--force')
    code = execute(root, event, 'contexto', command, env)
    state['steps']['contexto'] = code
    state['retry_context_completed_at'] = utc_now()
    if code != 0:
        state['status'] = 'ERROR_CONTEXTO'
        save_manifest(root, event, state)
        say(f'[{event}] API sin resolver; cuotas anteriores conservadas sin cambios.')
        return False

    try:
        origin = stage_file_from_log(root, event, 'context')
        dest = event_dir(root, event) / 'inputs' / f'match_engine_{event}.txt'
        dest.parent.mkdir(parents=True, exist_ok=True)
        pin_input(origin,dest,event,remove_source=True)
        if not match_header(dest, event):
            raise ValueError('Event ID del contexto no coincide')
        state['inputs']['context'] = {'path': str(dest.resolve()), 'sha256': sha(dest),
                                      'generated_from': str(origin.resolve())}
        json_origin = origin.with_suffix('.json')
        if json_origin.is_file():
            try:
                obj = json.loads(json_origin.read_text(encoding='utf-8-sig'))
                j_event = str((obj.get('match') or {}).get('event_id'))
                j_fixture = str((obj.get('fixture') or {}).get('fixture_id'))
                with dest.open(encoding='utf-8-sig', errors='replace') as fh:
                    header = ''.join(fh.readline() for _ in range(130))
                found_fid = re.search(r'(?m)^Fixture ID:\s*(\d+)\s*$', header)
                found_date = re.search(r'(?m)^Generado:\s*(\S+)', header)
                jdate = datetime.fromisoformat(str(obj.get('generated_at', '')).replace('Z', '+00:00'))
                tdate = datetime.fromisoformat(found_date.group(1).replace('Z', '+00:00')) if found_date else None
                if not (j_event == event and found_fid and j_fixture == found_fid.group(1)
                        and tdate and tdate.tzinfo and jdate.tzinfo
                        and abs((jdate - tdate).total_seconds()) <= 30):
                    raise ValueError('Identidad, fixture o tiempo del JSON contradicen al TXT')
                jdest = event_dir(root, event) / 'inputs' / f'match_engine_{event}.json'
                if json_origin.resolve() != jdest.resolve():
                    tmp = jdest.with_name(jdest.name+f'.tmp-{os.getpid()}')
                    try:
                        shutil.copy2(json_origin,tmp)
                        if sha(tmp) != sha(json_origin):
                            raise ValueError('JSON corrupto en retry-context')
                        tmp.replace(jdest)
                        json_origin.unlink()
                    finally:
                        tmp.unlink(missing_ok=True)
                state['inputs']['context_json'] = {'path': str(jdest.resolve()),
                                                   'sha256': sha(jdest),
                                                   'generated_from': str(json_origin.resolve())}
            except (ValueError, TypeError, OSError, json.JSONDecodeError, OverflowError) as err:
                state.setdefault('warnings', []).append(f'JSON complementario omitido: {err}')
                say(f'[{event}] WARN contexto JSON: {err}')
        else:
            state.setdefault('warnings', []).append('JSON API complementario no disponible')
        # Revalidar que el scraper anterior NO cambió durante el intento.
        if not match_header(old, event) or sha(old) != odds['sha256']:
            raise ValueError('Las cuotas Bwin cambiaron mientras se generaba el contexto')
    except (OSError, ValueError) as err:
        state['status'] = 'ERROR_ARCHIVOS'
        state['error'] = str(err)
        save_manifest(root, event, state)
        say(f'[{event}] ERROR snapshot contexto: {err}')
        return False

    try:
        identity_status = bind_identity(root,event,state)
    except (OSError,ValueError,KeyError) as exc:
        state['status']='ERROR_ARCHIVOS'
        state['error']='Identidad: '+str(exc)
        save_manifest(root,event,state)
        return False
    state['status'] = ('PREPARADO_PROVISIONAL'
                       if identity_status == 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN'
                       else 'PREPARADO')
    state.pop('error', None)
    save_manifest(root, event, state)
    say(f'[{event}] CONTEXTO RECUPERADO; cuotas originales preservadas; estado={state["status"]}.')
    return True


def retry_scraper_one(root: Path, event: str, python: str) -> bool:
    """Reintenta SOLO Bwin, preservando contexto API por Event ID y SHA256.

    Nunca ejecuta generate_context.py, ni reimporta un TXT antiguo distinto del
    producido en ESTE intento. Solo para partidas sin snapshot de cuotas válido.
    """
    state = load_manifest(root, event)
    ensure_event_operation_window(root, event, state,
                                  min_lead=FINISH_MIN_LEAD_MINUTES, operation='retry-scraper')
    if state.get('status') not in {'PARCIAL_SIN_CUOTAS', 'ERROR_ARCHIVOS'}:
        raise ValueError(f'{event}: estado={state.get("status")}; '
                         'retry-scraper requiere contexto válido y cuotas ausentes.')
    inputs = state.get('inputs') or {}
    context = inputs.get('context') or {}
    cpath = Path(context.get('path') or '')
    if not (context.get('sha256') and cpath.is_file() and
            match_header(cpath, event) and sha(cpath) == context['sha256']):
        raise ValueError(f'{event}: contexto API ausente, inválido o modificado; '
                         'no ejecutar scraper sin preservar la referencia.')
    if 'odds' in inputs:
        odds_meta = inputs['odds']
        opath = Path(odds_meta.get('path') or '')
        if (opath.is_file() and odds_meta.get('sha256')
                and sha(opath) == odds_meta['sha256'] and match_header(opath,event)):
            raise ValueError(f'{event}: ya existen cuotas válidas; '
                             'no sobrescribir el snapshot con retry-scraper.')
    old_status = state['status']
    state['status'] = 'REINTENTANDO_SCRAPER'
    state['retry_scraper_started_at'] = utc_now()
    save_manifest(root,event,state)
    env=os.environ.copy()
    env['PREMATCH_BATCH_MODE']='1'
    env['PREMATCH_EVENT_ID']=event
    env['PREMATCH_EVENT_DATA_DIR']=str((event_dir(root,event)/'inputs').resolve())
    code=execute(root,event,'scraper',[python,'-u','scraper_bwin.py','--url',state['url']],env)
    state['steps']['scraper']=code
    state['retry_scraper_completed_at']=utc_now()
    if code != 0:
        state['status']='PARCIAL_SIN_CUOTAS'
        save_manifest(root,event,state)
        say(f'[{event}] Scraper sin cuotas; contexto API previo conservado sin cambios.')
        return False
    try:
        origin=stage_file_from_log(root,event,'odds')
        dest=event_dir(root,event)/'inputs'/f'cuotas_bwin_{event}.txt'
        pin_input(origin,dest,event,remove_source=True)
        # Revalidar el contexto DESPUÉS del scraper, no solo antes.
        if not cpath.is_file() or sha(cpath) != context['sha256'] or not match_header(cpath,event):
            raise ValueError('El contexto API cambió durante retry-scraper')
        state['inputs']['odds']={'path':str(dest.resolve()),'sha256':sha(dest),
                                 'generated_from':str(origin.resolve())}
    except (OSError,ValueError) as exc:
        state['status']='ERROR_ARCHIVOS'
        state['error']=str(exc)
        save_manifest(root,event,state)
        say(f'[{event}] ERROR al persistir cuotas: {exc}')
        return False
    state['status']='PREPARADO'
    state.pop('error',None)
    save_manifest(root,event,state)
    say(f'[{event}] SCRAPER RECUPERADO; contexto intacto; listo para finish.')
    return True


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare',help='Scraper + API de 1–3 eventos concurrentes')
    p.add_argument('--url', action='append',required=True,help='Repite --url para cada partido')
    p.add_argument('--workers',type=int,default=2,help='Simultaneidad de partidos (máx. 3)')
    p.add_argument('--reuse-odds-selected',type=Path,help='Selección verificada y SHA para reutilizar cuotas del mismo evento')
    p.add_argument('--api-minute',type=int,default=180,help='Presupuesto compartido del lote, por minuto')
    p.add_argument('--api-second',type=int,default=3,help='Presupuesto compartido del lote, por segundo')
    q=sub.add_parser('retry-scraper',help='Reintenta solo Bwin en PARCIAL_SIN_CUOTAS; conserva API')
    q.add_argument('--event-id',action='append',required=True)
    q.add_argument('--workers',type=int,default=2)
    r=sub.add_parser('retry-context',help='Reintenta API-Football para ERROR_CONTEXTO sin repetir scraper Bwin')
    r.add_argument('--event-id',action='append',required=True)
    r.add_argument('--workers',type=int,default=2)
    r.add_argument('--api-minute',type=int,default=180)
    r.add_argument('--api-second',type=int,default=3)
    r.add_argument('--force',action='store_true',help='Ignora caché API (usa más llamadas)')
    r.add_argument('--temporal-identity-selected',type=Path,
                   help='selected_match.json con evidencia temporal Fase 3 verificable')
    f=sub.add_parser('finish',help='Validación + motor para reportes cualitativos por evento')
    f.add_argument('--event-id',action='append',required=True)
    f.add_argument('--report',help='Ruta explícita del informe; solo con un --event-id')
    f.add_argument('--workers',type=int,default=2)
    f.add_argument('--allow-no-odds',action='store_true')
    f.add_argument('--historical',action='store_true',help='Auditoría retrospectiva de snapshots prepartido; nunca apuestas en vivo')
    s=sub.add_parser('status',help='Manifiestos multi, o el handoff diario con --date (sin manifest.json)')
    s.add_argument('--event-id',action='append')
    s.add_argument('--date',help='YYYY-MM-DD, hoy o mañana. Lee outputs/discovery de prematch_daily; no exige manifest')
    s.add_argument('--window',choices=['normal','ampliada'])
    args=parser.parse_args(argv)
    root=args.root.expanduser().resolve()
    if args.action != 'status' and not (root/'config'/'constants.py').is_file():
        parser.error(f'No existe config/constants.py en {root}; ejecuta/copia el lanzador en la raíz scraper')
    python=sys.executable
    if args.action=='prepare':
        if not 1<=args.workers<=3 or not 1<=len(args.url)<=3:
            parser.error('Se permiten 1–3 partidos y 1–3 workers')
        if not (1<=args.api_minute<=240 and 1<=args.api_second<=4):
            parser.error('--api-minute entre 1 y 240; --api-second entre 1 y 4')
        ids=[identifier(url) for url in args.url]
        if len(set(ids))!=len(ids):
            parser.error('No repitas un mismo Event ID en un lote')
        shared=root/'outputs'/'matches'/'_shared'/'api_budget.json'
        shared.parent.mkdir(parents=True,exist_ok=True)
        say(f'Lote: {len(ids)} evento(s) · simultaneidad {args.workers} · API compartida {args.api_minute}/min, {args.api_second}/s')
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map={pool.submit(prepare_one,root,url,python,shared,args.api_minute,args.api_second,args.reuse_odds_selected):identifier(url)
                        for url in args.url}
            results=[]
            for task in concurrent.futures.as_completed(future_map):
                try:
                    results.append(task.result())
                except Exception as exc:
                    say(f'[{future_map[task]}] ERROR: {exc}')
                    results.append(False)
        return 0 if all(results) else 1
    if args.action=='retry-scraper':
        if not 1<=args.workers<=3 or not 1<=len(args.event_id)<=3:
            parser.error('Se permiten 1–3 eventos y 1–3 workers')
        ids=args.event_id
        if any(not re.fullmatch(r'\d{6,}',event) for event in ids) or len(set(ids))!=len(ids):
            parser.error('Event IDs incorrectos o duplicados')
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map={pool.submit(retry_scraper_one,root,event,python):event for event in ids}
            results=[]
            for task in concurrent.futures.as_completed(future_map):
                try:
                    results.append(task.result())
                except Exception as exc:
                    say(f'[{future_map[task]}] ERROR: {exc}')
                    results.append(False)
        return 0 if all(results) else 1
    if args.action=='retry-context':
        if not 1<=args.workers<=3 or not 1<=len(args.event_id)<=3:
            parser.error('Se permiten 1–3 eventos y 1–3 workers')
        if not 1<=args.api_minute<=240 or not 1<=args.api_second<=4:
            parser.error('Límites API inválidos')
        ids = args.event_id
        if any(not re.fullmatch(r'\d{6,}', event) for event in ids) or len(set(ids)) != len(ids):
            parser.error('Event IDs incorrectos o duplicados')
        shared=root/'outputs'/'matches'/'_shared'/'api_budget.json'
        shared.parent.mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map = {pool.submit(retry_context_one, root, event, python, shared,
                                      args.api_minute, args.api_second, args.force,
                                      args.temporal_identity_selected):event for event in ids}
            results = []
            for task in concurrent.futures.as_completed(future_map):
                try:
                    results.append(task.result())
                except Exception as exc:
                    say(f'[{future_map[task]}] ERROR: {exc}')
                    results.append(False)
        return 0 if all(results) else 1
    if args.action=='finish':
        if not 1<=args.workers<=3: parser.error('--workers debe ser 1, 2 o 3')
        if args.report and len(args.event_id)!=1:
            parser.error('--report solo puede usarse con un único Event ID')
        ids=args.event_id
        if len(set(ids))!=len(ids): parser.error('IDs duplicados')
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map={pool.submit(finish_one,root,event,python,args.report,args.allow_no_odds,args.historical):event
                        for event in ids}
            results=[]
            for task in concurrent.futures.as_completed(future_map):
                try:
                    results.append(task.result())
                except Exception as exc:
                    say(f'[{future_map[task]}] ERROR: {exc}')
                    results.append(False)
        return 0 if all(results) else 1
    parent=root/'outputs'/'matches'
    if getattr(args, 'date', None):
        import prematch_daily as daily_flow
        report = daily_flow.discovery_status(root, args.date, getattr(args, 'window', None))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        ready = report.get('artifacts', {}).get('handoff', {}).get('present') and report.get('status') not in (
            'NO_DAILY_HANDOFF', 'HANDOFF_UNREADABLE')
        if not args.event_id:
            return 0 if ready else 2
    ids=args.event_id or ([p.name for p in parent.iterdir() if p.is_dir() and p.name.isdigit()]
                          if parent.exists() else [])
    if not ids and not getattr(args, 'date', None):
        say('STATUS_MULTI_EMPTY: no hay outputs/matches/<event_id>/manifest.json. '
            'La jornada diaria se consulta con: python prematch_multi.py status --date YYYY-MM-DD')
        return 2
    for event in sorted(ids):
        try:
            d=load_manifest(root,event)
            report=(root/'instructions'/f'reporte_cualitativo_{event}.md')
            pinned=event_dir(root,event)/'inputs'/f'reporte_cualitativo_{event}.md'
            say(f'{event}: {d.get("status")} · informe={"LISTO" if (report.is_file() or pinned.is_file()) else "PENDIENTE"} '
                f'· validación={d.get("validation_state","sin ejecutar")}')
        except (ValueError,OSError) as exc:
            say(f'{event}: ERROR {exc}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
