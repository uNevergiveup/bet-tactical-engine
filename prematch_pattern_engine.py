#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PREMATCH Pattern Engine V0.1 — análisis DESCRIPTIVO y auditable.

Usa exclusivamente preselection V1.3 y preflights V0.3 ya producidos.
NO llama API-Football, NO visita Bwin y NO calcula picks, probabilidades, EV,
PPDA, altura de línea, mapas de calor o lesiones.

Ejemplo:
  python prematch_pattern_engine.py --preselection preselection.json \
    --fixture-id 1490597 --preflight market_preflight.json \
    --output-dir outputs/patterns/1490597

Las cifras de matchup son PROMEDIOS HEURÍSTICOS DESCRIPTIVOS, NO pronósticos.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = '0.1-pattern-engine'
MADRID = ZoneInfo('Europe/Madrid')
# Solo métricas efectivamente presentes en la preselección.
METRICS = {
    'goals': {'name': 'Goles', 'source': 'history.same_competition', 'bwin_kind': 'total_goals'},
    'corners': {'name': 'Córners', 'source': 'details', 'bwin_kind': 'corners'},
    'shots': {'name': 'Tiros totales', 'source': 'details', 'bwin_kind': 'shots'},
    'on_target': {'name': 'Tiros a puerta (equipo)', 'source': 'details', 'bwin_kind': 'shots_on_target'},
    'fouls': {'name': 'Faltas', 'source': 'details', 'bwin_kind': 'fouls'},
    'offsides': {'name': 'Fueras de juego', 'source': 'details', 'bwin_kind': 'offsides'},
    'yellow': {'name': 'Tarjetas amarillas', 'source': 'details', 'bwin_kind': 'cards'},
    'xg': {'name': 'xG (si está cubierto)', 'source': 'details', 'bwin_kind': None},
}
CSV_FIELDS = [
    'fixture_id','home','away','kickoff_madrid','team','opponent','team_side',
    'metric','metric_label','status','own_mean','opponent_conceded_mean',
    'matchup_descriptive_midpoint','team_conceded_mean','opponent_own_mean',
    'opponent_midpoint','relative_asymmetry_pct','own_n','opponent_conceded_n',
    'own_cv','opponent_conceded_cv','volatility','alignment','pattern_flag',
    'bwin_market_status','bwin_price_eligible_count','cautions',
]


