#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Orquestador PREMATCH de UN partido elegido por select_single_match.py.

No ejecuta Codex, ni IA externa. Reutiliza el pipeline existente prematch_multi.py,
limita la ejecución a un Event ID y documenta vínculos y hashes.

python prematch_single.py --selected /path/selected_match.json check
python prematch_single.py --selected /path/selected_match.json prepare
python prematch_single.py --selected /path/selected_match.json refresh-odds
python prematch_single.py --selected /path/selected_match.json finish --report /path/informe.md
python prematch_single.py --selected /path/selected_match.json status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote
from zoneinfo import ZoneInfo
from prematch_prospective_trial import TrialError

VERSION = '0.2-single-operation-windows'
TZ = ZoneInfo('Europe/Madrid')
# Temporal gates are deliberately operation-specific.  Full preparation and
# the initial selector still require the conservative 45-minute margin; data
# refreshes and the final validation path may run until T-5, never at/after
# kickoff.  Identity, SHA, Bwin-time and freshness checks remain independent.
PREPARE_MIN_LEAD_MINUTES = 45
REFRESH_MIN_LEAD_MINUTES = 5
FINISH_MIN_LEAD_MINUTES = 5
RE_CAPTURE = re.compile(r'(?im)^Captura:\s*(\S+)\s*$')
RE_EVENT = re.compile(r'(?im)^Event ID:\s*(\d{6,})\s*$')
RE_URL = re.compile(r'(?im)^URL:\s*(https://\S+)\s*$')


def local_now():
    raw = os.getenv('PREMATCH_NOW_UTC')
    if raw:
        return dt(raw, 'PREMATCH_NOW_UTC').astimezone(TZ)
    return datetime.now(TZ)


def iso_utc():
    raw = os.getenv('PREMATCH_NOW_UTC')
    if raw:
        return dt(raw, 'PREMATCH_NOW_UTC').astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def dt(value, label):
    try:
        when=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError('sin zona horaria')
        return when
    except (TypeError,ValueError) as e:
        raise ValueError(f'{label}: fecha ISO8601 con offset obligatoria ({e})') from e


def event_id(url):
    u=urlsplit(str(url))
    if u.scheme!='https' or u.hostname not in ('bwin.es','www.bwin.es'):
        raise ValueError('Solo URL HTTPS de bwin.es')
    m=re.search(r'-2:(\d{6,})$',unquote(u.path.rstrip('/')))
    if not m:raise ValueError('URL no contiene Event ID -2:<id>')
    return m.group(1)


def validate_selected(selected: dict, decision: dict):
    if selected.get('schema_version')!='1.0-selected-single-fixture' or selected.get('status')!='SELECTED_FOR_DEEP_RESEARCH':
        raise ValueError('selected_match.json no es una selección definitiva accionable')
    if decision.get('status')!='SELECTED':
        raise ValueError('selection_decision.json no confirma estado SELECTED')
    found=decision.get('provisional_or_selected') or {}
    fid=int(selected['fixture_id']); eid=str(selected['bwin_event_id']); url=selected['bwin_url']
    if event_id(url)!=eid:raise ValueError('Event ID en URL Bwin contradice selected_match.json')
    for k in ('fixture_id','bwin_event_id','bwin_url','home','away'):
        if str(selected[k])!=str(found.get(k)):
            raise ValueError(f'Inconsistencia selector/decisión: {k}')
    for k in ('league_id','season','home_api_id','away_api_id','odds_sha256','odds_txt',
              'temporal_identity_evidence'):
        if k in selected and str(selected[k])!=str(found.get(k)):
            raise ValueError(f'Inconsistencia selector/decisión: {k}')
    kickoff=dt(selected['kickoff_madrid'],'selected.kickoff_madrid')
    decision_kickoff=dt(found['kickoff_madrid'],'decision.kickoff_madrid')
    if kickoff.astimezone(timezone.utc)!=decision_kickoff.astimezone(timezone.utc):
        raise ValueError('Kickoff distinto entre decisión y seleccionado')
    if not (kickoff.astimezone(TZ).utcoffset()==kickoff.utcoffset()):
        raise ValueError('Offset de kickoff_madrid incoherente con Europe/Madrid')
    families=selected.get('market_families_ready_for_research') or []
    if set(families)!=set(found.get('market_families_ready') or []):
        raise ValueError('Familias investigables no coinciden con la decisión')
    tier=selected.get('selection_tier',found.get('selection_tier','A'))
    if tier not in ('A','B') or tier!=found.get('selection_tier',tier):
        raise ValueError('Nivel de investigación inválido o contradictorio')
    for field in ('research_eligible','eligible','research_missing_evidence','hard_block_reasons'):
        if field in selected or field in found:
            if selected.get(field)!=found.get(field):raise ValueError('Selección/decisión discrepan: '+field)
    if tier=='B' and (selected.get('eligible') is not False or selected.get('research_eligible') is not True
                      or not selected.get('research_missing_evidence') or selected.get('hard_block_reasons')):
        raise ValueError('Nivel B carece de advertencias o contiene bloqueo crítico')
    if tier=='A' and selected.get('eligible',True) is not True:
        raise ValueError('Nivel A debe conservar elegibilidad comercial')
    if fid<1 or not eid.isdigit():raise ValueError('Identificadores inválidos')
    return {'fixture_id':fid,'event_id':eid,'url':url,'kickoff':kickoff,'home':selected['home'],
            'away':selected['away'],'families':families,'selection_tier':tier,
            'selection_time':dt(selected['selection_asof_madrid'],'selection_asof_madrid')}


def load_context(args):
    root=args.root.expanduser().resolve()
    from prematch_paths import local_reference
    selected_path=local_reference(root, args.selected)
    decision_path=local_reference(root, args.decision) if args.decision else selected_path.parent/'selection_decision.json'
    if not selected_path.is_file():raise ValueError(f'Falta selección: {selected_path}')
    if not decision_path.is_file():raise ValueError(f'Falta decisión: {decision_path}; añade --decision')
    selected=read_json(selected_path)
    decision=read_json(decision_path)
    record=validate_selected(selected,decision)
    record.update(root=root, selected_path=selected_path, decision_path=decision_path,
                  selected_sha=sha(selected_path),decision_sha=sha(decision_path),
                  directory=root/'outputs'/'matches'/record['event_id'])
    record['selection']=selected
    return record


def freshness(ctx, *, min_lead=0, max_selection_age=0):
    now=local_now()
    remain=(ctx['kickoff']-now).total_seconds()/60
    age=(now-ctx['selection_time']).total_seconds()/60
    if age < -2: raise ValueError(f'Selección futura respecto al reloj actual ({age:.1f} min); comprueba zonas horarias')
    if remain <= 0: raise ValueError(f'El kickoff pasó ({ctx["kickoff"].astimezone(TZ).isoformat()}); PREMATCH bloqueado')
    if remain < min_lead: raise ValueError(f'Tiempo restante {remain:.1f} min < mínimo {min_lead} min')
    # La selección estratégica puede ser temprana. Si se pide explícitamente un
    # máximo positivo, se conserva la comprobación tradicional como opción.
    if max_selection_age > 0 and age > max_selection_age:
        raise ValueError(f'Selección caducada: {age:.1f} min > {max_selection_age} min; repite el selector')
    return remain,age


def ensure_prematch(ctx, *, min_lead=0):
    remain=(ctx['kickoff']-local_now()).total_seconds()/60
    if remain<=0:raise ValueError('El evento ya empezó: PREMATCH bloqueado')
    if remain<min_lead:raise ValueError(f'Margen {remain:.1f} min, se requieren {min_lead} min')
    return remain


def ensure_root(ctx, multi=None):
    root=ctx['root']
    multi=(root/multi).resolve() if multi else root/'prematch_multi.py'
    if not multi.is_file():raise ValueError(f'Falta {multi}. Copia la versión actual de prematch_multi.py a la raíz del proyecto.')
    if not (root/'scraper_bwin.py').is_file():raise ValueError(f'Falta scraper_bwin.py en {root}')
    if not (root/'generate_context.py').is_file():raise ValueError(f'Falta generate_context.py en {root}')
    if not (root/'config'/'constants.py').is_file():raise ValueError(f'Falta config/constants.py en {root}')
    return multi


def resolve_qualitative_report_path(
    root: Path,
    directory: Path,
    event_id: str,
    explicit_report: Path | str | None = None,
) -> Path:
    """Resuelve la ruta del informe cualitativo conservando --report explícito."""
    eid = str(event_id)
    if explicit_report:
        p = Path(explicit_report).expanduser()
        if p.is_absolute():
            return p
        if (root / p).is_file():
            return root / p
        if (directory / p).is_file():
            return directory / p
        return root / p
    candidates = [
        directory / 'instructions' / f'reporte_cualitativo_{eid}.md',
        directory / 'inputs' / f'reporte_cualitativo_{eid}.md',
        root / 'instructions' / f'reporte_cualitativo_{eid}.md',
    ]
    for cand in candidates:
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    return candidates[0]


def match_manifest(ctx, *, check_files=True, manifest=None):
    mpath=ctx['directory']/'manifest.json'
    if manifest is None:
        if not mpath.is_file():raise ValueError('No existe manifest.json del evento; ejecuta prepare')
        manifest=read_json(mpath)
    if str(manifest.get('event_id'))!=ctx['event_id'] or manifest.get('url')!=ctx['url']:
        raise ValueError('Manifest Event ID / URL distinto a la selección')
    inp=manifest.get('inputs') or {}
    for kind in inp:
        e=inp.get(kind)
        if not e:continue
        if check_files:
            from prematch_paths import local_reference
            path=local_reference(ctx['root'], e['path'], e.get('sha256'))
            e['path']=str(path)
            if not path.is_file() or not e.get('sha256') or sha(path)!=e['sha256']:
                raise ValueError(f'SHA/ruta de manifest.{kind} inválido: {path}')
    ident=inp.get('identity') or {}
    if ident:
        obj=read_json(Path(ident['path']))
        if (str(obj.get('bwin_event_id'))!=ctx['event_id'] or
                int(obj.get('api_fixture_id') or 0)!=ctx['fixture_id']):
            raise ValueError('match_identity.json contradice selección (Bwin/API fixture)')
        kick=dt(obj.get('kickoff_utc'),'match_identity.kickoff_utc')
        if abs((kick-ctx['kickoff']).total_seconds())>180:
            raise ValueError('Kickoff del fixture API no coincide con el seleccionado')
        if obj.get('identity_status') not in {
                'FIXTURE_AND_BWIN_TIME_VERIFIED',
                'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN',
                'FIXTURE_VERIFIED_BY_GENERATOR',
        }:
            raise ValueError('match_identity no tiene un estado verificable')
        selected=ctx.get('selection',{})
        for field in ('league_id','season'):
            if selected.get(field) is not None and str(selected[field])!=str(obj.get(field)):
                raise ValueError('Selección/contexto discrepan: '+field)
        for side in ('home','away'):
            expected=selected.get(side+'_api_id')
            if expected is not None and str(expected)!=str(obj.get(side,{}).get('api_team_id')):
                raise ValueError('Selección/contexto discrepan en team ID: '+side)
    elif manifest.get('status') in ('PREPARADO','VALIDADO','FINALIZADO'):
        raise ValueError('Manifest preparado sin identidad canónica verificada')
    research=manifest.get('research_selection')
    if research:
        selected=ctx.get('selection',{})
        if (research.get('selection_tier')!=ctx['selection_tier'] or
                research.get('research_missing_evidence')!=selected.get('research_missing_evidence',[]) or
                research.get('selected_sha256')!=ctx['selected_sha'] or
                research.get('decision_sha256')!=ctx['decision_sha']):
            raise ValueError('Manifest y selección investigadora discrepan')
    return manifest


