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
    return datetime.now(TZ)


def iso_utc():
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
    selected_path=(root/args.selected).resolve()
    decision_path=(root/args.decision).resolve() if args.decision else selected_path.parent/'selection_decision.json'
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


def match_manifest(ctx, *, check_files=True):
    mpath=ctx['directory']/'manifest.json'
    if not mpath.is_file():raise ValueError('No existe manifest.json del evento; ejecuta prepare')
    manifest=read_json(mpath)
    if str(manifest.get('event_id'))!=ctx['event_id'] or manifest.get('url')!=ctx['url']:
        raise ValueError('Manifest Event ID / URL distinto a la selección')
    inp=manifest.get('inputs') or {}
    for kind in ('odds','context','context_json','identity'):
        e=inp.get(kind)
        if not e:continue
        if check_files:
            path=Path(e['path'])
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
    if Path(selected['odds_txt']).resolve()!=expected.resolve():raise ValueError('REUSE_ODDS_SELECTION_PATH_MISMATCH')
    info=snapshot_info(expected,ctx,max_age=max_age)
    header=expected.read_text(encoding='utf-8-sig',errors='replace')[:6000]
    if RE_URL.search(header).group(1).strip()!=ctx['url']:raise ValueError('REUSE_ODDS_SELECTION_URL_MISMATCH')
    if info['sha256']!=selected['odds_sha256']:raise ValueError('REUSE_ODDS_SELECTION_SHA_MISMATCH')
    preflight=selected.get('preflight_path')
    if not preflight or not selected.get('preflight_sha256'):raise ValueError('REUSE_ODDS_PREFLIGHT_BINDING_MISSING')
    path=Path(preflight)
    if sha(path)!=selected['preflight_sha256']:raise ValueError('REUSE_ODDS_PREFLIGHT_SHA_MISMATCH')
    doc=read_json(path)
    for field, digest in (('source_discovery','source_sha256'),('source_preselection','source_preselection_sha256')):
        origin=Path(doc.get(field) or '')
        if not origin.is_file() or sha(origin)!=doc.get(digest):raise ValueError('REUSE_ODDS_ORIGINAL_SOURCE_SHA_MISMATCH')
    rows=[r for r in doc.get('matches',[]) if r.get('fixture_id')==ctx['fixture_id'] and str(r.get('bwin_event_id'))==ctx['event_id'] and r.get('bwin_url')==ctx['url']]
    if len(rows)!=1:raise ValueError('REUSE_ODDS_PREFLIGHT_IDENTITY_MISMATCH')
    snapshot=rows[0].get('snapshot') or {}
    if any(rows[0].get(k)!=ctx[k] for k in ('home','away')):raise ValueError('REUSE_ODDS_PREFLIGHT_TEAMS_MISMATCH')
    if snapshot.get('sha256')!=info['sha256'] or Path(snapshot.get('snapshot_path','')).resolve()!=expected.resolve():
        raise ValueError('REUSE_ODDS_PREFLIGHT_SNAPSHOT_MISMATCH')
    if dt(rows[0]['kickoff_madrid'],'preflight.kickoff')!=ctx['kickoff']:raise ValueError('REUSE_ODDS_PREFLIGHT_KICKOFF_MISMATCH')
    info['temporal_identity']=validate_temporal_identity_evidence(
        ctx,max_age_minutes=180)
    return info


