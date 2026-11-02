#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Puerta de calidad para cuotas Bwin PREMATCH. No estima probabilidades ni EV.

Entrada: lista (mercado, selección, cuota) de la captura + registro API-Football
        de preselection V1.3 del mismo fixture.
Salida: estados independientes de precio, cobertura por familia, y controles
        de modelado. Nunca altera los datos originales.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import re
import unicodedata

VERSION = '0.1-market-quality-gate'
QUALITY_STATUSES = ('DATA_READY_FOR_RESEARCH','PARTIAL_DATA','INSUFFICIENT_DATA',
                    'DETAIL_NOT_EVALUATED','NEEDS_MODEL_OR_DISTRIBUTION',
                    'NEEDS_PERIOD_SPLIT','PLAYER_DATA_AND_XI_REQUIRED',
                    'NO_STATISTICAL_SOURCE','IDENTITY_MISMATCH')


def normalized_name(value):
    s=unicodedata.normalize('NFKD',str(value or ''))
    return ''.join(x for x in s if not unicodedata.combining(x)).casefold().strip()


def load_preselection(path:Path):
    obj=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(obj.get('matches'),list):
        raise ValueError('La preselección no contiene matches[]')
    index={}
    for row in obj['matches']:
        fid=row.get('fixture_id')
        if fid is not None:
            fid=int(fid)
            if fid in index:raise ValueError(f'fixture_id duplicado: {fid}')
            index[fid]=row
    return obj,index


def market_kind(market):
    n=normalized_name(market)
    if 'crea tus apuestas' in n or (' y ' in n and any(x in n for x in ('ambos equipos marcan','total de goles'))):
        return 'combined_or_builder'
    if 'corner' in n or 'corners' in n or 'saque de esquina' in n: return 'corners'
    if 'tarjeta' in n or 'amonestacion' in n: return 'cards'
    if 'fuera de juego' in n or 'offside' in n: return 'offsides'
    if 'falta' in n: return 'fouls'
    if 'tiros a porteria' in n or 'tiros a puerta' in n or 'remates a puerta' in n: return 'shots_on_target'
    if 'tiro' in n or 'remate' in n: return 'shots'
    if 'asistencia' in n: return 'assists'
    if 'goleadores' in n or 'goleador' in n: return 'goalscorers'
    if 'marcador exacto' in n: return 'correct_score'
    if 'handicap' in n: return 'handicap'
    if 'descanso/final' in n or 'descanso o final' in n: return 'half_full_time'
    if '1er equipo en marcar' in n or 'primer equipo en marcar' in n: return 'first_team_to_score'
    if 'ambos equipos marcan' in n: return 'both_teams_to_score'
    if 'total de goles' in n: return 'total_goals'
    if 'doble oportunidad' in n: return 'double_chance'
    if 'resultado' in n or 'ganador' in n: return 'result'
    return 'other'


def is_period(market):
    n=normalized_name(market)
    return any(x in n for x in ('1a parte','2a parte','primer tiempo','segundo tiempo','1er tiempo','2do tiempo'))


def is_player_market(market, kind):
    n=normalized_name(market)
    return kind in ('goalscorers','assists') or 'jugador' in n or 'recibe una tarjeta' in n


def market_data_status(market, kind, statistical_row, identity_ok):
    """Separa SUFICIENCIA DE DATOS del mercado de una probabilidad predicha.

    La evidencia de totales NO se traslada automáticamente a props de jugador,
    parciales, combinadas o marcadores exactos.
    """
    if not identity_ok:return ('IDENTITY_MISMATCH',None,'Identidad entre discovery y preselection no coincide')
    if statistical_row is None:return ('NO_STATISTICAL_SOURCE',None,'Adjunta --preselection para cruzar datos')
    if not statistical_row.get('details_checked'):
        return ('DETAIL_NOT_EVALUATED',None,'Estadísticas detalladas de este fixture pendientes')
    if is_player_market(market,kind):
        return ('PLAYER_DATA_AND_XI_REQUIRED',None,'Falta distribución individual y confirmación de XI/rol')
    if kind=='combined_or_builder':
        return ('NEEDS_MODEL_OR_DISTRIBUTION',None,'Mercado combinado: requiere distribución conjunta y dependencias')
    if kind in ('correct_score','result','double_chance','handicap','half_full_time',
                'first_team_to_score','both_teams_to_score'):
        return ('NEEDS_MODEL_OR_DISTRIBUTION',None,'Los promedios no modelan probabilidades de este mercado')
    if kind=='other':
        return ('NEEDS_MODEL_OR_DISTRIBUTION',None,'Mercado sin mapeo estadístico específico')
    if is_period(market):
        return ('NEEDS_PERIOD_SPLIT',None,'Preselección contiene agregados FT; no hay distribución por mitad')
    mapping={'corners':'corners','cards':'yellow','fouls':'fouls',
             'offsides':'offsides','shots':'shots','shots_on_target':'on_target','total_goals':'GOAL_HISTORY'}
    profile_name=mapping.get(kind)
    if not profile_name:return ('NEEDS_MODEL_OR_DISTRIBUTION',None,'No hay modelo estadístico específico')
    if profile_name=='GOAL_HISTORY':
        h=(statistical_row.get('home_evidence') or {}).get('history') or {}
        a=(statistical_row.get('away_evidence') or {}).get('history') or {}
        ns=[]
        for side in (h,a):
            for metric in ('same_competition_goals_for','same_competition_goals_against'):
                n=(side.get(metric) or {}).get('n')
                if not isinstance(n,(int,float)): n=0
                ns.append(int(n))
        if min(ns)>=8:
            return ('DATA_READY_FOR_RESEARCH','GOAL_HISTORY',f'Históricos goles misma liga: mínimos {min(ns)} por serie; no demuestra EV')
        if min(ns)>=4:
            return ('PARTIAL_DATA','GOAL_HISTORY',f'Históricos goles misma liga incompletos: mínimo {min(ns)} por serie')
        return ('INSUFFICIENT_DATA','GOAL_HISTORY',f'Históricos goles misma liga escasos: mínimo {min(ns)} por serie')
    profile=(statistical_row.get('market_profiles') or {}).get(profile_name) or {}
    klass=profile.get('coverage_class')
    label={'SUFICIENTE':'DATA_READY_FOR_RESEARCH','PARCIAL':'PARTIAL_DATA','ESCASA':'INSUFFICIENT_DATA'}.get(klass,'INSUFFICIENT_DATA')
    ns=profile.get('n_series') or {}
    min_n=min([int(v) for v in ns.values() if isinstance(v,(int,float))],default=0)
    return (label,profile_name,f'Cobertura {klass or "NO CLASIFICADA"}; mínimo n={min_n} en series; no representa probabilidad de acierto')


