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

VERSION = '0.2-market-quality-gate'
QUALITY_STATUSES = ('DATA_READY_FOR_RESEARCH','PARTIAL_DATA','INSUFFICIENT_DATA',
                    'DETAIL_NOT_EVALUATED','NEEDS_MODEL_OR_DISTRIBUTION',
                    'NEEDS_PERIOD_SPLIT','PLAYER_DATA_AND_XI_REQUIRED',
                    'NO_STATISTICAL_SOURCE','IDENTITY_MISMATCH')

EPISTEMIC_EVALUATION_STATES = (
    'NO_MARKET_CAPTURED',
    'MARKET_CAPTURED_UNMAPPED',
    'INSUFFICIENT_STATISTICAL_EVIDENCE',
    'MODEL_UNAVAILABLE',
    'MODEL_EVALUATION_PENDING',
    'MODEL_EVALUATED_NO_VALUE',
    'MODEL_EVALUATED_VALUE_CANDIDATE',
)

MONITORED_FAMILIES = (
    'result',
    'double_chance',
    'both_teams_to_score',
    'total_goals',
    'corners',
    'cards',
    'shots',
    'shots_on_target',
    'fouls',
    'offsides',
)

_HALF_LINE_SEL_RE = re.compile(r'^(?:mas|menos)(?:\s+de)?\s+\d+(?:[\.,]5)$')


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
    if 'asistencia' in n: return 'assists'
    if 'goleador' in n or 'jugador' in n: return 'goalscorers'
    if 'crea tus apuestas' in n or (' y ' in n and any(x in n for x in (
            'ambos equipos marcan','total de goles','goles totales','multigol',
            'cualquier equipo gana','resultado del partido y','doble oportunidad y','gana y ambos'))):
        return 'combined_or_builder'
    if 'total de goles entre el' in n or 'mas goles - entre el' in n or 'entre el min.' in n:
        return 'combined_or_builder'
    if 'corner' in n or 'corners' in n or 'saque de esquina' in n: return 'corners'
    if 'tarjeta' in n or 'amonestacion' in n: return 'cards'
    if 'fuera de juego' in n or 'offside' in n: return 'offsides'
    if 'falta' in n: return 'fouls'
    if 'tiros a porteria' in n or 'tiros a puerta' in n or 'remates a puerta' in n: return 'shots_on_target'
    if 'tiro' in n or 'remate' in n: return 'shots'
    if 'marcador exacto' in n: return 'correct_score'
    if 'handicap' in n: return 'handicap'
    if 'descanso/final' in n or 'descanso o final' in n: return 'half_full_time'
    if '1er equipo en marcar' in n or 'primer equipo en marcar' in n: return 'first_team_to_score'
    if 'ambos equipos marcan' in n: return 'both_teams_to_score'
    if 'doble oportunidad' in n: return 'double_chance'
    if ('total de goles' in n or 'goles totales' in n or 'multigol' in n
            or 'habra gol' in n or 'goles se marcan' in n or 'marca en ambas partes' in n
            or bool(re.search(r'(?:^|\s-\s*)marca$', n))):
        return 'total_goals'
    if any(tok in n for tok in (
            'resultado','ganador','1x2','no cuenta','gana sin recibir goles',
            'porteria a cero','margen de victoria','gana ambas partes',
            'empieza perdiendo','gana al descanso','desarrollo del partido',
            'gana la 1','cualquier equipo gana','mas goles')):
        return 'result'
    return 'other'


def is_period(market):
    n=normalized_name(market)
    return any(x in n for x in ('1a parte','2a parte','primer tiempo','segundo tiempo','1er tiempo','2do tiempo'))


def is_player_market(market, kind):
    n=normalized_name(market)
    return kind in ('goalscorers','assists') or 'jugador' in n or 'goleador' in n or 'recibe una tarjeta' in n


def _is_half_line_selection(selection):
    if selection is None:
        return True
    return bool(_HALF_LINE_SEL_RE.match(normalized_name(selection)))