def _source_path(root, value):
    path=Path(value or '')
    return path if path.is_absolute() else root/path


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
    preflight=_source_path(ctx['root'],selected.get('preflight_path'))
    if (not preflight.is_file() or sha(preflight)!=selected.get('preflight_sha256') or
            sha(preflight)!=contract.get('preflight_sha256')):
        raise ValueError('TEMPORAL_IDENTITY_PREFLIGHT_SHA_MISMATCH')
    doc=read_json(preflight)
    discovery=_source_path(ctx['root'],doc.get('source_discovery'))
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
    code=run(cmd,ctx['root'])
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
    for folder in ('validation','probabilistic','analysis'):
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
        # No se modifica la identidad, el contexto API ni sus hashes.
        m['inputs']['odds']={'path':str(path.resolve()),'sha256':snap['sha256'],
                             'generated_from':str(path.resolve()),'refreshed_at_utc':iso_utc()}
        m.setdefault('steps',{})['scraper_refresh']=0
        m['odds_refresh_at_utc']=iso_utc()
        m['invalidated_by_odds_refresh']={'previous_status':m.get('status'),'previous_sha256':previous['sha256'],
                                          'invalidated_at_utc':iso_utc(),'stages':['validation','probabilistic','codex_analysis','final_review']}
        m['status']='PREPARADO'
        m.pop('codex_prompt',None)
        m.pop('validation_state',None)
        atomic_json(ctx['directory']/'manifest.json',m)
        update_state(ctx,'refresh-odds','OK',{'odds':snap,'previous_sha256':previous['sha256'],
                                              'archived_previous':str(preserved),'log':str(log)})
        print(f'Cuotas actualizadas para {ctx["event_id"]}; contexto API NO repetido; SHA manifest actualizado.')
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
    if manifest.get('status') not in {'PREPARADO','VALIDADO','BLOQUEADO_VALIDACION','ERROR_VALIDACION','ERROR_MOTOR'}:
        raise ValueError(f'No procede finish: manifest={manifest.get("status")}')
    odds=manifest.get('inputs',{}).get('odds') or {}
    if not odds:raise ValueError('Sin cuotas en manifest: no se autoriza la fase final')
    # Las cuotas deben ser recientes, incluso si el scraping acabó correctamente.
    if not args.historical:
        snap=snapshot_info(Path(odds['path']),ctx,max_age=min(15,args.max_odds_age_minutes))
        from prematch_daily import final_odds_check
        final_odds_check({'kickoff_madrid':ctx['kickoff'].isoformat(),'capture_madrid':snap['capture']},local_now())
    report=Path(args.report).expanduser() if args.report else ctx['root']/'instructions'/f'reporte_cualitativo_{ctx["event_id"]}.md'
    if not report.is_absolute():report=ctx['root']/report
    if not report.is_file() and not args.report:
        report=ctx['directory']/'inputs'/f'reporte_cualitativo_{ctx["event_id"]}.md'
    if not report.is_file() or not report.stat().st_size:raise ValueError(f'Informe cualitativo ausente: {report}')
    report_head=report.read_text(encoding='utf-8-sig',errors='replace')[:12000]
    if 'STATUS: PENDIENTE_MANUAL' in report_head:raise ValueError('Informe cualitativo es una plantilla pendiente, no evidencia')
    if (ctx['directory']/'release_state.json').is_file() and not args.historical:
        from prematch_release import validate_external_inputs
        validate_external_inputs(ctx,report)
    labels=re.findall(r'(?im)\b(?:event[_ ]id(?: bwin)?|evento bwin)\s*[:|]\s*(\d{6,})\b',report_head)
    if any(x!=ctx['event_id'] for x in labels):raise ValueError('Informe cualitativo menciona otro Event ID')
    cmd=[sys.executable,str(multi),'--root',str(ctx['root']),'finish','--event-id',ctx['event_id'],
         '--report',str(report),'--workers','1']
    if args.historical:cmd.append('--historical')
    code=run(cmd,ctx['root'])
    if code:raise ValueError(f'finish devolvió {code}; revisa logs/validacion.log y logs/motor.log')
    m=match_manifest(ctx)
    expected='AUDITORIA_HISTORICA' if args.historical else 'FINALIZADO'
    if m.get('status')!=expected:raise ValueError(f'finish terminó {m.get("status")} y no {expected}')
    pinned_report=(m.get('inputs') or {}).get('report') or {}
    report_actual=Path(pinned_report.get('path') or report)
    if not report_actual.is_file() or not pinned_report.get('sha256') or sha(report_actual)!=pinned_report['sha256']:
        raise ValueError('Informe anclado tras finish no coincide con manifest')
    update_state(ctx,'finish',expected,{'report':str(report_actual),'report_sha256':sha(report_actual),
                                        'codex_prompt':m.get('codex_prompt') if not args.historical else None})
    if not args.historical:
        if (ctx['kickoff']-local_now()).total_seconds()<=0:
            update_state(ctx,'postfinish_clock','PREMATCH_EXPIRED',{'note':'Validación y motor finalizaron después del kickoff; no usar prompt para apuestas'})
            print('ALERTA: se alcanzó el kickoff durante finish. La salida es solo documental, NO lanzar Codex para picks PREMATCH.')
            return 3
        print('ÚNICO prompt para Codex:',m.get('codex_prompt') or '(ver manifest.json)')
        print('Codex NO se ha ejecutado automáticamente.')
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
            if m.get('codex_prompt'):print('Prompt Codex:',m['codex_prompt'])
        except ValueError as exc:print('ADVERTENCIA MANIFEST:',exc)
    else:print('Manifest: NO PREPARADO')
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path.cwd())
    p.add_argument('--selected',type=Path,required=True,help='selected_match.json definitivo del selector')
    p.add_argument('--decision',type=Path,help='selection_decision.json (si no, junto al selected_match.json)')
    p.add_argument('--multi',type=Path,default=Path('prematch_multi.py'),help='Pipeline anterior, sin modificar')
    p.add_argument('--max-selection-age-minutes',type=int,default=0,
                   help='0 = sin caducidad de selección; el kickoff y cuotas mantienen sus límites')
    p.add_argument('--max-odds-age-minutes',type=int,default=60)
    p.add_argument('--min-lead-minutes',type=int,default=PREPARE_MIN_LEAD_MINUTES,help='Margen mínimo para prepare/check')
    p.add_argument('--min-refresh-lead-minutes',type=int,default=REFRESH_MIN_LEAD_MINUTES,help='Margen mínimo para actualizar cuotas/contexto')
    p.add_argument('--min-finish-lead-minutes',type=int,default=FINISH_MIN_LEAD_MINUTES,help='Margen mínimo para iniciar finish (T-5 por defecto)')
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
    rc=s.add_parser('retry-context',help='Reintenta contexto API si falló, sin repetir cuotas')
    rc.add_argument('--api-minute',type=int,default=180)
    rc.add_argument('--api-second',type=int,default=3)
    rc.add_argument('--force-api',action='store_true',help='Omitir caché API (consume llamadas)')
    s.add_parser('retry-scraper',help='Reintenta scraper fallido, conserva contexto API')
    ff=s.add_parser('finish',help='Informe -> validador -> motor; genera prompt único Codex')
    ff.add_argument('--report',type=Path,help='Reporte cualitativo del seleccionado')
    ff.add_argument('--historical',action='store_true',help='Solo auditoría posterior al inicio: nunca Codex accionable')
    args=p.parse_args(argv)
    if (args.max_selection_age_minutes<0 or args.max_odds_age_minutes<1 or
            args.min_lead_minutes<45 or args.min_refresh_lead_minutes<5 or
            args.min_finish_lead_minutes<FINISH_MIN_LEAD_MINUTES):
        p.error('Límites de frescura inválidos: check/prepare >=45, refresh >=5 y finish >=5 minutos')
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
        if args.action in ('retry-context','retry-scraper'):return cmd_retry(ctx,args,args.action)
        if args.action=='finish':return cmd_finish(ctx,args)
    except (ValueError,KeyError,TypeError,OSError,TrialError) as exc:
        print('BLOQUEADO:',exc,file=sys.stderr)
        return 2
    return 2


if __name__=='__main__':
    sys.exit(main())