def snapshot_info(path,ctx, *, max_age=None):
    if not path.is_file():raise ValueError(f'Snapshot de cuotas ausente: {path}')
    content=path.read_text(encoding='utf-8-sig',errors='replace')
    m=RE_EVENT.search(content[:6000]);u=RE_URL.search(content[:6000]);t=RE_CAPTURE.search(content[:6000])
    if not m or m.group(1)!=ctx['event_id']:
        raise ValueError('Event ID no coincide en el TXT Bwin')
    if not u or event_id(u.group(1))!=ctx['event_id']:
        raise ValueError('URL del TXT apunta a otro evento')
    if not t:raise ValueError('Falta marca de captura en el TXT')
    captured=dt(t.group(1),'TXT.Captura')
    if captured>=ctx['kickoff']:raise ValueError('El TXT declara captura tras kickoff; no es prematch')
    age=(local_now()-captured).total_seconds()/60
    if age < -2:raise ValueError('El TXT declara una captura futura; reloj incongruente')
    if max_age is not None and age>max_age:raise ValueError(f'Cuotas antiguas: {age:.1f} min > {max_age} min; refresh-odds')
    return {'capture':captured.isoformat(),'age_minutes':round(age,2),'sha256':sha(path),'size':path.stat().st_size}


def state_path(ctx):return ctx['directory']/'single_execution.json'


def validate_reuse_odds(ctx, max_age=60):
    selected=ctx['selection']
    expected=ctx['directory']/'inputs'/f'cuotas_bwin_{ctx["event_id"]}.txt'
    if not selected.get('odds_sha256') or not selected.get('odds_txt'):
        raise ValueError('REUSE_ODDS_SELECTION_BINDING_MISSING: regenerar con selector y preflight originales')
    from prematch_paths import local_reference
    if local_reference(ctx['root'],selected['odds_txt'],selected['odds_sha256'])!=expected.resolve():raise ValueError('REUSE_ODDS_SELECTION_PATH_MISMATCH')
    info=snapshot_info(expected,ctx,max_age=max_age)
    header=expected.read_text(encoding='utf-8-sig',errors='replace')[:6000]
    if RE_URL.search(header).group(1).strip()!=ctx['url']:raise ValueError('REUSE_ODDS_SELECTION_URL_MISMATCH')
    if info['sha256']!=selected['odds_sha256']:raise ValueError('REUSE_ODDS_SELECTION_SHA_MISMATCH')
    preflight=selected.get('preflight_path')
    if not preflight or not selected.get('preflight_sha256'):raise ValueError('REUSE_ODDS_PREFLIGHT_BINDING_MISSING')
    path=local_reference(ctx['root'],preflight,selected['preflight_sha256'])
    if sha(path)!=selected['preflight_sha256']:raise ValueError('REUSE_ODDS_PREFLIGHT_SHA_MISMATCH')
    doc=read_json(path)
    for field, digest in (('source_discovery','source_sha256'),('source_preselection','source_preselection_sha256')):
        origin=local_reference(ctx['root'],doc.get(field),doc.get(digest))
        if not origin.is_file() or sha(origin)!=doc.get(digest):raise ValueError('REUSE_ODDS_ORIGINAL_SOURCE_SHA_MISMATCH')
    rows=[r for r in doc.get('matches',[]) if r.get('fixture_id')==ctx['fixture_id'] and str(r.get('bwin_event_id'))==ctx['event_id'] and r.get('bwin_url')==ctx['url']]
    if len(rows)!=1:raise ValueError('REUSE_ODDS_PREFLIGHT_IDENTITY_MISMATCH')
    snapshot=rows[0].get('snapshot') or {}
    if any(rows[0].get(k)!=ctx[k] for k in ('home','away')):raise ValueError('REUSE_ODDS_PREFLIGHT_TEAMS_MISMATCH')
    if snapshot.get('sha256')!=info['sha256'] or local_reference(ctx['root'],snapshot.get('snapshot_path'),snapshot.get('sha256'))!=expected.resolve():
        raise ValueError('REUSE_ODDS_PREFLIGHT_SNAPSHOT_MISMATCH')
    if dt(rows[0]['kickoff_madrid'],'preflight.kickoff')!=ctx['kickoff']:raise ValueError('REUSE_ODDS_PREFLIGHT_KICKOFF_MISMATCH')
    info['temporal_identity']=validate_temporal_identity_evidence(
        ctx,max_age_minutes=180)
    return info


def _source_path(root, value, digest=None):
    from prematch_paths import local_reference
    return local_reference(root, value, digest)


def validate_temporal_identity_evidence(ctx, *, max_age_minutes=180, reference_now=None):
    """Revalidate Phase 3's event-specific Bwin kickoff evidence end to end."""
    selected=ctx['selection'];contract=selected.get('temporal_identity_evidence')
    if not isinstance(contract,dict):
        raise ValueError('TEMPORAL_IDENTITY_CONTRACT_MISSING')
    exact={'fixture_id':ctx['fixture_id'],'bwin_event_id':ctx['event_id'],
           'bwin_url':ctx['url'],'home':ctx['home'],'away':ctx['away']}
    for key,value in exact.items():
        if str(contract.get(key))!=str(value):
            raise ValueError('TEMPORAL_IDENTITY_WRONG_EVENT: '+key)
    if contract.get('schema_version')!='1.0-phase3-temporal-identity' or contract.get('status')!='BWIN_API_KICKOFF_VERIFIED':
        raise ValueError('TEMPORAL_IDENTITY_CONTRACT_INVALID')
    preflight=_source_path(ctx['root'],selected.get('preflight_path'),selected.get('preflight_sha256'))
    if (not preflight.is_file() or sha(preflight)!=selected.get('preflight_sha256') or
            sha(preflight)!=contract.get('preflight_sha256')):
        raise ValueError('TEMPORAL_IDENTITY_PREFLIGHT_SHA_MISMATCH')
    doc=read_json(preflight)
    discovery=_source_path(ctx['root'],doc.get('source_discovery'),doc.get('source_sha256'))
    if (not discovery.is_file() or sha(discovery)!=doc.get('source_sha256') or
            sha(discovery)!=contract.get('discovery_sha256')):
        raise ValueError('TEMPORAL_IDENTITY_DISCOVERY_SHA_MISMATCH')
    source=read_json(discovery)
    rows=[r for r in doc.get('matches',[]) if str(r.get('fixture_id'))==str(ctx['fixture_id'])
          and str(r.get('bwin_event_id'))==ctx['event_id'] and r.get('bwin_url')==ctx['url']]
    origins=[r for r in source.get('matches',[]) if str(r.get('fixture_id'))==str(ctx['fixture_id'])
             and str(r.get('bwin_event_id'))==ctx['event_id'] and r.get('bwin_url')==ctx['url']]
    if len(rows)!=1 or len(origins)!=1:
        raise ValueError('TEMPORAL_IDENTITY_SOURCE_IDENTITY_MISMATCH')
    row=rows[0];origin=origins[0]
    if any(row.get(k)!=ctx[k] for k in ('home','away')):
        raise ValueError('TEMPORAL_IDENTITY_TEAMS_MISMATCH')
    if dt(row.get('kickoff_madrid'),'preflight.kickoff')!=ctx['kickoff']:
        raise ValueError('TEMPORAL_IDENTITY_KICKOFF_MISMATCH')
    evidence=row.get('time_evidence') or {}
    accepted={'DISPLAY_TIME_CORROBORATED','COUNTDOWN_TIME_CORROBORATED','ABSOLUTE_TIME_VERIFIED'}
    if evidence.get('status') not in accepted:
        raise ValueError('TEMPORAL_IDENTITY_EVIDENCE_NOT_VERIFIED')
    if evidence.get('status') in {'DISPLAY_TIME_CORROBORATED','COUNTDOWN_TIME_CORROBORATED'}:
        if source.get('user_time_reference')!='Europe/Madrid' or contract.get('timezone')!='Europe/Madrid':
            raise ValueError('TEMPORAL_IDENTITY_TIMEZONE_UNKNOWN')
    now=reference_now or local_now()
    observed=dt(evidence.get('observed_at_utc'),'time_evidence.observed_at_utc')
    age=(now-observed).total_seconds()/60
    if age < -2 or age > max_age_minutes:
        raise ValueError(f'TEMPORAL_IDENTITY_EVIDENCE_STALE: {age:.1f} min')
    from bwin_market_preflight import validate_match
    rebuilt=validate_match(origin,source.get('catalog_observed',[]),now,0,max_age_minutes)
    if rebuilt.get('status')!='READY_FOR_SCRAPER':
        raise ValueError('TEMPORAL_IDENTITY_SOURCE_REVALIDATION_FAILED: '+','.join(rebuilt.get('reasons') or []))
    rebuilt_evidence=rebuilt.get('time_evidence') or {}
    for key in ('status','source','observed_at_utc','difference_minutes','catalog_text'):
        if str(evidence.get(key))!=str(rebuilt_evidence.get(key)):
            raise ValueError('TEMPORAL_IDENTITY_EVIDENCE_CONTRADICTION: '+key)
    bwin_value=(evidence.get('bwin_visible_kickoff_madrid') or
                evidence.get('bwin_inferred_kickoff_madrid') or
                origin.get('inspection',{}).get('event_start_machine_readable'))
    api_value=evidence.get('api_kickoff_madrid') or row.get('kickoff_madrid')
    bwin_kickoff=dt(bwin_value,'Bwin kickoff evidence')
    api_kickoff=dt(api_value,'API kickoff evidence')
    difference=abs((bwin_kickoff-api_kickoff).total_seconds())/60
    tolerance=4 if evidence.get('status')=='COUNTDOWN_TIME_CORROBORATED' else 3
    if difference>tolerance or api_kickoff.astimezone(timezone.utc)!=ctx['kickoff'].astimezone(timezone.utc):
        raise ValueError(f'TEMPORAL_IDENTITY_KICKOFF_CONFLICT: {difference:.1f} min')
    expected_contract={
        'evidence_status':evidence.get('status'),'source':evidence.get('source'),
        'observed_at_utc':evidence.get('observed_at_utc'),
        'bwin_kickoff_madrid':bwin_kickoff.astimezone(TZ).isoformat(),
        'api_kickoff_madrid':api_kickoff.astimezone(TZ).isoformat(),
        'difference_minutes':evidence.get('difference_minutes'),
        'catalog_text':evidence.get('catalog_text'),
    }
    for key,value in expected_contract.items():
        if str(contract.get(key))!=str(value):
            raise ValueError('TEMPORAL_IDENTITY_CONTRACT_SOURCE_MISMATCH: '+key)
    return {**contract,'kickoff_utc':bwin_kickoff.astimezone(timezone.utc).isoformat(),
            'verified_at_utc':now.astimezone(timezone.utc).isoformat(),
            'preflight_path':str(preflight.resolve()),'discovery_path':str(discovery.resolve())}


def update_state(ctx,step,result,details=None):
    p=state_path(ctx)
    o=read_json(p) if p.is_file() else {}
    if o and (str(o.get('bwin_event_id'))!=ctx['event_id'] or int(o.get('fixture_id') or 0)!=ctx['fixture_id']):
        raise ValueError('single_execution.json pertenece a otro fixture')
    if o and (o.get('selected_sha256')!=ctx['selected_sha'] or o.get('decision_sha256')!=ctx['decision_sha']):
        raise ValueError('Los archivos de selección cambiaron desde la preparación; requiere auditoría')
    o.update(schema_version=VERSION,fixture_id=ctx['fixture_id'],bwin_event_id=ctx['event_id'],
             bwin_url=ctx['url'],kickoff_madrid=ctx['kickoff'].astimezone(TZ).isoformat(),
             selected_path=str(ctx['selected_path']),selected_sha256=ctx['selected_sha'],
             decision_path=str(ctx['decision_path']),decision_sha256=ctx['decision_sha'])
    o.setdefault('steps',{})[step]={'time_utc':iso_utc(),'result':result,**(details or {})}
    atomic_json(p,o)