def _goal_history_readiness(statistical_row):
    h=(statistical_row.get('home_evidence') or {}).get('history') or {}
    a=(statistical_row.get('away_evidence') or {}).get('history') or {}
    sc_ns=[]; all_ns=[]
    for side in (h,a):
        for metric in ('same_competition_goals_for','same_competition_goals_against'):
            n=(side.get(metric) or {}).get('n')
            sc_ns.append(int(n) if isinstance(n,(int,float)) else 0)
        for metric in ('goals_for','goals_against'):
            n=(side.get(metric) or {}).get('n')
            if isinstance(n,(int,float)):
                all_ns.append(int(n))
    sc_min=min(sc_ns) if sc_ns else 0
    all_min=min(all_ns) if len(all_ns)==4 else None
    if sc_min>=8:
        return ('DATA_READY_FOR_RESEARCH','GOAL_HISTORY',sc_min,all_min,
                f'Históricos goles misma liga: mínimos {sc_min} por serie; no demuestra EV')
    if sc_min>=4 and all_min is not None and all_min>=12:
        return ('DATA_READY_FOR_RESEARCH','GOAL_HISTORY_MIXED_COMPETITION',sc_min,all_min,
                f'Históricos goles (muestra mixta liga+competición: misma competición mín {sc_min}, historial completo mín {all_min}); no demuestra EV')
    if sc_min>=4 or (all_min is not None and all_min>=8):
        return ('PARTIAL_DATA','GOAL_HISTORY',sc_min,all_min,
                f'Históricos goles misma liga incompletos: mínimo {sc_min} por serie')
    return ('INSUFFICIENT_DATA','GOAL_HISTORY',sc_min,all_min,
            f'Históricos goles misma liga escasos: mínimo {sc_min} por serie')


def _model_availability(market, kind, selection=None):
    """Determina si bet_engine/markets/bwin_evaluator.py dispone de estimador explícito para este mercado."""
    n=normalized_name(market)
    is_h1=any(x in n for x in ('1a parte','primer tiempo','1er tiempo'))
    is_h2=any(x in n for x in ('2a parte','segundo tiempo','2do tiempo'))
    is_interval=('entre el ' in n or 'entre el min.' in n or 'al min.' in n or 'parte con mas' in n
                 or 'parte con más' in n or '1° corner' in n or '1o corner' in n)
    if is_h2 or is_interval:
        return (False, None, False)
    if kind=='total_goals':
        if 'total de goles' in n and not any(tok in n for tok in ('goles totales','par/impar','multigol','(0-')):
            if not is_h1 and _is_half_line_selection(selection):
                return (True, 'GOALS_FT', False)
            if is_h1 and _is_half_line_selection(selection):
                return (True, 'GOALS_H1', True)
        return (False, None, False)
    if kind in ('corners','cards','fouls','shots','shots_on_target'):
        stat_map={'corners':'CORNERS','cards':'YELLOW_CARDS','fouls':'FOULS','shots':'SHOTS','shots_on_target':'ON_TARGET'}
        if not is_h1 and 'total de ' in n and not any(tok in n for tok in ('par/impar','(0-','parte con')):
            if _is_half_line_selection(selection):
                return (True, stat_map[kind], False)
        return (False, None, False)
    if kind=='result':
        if n.startswith('resultado del partido') or n=='resultado del partido':
            return (True, 'GOALS_H1' if is_h1 else 'GOALS_FT', is_h1)
        if n.startswith('ganador sin empate'):
            return (True, 'GOALS_H1' if is_h1 else 'GOALS_FT', is_h1)
        return (False, None, False)
    if kind=='double_chance':
        if n.startswith('doble oportunidad') and ' y ' not in n:
            return (True, 'GOALS_H1' if is_h1 else 'GOALS_FT', is_h1)
        return (False, None, False)
    if kind=='both_teams_to_score':
        if n in ('ambos equipos marcan | tiempo reglamentario','ambos equipos marcan','ambos equipos marcan | 1a parte'):
            return (True, 'GOALS_H1' if is_h1 else 'GOALS_FT', is_h1)
        return (False, None, False)
    if kind=='correct_score':
        if not is_h1 and 'multiple' not in n and n.startswith('marcador exacto'):
            return (True, 'GOALS_FT', False)
        if is_h1 and 'multiple' not in n and n.startswith('marcador exacto'):
            return (True, 'GOALS_H1', True)
        return (False, None, False)
    if kind=='handicap':
        if not is_h1 and n.startswith('handicap 1x2') and any(off in n for off in ('(-1)','(-2)','(+1)','(+2)')):
            return (True, 'GOALS_FT', False)
        return (False, None, False)
    if kind=='combined_or_builder':
        if not is_h1 and any(n.startswith(pfx) for pfx in (
                'resultado (1x2) y ambos equipos marcan',
                'ambos equipos marcan y total de goles 2,5',
                'ambos equipos marcan y cualquier equipo gana')):
            return (True, 'GOALS_FT', False)
        return (False, None, False)
    return (False, None, False)


