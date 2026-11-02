#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PREMATCH Opportunity Scanner V0.1 — auditoría de líneas sin sugerir picks.

Consume preselection V1.3, pattern_engine V0.1 (opcional), market_preflight V0.3
junto a cada TXT original. No llama API ni hace scraping. No declara EV validado.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = '0.1-opportunity-scanner'
MADRID = ZoneInfo('Europe/Madrid')
HEADER_ID = re.compile(r'^Event ID:\s*(\d+)\s*$', re.I | re.M)
HEADER_DATE = re.compile(r'^Captura:\s*(\S+)\s*$', re.I | re.M)
HEADER_N = re.compile(r'^Acordeones:\s*(\d+)\s*\|\s*Selecciones:\s*(\d+)\s*$', re.I | re.M)
MARKET = re.compile(r'^\s*MERCADO:\s*(.+?)\s*$', re.I)
INLINE = re.compile(r'^\s*(.{1,200}?)\s+\:\s+(\d{1,4}(?:[.,]\d{1,3})?)\s*$')
ODDS = re.compile(r'^(mas|menos)\s+de\s+(\d+(?:[.,]\d+)?)$', re.I)
HALF = re.compile(r'^(.*?)(?:\s*\|\s*tiempo reglamentario)\s*$', re.I)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dt(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return t if t.tzinfo is not None and t.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def norm(value: str) -> str:
    s = unicodedata.normalize('NFKD', str(value))
    return ' '.join(''.join(c for c in s if not unicodedata.combining(c)).lower().split())


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def parse_bwin_txt(path: Path, eid: str) -> tuple[list[dict], dict]:
    """Solo formato real de la captura proporcionada. Fail closed ante líneas ambiguas."""
    txt = path.read_text(encoding='utf-8-sig', errors='strict')
    header = txt[:1500]
    m_id = HEADER_ID.search(header)
    m_time = HEADER_DATE.search(header)
    m_count = HEADER_N.search(header)
    if not m_id or m_id.group(1) != eid:
        raise ValueError('TXT_EVENT_ID_MISMATCH')
    stamp = dt(m_time.group(1)) if m_time else None
    if stamp is None:
        raise ValueError('TXT_CAPTURE_TIME_INVALID')
    rows, rejected = [], []
    market = None
    for lineno, raw in enumerate(txt.splitlines(), 1):
        mm = MARKET.fullmatch(raw.strip())
        if mm:
            market = mm.group(1).strip()
            continue
        if market is None or not raw.strip():
            continue
        mm = INLINE.fullmatch(raw.strip())
        if mm:
            selection = mm.group(1).strip()
            # Sin interpretación contextual de cabeceras o JSON como selecciones.
            if norm(selection).startswith(('mercado', 'captura', 'aviso de cobertura')):
                rejected.append(lineno)
                continue
            try:
                price = float(mm.group(2).replace(',', '.'))
            except ValueError:
                rejected.append(lineno)
                continue
            if not (1 < price <= 1000 and math.isfinite(price)):
                rejected.append(lineno)
                continue
            rows.append({'market': market, 'selection': selection, 'odd': price, 'txt_line': lineno})
        elif ':' in raw:
            rejected.append(lineno)
    declared = int(m_count.group(2)) if m_count else None
    if rejected:
        raise ValueError(f'TXT_UNPARSED_COLON_LINES: {rejected[:8]}')
    if declared is not None and len(rows) != declared:
        raise ValueError(f'TXT_SELECTION_COUNT_MISMATCH: {len(rows)} vs {declared}')
    if not rows:
        raise ValueError('TXT_NO_ODDS_PARSED')
    unique = [(r['market'], r['selection'], r['odd']) for r in rows]
    if len(set(unique)) != len(unique):
        raise ValueError('TXT_DUPLICATED_TRIPLES')
    return rows, {'event_id': eid, 'capture_madrid': stamp.astimezone(MADRID).isoformat(),
                  'raw_count': len(rows), 'declared_count': declared, 'sha256': sha(path),
                  'snapshot_file': str(path)}


def identify_market(market: str, home: str, away: str) -> tuple[str, str | None, str]:
    """(metric, side, structural status). Evita confundir periodos y props."""
    t = norm(market)
    if any(v in t for v in ('crea tus apuestas', 'combinad', 'ambos equipos marcan y', 'resultado (1x2) y')):
        return 'combined', None, 'REQUIERE_MODELO_CONJUNTO'
    if 'jugador' in t or 'goleador' in t:
        return 'player', None, 'REQUIERE_MODELO_JUGADOR_Y_XI'
    if '1a parte' in t or '1er tiempo' in t or '2a parte' in t or 'descanso' in t:
        return 'period', None, 'REQUIERE_MODELO_POR_PERIODO'
    if 'tiempo reglamentario' not in t:
        return 'unmapped', None, 'PERIODO_NO_ACREDITADO'
    hm = HALF.fullmatch(t)
    base = hm.group(1).strip() if hm else t
    side = None
    if base.startswith(norm(home) + ' - '):
        side = 'home'; base = base[len(norm(home)) + 3:]
    elif base.startswith(norm(away) + ' - '):
        side = 'away'; base = base[len(norm(away)) + 3:]
    if base == 'total de goles':
        return 'goals', side, 'MODELABLE'
    if base in ('total de corners', 'total corners', 'total de saques de esquina'):
        return 'corners', side, 'MODELABLE'
    if 'tarjeta' in t:
        return 'cards', side, 'REQUIERE_MODELO_ARBITRO_Y_DISCIPLINA'
    if 'fuera de juego' in t or 'offside' in t:
        return 'offsides', side, 'REQUIERE_MODELO_OFFSIDES'
    if 'falta' in t:
        return 'fouls', side, 'REQUIERE_MODELO_FALTAS'
    return 'other', side, 'SIN_MODELO_ESPECIFICO'


def parse_line(selection: str):
    m = ODDS.fullmatch(norm(selection))
    if not m:
        return None
    threshold = float(m.group(2).replace(',', '.'))
    # Exigir línea x.5 para evitar líneas enteras con reembolso/push.
    if not math.isclose(threshold % 1, 0.5, abs_tol=1e-8):
        return None
    return {'direction': 'OVER' if m.group(1) == 'mas' else 'UNDER', 'line': threshold}


def model_inputs(fixture: dict, metric: str):
    h, a = fixture.get('home_evidence') or {}, fixture.get('away_evidence') or {}
    if metric != 'goals' and not fixture.get('details_checked'):
        return None
    if metric == 'goals':
        hx, ax = h.get('history') or {}, a.get('history') or {}
        hown = hx.get('same_competition_goals_for') or {}
        hcon = hx.get('same_competition_goals_against') or {}
        aown = ax.get('same_competition_goals_for') or {}
        acon = ax.get('same_competition_goals_against') or {}
    else:
        hx, ax = h.get('details') or {}, a.get('details') or {}
        hown = (hx.get('own') or {}).get(metric) or {}
        hcon = (hx.get('conceded') or {}).get(metric) or {}
        aown = (ax.get('own') or {}).get(metric) or {}
        acon = (ax.get('conceded') or {}).get(metric) or {}
    series = [hown, acon, aown, hcon]
    if not all(isinstance(z.get('n'), int) and z.get('n') >= 1 and
               isinstance(z.get('mean'), (int,float)) and math.isfinite(z['mean']) and z['mean'] >= 0
               for z in series):
        return None
    nmin = min(int(s['n']) for s in series)
    lam_home = (hown['mean'] + acon['mean']) / 2
    lam_away = (aown['mean'] + hcon['mean']) / 2
    cvmax = max([s['cv'] for s in series if isinstance(s.get('cv'), (int,float)) and
                 math.isfinite(s['cv'])], default=None)
    return {'lambda_home': lam_home, 'lambda_away': lam_away,
            'lambda_total': lam_home+lam_away, 'n_min': nmin, 'cv_max': cvmax,
            'series_n': [s['n'] for s in series],
            'method': 'MEAN_FOR_CONCEDED_MIDPOINT_INDEPENDENT_COUNT_POISSON'}


def poisson_cdf(k: int, lam: float) -> float:
    if k < 0: return 0.0
    if lam < 0 or not math.isfinite(lam): raise ValueError('lambda inválida')
    if lam == 0: return 1.0
    return min(1.0, max(0.0, math.fsum(math.exp(-lam + i*math.log(lam) - math.lgamma(i+1))
                                       for i in range(k+1))))


def poisson_p(line: float, direction: str, lam: float) -> float:
    k = int(math.floor(line))
    p_under = poisson_cdf(k,lam)
    return p_under if direction == 'UNDER' else 1-p_under


def match_identity(f: dict, pf: dict) -> list[str]:
    reasons=[]
    if str(f.get('fixture_id')) != str(pf.get('fixture_id')):
        reasons.append('FIXTURE_ID_MISMATCH')
    for field in ('home', 'away'):
        if norm(f.get(field, '')) != norm(pf.get(field, '')):
            reasons.append(field.upper()+'_MISMATCH')
    a,b=dt(f.get('kickoff_madrid')),dt(pf.get('kickoff_madrid'))
    if a is None or b is None or abs((a-b).total_seconds()) > 180:
        reasons.append('KICKOFF_MISMATCH')
    return reasons


def preflight_for_fixture(root: dict, fixture_id: int):
    m=[e for e in root.get('matches',[]) if str(e.get('fixture_id'))==str(fixture_id)]
    if len(m)>1:raise ValueError(f'DUPLICATE_PREFLIGHT_FIXTURE {fixture_id}')
    return m[0] if m else None


def validate_snapshot(f, pf, pfroot, pfpath, prepath, override, asof, age_limit, lead_min, historical):
    reasons=match_identity(f,pf)
    if pf.get('status') not in ('MARKET_TRIPLES_EXTRACTED', 'HISTORICAL_MARKET_TRIPLES_EXTRACTED'):
        reasons.append('PREFLIGHT_NOT_VALID')
    if pfroot.get('source_preselection_sha256') != sha(prepath):
        reasons.append('PRESELECTION_SHA_MISMATCH')
    if (pf.get('time_evidence') or {}).get('status') not in ('DISPLAY_TIME_CORROBORATED', 'COUNTDOWN_TIME_CORROBORATED', 'ABSOLUTE_TIME_VERIFIED'):
        reasons.append('BWIN_TIME_NOT_CORROBORATED')
    if (pf.get('time_evidence') or {}).get('status') in ('COUNTDOWN_TIME_CORROBORATED', 'ABSOLUTE_TIME_VERIFIED'):
        from bwin_market_preflight import validate_match
        try:
            discovery_path = Path(pfroot.get('source_discovery') or '')
            if sha(discovery_path) != pfroot.get('source_sha256'):
                raise ValueError('COUNTDOWN_DISCOVERY_SHA_MISMATCH')
            discovery = load_json(discovery_path)
            events = [m for m in discovery.get('matches', []) if str(m.get('fixture_id')) == str(f['fixture_id'])]
            if len(events) != 1 or match_identity(f, events[0]) or str(events[0].get('bwin_event_id')) != str(pf.get('bwin_event_id')):
                raise ValueError('COUNTDOWN_DISCOVERY_IDENTITY_MISMATCH')
            checked = validate_match(events[0], discovery.get('catalog_observed', []), asof, 0, 180)
            if checked['status'] != 'READY_FOR_SCRAPER':
                raise ValueError('COUNTDOWN_SOURCE_NOT_REVALIDATED')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reasons.append(str(exc))
    source = pf.get('snapshot') or {}
    if not source.get('event_id_verified_in_txt'):
        reasons.append('EVENT_ID_UNVERIFIED')
    eid = str(pf.get('bwin_event_id') or '')
    if not re.search(r'-2:'+re.escape(eid)+r'(?:\?|$)',str(pf.get('bwin_url') or '')):
        reasons.append('BWIN_URL_EVENT_ID_CONFLICT')
    path = Path(override) if override else Path(source.get('snapshot_path') or '')
    if not path.is_file():
        reasons.append('SNAPSHOT_PATH_NOT_ACCESSIBLE')
        return reasons, None, None
    if sha(path) != source.get('sha256'):
        reasons.append('SNAPSHOT_SHA256_MISMATCH')
    try: quotes, meta = parse_bwin_txt(path, eid)
    except (ValueError, UnicodeError) as e:
        reasons.append(str(e));return reasons,None,None
    if meta['sha256'] != source.get('sha256'):
        reasons.append('TXT_PREFLIGHT_SHA_MISMATCH')
    if meta['raw_count'] != (source.get('selection_triplets_verified') or 0):
        reasons.append('PARSED_COUNT_PREFLIGHT_MISMATCH')
    capture=dt(meta['capture_madrid'])
    official=dt(source.get('capture_declared_madrid'))
    ko=dt(f.get('kickoff_madrid'))
    if official is None or capture!=official:
        reasons.append('PREFLIGHT_CAPTURE_DATE_MISMATCH')
    if ko is None or capture >= ko:
        reasons.append('POST_KICKOFF_CAPTURE')
    age=(asof-capture).total_seconds()/60
    if age < 0:reasons.append('FUTURE_SNAPSHOT')
    if age > age_limit:reasons.append('STALE_SNAPSHOT')
    if ko is None or not historical and (ko-asof).total_seconds()/60 < lead_min:
        reasons.append('MATCH_STARTED_OR_SHORT_LEAD')
    # Un informe generado después de asof no acredita que fuera disponible en ese instante.
    gen = dt(pfroot.get('generated_at_utc'))
    if gen is None or gen > asof:
        reasons.append('PREFLIGHT_GENERATED_AFTER_ASOF')
    return reasons,quotes,meta


def evaluate_selection(quote, f, threshold_odd, min_n, sensitivity_g, sensitivity_c, edge_min):
    result={'market':quote['market'], 'selection':quote['selection'],
            'odd':quote['odd'], 'txt_line':quote['txt_line'],
            'metric':None,'team_side':None,'market_line':None,'direction':None,
            'data_n_min':None,'lambda_exploratory':None,'p_poisson_exploratory':None,
            'p_sensitivity_min':None,'p_sensitivity_max':None,
            'break_even_probability':None,'gap_point_pp':None,'gap_worst_pp':None,
            'status':None,'model_caveat':None}
    if quote['odd'] < threshold_odd:
        result['status']='CUOTA_BAJO_UMBRAL';return result
    metric,side,status=identify_market(quote['market'],f['home'],f['away'])
    result.update(metric=metric,team_side=side)
    if status != 'MODELABLE':
        result['status']=status;return result
    line=parse_line(quote['selection'])
    if not line:
        result['status']='SELECCION_NO_OVER_UNDER_X5';return result
    result.update(market_line=line['line'],direction=line['direction'])
    model=model_inputs(f, metric)
    if model is None:
        result['status']='DATOS_ESTADISTICOS_INCOMPLETOS';return result
    lam = model['lambda_total'] if side is None else model['lambda_home' if side=='home' else 'lambda_away']
    p=poisson_p(line['line'],line['direction'],lam)
    margin=sensitivity_g if metric=='goals' else sensitivity_c
    pl=poisson_p(line['line'],line['direction'],lam*(1-margin))
    ph=poisson_p(line['line'],line['direction'],lam*(1+margin))
    lo,hi=min(pl,ph),max(pl,ph)
    be=1/quote['odd']
    result.update(data_n_min=model['n_min'],lambda_exploratory=round(lam,4),
                  p_poisson_exploratory=round(p,5),p_sensitivity_min=round(lo,5),
                  p_sensitivity_max=round(hi,5),break_even_probability=round(be,5),
                  gap_point_pp=round(100*(p-be),2),gap_worst_pp=round(100*(lo-be),2),
                  model_caveat='Poisson simple NO calibrado; intervalo = escenario +/- '+str(int(margin*100))+'% lambda, NO IC estadístico')
    if model['n_min']<min_n:
        result['status']='MUESTRA_CORTA_MODELO_SOLO_DIAGNOSTICO'
    elif lo > be + edge_min/100:
        result['status']='DISCREPANCIA_EXPLORATORIA_REVISAR'
    elif hi < be:
        result['status']='SIN_DISCREPANCIA_BAJO_ESCENARIOS'
    else:
        result['status']='RESULTADO_SENSIBLE_A_SUPUESTOS'
    return result


def produce(args):
    now = datetime.now(MADRID)
    asof = dt(args.asof) if args.asof else now
    if asof is None:raise ValueError('--asof exige fecha ISO con zona horaria')
    historical = args.asof is not None
    if (args.min_odd < 1.01 or args.min_n < 2 or not (0 <= args.min_gap_pp <= 50)
            or not (0 < args.sensitivity_goals < 1) or not (0 < args.sensitivity_corners < 1)
            or args.max_age_minutes <= 0 or args.min_lead_minutes < 0):
        raise ValueError('Parámetros de filtro inválidos')
    prepath=Path(args.preselection)
    pre=load_json(prepath)
    source_generated=dt(pre.get('generated_at_utc'))
    if source_generated is None or source_generated > asof:
        raise ValueError('PRESELECTION_GENERATED_AFTER_ASOF_OR_UNDATED')
    if not isinstance(pre.get('matches'),list):raise ValueError('Preselection sin matches[]')
    fixtures={int(m['fixture_id']):m for m in pre['matches']}
    patternref=None
    pattern_rows=[]
    pattern_at=None
    if args.pattern:
        pattpath=Path(args.pattern);patt=load_json(pattpath)
        sources=patt.get('data_sources') or {}
        if sources.get('preselection_sha256')!=sha(prepath):
            raise ValueError('PATTERN_PRESELECTION_SHA_MISMATCH')
        pa=dt(patt.get('asof_madrid'))
        if pa is None or pa > asof:
            raise ValueError('PATTERN_AFTER_ASOF')
        pattern_at=pa
        patternref={'path':str(pattpath),'sha256':sha(pattpath),'asof_madrid':pa.isoformat()}
        pattern_rows=patt.get('patterns') or []
        if not isinstance(pattern_rows,list):raise ValueError('PATTERN_ROWS_INVALID')
    snapshots={}
    for name in args.snapshot:
        path=Path(name)
        raw=path.read_text(encoding='utf-8-sig')[:1300]
        eid=HEADER_ID.search(raw)
        if not eid: raise ValueError('SNAPSHOT_HEADER_MISSING: '+name)
        if eid.group(1) in snapshots: raise ValueError('DUPLICATE_SNAPSHOT_EVENT: '+eid.group(1))
        snapshots[eid.group(1)]=path
    rows, audits=[] , []
    all_targets=set(args.fixture_id)
    for name in args.preflight:
        pfpath=Path(name); root=load_json(pfpath)
        if not isinstance(root.get('matches'),list):raise ValueError(f'PREFLIGHT_INVALID {name}')
        for pf in root['matches']:
            fid=int(pf['fixture_id'])
            if args.fixture_id and fid not in all_targets:continue
            if fid not in fixtures:raise ValueError('FIXTURE_ABSENT_FROM_PRESELECTION '+str(fid))
            if any(a['fixture_id']==fid for a in audits):raise ValueError('DUPLICATE_FIXTURE_PREFLIGHT '+str(fid))
            f=fixtures[fid]
            reasons, quotes, meta = validate_snapshot(f,pf,root,pfpath,prepath,snapshots.get(str(pf.get('bwin_event_id'))),
                                                      asof,args.max_age_minutes,args.min_lead_minutes,historical)
            cutoff=dt(f.get('analysis_cutoff_utc'))
            if cutoff is None or cutoff > asof:
                reasons.append('FIXTURE_ANALYSIS_CUTOFF_AFTER_ASOF')
            if pattern_at is not None and dt(f.get('kickoff_madrid')) and pattern_at >= dt(f['kickoff_madrid']):
                reasons.append('PATTERN_AFTER_FIXTURE_KICKOFF')
            audit={'fixture_id':fid,'home':f['home'],'away':f['away'],
                   'kickoff_madrid':f['kickoff_madrid'], 'bwin_event_id':pf.get('bwin_event_id'),
                   'preflight_path':str(pfpath), 'status':'VERIFIED' if not reasons else 'BLOCKED',
                   'reasons':reasons, 'snapshot':meta, 'captured':len(quotes or []) if not reasons else 0}
            audits.append(audit)
            if reasons:continue
            for quote in quotes:
                res=evaluate_selection(quote,f,args.min_odd,args.min_n,args.sensitivity_goals,
                                       args.sensitivity_corners,args.min_gap_pp)
                res.update(fixture_id=fid,home=f['home'],away=f['away'],bwin_event_id=pf.get('bwin_event_id'),
                           capture_madrid=meta['capture_madrid'])
                rows.append(res)
    for fid in args.fixture_id:
        if not any(a['fixture_id']==fid for a in audits):
            audits.append({'fixture_id':fid,'home':fixtures[fid]['home'] if fid in fixtures else None,
                           'away':fixtures[fid]['away'] if fid in fixtures else None,'status':'NO_PREFLIGHT',
                           'reasons':['NO_BWIN_PREFLIGHT'], 'captured':0})
    # Únicamente relaciona tendencias descriptivas con líneas DEL MISMO mercado y alcance.
    # Un equipo con más tiros a puerta NO avala props de un jugador.
    research_leads=[]
    verified_ids={a['fixture_id'] for a in audits if a['status']=='VERIFIED'}
    for pr in pattern_rows:
        if not isinstance(pr,dict) or pr.get('pattern_flag')!='ASIMETRIA_RELATIVA_ALTA':
            continue
        try: fid=int(pr.get('fixture_id'))
        except (ValueError,TypeError):continue
        if fid not in verified_ids:continue
        f=fixtures[fid]
        if norm(pr.get('home',''))!=norm(f.get('home','')) or norm(pr.get('away',''))!=norm(f.get('away','')):
            raise ValueError('PATTERN_FIXTURE_IDENTITY_CONFLICT '+str(fid))
        metric=pr.get('metric')
        side=pr.get('team_side')
        if metric not in ('goals','corners') or side not in ('home','away'):
            continue
        applicable=[r for r in rows if r['fixture_id']==fid and r['metric']==metric and
                    r['market_line'] is not None and r['team_side'] in (side,None) and
                    r['odd']>=args.min_odd]
        if not applicable:continue
        direct=[r for r in applicable if r['team_side']==side]
        research_leads.append({'fixture_id':fid,'team':pr.get('team'),'team_side':side,
                               'metric':metric,'relative_asymmetry_pct':pr.get('relative_asymmetry_pct'),
                               'matching_market_lines':len(applicable),
                               'relation':'DIRECT_TEAM_MARKET' if direct else 'INDIRECT_TEAM_TREND_TO_MATCH_TOTAL',
                               'status':'DESCRIPTIVE_PATTERN_WITH_MARKET_NO_VERIFIED_VALUE',
                               'warning':'La asimetría NO prueba probabilidad, causalidad ni valor de cuota'})
    rank=[]
    for a in audits:
        these=[r for r in rows if r['fixture_id']==a['fixture_id']]
        statuses=Counter(r['status'] for r in these)
        signals=[r for r in these if r['status']=='DISCREPANCIA_EXPLORATORIA_REVISAR']
        rank.append({'fixture_id':a['fixture_id'],'home':a.get('home'),'away':a.get('away'),
                     'status':a['status'], 'market_selections':len(these),
                     'price_eligible':sum(r['odd']>=args.min_odd for r in these),
                     'model_diagnostics':sum(r['p_poisson_exploratory'] is not None for r in these),
                     'unvalidated_discrepancies':len(signals),
                     'distinct_model_signal_groups':len({(r['metric'],r['team_side']) for r in signals}),
                     'descriptive_pattern_market_links':sum(x['fixture_id']==a['fixture_id'] for x in research_leads),
                     'best_exploratory_gap_worst_pp':max((r['gap_worst_pp'] for r in signals), default=None),
                     'status_counts':dict(statuses),
                     'research_priority_score':fixtures.get(a['fixture_id'],{}).get('research_priority_score')})
    rank.sort(key=lambda z:(z['status']!='VERIFIED', -z['distinct_model_signal_groups'],
                            -z['descriptive_pattern_market_links'],
                            -(z['best_exploratory_gap_worst_pp'] or 0),
                            -(z['research_priority_score'] or 0)))
    snapshot_ready=sum(a['status']=='VERIFIED' for a in audits)
    payload={
        'schema_version':SCHEMA,'asof_madrid':asof.astimezone(MADRID).isoformat(),
        'mode':'RETROSPECTIVE_AUDIT' if historical else 'PREMATCH_SNAPSHOT_SCREEN',
        'deep_research_authorized':False,'selection_status':'NEEDS_CALIBRATED_MODEL_AND_CONTEXT',
        'sources':{'preselection':str(prepath),'preselection_sha256':sha(prepath),
                   'pattern':patternref,'preflights':list(args.preflight)},
        'parameters':{'min_odd':args.min_odd,'min_n_for_price_screen':args.min_n,
                      'sensitivity_goals_lambda_pct':100*args.sensitivity_goals,
                      'sensitivity_corners_lambda_pct':100*args.sensitivity_corners,
                      'min_gap_pp':args.min_gap_pp,'max_snapshot_age_minutes':args.max_age_minutes,
                      'min_lead_minutes':args.min_lead_minutes},
        'methodology':{
            'distribution':'Poisson simplificada para exploración de totales FT: NO calibrada ni validada',
            'lambda':'Media aritmética entre producción propia y concesión del rival, por equipo; suma para FT',
            'sensitivity':'Perturbación fija de lambda, NO intervalos de confianza',
            'unsupported':'Props de jugador, tarjetas, offsides, faltas, combinadas, medias partes, handicap, etc. sin modelo',
            'risk':'Sin ajuste de localía, rival, alineaciones, entrenador, arbitraje, sobredispersión ni sesgo de selección',
            'profitability':'NO se calcula EV validado, stake ni pick; diferencias son señales exploratorias no apostables',
        },
        'counts':{'preflight_fixtures':len(audits),'verified_fixtures':snapshot_ready,
                  'captured_selections':len(rows),
                  'price_eligible':sum(r['odd']>=args.min_odd for r in rows),
                  'diagnostics':sum(r['p_poisson_exploratory'] is not None for r in rows),
                  'unvalidated_discrepancies':sum(r['status']=='DISCREPANCIA_EXPLORATORIA_REVISAR' for r in rows),
                  'descriptive_pattern_market_links':len(research_leads)},
        'audits':audits,'research_leads_descriptive_not_picks':research_leads,
        'ranking_for_followup_not_betting':rank,'selection_reviews':rows,
    }
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    (out/'opportunity_scan.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    keys=['fixture_id','home','away','bwin_event_id','capture_madrid','market','selection','odd','txt_line',
          'metric','team_side','market_line','direction','data_n_min','lambda_exploratory',
          'p_poisson_exploratory','p_sensitivity_min','p_sensitivity_max','break_even_probability',
          'gap_point_pp','gap_worst_pp','status','model_caveat']
    with (out/'opportunity_review.csv').open('w',encoding='utf-8-sig',newline='') as file:
        w=csv.DictWriter(file,fieldnames=keys);w.writeheader();w.writerows(rows)
    with (out/'opportunity_ranking.csv').open('w',encoding='utf-8-sig',newline='') as file:
        fields=['fixture_id','home','away','status','market_selections','price_eligible','model_diagnostics',
                'unvalidated_discrepancies','distinct_model_signal_groups','descriptive_pattern_market_links',
                'best_exploratory_gap_worst_pp','research_priority_score']
        w=csv.DictWriter(file,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k) for k in fields} for r in rank)
    lines=['# PREMATCH Opportunity Scanner V0.1','',
           f'- **Modo:** {payload["mode"]}', f'- **As of Madrid:** {payload["asof_madrid"]}',
           '- **Deep Research autorizado:** NO. Falta calibración y contexto verificado.',
           f'- **Capturas verificadas:** {snapshot_ready}/{len(audits)}',
           f'- **Cuotas leídas:** {len(rows)}; >= {args.min_odd:.2f}: {payload["counts"]["price_eligible"]}',
           f'- **Comparaciones Poisson exploratorias:** {payload["counts"]["diagnostics"]}',
           f'- **Discrepancias SIN validación:** {payload["counts"]["unvalidated_discrepancies"]}',
           f'- **Patrones descriptivos vinculados a mercados reales:** {len(research_leads)}',
           '', '## Auditoría por partido', '',
           '| Partido | Estado | Cuotas | Poisson exploratorio | Discrepancias sin validar |',
           '|---|---|---:|---:|---:|']
    for a in rank:
        lines.append(f'| {a.get("home") or "?"}–{a.get("away") or "?"} | {a["status"]} | '+
                     f'{a["market_selections"]} | {a["model_diagnostics"]} | {a["unvalidated_discrepancies"]} |')
    lines+=['','## Hipótesis descriptivas relacionadas con mercados', '',
            'Relación por métrica, no confirmación causal ni cuota infravalorada.', '']
    for h in research_leads:
        lines.append(f'- `{h["fixture_id"]}` {h["team"]}: {h["metric"]}, asimetría descriptiva '+
                     f'{h["relative_asymmetry_pct"]}%, relación {h["relation"]}, '+
                     f'{h["matching_market_lines"]} selecciones del mercado relacionado. '+
                     'No se ha demostrado valor.')
    if not research_leads:lines.append('No hay patrones con relación admisible a mercados de la captura.')
    lines+=['', '## Líneas para auditoría posterior', '',
            'Los siguientes resultados **NO son picks** ni demuestran valor esperado. Los escenarios lambda no son intervalos estadísticos.', '']
    review=sorted((r for r in rows if r['p_poisson_exploratory'] is not None),
                  key=lambda r:(r['status']!='DISCREPANCIA_EXPLORATORIA_REVISAR',-(r['gap_worst_pp'] or -999)))[:12]
    for r in review:
        lines.append(f'- `{r["fixture_id"]}` {r["market"]} / {r["selection"]} @ {r["odd"]:.3f}: '+
                     f'{r["status"]}; n_min={r["data_n_min"]}; '+
                     f'P Poisson exploratoria={r["p_poisson_exploratory"]:.1%}; '+
                     f'equilibrio Bwin={r["break_even_probability"]:.1%}; '+
                     f'sensibilidad={r["p_sensitivity_min"]:.1%}–{r["p_sensitivity_max"]:.1%}.')
    if not review:lines.append('Ninguna línea permite siquiera una aproximación bajo el modelo inicial.')
    lines+=['','## Limitaciones obligatorias','',
            '- Las líneas de jugador no se extrapolan desde estadísticas de equipo.',
            '- Las cuotas históricas se auditan; no se presentan como vigentes.',
            '- El modelo Poisson independiente puede estar mal especificado, especialmente en córners.',
            '- La coincidencia de una diferencia positiva no acredita rentabilidad.',
            '- No se asigna ganador definitivo ni se autoriza consumo automático de Gemini/Grok.', '']
    (out/'opportunity_report.md').write_text('\n'.join(lines),encoding='utf-8')
    return payload