def backup(path, directory):
    if not path.is_file():return None
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/(path.stem+'.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.'+sha(path)[:12]+path.suffix)
    shutil.copy2(path,target)
    if sha(path)!=sha(target):raise ValueError('Falló respaldo SHA de archivo anterior')
    return target


def run(cmd,root,env=None,log=None):
    print('EJECUTANDO:', ' '.join(cmd),flush=True)
    if log is None:
        return subprocess.run(cmd,cwd=root,env=env,check=False).returncode
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w',encoding='utf-8') as fh:
        proc=subprocess.run(cmd,cwd=root,env=env,check=False,stdout=fh,stderr=subprocess.STDOUT)
    return proc.returncode


def cmd_prepare(ctx,args):
    fresh,sel_age=freshness(ctx,min_lead=args.min_lead_minutes,max_selection_age=args.max_selection_age_minutes)
    multi=ensure_root(ctx,args.multi)
    if args.reuse_odds:validate_reuse_odds(ctx,args.max_odds_age_minutes)
    mpath=ctx['directory']/'manifest.json'
    if mpath.exists() and not args.force_prepare:
        raise ValueError('Ya existe manifest.json: usa status/finish. Si deseas rehacer API y cuotas, usa --force-prepare explícitamente.')
    # Preservar la captura del preflight anterior en archivo de auditoría.
    odds=ctx['directory']/'inputs'/f'cuotas_bwin_{ctx["event_id"]}.txt'
    old_backup=backup(odds,ctx['directory']/'archives'/'before_prepare')
    if mpath.exists():backup(mpath,ctx['directory']/'archives'/'before_prepare')
    if args.force_prepare:
        archive=ctx['directory']/'archives'/('force_prepare_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        for folder in ('inputs','validation','probabilistic','analysis'):
            source=ctx['directory']/folder
            if source.is_dir():shutil.copytree(source,archive/folder)
        for name in ('single_execution.json','release_state.json'):
            source=ctx['directory']/name
            if source.is_file():
                archive.mkdir(parents=True,exist_ok=True)
                shutil.move(str(source),str(archive/name))
    cmd=[sys.executable,str(multi),'--root',str(ctx['root']),'prepare','--url',ctx['url'],
         '--workers','1','--api-minute',str(args.api_minute),'--api-second',str(args.api_second)]
    if args.reuse_odds:cmd+=['--reuse-odds-selected',str(ctx['selected_path'])]
    env=os.environ.copy()
    env['PREMATCH_TEMPORAL_IDENTITY_SELECTED']=str(ctx['selected_path'])
    validate_temporal_identity_evidence(ctx)
    code=run(cmd,ctx['root'],env=env)
    if code:raise ValueError(f'prepare devolvió código {code}; revisa outputs/matches/{ctx["event_id"]}/logs/')
    manifest=match_manifest(ctx)
    if manifest.get('status')!='PREPARADO':raise ValueError(f'prepare no terminó PREPARADO: {manifest.get("status")}')
    manifest['research_selection']={'selection_tier':ctx['selection_tier'],
        'research_eligible':True,'eligible':ctx['selection'].get('eligible',True),
        'research_missing_evidence':ctx['selection'].get('research_missing_evidence',[]),
        'hard_block_reasons':ctx['selection'].get('hard_block_reasons',[]),
        'selected_sha256':ctx['selected_sha'],'decision_sha256':ctx['decision_sha'],
        'actionable_bet':False}
    atomic_json(mpath,manifest)
    manifest=match_manifest(ctx)
    oddspath=Path(manifest['inputs']['odds']['path'])
    snap=snapshot_info(oddspath,ctx,max_age=args.max_odds_age_minutes)
    update_state(ctx,'prepare','OK',{'old_odds_archived':str(old_backup) if old_backup else None,
                                     'manifest_sha256':sha(mpath),'odds':snap})
    label='cuotas seleccionadas reutilizadas por SHA' if args.reuse_odds else 'cuotas recién capturadas'
    print(f'PREPARADO {ctx["event_id"]}: fixture API {ctx["fixture_id"]}; {label}; contexto fijado por SHA.')
    print(f'SIGUIENTE: informe cualitativo para el evento {ctx["event_id"]}; luego finish --report ...')
    return 0


def cmd_verify_temporal_identity(ctx,args):
    """Promote an already acquired provisional context without API or scraping."""
    ensure_prematch(ctx,min_lead=args.min_refresh_lead_minutes)
    evidence=validate_temporal_identity_evidence(ctx,max_age_minutes=180)
    manifest=match_manifest(ctx)
    if manifest.get('status') not in {'PREPARADO_PROVISIONAL','PREPARADO'}:
        raise ValueError(f'verify-temporal-identity no procede con manifest={manifest.get("status")}')
    inputs=manifest.get('inputs') or {}
    txt=Path((inputs.get('context') or {}).get('path') or '')
    js=Path((inputs.get('context_json') or {}).get('path') or '')
    identity=Path((inputs.get('identity') or {}).get('path') or '')
    payload=read_json(js)
    match=payload.get('match') or {};fixture=payload.get('fixture') or {}
    if (str(match.get('event_id'))!=ctx['event_id'] or match.get('url')!=ctx['url'] or
            int(fixture.get('fixture_id') or 0)!=ctx['fixture_id']):
        raise ValueError('TEMPORAL_IDENTITY_CONTEXT_MISMATCH')
    fixture_kickoff=dt(fixture.get('date'),'context.fixture.date')
    if fixture_kickoff.astimezone(timezone.utc).isoformat()!=evidence['kickoff_utc']:
        raise ValueError('TEMPORAL_IDENTITY_CONTEXT_KICKOFF_CONFLICT')
    if match.get('identity_status') not in {'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN',
                                            'FIXTURE_AND_BWIN_TIME_VERIFIED'}:
        raise ValueError('TEMPORAL_IDENTITY_CONTEXT_STATUS_INVALID')
    if match.get('home_name_raw')!=ctx['home']:
        raise ValueError('TEMPORAL_IDENTITY_CONTEXT_HOME_MISMATCH')
    existing_evidence=match.get('temporal_identity_evidence') or {}
    immutable_evidence=('schema_version','status','fixture_id','bwin_event_id','bwin_url',
                        'home','away','timezone','evidence_status','source','observed_at_utc',
                        'bwin_kickoff_madrid','api_kickoff_madrid','difference_minutes',
                        'catalog_text','discovery_sha256','preflight_sha256','kickoff_utc')
    if (match.get('identity_status')=='FIXTURE_AND_BWIN_TIME_VERIFIED' and
            all(existing_evidence.get(k)==evidence.get(k) for k in immutable_evidence) and
            manifest.get('identity_status')=='FIXTURE_AND_BWIN_TIME_VERIFIED' and
            manifest.get('status')=='PREPARADO'):
        print(f'IDENTIDAD TEMPORAL YA VERIFICADA: Bwin {ctx["event_id"]} / fixture {ctx["fixture_id"]}; sin cambios.')
        return 0
    payload['match']['event_date']=evidence['bwin_kickoff_madrid']
    payload['match']['identity_status']='FIXTURE_AND_BWIN_TIME_VERIFIED'
    payload['match']['temporal_identity_evidence']=evidence
    old='API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN';new='FIXTURE_AND_BWIN_TIME_VERIFIED'
    text=txt.read_text(encoding='utf-8-sig')
    if old in text:
        if text.count(old)!=3:
            raise ValueError('TEMPORAL_IDENTITY_CONTEXT_TEXT_UNEXPECTED')
        text=text.replace(old,new)
    elif text.count(new)!=3:
        raise ValueError('TEMPORAL_IDENTITY_CONTEXT_TEXT_STATUS_MISSING')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive=ctx['directory']/'archives'/('temporal_identity_'+stamp)
    for path in (txt,js,identity,ctx['directory']/'manifest.json'):
        backup(path,archive)
    tmp_json=js.with_name('.'+js.name+'.temporal.tmp')
    tmp_txt=txt.with_name('.'+txt.name+'.temporal.tmp')
    tmp_identity=identity.with_name('.'+identity.name+'.temporal.tmp')
    try:
        tmp_json.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp_txt.write_text(text,encoding='utf-8')
        from match_identity import write_identity,validate_identity
        write_identity(tmp_json,tmp_identity)
        ident=read_json(tmp_identity)
        validate_identity(ident,payload,ctx['event_id'],ctx['url'])
        tmp_json.replace(js);tmp_txt.replace(txt);tmp_identity.replace(identity)
    finally:
        for path in (tmp_json,tmp_txt,tmp_identity):path.unlink(missing_ok=True)
    inputs['context']['sha256']=sha(txt)
    inputs['context_json']['sha256']=sha(js)
    inputs['identity']['sha256']=sha(identity)
    manifest['identity_status']='FIXTURE_AND_BWIN_TIME_VERIFIED'
    manifest['status']='PREPARADO'
    manifest['temporal_identity_evidence']=evidence
    manifest['temporal_identity_verified_at_utc']=iso_utc()
    previous_archive=manifest.get('temporal_identity_archive')
    if previous_archive:
        manifest.setdefault('temporal_identity_revalidation_archives',[]).append(str(archive.resolve()))
    else:
        manifest['temporal_identity_archive']=str(archive.resolve())
    manifest['warnings']=[w for w in manifest.get('warnings',[]) if 'BWIN_KICKOFF_UNVERIFIED' not in w]
    atomic_json(ctx['directory']/'manifest.json',manifest)
    match_manifest(ctx)
    update_state(ctx,'verify-temporal-identity','OK',{
        'manifest_sha256':sha(ctx['directory']/'manifest.json'),
        'preflight_sha256':evidence['preflight_sha256'],
        'discovery_sha256':evidence['discovery_sha256']})
    print(f'IDENTIDAD TEMPORAL VERIFICADA: Bwin {ctx["event_id"]} / fixture {ctx["fixture_id"]}; sin API ni scraper.')
    return 0


def odds_changes(before, after, event):
    """Compare exact contracts; absence in a capture is not suspension."""
    from prematch_opportunity_scanner import parse_bwin_txt
    old, _ = parse_bwin_txt(before, event)
    new, _ = parse_bwin_txt(after, event)
    a = {(r['market'], r['selection']): r['odd'] for r in old}
    b = {(r['market'], r['selection']): r['odd'] for r in new}
    def row(key): return {'market': key[0], 'selection': key[1]}
    return {'added': [row(k) for k in sorted(b.keys()-a.keys())],
            'not_observed_in_new_capture': [row(k) for k in sorted(a.keys()-b.keys())],
            'price_changes': [{**row(k), 'before': a[k], 'after': b[k]}
                              for k in sorted(a.keys() & b.keys()) if a[k] != b[k]],
            'suspension_status': 'NOT_INFERRED_FROM_ABSENCE',
            'affected_modules': ['market_mapping', 'price_evaluation', 'final_validation'],
            'reusable_modules': ['historical_statistics'],
            'reevaluation_status': 'PENDING_EXPLICIT_VALIDATION_NO_AUTOMATIC_PICKS'}


def cmd_refresh(ctx,args):
    ensure_prematch(ctx,min_lead=args.min_refresh_lead_minutes)
    ensure_root(ctx,args.multi)
    m=match_manifest(ctx)
    if m.get('status') not in {'PREPARADO','VALIDADO','FINALIZADO','BLOQUEADO_VALIDACION','ERROR_VALIDACION','ERROR_MOTOR'}:
        raise ValueError(f'refresh-odds no procede con estado {m.get("status")}')
    ref=m.get('inputs',{}).get('odds') or {}
    path=Path(ref.get('path') or '')
    expected=ctx['directory']/'inputs'/f'cuotas_bwin_{ctx["event_id"]}.txt'
    if path.resolve()!=expected.resolve():raise ValueError('Ruta de snapshot distinta de carpeta Event ID')
    previous=snapshot_info(path,ctx)
    archive=ctx['directory']/'archives'/('refresh_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    preserved=backup(path,archive)
    backup(ctx['directory']/'manifest.json',archive)
    for folder in ('validation','probabilistic','analysis','shadow'):
        source=ctx['directory']/folder
        if source.is_dir():shutil.copytree(source,archive/folder)
    for name in (f'CODEX_{ctx["event_id"]}.md','analysis_input_binding.json','final_review.json'):
        backup(ctx['directory']/name,archive)
    env=os.environ.copy()
    env['PREMATCH_BATCH_MODE']='1'
    env['PREMATCH_EVENT_ID']=ctx['event_id']
    env['PREMATCH_EVENT_DATA_DIR']=str(expected.parent.resolve())
    log=ctx['directory']/'logs'/'single_refresh_scraper.log'
    cmd=[sys.executable,'-u','scraper_bwin.py','--url',ctx['url']]
    try:
        code=run(cmd,ctx['root'],env=env,log=log)
        if code:raise ValueError(f'El scraper devolvió {code}; log: {log}')
        log_text=log.read_text(encoding='utf-8',errors='replace')
        declared=re.findall(r'(?m)^TXT:\s*(.+?\.txt)\s*$',log_text)
        if not declared:raise ValueError('Scraper no declaró TXT en el log; no se acepta archivo residual')
        origin=Path(declared[-1].strip())
        if not origin.is_absolute():origin=ctx['root']/origin
        if origin.resolve()!=expected.resolve():
            raise ValueError(f'El scraper escribió en {origin} pero se esperaba {expected}')
        snap=snapshot_info(path,ctx,max_age=args.max_odds_age_minutes)
        for kind in ('context','context_json','identity'):
            entry=(m.get('inputs') or {}).get(kind)
            if entry:
                file=Path(entry['path'])
                if not file.is_file() or sha(file)!=entry['sha256']:
                    raise ValueError(f'{kind} cambió durante el refresco de cuotas')
        if dt(snap['capture'],'capture')<=dt(previous['capture'],'old_capture'):
            raise ValueError('La captura no es posterior a la anterior: no hay refresco verificable')
        delta = odds_changes(preserved, path, ctx['event_id'])
        atomic_json(archive/'odds_delta.json', delta)
        # No se modifica la identidad, el contexto API ni sus hashes.
        m['inputs']['odds']={'path':str(path.resolve()),'sha256':snap['sha256'],
                             'generated_from':str(path.resolve()),'refreshed_at_utc':iso_utc()}
        m.setdefault('steps',{})['scraper_refresh']=0
        m['odds_refresh_at_utc']=iso_utc()
        m['odds_delta']={'path':str((archive/'odds_delta.json').resolve()),
                         'sha256':sha(archive/'odds_delta.json')}
        m['invalidated_by_odds_refresh']={'previous_status':m.get('status'),'previous_sha256':previous['sha256'],
                                          'invalidated_at_utc':iso_utc(),'stages':['validation','probabilistic','shadow','codex_analysis','final_review']}
        m['status']='PREPARADO'
        m['next_action']='finalize'
        for step in ('validacion', 'motor'):
            m.get('steps', {}).pop(step, None)
        m.pop('codex_prompt',None)
        m.pop('validation_state',None)
        m.pop('validation_artifact',None)
        m.pop('shadow_evaluation',None)
        # Invalidate stale Shadow summary and Codex handoff (already archived above)
        stale_shadow = ctx['directory'] / 'shadow' / f'shadow_summary_{ctx["event_id"]}.json'
        if stale_shadow.is_file():
            stale_shadow.unlink()
        stale_codex = ctx['directory'] / f'CODEX_{ctx["event_id"]}.md'
        if stale_codex.is_file():
            stale_codex.unlink()
        atomic_json(ctx['directory']/'manifest.json',m)
        reeval = {'executed': False, 'reason': 'NO_CONTEXT_JSON'}
        has_context_json = bool((m.get('inputs') or {}).get('context_json'))
        if has_context_json:
            try:
                from bet_engine.orchestration import run_shadow_for_event_dir
                sh_res = run_shadow_for_event_dir(
                    ctx['root'],
                    ctx['event_id'],
                    historical=False,
                    now_utc=iso_utc(),
                    max_odds_age_minutes=args.max_odds_age_minutes,
                    min_lead_minutes=max(REFRESH_MIN_LEAD_MINUTES, int(args.min_refresh_lead_minutes)),
                )
                reeval = {'executed': bool(sh_res.get('ok')), 'shadow': sh_res}
            except Exception as exc:
                reeval = {'executed': False, 'error': str(exc)}
        if not has_context_json:
            shadow_sync_state = 'SHADOW_PENDING_NO_CONTEXT'
            step_result = 'ODDS_UPDATED_SHADOW_PENDING'
            msg = (
                f'Cuotas actualizadas para {ctx["event_id"]}; contexto API NO repetido; '
                f'Shadow pendiente (sin context_json); pendiente de finalize.'
            )
        elif reeval.get('executed') and (reeval.get('shadow') or {}).get('ok'):
            shadow_sync_state = 'SHADOW_UPDATED'
            step_result = 'ODDS_UPDATED_SHADOW_UPDATED'
            msg = (
                f'Cuotas actualizadas para {ctx["event_id"]}; contexto API NO repetido; '
                f'SHA manifest y Shadow reevaluados; pendiente de finalize.'
            )
        else:
            shadow_sync_state = 'SHADOW_FAILED'
            step_result = 'ODDS_UPDATED_SHADOW_FAILED'
            err_detail = reeval.get('error') or (reeval.get('shadow') or {}).get('error') or 'UNKNOWN'
            msg = (
                f'[WARN] Cuotas actualizadas para {ctx["event_id"]}, pero Shadow falló '
                f'({shadow_sync_state}: {err_detail}); sin resultados Shadow vigentes; pendiente de finalize.'
            )
        m['shadow_sync_state'] = shadow_sync_state
        atomic_json(ctx['directory']/'manifest.json',m)
        update_state(ctx,'refresh-odds',step_result,{'odds':snap,'previous_sha256':previous['sha256'],
                                                     'archived_previous':str(preserved),'log':str(log),
                                                     'shadow_sync_state':shadow_sync_state,
                                                     'reevaluation':reeval})
        print(msg)
        return 0
    except Exception:
        # Restaurar snapshot previo, excepto si ya se actualizó manifiesto.
        # Si fallara la actualización del estado posterior, mantener consistencia con el manifest.
        current=read_json(ctx['directory']/'manifest.json')
        same_hash=(current.get('inputs',{}).get('odds') or {}).get('sha256')==ref.get('sha256')
        if same_hash and preserved and preserved.is_file():
            shutil.copy2(preserved,path)
            if sha(path)!=previous['sha256']:
                raise RuntimeError('ERROR CRÍTICO restaurando captura anterior')
        raise


def cmd_acquire_lineups(ctx, args):
    """Target-only fresh API acquisition, then the existing offline delta path."""
    from bet_engine.context.lineup_manager import evaluate_fixture_lineup_state, extract_fixture_team_identity

    cutoff_min = int(getattr(args, 'lineup_cutoff_minutes', 15))
    ensure_prematch(ctx, min_lead=args.min_refresh_lead_minutes)
    manifest = match_manifest(ctx)
    if not all(k in manifest.get('inputs', {}) for k in ('context', 'context_json', 'identity')):
        raise ValueError('LINEUPS_REQUIRE_PREPARED_CONTEXT')
    from generate_context import APIFootballClient, API_KEY, BwinMatch, resolve_selected_fixture
    from prematch_cache_record import store_acquisition
    if not API_KEY: raise ValueError('API_CREDENTIALS_MISSING')
    api = APIFootballClient(API_KEY)
    match = BwinMatch(url=ctx['url'], event_id=int(ctx['event_id']),
        home_name_raw=ctx['home'], away_name_raw=ctx['away'],
        home_name=ctx['home'], away_name=ctx['away'], home_gender='UNKNOWN', away_gender='UNKNOWN',
        event_date=ctx['kickoff'].isoformat())
    resolve_selected_fixture(match, api, ctx)
    payload = api.get('fixtures/lineups', {'fixture': ctx['fixture_id']}, use_cache=False)
    snapshot = store_acquisition(ctx['directory']/'context_acquisitions', payload)
    now = local_now()
    ensure_prematch(ctx, min_lead=args.min_refresh_lead_minutes)
    if payload.get('errors'): raise ValueError('LINEUPS_PROVIDER_ERROR: snapshot preserved')

    ctx_json_path = Path((manifest.get('inputs') or {}).get('context_json', {}).get('path') or '')
    ctx_doc = read_json(ctx_json_path) if ctx_json_path.is_file() else {}
    id_path = Path((manifest.get('inputs') or {}).get('identity', {}).get('path') or '')
    id_doc = read_json(id_path) if id_path.is_file() else {}
    team_ident = extract_fixture_team_identity(ctx_doc, identity_doc=id_doc, selection_doc=ctx.get('selection'))
    home_id = team_ident.get('home_id') if team_ident.get('home_id') is not None else ctx['selection'].get('home_api_id')
    away_id = team_ident.get('away_id') if team_ident.get('away_id') is not None else ctx['selection'].get('away_api_id')
    resp_list = payload.get('response') or []
    qual_md = None
    for qk in ('qualitative_research', 'report'):
        qref = (manifest.get('inputs') or {}).get(qk) or {}
        if qref.get('path') and Path(qref['path']).is_file():
            qual_md = Path(qref['path']).read_text(encoding='utf-8-sig', errors='replace')
            break

    lu_eval = evaluate_fixture_lineup_state(
        api_lineups_response=resp_list,
        qualitative_markdown=qual_md,
        fixture_id=int(ctx['fixture_id']),
        event_id=str(ctx['event_id']),
        home_id=home_id,
        away_id=away_id,
        home_name=str(team_ident.get('home_name') or ctx['home']),
        away_name=str(team_ident.get('away_name') or ctx['away']),
        home_aliases=team_ident.get('home_aliases'),
        away_aliases=team_ident.get('away_aliases'),
        is_national_team=bool(team_ident.get('is_national_team')),
        kickoff_utc=ctx['kickoff'],
        now_utc=now,
        lineup_cutoff_minutes=cutoff_min,
        api_acquired_at_utc=payload.get('_acquired_at_utc') or now.astimezone(timezone.utc),
        last_verified_state=manifest.get('lineup_state'),
    )
    if not lu_eval['confirmed_complete_22']:
        from bet_engine.context.lineup_manager import prepare_manual_lineup_inbox

        inbox_info = prepare_manual_lineup_inbox(
            ctx['directory'],
            event_id=str(ctx['event_id']),
            fixture_id=int(ctx['fixture_id']),
            home_name=ctx['home'],
            away_name=ctx['away'],
            home_id=home_id,
            away_id=away_id,
            kickoff_iso=ctx['kickoff'].isoformat(),
        )
        manifest['lineup_state'] = {
            'status': lu_eval['status'],
            'confirmed_complete_22': False,
            'confirmed_sides': lu_eval.get('confirmed_sides'),
            'starter_counts': lu_eval.get('starter_counts'),
            'lineup_cutoff_minutes': cutoff_min,
            'minutes_to_kickoff': lu_eval.get('minutes_to_kickoff'),
            'reason': lu_eval.get('reason'),
            'absence_interpretation': 'NOT_INFERRED_AS_INJURY_OR_SUSPENSION',
            'manual_inbox': inbox_info,
            'snapshot': str(snapshot),
            'sha256': sha(snapshot),
        }
        atomic_json(ctx['directory'] / 'manifest.json', manifest)
        audit_path = ctx['directory'] / 'lineups' / f'lineup_audit_{ctx["event_id"]}.json'
        atomic_json(audit_path, {
            'event_id': str(ctx['event_id']),
            'fixture_id': int(ctx['fixture_id']),
            'checked_at_utc': now.astimezone(timezone.utc).isoformat(),
            'lineup_evaluation': lu_eval,
            'manual_inbox': inbox_info,
            'snapshot': str(snapshot),
            'sha256': sha(snapshot),
            'context_unchanged': True,
        })
        update_state(ctx, 'acquire-lineups', lu_eval['status'],
                     {'snapshot': str(snapshot), 'sha256': sha(snapshot), 'context_unchanged': True, 'manual_inbox': inbox_info['manual_dir']})
        print(f'XI no confirmado completo ({lu_eval["status"]}: {lu_eval.get("reason")}); adquisición conservada y bandeja manual lista en {inbox_info["manual_dir"]}.')
        return 0
    args.snapshot = snapshot; args.snapshot_sha256 = sha(snapshot)
    return cmd_refresh_context(ctx, args)


def cmd_refresh_context(ctx, args):
    ensure_prematch(ctx, min_lead=args.min_refresh_lead_minutes)
    manifest = match_manifest(ctx)
    from prematch_context_delta import refresh_lineups
    now = local_now()
    updated, out = refresh_lineups(ctx, manifest, args.snapshot, args.snapshot_sha256, now)
    # Preserve every prior downstream artifact before any later finish rewrites it.
    for folder in ('validation', 'probabilistic', 'analysis', 'shadow'):
        source = ctx['directory'] / folder
        if source.is_dir():
            shutil.copytree(source, out / 'previous_outputs' / folder)
    for name in (f'CODEX_{ctx["event_id"]}.md', 'analysis_input_binding.json', 'final_review.json'):
        backup(ctx['directory'] / name, out / 'previous_outputs')
    for stale_path in (
        ctx['directory'] / 'shadow' / f'shadow_summary_{ctx["event_id"]}.json',
        ctx['directory'] / f'CODEX_{ctx["event_id"]}.md',
    ):
        if stale_path.is_file():
            stale_path.unlink()
    atomic_json(ctx['directory'] / 'manifest.json', updated)

    # Req 11: Re-ejecutar automáticamente las evaluaciones afectadas (Shadow sobre contexto actualizado)
    reeval = {'executed': False}
    try:
        from bet_engine.orchestration import run_shadow_for_event_dir
        cutoff_min = int(getattr(args, 'lineup_cutoff_minutes', 15))
        sh_res = run_shadow_for_event_dir(
            ctx['root'],
            ctx['event_id'],
            historical=False,
            now_utc=now.astimezone(timezone.utc).isoformat(),
            min_lead_minutes=max(REFRESH_MIN_LEAD_MINUTES, int(args.min_refresh_lead_minutes)),
            lineup_cutoff_minutes=cutoff_min,
        )
        reeval = {'executed': bool(sh_res.get('ok')), 'shadow': sh_res}
    except Exception as exc:
        reeval = {'executed': False, 'error': str(exc)}

    shadow_ok = bool(reeval.get('executed') and (reeval.get('shadow') or {}).get('ok'))
    shadow_sync_state = 'SHADOW_UPDATED' if shadow_ok else 'SHADOW_FAILED'
    step_result = 'CONTEXT_UPDATED_SHADOW_UPDATED' if shadow_ok else 'CONTEXT_UPDATED_SHADOW_FAILED'
    updated['shadow_sync_state'] = shadow_sync_state
    atomic_json(ctx['directory'] / 'manifest.json', updated)

    audit_path = ctx['directory'] / 'lineups' / f'lineup_audit_{ctx["event_id"]}.json'
    atomic_json(audit_path, {
        'event_id': str(ctx['event_id']),
        'fixture_id': int(ctx['fixture_id']),
        'refreshed_at_utc': now.astimezone(timezone.utc).isoformat(),
        'lineup_state': updated.get('lineup_state'),
        'context_update_dir': str(out),
        'shadow_sync_state': shadow_sync_state,
        'reevaluation': reeval,
        'original_inputs_untouched': True,
    })
    update_state(ctx, 'refresh-context', step_result, {'run': str(out), 'shadow_sync_state': shadow_sync_state, 'reevaluation': reeval})
    if shadow_ok:
        print(f'Contexto diferencial local incorporado: {out}; evaluación Shadow reejecutada y finish pendiente.')
    else:
        err_detail = reeval.get('error') or (reeval.get('shadow') or {}).get('error') or 'UNKNOWN'
        print(f'[WARN] Contexto diferencial incorporado: {out}, pero la reevaluación Shadow falló ({shadow_sync_state}: {err_detail}); sin resultados Shadow vigentes; finish pendiente.')
    return 0


def cmd_retry(ctx,args,kind):
    ensure_prematch(ctx,min_lead=args.min_refresh_lead_minutes)
    multi=ensure_root(ctx,args.multi)
    manifest=match_manifest(ctx)
    permitted={'retry-context':{'ERROR_CONTEXTO','ERROR_ARCHIVOS','PREPARADO_PROVISIONAL'},
               'retry-scraper':{'PARCIAL_SIN_CUOTAS','ERROR_ARCHIVOS'}}
    if manifest.get('status') not in permitted[kind]:
        raise ValueError(f'{kind} no procede con manifest={manifest.get("status")}')
    cmd=[sys.executable,str(multi),'--root',str(ctx['root']),kind,'--event-id',ctx['event_id'],'--workers','1']
    if kind=='retry-context':
        cmd.extend(['--api-minute',str(args.api_minute),'--api-second',str(args.api_second)])
        cmd.extend(['--temporal-identity-selected',str(ctx['selected_path'])])
        if args.force_api:cmd.append('--force')
    code=run(cmd,ctx['root'])
    if code:raise ValueError(f'{kind} falló, código {code}; revisar logs del mismo evento')
    m=match_manifest(ctx)
    if m.get('status') not in {'PREPARADO', 'PREPARADO_PROVISIONAL'}:
        raise ValueError(f'Estado tras {kind}: {m.get("status")}')
    update_state(ctx,kind,'OK',{'manifest_sha256':sha(ctx['directory']/'manifest.json')})
    print(f'{kind} correcto para {ctx["event_id"]}; evento único conservado.')
    return 0


def cmd_finish(ctx,args):
    remain=(ctx['kickoff']-local_now()).total_seconds()/60
    if args.historical:
        if remain>0:raise ValueError('Auditoría histórica solo disponible después del kickoff')
    else:
        ensure_prematch(ctx,min_lead=args.min_finish_lead_minutes)
    multi=ensure_root(ctx,args.multi)
    manifest=match_manifest(ctx)
    allowed_statuses = {'PREPARADO','VALIDADO','FINALIZADO','BLOQUEADO_VALIDACION','ERROR_VALIDACION','ERROR_MOTOR'}
    if manifest.get('status') not in allowed_statuses:
        raise ValueError(f'No procede finish: manifest={manifest.get("status")}')
    prelim_archive = None
    is_preliminary_resume = False
    if manifest.get('status') == 'FINALIZADO' and not args.historical:
        from prematch_case import _evaluate_closure
        case_file = ctx['directory'] / 'case.json'
        case_like = read_json(case_file) if case_file.is_file() else {'event_id': ctx['event_id'], 'kickoff_madrid': ctx['kickoff'].isoformat()}
        closure = _evaluate_closure(ctx['directory'], case_like, manifest)
        if closure.get('final_prematch_valid'):
            raise ValueError('FINAL_PREMATCH_CLOSURE_ALREADY_VALID: ya existe un cierre final PREMATCH válido; no se duplica ni sobrescribe')
        is_preliminary_resume = True
    odds=manifest.get('inputs',{}).get('odds') or {}
    if not odds:raise ValueError('Sin cuotas en manifest: no se autoriza la fase final')
    # Las cuotas deben ser recientes, incluso si el scraping acabó correctamente.
    if not args.historical:
        snap=snapshot_info(Path(odds['path']),ctx,max_age=min(15,args.max_odds_age_minutes))
        from prematch_daily import final_odds_check
        final_odds_check({'kickoff_madrid':ctx['kickoff'].isoformat(),'capture_madrid':snap['capture']},local_now())
    report=resolve_qualitative_report_path(ctx['root'], ctx['directory'], ctx['event_id'], args.report)
    if not report.is_file() or not report.stat().st_size:raise ValueError(f'Informe cualitativo ausente: {report}')
    report_head=report.read_text(encoding='utf-8-sig',errors='replace')[:12000]
    if 'STATUS: PENDIENTE_MANUAL' in report_head:raise ValueError('Informe cualitativo es una plantilla pendiente, no evidencia')
    if (ctx['directory']/'release_state.json').is_file() and not args.historical:
        from prematch_release import validate_external_inputs
        validate_external_inputs(ctx,report)
    labels=re.findall(r'(?im)\b(?:event[_ ]id(?: bwin)?|evento bwin)\s*[:|]\s*(\d{6,})\b',report_head)
    if any(x!=ctx['event_id'] for x in labels):raise ValueError('Informe cualitativo menciona otro Event ID')
    if is_preliminary_resume:
        prelim_archive = ctx['directory']/'archives'/('preliminary_closure_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        backup(ctx['directory']/'manifest.json', prelim_archive)
        backup(state_path(ctx), prelim_archive)
        for folder in ('validation','probabilistic','analysis','shadow','dossier'):
            source = ctx['directory']/folder
            if source.is_dir():
                shutil.copytree(source, prelim_archive/folder)
        for name in (f'CODEX_{ctx["event_id"]}.md','analysis_input_binding.json','final_review.json'):
            backup(ctx['directory']/name, prelim_archive)
    cutoff_min = int(getattr(args, 'lineup_cutoff_minutes', 15))
    if not args.historical and getattr(args, 'require_confirmed_lineups', False):
        from prematch_case import _evaluate_closure
        case_file = ctx['directory'] / 'case.json'
        case_like = read_json(case_file) if case_file.is_file() else {'event_id': ctx['event_id'], 'kickoff_madrid': ctx['kickoff'].isoformat()}
        closure = _evaluate_closure(ctx['directory'], case_like, manifest, lineup_cutoff_minutes=cutoff_min)
        if not closure.get('lineups_confirmed_22'):
            raise ValueError(f'LINEUPS_NOT_CONFIRMED_22:{closure["lineups_status"]}')
    cmd=[sys.executable,str(multi),'--root',str(ctx['root']),'finish','--event-id',ctx['event_id'],
         '--report',str(report),'--workers','1','--lineup-cutoff-minutes',str(cutoff_min)]
    if args.historical:cmd.append('--historical')
    shadow_mode = getattr(args, 'shadow_mode', None)
    if shadow_mode is not None:
        cmd.append('--shadow-mode' if shadow_mode else '--no-shadow-mode')
    code=run(cmd,ctx['root'])
    if code:raise ValueError(f'finish devolvió {code}; revisa logs/validacion.log y logs/motor.log')
    m=match_manifest(ctx)
    expected='AUDITORIA_HISTORICA' if args.historical else 'FINALIZADO'
    if m.get('status')!=expected:raise ValueError(f'finish terminó {m.get("status")} y no {expected}')
    m['next_action'] = 'historical-audit-only' if args.historical else 'export-handoff'
    if prelim_archive is not None:
        m.setdefault('preliminary_closures_archived', []).append(str(prelim_archive.resolve()))
    if not args.historical and getattr(args, 'allow_unconfirmed_lineups', False):
        from prematch_case import _evaluate_closure
        case_file = ctx['directory'] / 'case.json'
        case_like = read_json(case_file) if case_file.is_file() else {'event_id': ctx['event_id'], 'kickoff_madrid': ctx['kickoff'].isoformat()}
        closure_now = _evaluate_closure(ctx['directory'], case_like, m, lineup_cutoff_minutes=cutoff_min)
        if not closure_now.get('lineups_confirmed_22'):
            m['eligible_for_actionable_pick'] = False
            m['actionable_bet'] = False
            m['closure_mode'] = 'RESEARCH_ONLY_UNCONFIRMED_LINEUPS'
            m['final_eligibility'] = 'RESEARCH_ONLY_MISSING_CONFIRMED_22_STARTERS'
    atomic_json(ctx['directory']/'manifest.json', m)
    pinned_report=(m.get('inputs') or {}).get('report') or {}
    report_actual=Path(pinned_report.get('path') or report)
    if not report_actual.is_file() or not pinned_report.get('sha256') or sha(report_actual)!=pinned_report['sha256']:
        raise ValueError('Informe anclado tras finish no coincide con manifest')
    if not args.historical and (ctx['kickoff']-local_now()).total_seconds()<=0:
        exp_archive = ctx['directory'] / 'archives' / ('expired_post_kickoff_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        exp_archive.mkdir(parents=True, exist_ok=True)
        codex_file = ctx['directory'] / f'CODEX_{ctx["event_id"]}.md'
        if codex_file.is_file():
            backup(codex_file, exp_archive)
            codex_file.unlink(missing_ok=True)
        handoff_file = ctx['directory'] / 'dossier' / f'handoff_codex_{ctx["event_id"]}.json'
        if handoff_file.is_file():
            backup(handoff_file, exp_archive)
            handoff_file.unlink(missing_ok=True)
        m['status'] = 'EXPIRADO_POST_KICKOFF'
        m['codex_prompt'] = None
        m['eligible_for_actionable_pick'] = False
        m['actionable_bet'] = False
        m['final_eligibility'] = 'EXPIRED_POST_KICKOFF_RETROSPECTIVE_ONLY'
        m['next_action'] = 'retrospective-audit-only'
        m['expired_post_kickoff_archive'] = str(exp_archive.resolve())
        atomic_json(ctx['directory'] / 'manifest.json', m)
        update_state(ctx, 'finish', 'PREMATCH_EXPIRED', {
            'report': str(report_actual),
            'report_sha256': sha(report_actual),
            'codex_prompt': None,
            'expired_archive': str(exp_archive),
        })
        update_state(ctx,'postfinish_clock','PREMATCH_EXPIRED',{'note':'Validación y motor finalizaron después del kickoff; prompt y estado accionable revocados'})
        print('ALERTA: se alcanzó el kickoff durante finish. Salida archivada como solo retrospectiva; prompt Codex revocado.')
        return 3
    update_state(ctx,'finish',expected,{'report':str(report_actual),'report_sha256':sha(report_actual),
                                        'codex_prompt':m.get('codex_prompt') if not args.historical else None,
                                        'preliminary_archive':str(prelim_archive) if prelim_archive else None})
    if not args.historical:
        print('ÚNICO prompt para Codex:',m.get('codex_prompt') or '(ver manifest.json)')
        print('Codex NO se ha ejecutado automáticamente.')
    return 0


def _reconcile_latest_lineups_for_close(ctx, args, manifest: dict, cutoff_min: int) -> dict:
    """Sincroniza XI manuales en bandeja o snapshots recientes antes de evaluar cierre."""
    from bet_engine.context.lineup_manager import scan_manual_lineup_inbox, evaluate_fixture_lineup_state, extract_fixture_team_identity
    from prematch_case import sync_manual_lineups

    inbox = scan_manual_lineup_inbox(ctx['directory'], event_id=str(ctx['event_id']))
    lu_state = manifest.get('lineup_state') or {}
    if inbox.get('json_candidates') and lu_state.get('status') != 'LINEUPS_MANUAL_VERIFIED':
        try:
            sync_manual_lineups(
                ctx['root'],
                ctx['event_id'],
                now_utc=iso_utc(),
                lineup_cutoff_minutes=cutoff_min,
                reevaluate=True,
            )
            manifest = match_manifest(ctx)
        except Exception:
            pass

    # Comprobar si existe una adquisición API de 22 titulares en context_acquisitions más reciente no aplicada
    acq_dir = ctx['directory'] / 'context_acquisitions'
    if acq_dir.is_dir() and not (manifest.get('lineup_state') or {}).get('confirmed_complete_22'):
        from prematch_cache_record import unpack_record
        inputs = manifest.get('inputs') or {}
        ctx_json_path = Path((inputs.get('context_json') or {}).get('path') or '')
        ctx_doc = read_json(ctx_json_path) if ctx_json_path.is_file() else {}
        id_path = Path((inputs.get('identity') or {}).get('path') or '')
        id_doc = read_json(id_path) if id_path.is_file() else {}
        team_ident = extract_fixture_team_identity(ctx_doc, identity_doc=id_doc, selection_doc=ctx.get('selection'))
        for acq_file in sorted(acq_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                raw_payload, acq_dt = unpack_record(read_json(acq_file))
                resp_list = raw_payload.get('response') or []
                if not resp_list:
                    continue
                lu_eval = evaluate_fixture_lineup_state(
                    api_lineups_response=resp_list,
                    fixture_id=int(ctx['fixture_id']),
                    event_id=str(ctx['event_id']),
                    home_id=team_ident.get('home_id') or ctx['selection'].get('home_api_id'),
                    away_id=team_ident.get('away_id') or ctx['selection'].get('away_api_id'),
                    home_name=str(team_ident.get('home_name') or ctx['home']),
                    away_name=str(team_ident.get('away_name') or ctx['away']),
                    kickoff_utc=ctx['kickoff'],
                    now_utc=local_now(),
                    lineup_cutoff_minutes=cutoff_min,
                    api_acquired_at_utc=acq_dt,
                )
                if lu_eval.get('confirmed_complete_22'):
                    args.snapshot = acq_file
                    args.snapshot_sha256 = sha(acq_file)
                    cmd_refresh_context(ctx, args)
                    manifest = match_manifest(ctx)
                    break
            except Exception:
                continue
    return manifest


def evaluate_close_prerequisites(ctx, args) -> dict:
    """Evalúa todos los requisitos de cierre PREMATCH antes de refrescar cuotas."""
    pending: list[dict] = []
    details: dict = {'event_id': ctx['event_id'], 'fixture_id': ctx['fixture_id']}
    now = local_now()
    remain_min = (ctx['kickoff'] - now).total_seconds() / 60.0
    details['minutes_to_kickoff'] = round(remain_min, 2)
    min_refresh_lead = max(REFRESH_MIN_LEAD_MINUTES, int(getattr(args, 'min_refresh_lead_minutes', REFRESH_MIN_LEAD_MINUTES)))
    min_finish_lead = max(FINISH_MIN_LEAD_MINUTES, int(getattr(args, 'min_finish_lead_minutes', FINISH_MIN_LEAD_MINUTES)))
    if remain_min <= 0:
        pending.append({'requirement': 'temporal_margin', 'code': 'POST_KICKOFF',
                        'detail': f'Partido ya iniciado o en kickoff (margen={remain_min:.1f} min)'})
    elif remain_min < min_refresh_lead or remain_min < min_finish_lead:
        pending.append({'requirement': 'temporal_margin', 'code': 'INSUFFICIENT_PREMATCH_MARGIN',
                        'detail': f'Margen insuficiente: {remain_min:.1f} min < {max(min_refresh_lead, min_finish_lead)} min'})

    manifest = None
    try:
        manifest = match_manifest(ctx)
        details['manifest_status'] = manifest.get('status')
        allowed_statuses = {'PREPARADO', 'VALIDADO', 'FINALIZADO', 'BLOQUEADO_VALIDACION', 'ERROR_VALIDACION', 'ERROR_MOTOR'}
        if manifest.get('status') not in allowed_statuses:
            pending.append({'requirement': 'manifest', 'code': 'INVALID_MANIFEST_STATUS',
                            'detail': f'Estado manifest no apto para cierre: {manifest.get("status")}'})
    except ValueError as exc:
        pending.append({'requirement': 'manifest', 'code': 'MANIFEST_INVALID_OR_MISSING', 'detail': str(exc)})

    ident_doc = None
    ctx_json_doc = None
    if manifest is not None:
        cutoff_min = int(getattr(args, 'lineup_cutoff_minutes', 15))
        manifest = _reconcile_latest_lineups_for_close(ctx, args, manifest, cutoff_min)
        from prematch_case import _evaluate_closure
        case_file = ctx['directory'] / 'case.json'
        case_like = read_json(case_file) if case_file.is_file() else {
            'event_id': ctx['event_id'], 'kickoff_madrid': ctx['kickoff'].isoformat()
        }
        if manifest.get('status') == 'FINALIZADO':
            closure_prev = _evaluate_closure(ctx['directory'], case_like, manifest)
            if closure_prev.get('final_prematch_valid'):
                pending.append({'requirement': 'closure', 'code': 'FINAL_PREMATCH_CLOSURE_ALREADY_VALID',
                                'detail': 'Ya existe un cierre final PREMATCH válido; no se duplica ni sobrescribe'})

        inputs = manifest.get('inputs') or {}
        ctx_txt = inputs.get('context') or {}
        ctx_json = inputs.get('context_json') or {}
        if not ctx_txt.get('path') or not Path(ctx_txt['path']).is_file() or not ctx_json.get('path') or not Path(ctx_json['path']).is_file():
            pending.append({'requirement': 'context', 'code': 'CONTEXT_MISSING',
                            'detail': 'Falta context o context_json en manifest.json'})
        else:
            try:
                ctx_json_doc = read_json(Path(ctx_json['path']))
                fx_doc = ctx_json_doc.get('fixture') or {}
                m_doc = ctx_json_doc.get('match') or {}
                if int(fx_doc.get('fixture_id') or 0) != int(ctx['fixture_id']):
                    pending.append({'requirement': 'context', 'code': 'CONTEXT_FIXTURE_ID_MISMATCH',
                                    'detail': f'fixture_id en context_json ({fx_doc.get("fixture_id")}) != {ctx["fixture_id"]}'})
                elif str(m_doc.get('event_id') or '') != str(ctx['event_id']):
                    pending.append({'requirement': 'context', 'code': 'CONTEXT_EVENT_ID_MISMATCH',
                                    'detail': f'event_id en context_json ({m_doc.get("event_id")}) != {ctx["event_id"]}'})
                else:
                    details['context_ok'] = True
            except Exception as exc:
                pending.append({'requirement': 'context', 'code': 'CONTEXT_JSON_INVALID', 'detail': str(exc)})

        ident_ref = inputs.get('identity') or {}
        ident_path = Path(ident_ref['path']) if ident_ref.get('path') else None
        if not ident_path or not ident_path.is_file():
            pending.append({'requirement': 'identity', 'code': 'IDENTITY_MISSING',
                            'detail': 'Falta match_identity.json en manifest.json'})
        elif manifest.get('identity_status') == 'API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN':
            pending.append({'requirement': 'identity', 'code': 'IDENTITY_BWIN_TIME_UNKNOWN',
                            'detail': 'Identidad con hora Bwin no verificada'})
        else:
            try:
                from match_identity import validate_identity
                ident_doc = read_json(ident_path)
                payload_for_id = ctx_json_doc if (ctx_json_doc and 'generated_at' in ctx_json_doc) else None
                validate_identity(ident_doc, payload_for_id, ctx['event_id'], ctx['url'])
                if int(ident_doc.get('api_fixture_id') or 0) != int(ctx['fixture_id']):
                    raise ValueError(f'api_fixture_id en match_identity ({ident_doc.get("api_fixture_id")}) != {ctx["fixture_id"]}')
                details['identity_ok'] = True
            except Exception as exc:
                pending.append({'requirement': 'identity', 'code': 'IDENTITY_VALIDATION_FAILED', 'detail': str(exc)})

        closure = _evaluate_closure(ctx['directory'], case_like, manifest, lineup_cutoff_minutes=cutoff_min)
        details['lineups_status'] = closure.get('lineups_status')
        details['lineups_confirmed_22'] = bool(closure.get('lineups_confirmed_22'))
        if not closure.get('lineups_confirmed_22') and not getattr(args, 'allow_unconfirmed_lineups', False):
            pending.append({'requirement': 'lineups', 'code': 'LINEUPS_NOT_CONFIRMED_22',
                            'detail': f'XI no confirmados (22 titulares requeridos): {closure.get("lineups_status")}'})

        if not inputs.get('odds'):
            pending.append({'requirement': 'odds', 'code': 'ODDS_MISSING_IN_MANIFEST',
                            'detail': 'Sin snapshot inicial de cuotas en manifest.json'})

    report = resolve_qualitative_report_path(ctx['root'], ctx['directory'], ctx['event_id'], getattr(args, 'report', None))
    details['report_path'] = str(report)
    if not report.is_file() or not report.stat().st_size:
        pending.append({'requirement': 'report', 'code': 'REPORT_MISSING',
                        'detail': f'Informe cualitativo ausente o vacío: {report}'})
    else:
        report_text = report.read_text(encoding='utf-8-sig', errors='replace')
        report_head = report_text[:12000]
        if 'STATUS: PENDIENTE_MANUAL' in report_head:
            pending.append({'requirement': 'report', 'code': 'REPORT_PLACEHOLDER',
                            'detail': 'Informe cualitativo es una plantilla pendiente (STATUS: PENDIENTE_MANUAL)'})
        labels = re.findall(r'(?im)\b(?:event[_ ]id(?:[_ ]bwin)?|evento bwin)\s*[:|=]\s*`?(\d{6,})`?\b', report_head)
        if any(x != ctx['event_id'] for x in labels):
            pending.append({'requirement': 'report', 'code': 'REPORT_EVENT_ID_CONFLICT',
                            'detail': f'Informe cualitativo menciona otro Event ID: {labels}'})
        fx_labels = re.findall(r'(?im)\b(?:fixture[_ ]id(?:[_ ]api)?|api[_ ]fixture[_ ]id)\s*[:|=]\s*`?(\d{4,})`?\b', report_head)
        if any(int(x) != int(ctx['fixture_id']) for x in fx_labels):
            pending.append({'requirement': 'report', 'code': 'REPORT_FIXTURE_ID_CONFLICT',
                            'detail': f'Informe cualitativo menciona otro Fixture ID: {fx_labels}'})
        if ident_doc and isinstance(ident_doc.get('home'), dict) and isinstance(ident_doc.get('away'), dict):
            try:
                import validate_prematch as vp
                from match_identity import compare_report_name
                rmeta = vp.report_meta(
                    report_text,
                    ident_doc['home'].get('api_name'),
                    ident_doc['away'].get('api_name'),
                    ident_doc['home'].get('bwin_name'),
                    ident_doc['away'].get('bwin_name'),
                )
                if rmeta.get('event_id_conflict'):
                    pending.append({'requirement': 'report', 'code': 'REPORT_CONFLICTING_EVENT_IDS',
                                    'detail': f'Event IDs contradictorios en informe: {rmeta.get("event_ids_found")}'})
                if rmeta.get('fixture_id_conflict'):
                    pending.append({'requirement': 'report', 'code': 'REPORT_CONFLICTING_FIXTURE_IDS',
                                    'detail': f'Fixture IDs contradictorios en informe: {rmeta.get("fixture_ids_found")}'})
                r_teams = rmeta.get('teams')
                if r_teams and len(r_teams) == 2:
                    for idx, side in enumerate(('home', 'away')):
                        other = 'away' if side == 'home' else 'home'
                        cmp_res = compare_report_name(r_teams[idx], ident_doc[side], ident_doc[other])
                        if cmp_res not in {'EXACT', 'ALIAS'}:
                            pending.append({'requirement': 'report', 'code': f'REPORT_TEAM_MISMATCH_{side.upper()}',
                                            'detail': f'Equipo {side} en informe ({r_teams[idx]!r}) no coincide con fixture ({ident_doc[side].get("api_name")!r})'})
            except Exception as exc:
                pending.append({'requirement': 'report', 'code': 'REPORT_METADATA_ERROR', 'detail': str(exc)})
        if (ctx['directory'] / 'release_state.json').is_file():
            try:
                from prematch_release import validate_external_inputs
                validate_external_inputs(ctx, report)
            except Exception as exc:
                pending.append({'requirement': 'report', 'code': 'REPORT_EXTERNAL_VALIDATION_FAILED',
                                'detail': str(exc)})

    return {'ok': not pending, 'pending_requirements': pending, 'details': details}


def cmd_close(ctx, args):
    """Coordina el cierre PREMATCH: precheck -> refresh-odds -> verificación -> finish."""
    ensure_root(ctx, args.multi)
    args.historical = False
    stage_records: dict = {}
    t_start = time.perf_counter()

    t_pre = time.perf_counter()
    precheck = evaluate_close_prerequisites(ctx, args)
    stage_records['precheck'] = {
        'elapsed_seconds': round(time.perf_counter() - t_pre, 4),
        'ok': precheck['ok'],
        'pending_requirements': precheck['pending_requirements'],
        'details': precheck['details'],
    }
    if not precheck['ok']:
        update_state(ctx, 'close', 'BLOCKED_PRECHECK', {
            'stages': stage_records,
            'pending_requirements': precheck['pending_requirements'],
            'total_elapsed_seconds': round(time.perf_counter() - t_start, 4),
        })
        print(json.dumps({
            'status': 'BLOCKED_PRECHECK',
            'event_id': ctx['event_id'],
            'fixture_id': ctx['fixture_id'],
            'pending_requirements': precheck['pending_requirements'],
            'stages': stage_records,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    t_ref = time.perf_counter()
    try:
        ref_code = cmd_refresh(ctx, args)
        if ref_code != 0:
            raise ValueError(f'refresh-odds devolvió código {ref_code}')
        m_after_refresh = match_manifest(ctx)
        odds_ref = (m_after_refresh.get('inputs') or {}).get('odds') or {}
        if not odds_ref.get('path'):
            raise ValueError('Sin cuotas en manifest tras refresh-odds')
        snap = snapshot_info(Path(odds_ref['path']), ctx, max_age=min(15, args.max_odds_age_minutes))
        remain_after_refresh = (ctx['kickoff'] - local_now()).total_seconds() / 60.0
        if remain_after_refresh <= 0 or remain_after_refresh < args.min_finish_lead_minutes:
            raise ValueError(
                f'Margen temporal insuficiente tras refresh-odds para ejecutar finish: '
                f'{remain_after_refresh:.1f} min < {args.min_finish_lead_minutes} min'
            )
        stage_records['refresh_odds'] = {
            'elapsed_seconds': round(time.perf_counter() - t_ref, 4),
            'ok': True,
            'odds_sha256': snap['sha256'],
            'odds_capture': snap['capture'],
            'odds_age_minutes': snap['age_minutes'],
            'minutes_to_kickoff_after': round(remain_after_refresh, 2),
        }
    except Exception as exc:
        stage_records['refresh_odds'] = {
            'elapsed_seconds': round(time.perf_counter() - t_ref, 4),
            'ok': False,
            'block': str(exc),
        }
        update_state(ctx, 'close', 'BLOCKED_REFRESH_ODDS', {
            'stages': stage_records,
            'error': str(exc),
            'total_elapsed_seconds': round(time.perf_counter() - t_start, 4),
        })
        print(f'BLOQUEADO en refresh-odds: {exc}', file=sys.stderr)
        return 2

    t_fin = time.perf_counter()
    try:
        fin_code = cmd_finish(ctx, args)
        if fin_code != 0:
            stage_records['finish'] = {
                'elapsed_seconds': round(time.perf_counter() - t_fin, 4),
                'ok': False,
                'returncode': fin_code,
                'block': 'POSTFINISH_KICKOFF_EXPIRED' if fin_code == 3 else f'FINISH_RETURNED_{fin_code}',
            }
            update_state(ctx, 'close', 'PREMATCH_EXPIRED' if fin_code == 3 else 'BLOCKED_FINISH', {
                'stages': stage_records,
                'total_elapsed_seconds': round(time.perf_counter() - t_start, 4),
            })
            return fin_code
        m_final = match_manifest(ctx)
        if m_final.get('status') != 'FINALIZADO':
            raise ValueError(f'Estado final inesperado tras finish: {m_final.get("status")}')
        final_odds_sha = ((m_final.get('inputs') or {}).get('odds') or {}).get('sha256')
        if final_odds_sha != stage_records['refresh_odds']['odds_sha256']:
            raise ValueError('SHA de cuotas cambió durante finish')
        # Verificar salidas reales de validador y motor si fueron generadas
        artifacts: dict = {}
        val_json_path = ctx['directory'] / 'validation' / f'validacion_{ctx["event_id"]}.json'
        if val_json_path.is_file():
            val_doc = read_json(val_json_path)
            val_blocks = [it for it in (val_doc.get('issues') or []) if it.get('severity') == 'BLOCK']
            if val_doc.get('status') == 'BLOQUEADO_PREPARTIDO' or val_blocks:
                raise ValueError(f'Validador reportó bloqueos: {[b.get("code") for b in val_blocks]}')
            artifacts['validation_json'] = {'path': str(val_json_path), 'sha256': sha(val_json_path), 'status': val_doc.get('status')}
        rank_json_path = ctx['directory'] / 'probabilistic' / f'ranking_probabilistico_{ctx["event_id"]}.json'
        if rank_json_path.is_file():
            rank_doc = read_json(rank_json_path)
            if str(rank_doc.get('event_id') or '') != str(ctx['event_id']):
                raise ValueError(f'Motor generó ranking para otro Event ID: {rank_doc.get("event_id")}')
            motor_odds_sha = ((rank_doc.get('input_snapshots') or {}).get('odds') or {}).get('sha256')
            if motor_odds_sha and motor_odds_sha != stage_records['refresh_odds']['odds_sha256']:
                raise ValueError(f'Motor utilizó cuotas con SHA {motor_odds_sha} != {stage_records["refresh_odds"]["odds_sha256"]}')
            artifacts['ranking_json'] = {'path': str(rank_json_path), 'sha256': sha(rank_json_path), 'odds_sha256': motor_odds_sha}
        mat_json_path = ctx['directory'] / 'probabilistic' / f'matriz_evidencia_{ctx["event_id"]}.json'
        if mat_json_path.is_file():
            artifacts['matriz_json'] = {'path': str(mat_json_path), 'sha256': sha(mat_json_path)}
        sh_json_path = ctx['directory'] / 'shadow' / f'shadow_summary_{ctx["event_id"]}.json'
        shadow_ref = m_final.get('shadow_evaluation') or {}
        if (shadow_ref.get('path') and sh_json_path.is_file()
                and Path(shadow_ref['path']).resolve() == sh_json_path.resolve()
                and shadow_ref.get('sha256') == sha(sh_json_path)
                and shadow_ref.get('validation_sha256') == (artifacts.get('validation_json') or {}).get('sha256')):
            artifacts['shadow_summary_json'] = {'path': str(sh_json_path), 'sha256': sha(sh_json_path)}
        if m_final.get('codex_prompt') and Path(m_final['codex_prompt']).is_file():
            cp = Path(m_final['codex_prompt'])
            artifacts['codex_prompt'] = {'path': str(cp), 'sha256': sha(cp)}

        remain_final = (ctx['kickoff'] - local_now()).total_seconds() / 60.0
        if remain_final <= 0:
            update_state(ctx, 'close', 'PREMATCH_EXPIRED', {
                'stages': stage_records,
                'total_elapsed_seconds': round(time.perf_counter() - t_start, 4),
            })
            return 3
        stage_records['finish'] = {
            'elapsed_seconds': round(time.perf_counter() - t_fin, 4),
            'ok': True,
            'status': m_final.get('status'),
            'minutes_to_kickoff_after': round(remain_final, 2),
            'artifacts': artifacts,
        }
    except Exception as exc:
        stage_records['finish'] = {
            'elapsed_seconds': round(time.perf_counter() - t_fin, 4),
            'ok': False,
            'block': str(exc),
        }
        update_state(ctx, 'close', 'BLOCKED_FINISH', {
            'stages': stage_records,
            'error': str(exc),
            'total_elapsed_seconds': round(time.perf_counter() - t_start, 4),
        })
        print(f'BLOQUEADO en finish: {exc}', file=sys.stderr)
        return 2

    update_state(ctx, 'close', 'FINALIZADO_COORDINADO', {
        'stages': stage_records,
        'total_elapsed_seconds': round(time.perf_counter() - t_start, 4),
        'codex_prompt': m_final.get('codex_prompt'),
        'eligible_for_actionable_pick': m_final.get('eligible_for_actionable_pick'),
    })
    return 0


def cmd_status(ctx,args):
    print(f'{ctx["home"]} - {ctx["away"]} | API {ctx["fixture_id"]} | Bwin {ctx["event_id"]}')
    print(f'Kickoff Madrid: {ctx["kickoff"].astimezone(TZ).isoformat()}')
    print(f'Selección: {ctx["selection_time"].astimezone(TZ).isoformat()}')
    now=local_now();print(f'Ahora: {now.isoformat()}, restante: {(ctx["kickoff"]-now).total_seconds()/60:.1f} min')
    print('Familias con investigación inicial:',', '.join(ctx['families']))
    if state_path(ctx).is_file():
        st=read_json(state_path(ctx))
        print('Ejecución única:',', '.join(f'{k}={v.get("result")}' for k,v in (st.get('steps') or {}).items()))
    if (ctx['directory']/'manifest.json').is_file():
        try:
            m=match_manifest(ctx)
            print('Manifest:',m.get('status'),'| validación:',m.get('validation_state','pendiente'))
            odds=m.get('inputs',{}).get('odds')
            if odds:
                snap=snapshot_info(Path(odds['path']),ctx)
                print('Cuotas:',snap['capture'],'| edad:',snap['age_minutes'],'min')
            if (ctx['directory'] / 'case.json').is_file():
                from prematch_case import _evaluate_closure
                closure = _evaluate_closure(ctx['directory'], read_json(ctx['directory'] / 'case.json'), m)
                note = '; '.join(closure['closure_reasons']) if closure['closure_reasons'] else 'criterios T-60/T-15 cumplidos'
                print('Cierre:', closure['closure_stage'], f"({note})", '| XI API:', closure['lineups_status'], '| Shadow:', closure['shadow_status'])
            if m.get('codex_prompt'):print('Prompt Codex:',m['codex_prompt'])
        except ValueError as exc:print('ADVERTENCIA MANIFEST:',exc)
    else:print('Manifest: NO PREPARADO')
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path.cwd())
    p.add_argument('--confirm-external',action='store_true')
    p.add_argument('--selected',type=Path,required=True,help='selected_match.json definitivo del selector')
    p.add_argument('--decision',type=Path,help='selection_decision.json (si no, junto al selected_match.json)')
    p.add_argument('--multi',type=Path,default=Path('prematch_multi.py'),help='Pipeline anterior, sin modificar')
    p.add_argument('--max-selection-age-minutes',type=int,default=0,
                   help='0 = sin caducidad de selección; el kickoff y cuotas mantienen sus límites')
    p.add_argument('--max-odds-age-minutes',type=int,default=60)
    p.add_argument('--min-lead-minutes',type=int,default=PREPARE_MIN_LEAD_MINUTES,help='Margen mínimo para prepare/check')
    p.add_argument('--min-refresh-lead-minutes',type=int,default=REFRESH_MIN_LEAD_MINUTES,help='Margen mínimo para actualizar cuotas/contexto')
    p.add_argument('--min-finish-lead-minutes',type=int,default=FINISH_MIN_LEAD_MINUTES,help='Margen mínimo para iniciar finish (T-5 por defecto)')
    p.add_argument('--lineup-cutoff-minutes',type=int,default=15,help='Corte prepartido para exigir 22 titulares confirmados (default 15)')
    p.add_argument('--now-utc',help='Reloj ISO con timezone explícito para simulación PREMATCH reproducible')
    s=p.add_subparsers(dest='action',required=True)
    check=s.add_parser('check',help='Validar selección y tiempo; no usar herramientas externas')
    check.add_argument('--reuse-odds',action='store_true',help='Exigir también contrato, SHA y frescura para reutilizar el TXT')
    s.add_parser('status',help='Estado del único evento seleccionado')
    s.add_parser('verify-temporal-identity',help='Revalidar y transmitir evidencia Fase 3 sin API ni scraper')
    pp=s.add_parser('prepare',help='Scraper+API-Football únicamente para el elegido')
    pp.add_argument('--api-minute',type=int,default=180)
    pp.add_argument('--api-second',type=int,default=3)
    pp.add_argument('--force-prepare',action='store_true',help='Rehacer desde cero; respaldar capturas y manifest')
    pp.add_argument('--reuse-odds',action='store_true',help='Reutiliza cuotas seleccionadas por SHA y prepara únicamente el contexto completo')
    s.add_parser('refresh-odds',help='Solo scraper Bwin, sin repetir API ni actualizar otros eventos')
    s.add_parser('acquire-lineups',help='API por Fixture ID y delta XI; conserva históricos')
    delta=s.add_parser('refresh-context',help='Incorpora XI de un registro local adquirido antes del kickoff; sin API')
    delta.add_argument('--snapshot',type=Path,required=True)
    delta.add_argument('--snapshot-sha256',required=True)
    rc=s.add_parser('retry-context',help='Reintenta contexto API si falló, sin repetir cuotas')
    rc.add_argument('--api-minute',type=int,default=180)
    rc.add_argument('--api-second',type=int,default=3)
    rc.add_argument('--force-api',action='store_true',help='Omitir caché API (consume llamadas)')
    s.add_parser('retry-scraper',help='Reintenta scraper fallido, conserva contexto API')
    ff=s.add_parser('finish',help='Informe -> validador -> motor; genera prompt único Codex')
    ff.add_argument('--report',type=Path,help='Reporte cualitativo del seleccionado')
    ff.add_argument('--historical',action='store_true',help='Solo auditoría posterior al inicio: nunca Codex accionable')
    ff.add_argument('--shadow-mode',action=argparse.BooleanOptionalAction,default=None)
    ff_lu=ff.add_mutually_exclusive_group()
    ff_lu.add_argument('--require-confirmed-lineups',action='store_true',help='Falla cerrado si faltan 22 titulares confirmados')
    ff_lu.add_argument('--allow-unconfirmed-lineups',action='store_true',help='Permite investigación sin 22 titulares confirmados (no accionable)')
    cl=s.add_parser('close',help='Coordinación segura PREMATCH: precheck -> refresh-odds -> finish')
    cl.add_argument('--report',type=Path,help='Reporte cualitativo del seleccionado')
    cl.add_argument('--shadow-mode',action=argparse.BooleanOptionalAction,default=None)
    cl_lu=cl.add_mutually_exclusive_group()
    cl_lu.add_argument('--require-confirmed-lineups',action='store_true',default=True,help='Falla cerrado si faltan 22 titulares confirmados (default)')
    cl_lu.add_argument('--allow-unconfirmed-lineups',action='store_true',help='Permite cierre de investigación sin 22 titulares confirmados (no accionable)')
    args=p.parse_args(argv)
    if getattr(args, 'allow_unconfirmed_lineups', False):
        args.require_confirmed_lineups = False
    if args.action in ('prepare','refresh-odds','retry-context','retry-scraper','acquire-lineups','close'):
        from prematch_execution_control import require_external_confirmation
        try:require_external_confirmation(args.confirm_external)
        except ValueError as error:p.error(str(error))
    if (args.max_selection_age_minutes<0 or args.max_odds_age_minutes<1 or
            args.min_lead_minutes<45 or args.min_refresh_lead_minutes<5 or
            args.min_finish_lead_minutes<FINISH_MIN_LEAD_MINUTES):
        p.error('Límites de frescura inválidos: check/prepare >=45, refresh >=5 y finish >=5 minutos')
    prev_now_env = os.environ.get('PREMATCH_NOW_UTC')
    if getattr(args, 'now_utc', None):
        os.environ['PREMATCH_NOW_UTC'] = str(args.now_utc)
    try:
        ctx=load_context(args)
        if args.action=='check':
            remain,age=freshness(ctx,min_lead=args.min_lead_minutes,max_selection_age=args.max_selection_age_minutes)
            if args.reuse_odds:validate_reuse_odds(ctx,args.max_odds_age_minutes)
            print(f'SELECCIÓN VÁLIDA PARA INVESTIGACIÓN NIVEL {ctx["selection_tier"]}: {ctx["home"]} - {ctx["away"]}; Bwin {ctx["event_id"]}; fixture {ctx["fixture_id"]}')
            print(f'Kickoff {ctx["kickoff"].astimezone(TZ).isoformat()} | margen {remain:.1f} min | selección hace {age:.1f} min')
            print('No se ha ejecutado el scraper ni API-Football.')
            return 0
        if args.action=='status':return cmd_status(ctx,args)
        if args.action=='verify-temporal-identity':return cmd_verify_temporal_identity(ctx,args)
        if args.action=='prepare':return cmd_prepare(ctx,args)
        if args.action=='refresh-odds':return cmd_refresh(ctx,args)
        if args.action=='acquire-lineups':return cmd_acquire_lineups(ctx,args)
        if args.action=='refresh-context':return cmd_refresh_context(ctx,args)
        if args.action in ('retry-context','retry-scraper'):return cmd_retry(ctx,args,args.action)
        if args.action=='finish':return cmd_finish(ctx,args)
        if args.action=='close':return cmd_close(ctx,args)
    except (ValueError,KeyError,TypeError,OSError,TrialError) as exc:
        print('BLOQUEADO:',exc,file=sys.stderr)
        return 2
    finally:
        if getattr(args, 'now_utc', None):
            if prev_now_env is None:
                os.environ.pop('PREMATCH_NOW_UTC', None)
            else:
                os.environ['PREMATCH_NOW_UTC'] = prev_now_env
    return 2


from prematch_execution_control import confirmed_cli
main = confirmed_cli(main)

if __name__=='__main__':
    sys.exit(main())