def _classify_market_epistemics(market, kind, statistical_row, identity_ok, selection=None):
    status, source, note = market_data_status(market, kind, statistical_row, identity_ok, selection=selection)
    has_model, model_family, requires_h1 = _model_availability(market, kind, selection=selection)

    if not identity_ok or statistical_row is None or not statistical_row.get('details_checked'):
        ev_state = 'INSUFFICIENT_STATISTICAL_EVIDENCE'
        ev_status = 'NOT_EVALUATED_IDENTITY_OR_SOURCE'
        mod_status = 'MODEL_AVAILABLE_PENDING_RUN' if has_model else 'MODEL_UNAVAILABLE'
        return status, source, note, ev_status, mod_status, ev_state

    if is_player_market(market, kind):
        return (status, source, note, 'PLAYER_EXPOSURE_AND_XI_REQUIRED', 'MODEL_UNAVAILABLE',
                'INSUFFICIENT_STATISTICAL_EVIDENCE')

    if kind == 'other':
        return (status, source, note, 'NOT_APPLICABLE_UNMAPPED', 'MODEL_UNAVAILABLE',
                'MARKET_CAPTURED_UNMAPPED')

    if status == 'NEEDS_PERIOD_SPLIT':
        mod_st = 'MODEL_AVAILABLE_PENDING_RUN' if has_model else 'MODEL_UNAVAILABLE'
        ep_st = 'INSUFFICIENT_STATISTICAL_EVIDENCE' if requires_h1 else 'MODEL_UNAVAILABLE'
        return (status, source, note, 'PERIOD_SPLIT_REQUIRED', mod_st, ep_st)

    if kind in ('result', 'double_chance', 'both_teams_to_score', 'handicap', 'correct_score', 'combined_or_builder'):
        goal_status, goal_src, _, _, goal_note = _goal_history_readiness(statistical_row)
        if has_model and goal_status == 'DATA_READY_FOR_RESEARCH':
            return (status, source or goal_src,
                    f'Evidencia de goles lista ({goal_src}); requiere ejecutar modelo probabilístico ({model_family})',
                    'STATISTICAL_EVIDENCE_READY', 'MODEL_AVAILABLE_PENDING_RUN', 'MODEL_EVALUATION_PENDING')
        if has_model:
            return (status, goal_src, goal_note,
                    'INSUFFICIENT_STATISTICAL_EVIDENCE', 'MODEL_AVAILABLE_PENDING_RUN', 'INSUFFICIENT_STATISTICAL_EVIDENCE')
        return (status, source, note, 'NOT_APPLICABLE_UNMODELLED', 'MODEL_UNAVAILABLE', 'MODEL_UNAVAILABLE')

    if kind == 'total_goals' and not has_model:
        return (status, source, 'Estructura no soportada por el evaluador binario (.5); requiere modelo específico',
                'STATISTICAL_EVIDENCE_READY' if status == 'DATA_READY_FOR_RESEARCH' else 'INSUFFICIENT_STATISTICAL_EVIDENCE',
                'MODEL_UNAVAILABLE', 'MODEL_UNAVAILABLE')

    if kind == 'corners' and not has_model:
        return (status, source, 'Estructura de córners sin modelo binario (.5) específico',
                'STATISTICAL_EVIDENCE_READY' if status == 'DATA_READY_FOR_RESEARCH' else 'INSUFFICIENT_STATISTICAL_EVIDENCE',
                'MODEL_UNAVAILABLE', 'MODEL_UNAVAILABLE')

    if status == 'DATA_READY_FOR_RESEARCH':
        mod_st = 'MODEL_AVAILABLE_PENDING_RUN' if has_model else 'MODEL_UNAVAILABLE'
        ep_st = 'MODEL_EVALUATION_PENDING' if has_model else 'MODEL_UNAVAILABLE'
        return (status, source, note, 'STATISTICAL_EVIDENCE_READY', mod_st, ep_st)

    if status in ('PARTIAL_DATA', 'INSUFFICIENT_DATA'):
        mod_st = 'MODEL_AVAILABLE_PENDING_RUN' if has_model else 'MODEL_UNAVAILABLE'
        ep_st = 'INSUFFICIENT_STATISTICAL_EVIDENCE' if has_model else 'MODEL_UNAVAILABLE'
        return (status, source, note, 'INSUFFICIENT_STATISTICAL_EVIDENCE', mod_st, ep_st)

    mod_st = 'MODEL_AVAILABLE_PENDING_RUN' if has_model else 'MODEL_UNAVAILABLE'
    return (status, source, note, 'INSUFFICIENT_STATISTICAL_EVIDENCE', mod_st, 'MODEL_UNAVAILABLE')