def main(argv=None):
    p=argparse.ArgumentParser(description='Explorador de mercados prepartido: sin scraping ni predicciones validadas')
    p.add_argument('--preselection',required=True)
    p.add_argument('--pattern',help='pattern_engine.json V0.1 opcional para trazabilidad')
    p.add_argument('--preflight',action='append',required=True,help='market_preflight.json; repetir por partido')
    p.add_argument('--snapshot',action='append',default=[],help='TXT existente si la ruta del preflight no existe en este equipo')
    p.add_argument('--fixture-id',type=int,action='append',default=[],help='Filtrar por fixture; repetir')
    p.add_argument('--asof',help='ISO con timezone; SOLO auditoría retrospectiva')
    p.add_argument('--output-dir',required=True)
    p.add_argument('--min-odd',type=float,default=1.20)
    p.add_argument('--min-n',type=int,default=15)
    p.add_argument('--max-age-minutes',type=int,default=120)
    p.add_argument('--min-lead-minutes',type=int,default=45)
    p.add_argument('--min-gap-pp',type=float,default=4.0)
    p.add_argument('--sensitivity-goals',type=float,default=0.25)
    p.add_argument('--sensitivity-corners',type=float,default=0.35)
    a=p.parse_args(argv)
    try:
        result=produce(a)
    except (ValueError,TypeError,KeyError,FileNotFoundError,json.JSONDecodeError) as e:
        print('ERROR:',e,file=sys.stderr);return 2
    print('AUDITORÍA:', result['mode'],'|',result['counts'])
    for r in result['audits']:
        print(f"[{r['fixture_id']}] {r['status']} "+(','.join(r['reasons']) if r['reasons'] else ''))
    print('Salida:',a.output_dir,'| Deep Research: NO AUTORIZADO por este módulo')
    return 0 if all(r['status']=='VERIFIED' for r in result['audits']) else 1


if __name__=='__main__':
    raise SystemExit(main())