def read_json(path: Path):
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(result, dict):
        raise ValueError(f'JSON raíz debe ser objeto: {path}')
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_dt(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    x = float(v)
    return x if math.isfinite(x) else None


def num_series(obj, key='mean'):
    if not isinstance(obj, dict):
        return {'mean': None, 'n': 0, 'cv': None}
    n = number(obj.get('n'))
    n = int(n) if n is not None and n >= 0 else 0
    return {'mean': number(obj.get(key)), 'n': n, 'cv': number(obj.get('cv'))}


def source_sides(fixture, metric):
    """Extrae cuatro series con n independientes; nunca convierte null en cero."""
    he = fixture.get('home_evidence') or {}
    ae = fixture.get('away_evidence') or {}
    if metric == 'goals':
        hh, ah = (he.get('history') or {}), (ae.get('history') or {})
        return {
            'home_own': num_series(hh.get('same_competition_goals_for')),
            'home_conceded': num_series(hh.get('same_competition_goals_against')),
            'away_own': num_series(ah.get('same_competition_goals_for')),
            'away_conceded': num_series(ah.get('same_competition_goals_against')),
        }
    if not fixture.get('details_checked'):
        return {k: num_series(None) for k in ('home_own','home_conceded','away_own','away_conceded')}
    hd = he.get('details') or {}
    ad = ae.get('details') or {}
    return {
        'home_own': num_series((hd.get('own') or {}).get(metric)),
        'home_conceded': num_series((hd.get('conceded') or {}).get(metric)),
        'away_own': num_series((ad.get('own') or {}).get(metric)),
        'away_conceded': num_series((ad.get('conceded') or {}).get(metric)),
    }


def identity_matches(fixture, preflight):
    if str(preflight.get('fixture_id')) != str(fixture.get('fixture_id')):
        return False
    for key in ('home', 'away'):
        if ' '.join(str(preflight.get(key, '')).casefold().split()) != ' '.join(str(fixture.get(key, '')).casefold().split()):
            return False
    a = parse_dt(preflight.get('kickoff_madrid'))
    b = parse_dt(fixture.get('kickoff_madrid'))
    return a is not None and b is not None and abs((a-b).total_seconds()) <= 180


def market_evidence(preflight, fixture, metric, asof, max_age_minutes=120):
    """Separar evidencia de mercado de evidencia estadística: preflight no es cuota vigente.

    Verifica cabecera del TXT si puede accederse; SHA y marca temporal si existe.
    Una captura posterior a 'asof' o demasiado antigua se invalida.
    """
    kind = METRICS[metric]['bwin_kind']
    if not kind:
        return ('NO_DIRECT_BWIN_KIND', 0, 'xG es una métrica de rendimiento, no una línea de goles directa')
    if preflight is None:
        return ('NO_BWIN_PREFLIGHT', 0, 'No se entregó preflight del fixture')
    if not identity_matches(fixture, preflight):
        return ('IDENTITY_MISMATCH', 0, 'Identidad fixture/equipos/kickoff diferente')
    generated = parse_dt(preflight.get('_report_generated_at'))
    if generated is None or generated > asof:
        return ('INVALID_PREFLIGHT_REPORT_TIME', 0, 'Preflight sin tiempo válido o generado después de asof')
    if preflight.get('status') not in ('MARKET_TRIPLES_EXTRACTED','HISTORICAL_MARKET_TRIPLES_EXTRACTED'):
        return ('SNAPSHOT_NOT_VALIDATED', 0, 'La captura no está reconocida como cuotas extraídas')
    snap = preflight.get('snapshot') or {}
    if not snap.get('event_id_verified_in_txt'):
        return ('EVENT_ID_NOT_VERIFIED', 0, 'Event ID sin acreditar en TXT')
    eid=str(preflight.get('bwin_event_id') or '')
    url=str(preflight.get('bwin_url') or '')
    if not eid or not re.search(r'(?:-2:|:)'+re.escape(eid)+r'(?:\?.*)?$',url):
        return ('EVENT_ID_MISMATCH', 0, 'Event ID y URL de Bwin no concuerdan')
    if (preflight.get('quality_gate') or {}).get('join_status') != 'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF':
        return ('DATA_JOIN_NOT_VERIFIED', 0, 'No consta join estadístico verificado')
    captured = parse_dt(snap.get('capture_declared_madrid'))
    kickoff = parse_dt(fixture.get('kickoff_madrid'))
    if not captured or not kickoff or captured >= kickoff:
        return ('INVALID_CAPTURE_TIMESTAMP', 0, 'Captura ausente, o posterior al kickoff')
    age = (asof-captured).total_seconds()/60
    if age < 0:
        return ('FUTURE_CAPTURE_TIMESTAMP', 0, 'Marca de captura posterior al tiempo de auditoría')
    if age > max_age_minutes:
        return ('STALE_SNAPSHOT', 0, f'Captura antigua: {age:.1f} minutos')
    snapshot_path = Path(str(snap.get('snapshot_path') or ''))
    if not snapshot_path.is_file():
        return ('SNAPSHOT_FILE_NOT_ACCESSIBLE', 0, 'No se puede verificar SHA del TXT (archivo no accesible)')
    if sha256(snapshot_path) != snap.get('sha256'):
        return ('SNAPSHOT_SHA256_MISMATCH', 0, 'El TXT cambió desde el preflight')
    txt_header = snapshot_path.read_text(encoding='utf-8-sig', errors='replace')[:1400]
    event_header = re.search(r'(?m)^Event ID:\s*(\d+)\s*$', txt_header)
    if not event_header or event_header.group(1)!=eid:
        return ('SNAPSHOT_EVENT_ID_CONFLICT',0,'Event ID del TXT no coincide')
    stats = ((preflight.get('quality_gate') or {}).get('market_kinds') or {}).get(kind) or {}
    count = int(stats.get('PRICE_ELIGIBLE') or 0)
    if count <= 0:
        return ('NO_PRICE_ELIGIBLE_MARKET', 0, 'No hay cuotas >= umbral en esta familia')
    if metric == 'on_target':
        return ('PLAYER_PROPS_NOT_TEAM_MARKET', 0,
                'Tiros a puerta Bwin capturados son props de jugador; no equivalen a tiros de equipo')
    return ('MARKET_FAMILY_PRICE_ELIGIBLE', count,
            'Cuotas por familia verificadas, NO una selección ni un precio actual asegurado')


def as_record(fixture, side, metric, series, market):
    own = series[f'{side}_own']
    opponent_side = 'away' if side == 'home' else 'home'
    conceded = series[f'{opponent_side}_conceded']
    team_conceded = series[f'{side}_conceded']
    opponent_own = series[f'{opponent_side}_own']
    name = fixture['home'] if side == 'home' else fixture['away']
    opp = fixture['away'] if side == 'home' else fixture['home']
    nums = [own['mean'], conceded['mean'], team_conceded['mean'], opponent_own['mean']]
    have_all = all(x is not None for x in nums)
    pair_n = min(own['n'], conceded['n'])
    status = ('SIN_DATOS' if not have_all or pair_n < 4 else
              'MUESTRA_PARCIAL' if min(own['n'], conceded['n'], opponent_own['n'], team_conceded['n']) < 8 else
              'MUESTRA_DESCRIPTIVA_OK')
    midpoint = round((own['mean']+conceded['mean'])/2, 3) if have_all else None
    opposite = round((opponent_own['mean']+team_conceded['mean'])/2, 3) if have_all else None
    diff = None
    if midpoint is not None and opposite is not None and opposite > 0:
        diff = round(100*(midpoint/opposite-1), 1)
    alignment = None
    if have_all:
        denom = max(own['mean'],conceded['mean'])
        alignment = round(min(own['mean'], conceded['mean'])/denom,3) if denom else 1.0
    cvs = [c for c in (own['cv'], conceded['cv']) if c is not None]
    volatility = ('DESCONOCIDA' if len(cvs)<2 else 'ELEVADA' if max(cvs)>=0.80 else
                  'MODERADA' if max(cvs)>=0.50 else 'MENOR')
    # No afirma ventaja absoluta: solo diferencia entre DOS tendencias del mismo partido.
    flag='SIN_EVIDENCIA_COMPARABLE'
    if status == 'MUESTRA_DESCRIPTIVA_OK' and diff is not None:
        flag = ('ASIMETRIA_RELATIVA_ALTA' if diff>=30 else
                'ASIMETRIA_INVERSA' if diff<=-23 else 'SIN_ASIMETRIA_CLARA')
    elif status == 'MUESTRA_PARCIAL':
        flag = 'PATRON_A_CONFIRMAR'
    notes = [
        'Índice de matchup = media(serie propia, concesión rival); NO es proyección calibrada',
        'Muestras agregadas sin ajuste por rival, localía ni composición actual del XI',
    ]
    if volatility=='ELEVADA':notes.append('Alta variabilidad histórica')
    if status!='MUESTRA_DESCRIPTIVA_OK':notes.append('Muestra insuficiente para señal descriptiva estable')
    if metric=='offsides':notes.append('Los offsides NO acreditan línea defensiva alta en metros')
    if metric=='yellow':notes.append('No incluye historial del árbitro')
    if metric=='xg':notes.append('xG depende de cobertura del proveedor; no equivale a goles observados')
    if market[0]!='MARKET_FAMILY_PRICE_ELIGIBLE':notes.append(f'Evidencia Bwin: {market[0]}')
    return {
        'fixture_id': fixture['fixture_id'],'home':fixture['home'],'away':fixture['away'],
        'kickoff_madrid':fixture['kickoff_madrid'],'team':name,'opponent':opp,
        'team_side':side,'metric':metric,'metric_label':METRICS[metric]['name'],
        'status':status,'own_mean':own['mean'], 'opponent_conceded_mean':conceded['mean'],
        'matchup_descriptive_midpoint':midpoint,'team_conceded_mean':team_conceded['mean'],
        'opponent_own_mean':opponent_own['mean'],'opponent_midpoint':opposite,
        'relative_asymmetry_pct':diff,'own_n':own['n'],
        'opponent_conceded_n':conceded['n'], 'own_cv':own['cv'],
        'opponent_conceded_cv':conceded['cv'],'volatility':volatility,
        'alignment':alignment,'pattern_flag':flag,'bwin_market_status':market[0],
        'bwin_price_eligible_count':market[1],'cautions':' | '.join(notes),
    }


def load_preflights(paths):
    result={}
    for path in paths:
        data=read_json(path)
        if not isinstance(data.get('matches'),list):
            raise ValueError(f'Preflight sin matches[]: {path}')
        for match in data['matches']:
            fid = int(match['fixture_id'])
            if fid in result:
                raise ValueError(f'Preflight duplicado fixture_id={fid}. Adjunta solo uno por evento')
            match=dict(match)
            match['_report_generated_at']=data.get('generated_at_utc')
            result[fid]=match
    return result


def select_candidates(source, ids, max_matches, asof, min_lead):
    all_matches=source.get('matches')
    if not isinstance(all_matches,list):
        raise ValueError('Preselection debe contener matches[]')
    byid={}
    for m in all_matches:
        fid = int(m['fixture_id'])
        if fid in byid:raise ValueError(f'fixture_id duplicado {fid}')
        byid[fid]=m
    if ids:
        missing=[fid for fid in ids if fid not in byid]
        if missing:raise ValueError(f'IDs ausentes de preselection: {missing}')
        pool=[byid[fid] for fid in dict.fromkeys(ids)]
    else:
        pool=sorted(byid.values(),key=lambda m:(-float(m.get('research_priority_score') or 0),int(m['fixture_id'])))
    selected=[]
    blocked=[]
    for m in pool:
        fid=m['fixture_id']
        kickoff=parse_dt(m.get('kickoff_madrid'))
        cutoff=parse_dt(m.get('analysis_cutoff_utc'))
        if not kickoff or (cutoff and cutoff>=kickoff):
            blocked.append({'fixture_id':fid,'reason':'INVALID_KICKOFF_OR_DATA_LEAKAGE'});continue
        if cutoff and cutoff>asof:
            blocked.append({'fixture_id':fid,'reason':'DATA_CUTOFF_AFTER_ASOF'});continue
        if (kickoff-asof).total_seconds() < min_lead*60:
            blocked.append({'fixture_id':fid,'reason':'PAST_OR_TOO_CLOSE_TO_KICKOFF'});continue
        selected.append(m)
        if len(selected)>=max_matches:break
    return selected,blocked


def analyze(preselection_path, preflight_paths, fixture_ids, max_matches, asof, min_lead=20,
            max_snapshot_age=120):
    src=read_json(preselection_path)
    gen=parse_dt(src.get('generated_at_utc'))
    if gen is not None and gen>asof:
        raise ValueError('Preselection se generó después de --asof: auditoría retrospectiva imposible sin filtración')
    preflights=load_preflights(preflight_paths)
    matches, blocked=select_candidates(src,fixture_ids,max_matches,asof,min_lead)
    rows=[];fixtures=[]
    for m in matches:
        fid=int(m['fixture_id']); p=preflights.get(fid)
        first=len(rows)
        for metric in METRICS:
            series=source_sides(m,metric)
            market=market_evidence(p,m,metric,asof,max_snapshot_age)
            for side in ('home','away'):
                rows.append(as_record(m,side,metric,series,market))
        related=rows[first:]
        flags=[r for r in related if r['pattern_flag'] in ('ASIMETRIA_RELATIVA_ALTA',)]
        markets=sorted(set(r['metric'] for r in related if r['bwin_market_status']=='MARKET_FAMILY_PRICE_ELIGIBLE'))
        fixtures.append({
            'fixture_id':fid,'home':m['home'],'away':m['away'],
            'kickoff_madrid':m['kickoff_madrid'],'research_priority_score':m.get('research_priority_score'),
            'data_readiness_score':m.get('data_readiness_score'),
            'details_checked':bool(m.get('details_checked')),
            'pattern_hypotheses_count':len(flags),
            'pattern_hypotheses_distinct_metrics':len(set(r['metric'] for r in flags)),
            'verified_price_market_metrics':markets,
            'preflight_status':p.get('status') if p else 'NOT_PROVIDED',
            'note':'Hipótesis descriptivas, no oportunidades verificadas ni EV',
        })
    return {
        'schema_version':VERSION,'asof_madrid':asof.astimezone(MADRID).isoformat(),
        'prematch_only':True,'data_sources':{
            'preselection':str(preselection_path),'preselection_sha256':sha256(preselection_path),
            'preflights':[{'path':str(p),'sha256':sha256(p)} for p in preflight_paths]},
        'methodology':{
            'available_metrics':list(METRICS),
            'midpoint':'(media a favor del equipo + media concedida por rival)/2; SOLO descriptivo',
            'relative_asymmetry':'comparación de índices descriptivos de los dos equipos, no línea de apuesta',
            'sample_requirement':'mínimo 8 observaciones por cada una de las 4 series para hipótesis de asimetría',
            'missing':'null no se interpreta como cero',
            'not_implemented':['probabilidades','EV','calibración','precios justos','lesiones','árbitros','jugadores','PPDA','altura defensiva en metros','datos tracking X/Y','causalidad táctica'],
            'no_pick':'Ninguna señal constituye un pick ni justifica ejecutar Deep Research por sí sola',
        },
        'counts':{'fixtures_processed':len(matches),'pattern_rows':len(rows),
                  'hypotheses_descriptive':sum(r['pattern_flag'] in ('ASIMETRIA_RELATIVA_ALTA',) for r in rows),
                  'bwin_families_price_evidenced':sum(r['bwin_market_status']=='MARKET_FAMILY_PRICE_ELIGIBLE' for r in rows),
                  'blocked_candidates':len(blocked)},
        'fixtures':fixtures,'patterns':rows,'blocked':blocked,
    }


def format_report(data):
    counts=data['counts']
    out=[f'# PREMATCH Pattern Engine {VERSION}', '',
         f"Fecha de referencia Madrid: `{data['asof_madrid']}`",'',
         '**Informe exploratorio: NO contiene picks, EV ni probabilidades.**','',
         f"Partidos procesados: **{counts['fixtures_processed']}** · Filas de patrones: **{counts['pattern_rows']}** · Hipótesis descriptivas: **{counts['hypotheses_descriptive']}**",'',
         '| Partido | Fixture API | Hipótesis descriptivas | Métricas con Bwin verificado |',
         '|---|---:|---:|---|']
    for m in data['fixtures']:
        out.append(f"| {m['home']} – {m['away']} | {m['fixture_id']} | {m['pattern_hypotheses_count']} | {', '.join(m['verified_price_market_metrics']) or 'No acreditado'} |")
    out.extend(['','## Hipótesis a contrastar',''])
    found=[p for p in data['patterns'] if p['pattern_flag'] in ('ASIMETRIA_RELATIVA_ALTA',)]
    for p in found[:40]:
        out.append(f"- **{p['home']}–{p['away']}**: {p['metric_label']}, {p['team']}; índice descriptivo {p['matchup_descriptive_midpoint']} frente a {p['opponent_midpoint']} del rival; n≥8 en las cuatro series. Volatilidad: {p['volatility']}; Bwin: `{p['bwin_market_status']}`. No implica cuota de valor.")
    if not found:out.append('No se detectaron asimetrías descriptivas con las muestras disponibles.')
    out.extend(['','## Limitaciones y siguiente fase','',
                'El índice no incluye ajuste de calidad de adversarios, estilos, localía, entrenador, árbitro ni XI. No se debe usar para estimar la probabilidad de superar una línea.',
                'No se puede deducir altura defensiva, presión PPDA ni tracking X/Y de las posiciones de la cuadrícula de alineaciones.',
                'Siguiente fase: historial por equipo y árbitro ajustado, modelos por línea Bwin, incertidumbre y auditoría contextual de hipótesis.',
                ''])
    return '\n'.join(out)


def main(argv=None):
    ap=argparse.ArgumentParser(description='Patrones descriptivos PREMATCH en preselection V1.3; cero redes y cero picks.')
    ap.add_argument('--preselection',required=True,type=Path)
    ap.add_argument('--preflight',action='append',default=[],type=Path,help='Repetible, un preflight por fixture')
    ap.add_argument('--fixture-id',action='append',type=int,default=[],help='Repetible: restringe a fixtures concretos')
    ap.add_argument('--top',type=int,default=15,help='Max fixtures desde ranking de preselección (por defecto 15)')
    ap.add_argument('--min-lead-minutes',type=int,default=20)
    ap.add_argument('--max-snapshot-age-minutes',type=int,default=120)
    ap.add_argument('--asof',help='ISO con offset. Para auditoría histórica; nunca convierte cuotas históricas en vigentes')
    ap.add_argument('--output-dir',type=Path,required=True)
    args=ap.parse_args(argv)
    if args.top<1 or args.min_lead_minutes<0 or args.max_snapshot_age_minutes<0:
        ap.error('Umbrales numéricos fuera de rango')
    asof=parse_dt(args.asof) if args.asof else datetime.now(timezone.utc)
    if asof is None:ap.error('--asof requiere fecha ISO8601 con offset explícito')
    try:
        result=analyze(args.preselection,args.preflight,args.fixture_id,args.top,asof,
                       args.min_lead_minutes,args.max_snapshot_age_minutes)
        out=args.output_dir
        out.mkdir(parents=True,exist_ok=True)
        (out/'pattern_engine.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        with (out/'pattern_evidence.csv').open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=CSV_FIELDS,extrasaction='ignore'); writer.writeheader(); writer.writerows(result['patterns'])
        (out/'pattern_report.md').write_text(format_report(result),encoding='utf-8')
        # Entrega compacta para IA posterior; sin inventar contexto ni pasar 188 fixtures innecesariamente.
        brief={'schema_version':VERSION,'asof_madrid':result['asof_madrid'],
               'fixture_summaries':result['fixtures'],
               'hypotheses':[p for p in result['patterns'] if p['pattern_flag'] in ('ASIMETRIA_RELATIVA_ALTA',)],
               'instructions':'Auditar hipótesis; NO inventar lesiones, PPDA, tracking, probabilidades o picks; requerir fuentes fechadas y enfrentamientos contrarios.'}
        (out/'llm_pattern_brief.json').write_text(json.dumps(brief,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        ap.exit(2,f'ERROR: {exc}\n')
    print(f"Pattern Engine {VERSION}: partidos={result['counts']['fixtures_processed']} | filas={result['counts']['pattern_rows']} | hipótesis descriptivas={result['counts']['hypotheses_descriptive']}")
    print(f"Bwin familias verificadas en filas={result['counts']['bwin_families_price_evidenced']} | bloqueados={result['counts']['blocked_candidates']}")
    print(f"Archivos: {args.output_dir}/pattern_engine.json, pattern_evidence.csv, pattern_report.md, llm_pattern_brief.json")
    if not result['fixtures']:
        print('AVISO: No hay fixtures prepartido en la fecha de referencia; cero picks o investigaciones permitidas.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