def market_data_status(market, kind, statistical_row, identity_ok, *, selection=None):
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
        has_model, _, _ = _model_availability(market, kind, selection=selection)
        status, src, note = _goal_history_readiness(statistical_row)[:2] + (_goal_history_readiness(statistical_row)[4],)
        if not has_model:
            return ('NEEDS_MODEL_OR_DISTRIBUTION', src, 'Mercado de goles no binario (.5): requiere modelo específico')
        return (status, src, note)
    has_model, _, _ = _model_availability(market, kind, selection=selection)
    profile=(statistical_row.get('market_profiles') or {}).get(profile_name) or {}
    klass=profile.get('coverage_class')
    label={'SUFICIENTE':'DATA_READY_FOR_RESEARCH','PARCIAL':'PARTIAL_DATA','ESCASA':'INSUFFICIENT_DATA'}.get(klass,'INSUFFICIENT_DATA')
    ns=profile.get('n_series') or {}
    min_n=min([int(v) for v in ns.values() if isinstance(v,(int,float))],default=0)
    if not has_model:
        return ('NEEDS_MODEL_OR_DISTRIBUTION', profile_name, f'Cobertura {klass or "NO CLASIFICADA"} (mín n={min_n}), estructura sin modelo binario (.5)')
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
    tiers=Counter();data_statuses=Counter();eval_states=Counter();kinds=defaultdict(lambda:Counter())
    for record in rows:
        try:odd=Decimal(str(record['odd']))
        except (InvalidOperation,KeyError):raise ValueError('Cuota inválida en filas parseadas')
        if odd<=Decimal('1') or not odd.is_finite():raise ValueError('Cuota debe ser >1')
        if odd<min_observation: tier='BELOW_OBSERVATION'
        elif odd<min_candidate:tier='LOW_ODD_OBSERVATION'
        else:tier='PRICE_ELIGIBLE'
        kind=market_kind(record['market'])
        status,source,note,ev_status,mod_status,ep_state=_classify_market_epistemics(
            record['market'],kind,stats_row,join_ok,selection=record.get('selection'))
        decision=('PRICE_FILTERED' if tier=='BELOW_OBSERVATION' else
                  'LOW_ODD_OBSERVATION' if tier=='LOW_ODD_OBSERVATION' else
                  'READY_FOR_RESEARCH' if status=='DATA_READY_FOR_RESEARCH' else
                  'PENDING_EVIDENCE')
        obj={'market':record['market'],'selection':record['selection'],
             'odd':str(odd),'market_kind':kind,'price_tier':tier,
             'data_status':status,'data_source':source or '',
             'decision':decision,'rationale':note,
             'evidence_status':ev_status,'model_status':mod_status,'evaluation_state':ep_state}
        results.append(obj);tiers[tier]+=1;data_statuses[status]+=1;eval_states[ep_state]+=1
        kinds[kind]['total']+=1;kinds[kind][tier]+=1
        if decision=='READY_FOR_RESEARCH':kinds[kind]['researchable']+=1
    missing_families=[fam for fam in MONITORED_FAMILIES if kinds.get(fam,{}).get('total',0)==0]
    family_eval_states={fam:'NO_MARKET_CAPTURED' for fam in missing_families}
    for fam, info in kinds.items():
        fam_states=Counter(r['evaluation_state'] for r in results if r['market_kind']==fam)
        family_eval_states[fam]=fam_states.most_common(1)[0][0] if fam_states else 'NO_MARKET_CAPTURED'
    summary={'schema_version':VERSION,'minimum_candidate_odd':str(min_candidate),
             'minimum_observation_odd':str(min_observation),'captured_valid_selections':len(results),
             'price_eligible':tiers['PRICE_ELIGIBLE'],
             'low_odd_observation':tiers['LOW_ODD_OBSERVATION'],
             'below_observation':tiers['BELOW_OBSERVATION'],
             'researchable_after_data_gate':sum(r['decision']=='READY_FOR_RESEARCH' for r in results),
             'price_eligible_pending_evidence':sum(r['decision']=='PENDING_EVIDENCE' for r in results),
             'join_status':join_status,'statistical_source_present':stats_row is not None,
             'data_status_counts':dict(sorted(data_statuses.items())),
             'evaluation_state_counts':dict(sorted(eval_states.items())),
             'missing_market_families':missing_families,
             'family_evaluation_states':dict(sorted(family_eval_states.items())),
             'market_kinds':{k:dict(v) for k,v in sorted(kinds.items())},
             'note':'READY_FOR_RESEARCH y MODEL_EVALUATION_PENDING NO equivalen a pick, EV positivo ni NO BET estadístico.'}
    return summary, results
