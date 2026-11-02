#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PREMATCH Prospective Trial V0.3. Ventanas configuradas por perfiles, sin picks.

PREPARE: API fixtures -> primeras divisiones -> preselección -> patrones -> discovery Bwin.
SCAN: captura nuevas cuotas por evento -> preflight -> Opportunity Scanner (no calibrado).

Nunca ejecuta un partido pasado, no acepta --asof y NO habilita Grok/Gemini/Codex.
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

def window_path(root:Path, day:date, start:time=time(16,0), end:time=time(2,0)):
    begin,finish=local_window(day,start,end)
    return root/'outputs'/'discovery'/f'{begin.date():%Y-%m-%d}_{begin:%H%M}_{finish:%H%M}'

def allowed_window(day, current, start:time=time(16,0), end:time=time(2,0)):
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
        process=subprocess.Popen(command,cwd=str(root),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                 text=True,encoding='utf-8',errors='replace',bufsize=1)
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
    from preselect_matches import save_report, assign_ranks
    check_files(root,['discover_fixtures.py','filter_major_leagues.py','preselect_matches.py',
                      'prematch_pattern_engine.py','bwin_discovery.py'])
    if (folder/'prospective_trial/enriched/preselection.json').exists():
        archive=root/'outputs/discovery/archives'/f'{folder.name}_{now_madrid():%Y%m%dT%H%M%S%f}'
        shutil.copytree(folder,archive)
    base=folder/'prospective_trial'; logs=base/'logs'
    run(root,'discover_fixtures.py',['--date',day.isoformat(),'--start',args.start.strftime('%H:%M'),
                                   '--end',args.end.strftime('%H:%M'),'--show','12'],logs)
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
    run(root,'filter_major_leagues.py',['--input',str(folder/'fixtures.json')],logs)
    major=read(folder/'fixtures_major.json')
    for f in major.get('excluded',[])+major.get('review',[]):
        ledger.record(f,'FILTRADO_POR_LIGA','league_filter',f.get('filter_reason'))
    for f in major['fixtures']:
        reason=scope_reason(f,now_madrid(),args.min_lead,getattr(args,'horizon_hours',3))
        if reason:
            ledger.record(f,'EXCLUIDO_POR_HORARIO','scope',reason)
    # max-matches is a batch size. No candidate in scope is dropped by this limit.
    selection=select_fixtures(major,args.team,0,now_madrid(),args.min_lead,getattr(args,'horizon_hours',3))
    from prematch_tactical_signal_engine import diversified_order,run_engine
    if getattr(args,'evaluation_budget',0):selection=diversified_order(selection)
    all_selection=list(selection)
    budget=getattr(args,'evaluation_budget',0)
    if budget:
        selection=selection[:budget]
        for f in all_selection[budget:]:ledger.record(f,'DIFERIDO_POR_PRESUPUESTO','preselection','EVALUATION_BUDGET_NOT_ANALYZED')
    selected_ids={str(f['fixture_id']) for f in selection}
    for f in major['fixtures']:
        if str(f['fixture_id']) not in {str(x['fixture_id']) for x in all_selection} and not scope_reason(f,now_madrid(),args.min_lead,getattr(args,'horizon_hours',3)):
            ledger.record(f,'EXCLUIDO_POR_FILTRO_MANUAL','scope','TEAM_FILTER')
    ledger.write()
    if not selection:
        raise TrialError('No hay partidos futuros y elegibles en el horizonte/ventana solicitado. No se consultó Bwin.')
    subset=dict(major,fixtures=selection)
    subset['counts']=dict(major.get('counts',{}),eligible=len(selection))
    subset['trial_subset']={'source_sha256':sha(folder/'fixtures_major.json'),'count':len(selection),
                            'purpose':'ALL_ELIGIBLE_IN_SCOPE_NO_CHRONOLOGICAL_TRUNCATION'}
    trial_input=base/'fixtures_major_test.json';dump(trial_input,subset)
    batch_docs=[]; batch_sources=[]
    for i in range(0,len(selection),args.max_matches):
        batch=selection[i:i+args.max_matches]; dest=base/'enrichment_batches'/str(i//args.max_matches+1)
        inp=dest/'fixtures.json';dump(inp,dict(subset,fixtures=batch))
        run(root,'preselect_matches.py',['--input',str(inp),'--output-dir',str(dest),
            '--max-matches',str(len(batch)),'--history-limit',str(args.history),'--detail-limit',str(args.details)],logs)
        path=dest/'preselection.json';doc=read(path)
        if doc.get('source_filter_sha256')!=sha(inp):raise TrialError('BATCH_SOURCE_SHA_MISMATCH')
        batch_docs.append(doc);batch_sources.append({'path':str(path),'sha256':sha(path),'input':str(inp),'input_sha256':sha(inp)})
    pre=copy.deepcopy(batch_docs[0]);pre['matches']=[f for d in batch_docs for f in d['matches']]
    if len({str(f['fixture_id']) for f in pre['matches']})!=len(pre['matches']):raise TrialError('DUPLICATE_BATCH_FIXTURE')
    if {str(f['fixture_id']) for f in pre['matches']}!=selected_ids:raise TrialError('INCOMPLETE_ENRICHMENT_BATCHES')
    pre['generated_at_utc']=now_madrid().astimezone(timezone.utc).isoformat()
    pre['source_filter_sha256']=sha(trial_input);pre['source_batches']=batch_sources
    for field in ('counts','api_usage'):
        pre[field]={k:sum(d.get(field,{}).get(k,0) or 0 for d in batch_docs) for k in {k for d in batch_docs for k in d.get(field,{})}}
    pre['api_errors']={f'batch_{i}_{k}':v for i,d in enumerate(batch_docs) for k,v in d.get('api_errors',{}).items()}
    pre['history_seasons_used']={k:v for d in batch_docs for k,v in d.get('history_seasons_used',{}).items()}
    assign_ranks(pre['matches']);outpre=base/'enriched';save_report(pre,outpre);prepath=outpre/'preselection.json'
    for f in pre['matches']:
        ready=f.get('data_readiness_score') is not None and f.get('details_checked')
        ledger.record(f,'PENDIENTE_BWIN' if ready else 'SIN_COBERTURA','preselection',f.get('data_readiness_status'))
    patternfolder=base/'patterns'
    run(root,'prematch_pattern_engine.py',['--preselection',str(prepath),'--top',str(len(selection)),
        '--min-lead-minutes',str(args.min_lead),'--output-dir',str(patternfolder)],logs)
    # Tactical evidence is computed locally before spending the Bwin budget.
    tactical=run_engine(prepath,patternfolder/'pattern_engine.json',base/'tactical',root,
        profiles=root/'config/tactical_profiles/profiles.json')
    tactical_scores={fid:x['priority_score'] for fid,x in tactical['fixtures'].items()}
    ordered=diversified_order(pre['matches'],tactical_scores)
    dump(base/'budget_assignment.json',{'strategy':'ROUND_ROBIN_TWO_HOUR_SLOTS_TACTICAL_QUALITY_WITHIN_SLOT',
        'evaluation_fixture_ids':[f['fixture_id'] for f in selection],
        'bwin_order_fixture_ids':[f['fixture_id'] for f in ordered],
        'tactical_scores':tactical_scores,'warning':'NO_EV_NO_CALIBRATED_PROBABILITY'})
    discovery_deferred=[]
    budget=getattr(args,'discovery_budget',0)
    if budget:
        discovery_deferred=ordered[budget:];ordered=ordered[:budget]
        for f in discovery_deferred:ledger.record(f,'DIFERIDO_POR_PRESUPUESTO','bwin_discovery','DISCOVERY_BUDGET_NOT_ANALYZED')
    ledger.write()
    docs=[];bwinfolder=base/'bwin'
    for i in range(0,len(ordered),args.bwin_top):
        dest=bwinfolder/'batches'/str(i//args.bwin_top+1)
        cmd=['--input',str(prepath),'--headless','--output-dir',str(dest)]
        for f in ordered[i:i+args.bwin_top]:cmd+=['--fixture-id',str(f['fixture_id'])]
        run(root,'bwin_discovery.py',cmd,logs);docs.append(read(dest/'bwin_discovery.json'))
    bwin={'schema_version':'2.0-batched-discovery','generated_at_utc':now_madrid().astimezone(timezone.utc).isoformat(),
          'source_input':str(prepath),'source_input_sha256':sha(prepath),'prematch_only':True,
          'matches':[m for d in docs for m in d['matches']],
          'catalog_observed':[m for d in docs for m in d.get('catalog_observed',[])],
          'source_batches':[str(bwinfolder/'batches'/str(i+1)/'bwin_discovery.json') for i in range(len(docs))]}
    dump(bwinfolder/'bwin_discovery.json',bwin)
    for f in bwin['matches']:
        status=f.get('status','')
        if not f.get('bwin_event_id'):ledger.record(f,'SIN_BWIN','bwin_discovery',status)
        elif 'REVIEW' in status:ledger.record(f,'IDENTIDAD_INCORRECTA','bwin_discovery',status)
        else:ledger.record(f,'PENDIENTE_CUOTAS','bwin_discovery',status)
    ledger.write();begin,finish=local_window(day,args.start,args.end)
    summary={'schema_version':'2.0-prospective-trial','stage':'PREPARED','day':day.isoformat(),
        'generated_madrid':now_madrid().isoformat(),'window_start_madrid':begin.isoformat(),
        'window_end_madrid':finish.isoformat(),'fixtures_considered':len(selection),
        'candidate_fixtures':all_selection,'scope':ledger.scope,
        'evaluation_deferred_ids':[f['fixture_id'] for f in all_selection if str(f['fixture_id']) not in selected_ids],
        'discovery_deferred_ids':[f['fixture_id'] for f in discovery_deferred],
        'sources':{'preselection':str(prepath),'pattern':str(patternfolder/'pattern_engine.json'),
                   'discovery':str(bwinfolder/'bwin_discovery.json')}}
    dump(base/'prospective_test_summary.json',summary)
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
    p.add_argument('--details',type=int,default=8)
    p.add_argument('--min-lead',type=int,default=60)
    args=p.parse_args(argv)
    if args.evaluation_budget<0 or args.discovery_budget<0 or not __import__("math").isfinite(args.horizon_hours) or args.horizon_hours<0 or args.discovery_retries<0 or args.max_odds_age<1 or args.max_matches<1 or args.bwin_top<1 or args.max_scrapes<0 or args.history<4 or args.details<1 or args.details>args.history or args.min_lead<45:
        p.error('Límites inválidos: max-matches/bwin-top >0, max-scrapes >=0, history>=4, 1<=details<=history y min-lead>=45')
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
        day = day or automatic_day(now_madrid(),args.start,args.end)
        begin,finish=local_window(day,args.start,args.end)
        folder=window_path(root,day,args.start,args.end)
        print(f'CONFIG: {cfg_path} ({"JSON" if from_file else "integrada; instala el JSON para editarla"})',flush=True)
        print(f'PERFIL: {window_name} | VENTANA: {begin.isoformat()} -> {finish.isoformat()}',flush=True)
        print(f'SALIDA: {folder}',flush=True)
        print(f'HORIZONTE: {args.horizon_hours} h (0=toda la ventana)',flush=True)
        if args.show_config:
            return 0
        allowed_window(day,now_madrid(),args.start,args.end)
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