def check_join(discovery_row, stats_row):
    if stats_row is None:return False,'FIXTURE_ID_NOT_IN_PRESELECTION'
    if not discovery_row:return False,'NO_DISCOVERY_MATCH'
    for field in ('fixture_id','home','away','gender'):
        a=discovery_row.get(field);b=stats_row.get(field)
        if field=='fixture_id':
            if str(a)!=str(b):return False,'FIXTURE_ID_MISMATCH'
        elif field=='gender':
            if a and b and normalized_name(a)!=normalized_name(b):return False,'GENDER_MISMATCH'
        elif normalized_name(a)!=normalized_name(b):return False,field.upper()+'_MISMATCH'
    ka=discovery_row.get('kickoff_madrid');kb=stats_row.get('kickoff_madrid')
    try:
        da=datetime.fromisoformat(ka);db=datetime.fromisoformat(kb)
        if not da.tzinfo or not db.tzinfo or abs((da-db).total_seconds())>180:
            return False,'KICKOFF_MISMATCH'
    except (ValueError,TypeError):return False,'KICKOFF_MISSING_OR_INVALID'
    return True,'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF'


def analyze_rows(rows,discovery_row=None,stats_row=None,*,min_candidate=Decimal('1.20'),min_observation=Decimal('1.10')):
    min_candidate=Decimal(str(min_candidate)); min_observation=Decimal(str(min_observation))
    if not (Decimal('1')<min_observation<=min_candidate):
        raise ValueError('Umbrales inválidos: 1 < min-observation <= min-candidate')
    join_ok,join_status=check_join(discovery_row,stats_row) if stats_row is not None else (True,'NO_PRESELECTION_SUPPLIED')
    results=[]
    tiers=Counter();data_statuses=Counter();kinds=defaultdict(lambda:Counter())
    for record in rows:
        try:odd=Decimal(str(record['odd']))
        except (InvalidOperation,KeyError):raise ValueError('Cuota inválida en filas parseadas')
        if odd<=Decimal('1') or not odd.is_finite():raise ValueError('Cuota debe ser >1')
        if odd<min_observation: tier='BELOW_OBSERVATION'
        elif odd<min_candidate:tier='LOW_ODD_OBSERVATION'
        else:tier='PRICE_ELIGIBLE'
        kind=market_kind(record['market'])
        status,source,note=market_data_status(record['market'],kind,stats_row,join_ok)
        decision=('PRICE_FILTERED' if tier=='BELOW_OBSERVATION' else
                  'LOW_ODD_OBSERVATION' if tier=='LOW_ODD_OBSERVATION' else
                  'READY_FOR_RESEARCH' if status=='DATA_READY_FOR_RESEARCH' else
                  'PENDING_EVIDENCE')
        obj={'market':record['market'],'selection':record['selection'],
             'odd':str(odd),'market_kind':kind,'price_tier':tier,
             'data_status':status,'data_source':source or '',
             'decision':decision,'rationale':note}
        results.append(obj);tiers[tier]+=1;data_statuses[status]+=1
        kinds[kind]['total']+=1;kinds[kind][tier]+=1
        if decision=='READY_FOR_RESEARCH':kinds[kind]['researchable']+=1
    summary={'schema_version':VERSION,'minimum_candidate_odd':str(min_candidate),
             'minimum_observation_odd':str(min_observation),'captured_valid_selections':len(results),
             'price_eligible':tiers['PRICE_ELIGIBLE'],
             'low_odd_observation':tiers['LOW_ODD_OBSERVATION'],
             'below_observation':tiers['BELOW_OBSERVATION'],
             'researchable_after_data_gate':sum(r['decision']=='READY_FOR_RESEARCH' for r in results),
             'price_eligible_pending_evidence':sum(r['decision']=='PENDING_EVIDENCE' for r in results),
             'join_status':join_status,'statistical_source_present':stats_row is not None,
             'data_status_counts':dict(sorted(data_statuses.items())),
             'market_kinds':{k:dict(v) for k,v in sorted(kinds.items())},
             'note':'READY_FOR_RESEARCH NO equivale a pick, EV positivo o modelo probabilístico validado.'}
    return summary, results
