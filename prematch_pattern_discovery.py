#!/usr/bin/env python3
"""Descubrimiento híbrido V2, determinista y offline para PREMATCH.

Genera cobertura observada, hipótesis deportivas y una cola de investigación
independiente de Bwin. Opcionalmente enlaza después un scanner ya existente,
sin convertir ninguna señal en pick, probabilidad, EV o stake.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

MADRID=ZoneInfo('Europe/Madrid')
MATRIX_SCHEMA='2.0-observed-coverage-matrix'
DISCOVERY_SCHEMA='2.0-hybrid-pattern-discovery'
QUEUE_SCHEMA='2.0-sport-research-queue'
LINK_SCHEMA='2.0-commercial-link-report'
METRICS=('goals','corners','shots','on_target','fouls','yellow','offsides','xg')
FAMILY={'goals':'goals','corners':'corners','shots':'shots','on_target':'shots',
        'fouls':'fouls','yellow':'cards','offsides':'offsides','xg':'goals'}
ASSOCIATIONS=(('shots','corners'),('blocked_shots','corners'),('fouls','yellow'))
UNSUPPORTED={
 'players':{'status':'NOT_IMPLEMENTED','missing':['player_id','minutes','lineup_status','player_statistics','literal_market_rule']},
 'periods':{'status':'NOT_IMPLEMENTED','missing':['period_splits','period_xg','settlement_rule']},
 'coaches':{'status':'NOT_AVAILABLE','missing':['current_coach_id','dated_regime','observed_tactical_claims']},
 'referees':{'status':'NOT_AVAILABLE','missing':['referee_id','comparable_history','assignment_asof']},
 'ppda':{'status':'NOT_IMPLEMENTED','missing':['event_or_tracking_data','possession_sequences']},
}


class DiscoveryError(RuntimeError):pass


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):
    value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict):raise DiscoveryError(f'JSON raíz inválida: {path}')
    return value
def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def dt(value):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError,TypeError):return None
def finite(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
def parse_asof(value):
    if value is None:return None
    parsed=dt(value)
    if parsed is None or parsed.tzinfo is None:raise DiscoveryError('ASOF_INVALID_OR_MISSING_OFFSET')
    return parsed
def stat(values):
    values=[float(v) for v in values if finite(v)]
    if not values:return {'n':0,'mean':None,'stdev':None,'cv':None,'min':None,'max':None}
    avg=mean(values);sd=pstdev(values)
    return {'n':len(values),'mean':round(avg,4),'stdev':round(sd,4),
            'cv':round(sd/abs(avg),4) if abs(avg)>1e-9 else (0.0 if sd==0 else None),
            'min':min(values),'max':max(values)}
def coverage(numerator,denominator):return round(100*numerator/denominator,2) if denominator else None
def loo(values):
    values=[float(v) for v in values if finite(v)]
    if len(values)<4:return None
    means=[mean(values[:i]+values[i+1:]) for i in range(len(values))]
    return [round(min(means),4),round(max(means),4)]
def pearson(pairs):
    pairs=[(float(a),float(b)) for a,b in pairs if finite(a) and finite(b)]
    if len(pairs)<4:return None
    av,bv=mean(a for a,_ in pairs),mean(b for _,b in pairs)
    den=math.sqrt(sum((a-av)**2 for a,_ in pairs)*sum((b-bv)**2 for _,b in pairs))
    return round(sum((a-av)*(b-bv) for a,b in pairs)/den,4) if den else None


def clean_observations(fixture,side,asof=None):
    cutoffs=[dt(fixture.get('analysis_cutoff_utc')),dt(fixture.get('kickoff_madrid')),asof]
    cutoffs=[x for x in cutoffs if x is not None]
    if len(cutoffs)<3:return []
    cutoff=min(cutoffs)
    team_id=fixture.get(side+'_id');seen={}
    for row in (fixture.get('statistical_observations') or {}).get(side,[]):
        if not isinstance(row,dict):continue
        at=dt(row.get('kickoff_utc'));fid=row.get('fixture_id')
        if (str(row.get('team_id'))!=str(team_id) or not at or at>=cutoff or
            row.get('status')!='FT' or row.get('league_id')!=fixture.get('league_id') or
            row.get('season')!=fixture.get('season') or fid is None):continue
        quality=sum(finite(v) for block in (row.get('own') or {},row.get('conceded') or {}) for v in block.values())
        old=seen.get(str(fid))
        if old is None or quality>old[0]:seen[str(fid)]=(quality,row)
    return [x[1] for x in sorted(seen.values(),key=lambda item:str(item[1].get('kickoff_utc','')),reverse=True)]


def request_state(row):
    explicit=row.get('statistics_request_status')
    if explicit in ('NOT_REQUESTED','REQUESTED_MISSING','VALID'):return explicit
    if row.get('statistics_requested'):return 'VALID' if row.get('own') or row.get('conceded') else 'REQUESTED_MISSING'
    return 'LEGACY_AMBIGUOUS_NOT_REQUESTED_OR_MISSING'


def build_coverage(pre,pre_sha,asof):
    buckets={}
    dedup=set()
    for fixture in pre.get('matches',[]):
        for side in ('home','away'):
            for row in clean_observations(fixture,side,asof):
                for source,block in (('TEAM_PRODUCTION',row.get('own') or {}),('OPPONENT_CONCESSION',row.get('conceded') or {})):
                    for metric in METRICS:
                        key=(fixture.get('league_id'),fixture.get('season'),fixture.get('gender'),metric,source)
                        record_key=key+(str(row.get('team_id')),str(row.get('fixture_id')))
                        if record_key in dedup:continue
                        dedup.add(record_key)
                        b=buckets.setdefault(key,{'history':set(),'requested':set(),'valid':set(),'missing':set(),'not_requested':set(),'unverifiable':set(),'reasons':Counter()})
                        fid=(str(row.get('team_id')),str(row.get('fixture_id')));b['history'].add(fid)
                        state=request_state(row);value=block.get(metric)
                        if metric=='goals':
                            if finite(value):b['valid'].add(fid)
                            else:b['missing'].add(fid);b['reasons']['GOAL_FIELD_MISSING']+=1
                        elif state=='NOT_REQUESTED':b['not_requested'].add(fid);b['reasons']['DETAIL_NOT_REQUESTED']+=1
                        elif state.startswith('LEGACY_AMBIGUOUS'):
                            b['unverifiable'].add(fid);b['reasons']['LEGACY_REQUEST_STATUS_UNVERIFIABLE']+=1
                        else:
                            b['requested'].add(fid)
                            if finite(value):b['valid'].add(fid)
                            else:b['missing'].add(fid);b['reasons']['FIELD_MISSING_IN_REQUESTED_RESPONSE']+=1
    rows=[]
    for key,b in sorted(buckets.items(),key=lambda x:tuple(str(v) for v in x[0])):
        lid,season,gender,metric,source=key
        requested=b['history'] if metric=='goals' else b['requested']
        rows.append({'league_id':lid,'season':season,'gender':gender,'metric':metric,'source_type':source,
            'history_available_n':len(b['history']),'details_requested_n':len(requested),
            'valid_observations_n':len(b['valid']),'missing_provider_n':len(b['missing']),
            'not_requested_n':len(b['not_requested']),'request_status_unverifiable_n':len(b['unverifiable']),
            'requested_coverage_pct':coverage(len(b['valid']),len(requested)),
            'effective_coverage_pct':coverage(len(b['valid']),len(b['history'])),
            'history_observations':sorted([{'team_id':x[0],'fixture_id':x[1]} for x in b['history']],key=lambda x:(x['team_id'],x['fixture_id'])),
            'history_fixture_ids':sorted([f'{x[0]}:{x[1]}' for x in b['history']]),'valid_fixture_ids':sorted([f'{x[0]}:{x[1]}' for x in b['valid']]),
            'missing_fixture_ids':sorted([f'{x[0]}:{x[1]}' for x in b['missing']]),'not_requested_fixture_ids':sorted([f'{x[0]}:{x[1]}' for x in b['not_requested']]),
            'request_status_unverifiable_fixture_ids':sorted([f'{x[0]}:{x[1]}' for x in b['unverifiable']]),
            'missing_reasons':dict(sorted(b['reasons'].items())),
            'scope':'OBSERVED_SAMPLE_ONLY_NOT_PROVIDER_WHITELIST'})
    by_metric=Counter(r['metric'] for r in rows);by_source=Counter(r['source_type'] for r in rows)
    return {'schema_version':MATRIX_SCHEMA,'asof_madrid':asof.astimezone(MADRID).isoformat(),
            'preselection_sha256':pre_sha,'rows':rows,
            'counts':{'rows':len(rows),'leagues':len({(r['league_id'],r['season'],r['gender']) for r in rows}),
                      'by_metric':dict(by_metric),'by_source_type':dict(by_source)},
            'denominator_policy':'null when denominator is zero; null is never zero','actionable_bet':False}


def series(rows,block,metric,venue=None):
    selected=[r for r in rows if venue is None or r.get('venue')==venue]
    values=[(r,(r.get(block) or {}).get(metric)) for r in selected]
    valid=[(r,float(v)) for r,v in values if finite(v)]
    requested=[r for r in selected if metric=='goals' or request_state(r) in ('VALID','REQUESTED_MISSING')]
    missing=[r for r in requested if not finite((r.get(block) or {}).get(metric))]
    return {'statistics':stat([v for _,v in valid]),'values':[v for _,v in valid],
            'fixture_ids':[r['fixture_id'] for r,_ in valid],'history_n':len(selected),
            'details_requested_n':len(requested),'missing_provider_n':len(missing),
            'not_requested_n':len(selected)-len(requested),
            'requested_coverage_pct':coverage(len(valid),len(requested)),
            'effective_coverage_pct':coverage(len(valid),len(selected)),'leave_one_out_mean_range':loo([v for _,v in valid])}


def base_pattern(fixture,side,metric,segment,mode,definition,own,opp,status,reasons,effect,counter):
    team=fixture.get(side);other='away' if side=='home' else 'home'
    return {'pattern_id':f"{fixture['fixture_id']}:{side}:{metric}:{mode}:{segment}",
      'fixture_id':fixture['fixture_id'],'home':fixture.get('home'),'away':fixture.get('away'),
      'team':team,'team_id':fixture.get(side+'_id'),'opponent':fixture.get(other),
      'team_side':side,'league_id':fixture.get('league_id'),'season':fixture.get('season'),
      'gender':fixture.get('gender'),'kickoff_madrid':fixture.get('kickoff_madrid'),
      'metric':metric,'family':FAMILY.get(metric,metric),'segment':segment,'definition':definition,
      'source_mode':'PRESELECTION_STATISTICAL_OBSERVATIONS','discovery_mode':mode,
      'validation_status':status,'validation_reasons':reasons,'n_own':own['statistics']['n'],
      'n_opponent_conceded':opp['statistics']['n'],'history_n':{'own':own['history_n'],'opponent':opp['history_n']},
      'details_requested_n':{'own':own['details_requested_n'],'opponent':opp['details_requested_n']},
      'coverage_requested_pct':{'own':own['requested_coverage_pct'],'opponent':opp['requested_coverage_pct']},
      'coverage_effective_pct':{'own':own['effective_coverage_pct'],'opponent':opp['effective_coverage_pct']},
      'own_statistics':own['statistics'],'opponent_conceded_statistics':opp['statistics'],
      'effect_descriptive':effect,'evidence_fixture_ids':{'own':own['fixture_ids'],'opponent':opp['fixture_ids']},
      'counterevidence':counter,'sensitivity':{'own_leave_one_out':own['leave_one_out_mean_range'],
                                             'opponent_leave_one_out':opp['leave_one_out_mean_range']},
      'sample_sufficiency':status not in ('INSUFFICIENT_DATA',),
      'coverage_sufficiency':all((v or 0)>=40 for v in (own['effective_coverage_pct'],opp['effective_coverage_pct'])),
      'effect_quantified':bool(effect and any(v is not None and v != 0 for k,v in effect.items() if 'difference' in k or k in ('pearson','home_minus_away_mean','recent_minus_previous_mean'))),
      'contradiction_status':'PRESENT' if counter else 'NONE',
      'stability_status':'AVAILABLE' if own['statistics'].get('cv') is not None and opp['statistics'].get('cv') is not None else 'UNAVAILABLE',
      'sensitivity_status':'AVAILABLE' if own.get('leave_one_out_mean_range') is not None and opp.get('leave_one_out_mean_range') is not None else 'UNAVAILABLE',
      'uncertainty_status':'DESCRIPTIVE_ONLY_NO_CALIBRATION',
      'temporal_validation_status':'NOT_AVAILABLE_NO_HOLDOUT',
      'baseline_status':'OBSERVED_WITHIN_FIXTURE_SAMPLE' if own['statistics']['n'] else 'BASELINE_UNAVAILABLE',
      'invalidation_conditions':['POST_CUTOFF_DATA','TEAM_OR_FIXTURE_ID_MISMATCH','DIFFERENT_LEAGUE_OR_SEASON','SAMPLE_OR_COVERAGE_BELOW_CONFIG'],
      'not_probability_or_ev':True}


def explicit_patterns(fixture,observations,min_n,min_effective):
    output=[]
    for side in ('home','away'):
        opposite='away' if side=='home' else 'home';venue='HOME' if side=='home' else 'AWAY';oppvenue='AWAY' if side=='home' else 'HOME'
        ownrows,opprows=observations[side],observations[opposite]
        for metric in METRICS:
            own=series(ownrows,'own',metric,venue);opp=series(opprows,'conceded',metric,oppvenue)
            recent=series(ownrows[:5],'own',metric);previous=series(ownrows[5:10],'own',metric)
            n=min(own['statistics']['n'],opp['statistics']['n']);effective=min(own['effective_coverage_pct'] or 0,opp['effective_coverage_pct'] or 0)
            reasons=[]
            if n<min_n:status='INSUFFICIENT_DATA';reasons.append('MIN_SAMPLE_NOT_MET')
            elif effective<min_effective:status='INSUFFICIENT_DATA';reasons.append('MIN_EFFECTIVE_COVERAGE_NOT_MET')
            direction=None;delta=None
            if own['statistics']['mean'] is not None and opp['statistics']['mean'] is not None:
                delta=round(own['statistics']['mean']-opp['statistics']['mean'],4)
                direction='OWN_HIGHER_THAN_RIVAL_CONCESSION' if delta>0 else 'OWN_LOWER_THAN_RIVAL_CONCESSION' if delta<0 else 'ALIGNED'
            if not reasons:
                reasons.extend([] if delta not in (None,0) else ['NO_DIRECTIONAL_EFFECT'])
                status='DESCRIPTIVE_SUPPORTED' if delta not in (None,0) and own['statistics'].get('cv') is not None and opp['statistics'].get('cv') is not None and own.get('leave_one_out_mean_range') is not None and opp.get('leave_one_out_mean_range') is not None else 'EXPLORATORY_UNVALIDATED'
            counter=[]
            if recent['statistics']['n']>=3 and previous['statistics']['n']>=3:
                trend=recent['statistics']['mean']-previous['statistics']['mean']
                if delta is not None and delta*trend<0:counter.append('RECENT_TREND_OPPOSES_MATCHUP_DIRECTION')
            output.append(base_pattern(fixture,side,metric,'CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE','EXPLICIT',
              'Producción del equipo en su sede actual frente a concesión rival en la sede opuesta.',own,opp,status,reasons,
              {'mean_difference':delta,'direction':direction},counter))
    return output


def exploratory_patterns(fixture,observations,min_n):
    output=[]
    empty={'statistics':stat([]),'values':[],'fixture_ids':[],'history_n':0,'details_requested_n':0,
           'missing_provider_n':0,'not_requested_n':0,'requested_coverage_pct':None,'effective_coverage_pct':None,'leave_one_out_mean_range':None}
    for side in ('home','away'):
        rows=observations[side]
        for metric in METRICS:
            home,away=series(rows,'own',metric,'HOME'),series(rows,'own',metric,'AWAY')
            if min(home['statistics']['n'],away['statistics']['n'])>=min_n:
                delta=round(home['statistics']['mean']-away['statistics']['mean'],4)
                output.append(base_pattern(fixture,side,metric,'HOME_VS_AWAY','EXPLORATORY',
                  'Interacción descubierta entre producción y localía dentro del histórico elegible.',home,away,
                  'EXPLORATORY_UNVALIDATED',['SAME_SAMPLE_DISCOVERY_NO_TEMPORAL_HOLDOUT'],
                  {'home_minus_away_mean':delta,'direction':'HOME_HIGHER' if delta>0 else 'AWAY_HIGHER' if delta<0 else 'ALIGNED'},[]))
            recent,previous=series(rows[:5],'own',metric),series(rows[5:10],'own',metric)
            if min(recent['statistics']['n'],previous['statistics']['n'])>=3:
                delta=round(recent['statistics']['mean']-previous['statistics']['mean'],4)
                output.append(base_pattern(fixture,side,metric,'RECENT_VS_PREVIOUS','EXPLORATORY',
                  'Interacción descubierta entre métrica y segmento temporal reciente/anterior.',recent,previous,
                  'EXPLORATORY_UNVALIDATED',['SAME_SAMPLE_DISCOVERY_NO_FORWARD_VALIDATION'],
                  {'recent_minus_previous_mean':delta,'direction':'RECENT_HIGHER' if delta>0 else 'RECENT_LOWER' if delta<0 else 'ALIGNED'},[]))
        for left,right in ASSOCIATIONS:
            pairs=[((r.get('own') or {}).get(left),(r.get('own') or {}).get(right),r['fixture_id']) for r in rows]
            valid=[(a,b,fid) for a,b,fid in pairs if finite(a) and finite(b)]
            corr=pearson([(a,b) for a,b,_ in valid])
            if corr is None:continue
            own={'statistics':stat([a for a,_,_ in valid]),'values':[a for a,_,_ in valid],
                 'fixture_ids':[fid for _,_,fid in valid],'history_n':len(rows),'details_requested_n':len(valid),
                 'missing_provider_n':0,'not_requested_n':len(rows)-len(valid),'requested_coverage_pct':100.0,
                 'effective_coverage_pct':coverage(len(valid),len(rows)),'leave_one_out_mean_range':loo([a for a,_,_ in valid])}
            opp={**empty,'statistics':stat([b for _,b,_ in valid]),'values':[b for _,b,_ in valid],
                 'fixture_ids':[fid for _,_,fid in valid],'history_n':len(rows),'details_requested_n':len(valid),
                 'requested_coverage_pct':100.0,'effective_coverage_pct':coverage(len(valid),len(rows)),
                 'leave_one_out_mean_range':loo([b for _,b,_ in valid])}
            output.append(base_pattern(fixture,side,f'{left}__{right}','PAIRED_FIXTURE_ASSOCIATION','EXPLORATORY',
              f'Asociación observada {left}–{right} emparejada por fixture; no implica causalidad.',own,opp,
              'EXPLORATORY_UNVALIDATED',['MULTIPLE_HYPOTHESIS_EXPLORATION','NO_TEMPORAL_VALIDATION','CORRELATION_NOT_CAUSALITY'],
              {'pearson':corr,'paired_n':len(valid)},[]))
    return output


def build_discovery(pre,pre_sha,asof,min_n=4,min_effective=40.0):
    patterns=[];fixture_counts={}
    for fixture in pre.get('matches',[]):
        observations={side:clean_observations(fixture,side,asof) for side in ('home','away')}
        found=explicit_patterns(fixture,observations,min_n,min_effective)+exploratory_patterns(fixture,observations,min_n)
        patterns.extend(found);fixture_counts[str(fixture['fixture_id'])]=len(found)
    counts=Counter(p['validation_status'] for p in patterns);modes=Counter(p['discovery_mode'] for p in patterns)
    metrics=Counter(p['metric'] for p in patterns)
    return {'schema_version':DISCOVERY_SCHEMA,'asof_madrid':asof.astimezone(MADRID).isoformat(),
      'preselection_sha256':pre_sha,'configuration':{'min_sample':min_n,'min_effective_coverage_pct':min_effective,
       'false_association_policy':'Exploratory interactions remain unvalidated without a temporal holdout.',
       'universal_opportunity_thresholds':False},'patterns':patterns,
      'unsupported_dimensions':UNSUPPORTED,'counts':{'patterns':len(patterns),'fixtures':len(fixture_counts),
       'by_status':dict(counts),'by_mode':dict(modes),'by_metric':dict(metrics),'by_fixture':fixture_counts},'actionable_bet':False}


def quality(pattern):
    base={'DESCRIPTIVE_SUPPORTED':45,'EXPLORATORY_UNVALIDATED':20,'CONTRADICTORY':10,'INSUFFICIENT_DATA':0}.get(pattern['validation_status'],0)
    n=min(pattern.get('n_own') or 0,pattern.get('n_opponent_conceded') or pattern.get('n_own') or 0)
    cov=min((v or 0) for v in pattern.get('coverage_effective_pct',{}).values())
    cvs=[x for x in (pattern.get('own_statistics',{}).get('cv'),pattern.get('opponent_conceded_statistics',{}).get('cv')) if finite(x)]
    stability=10/(1+max(cvs)) if cvs else 0
    return round(min(100,base+20*min(n/10,1)+20*cov/100+stability),2)


def build_queue(discovery):
    items=[]
    for p in discovery['patterns']:
        item={k:p.get(k) for k in ('pattern_id','fixture_id','home','away','team','team_side','metric','family','segment','discovery_mode','validation_status','validation_reasons','n_own','n_opponent_conceded','coverage_effective_pct','effect_descriptive','counterevidence')}
        item['sport_research_priority']=quality(p);item['bwin_status']='NOT_EVALUATED_PRE_BWIN';item['not_commercial_queue']=True
        items.append(item)
    items.sort(key=lambda x:(-x['sport_research_priority'],str(x['fixture_id']),x['pattern_id']))
    groups=defaultdict(list)
    for x in items: groups[str(x['fixture_id'])].append(x)
    priority=[]
    for fid,vals in groups.items():
        fams=[]
        for x in sorted(vals,key=lambda z:(-z['sport_research_priority'],z['pattern_id'])):
            if x['family'] not in fams:fams.append(x['family'])
        priority.append({'fixture_id':fid,'fixture_sport_priority':round(max(x['sport_research_priority'] for x in vals),2),
                         'families':fams,'top_pattern_ids':[x['pattern_id'] for x in sorted(vals,key=lambda z:-z['sport_research_priority'])[:5]],
                         'reasons':['MAX_PATTERN_SPORT_RESEARCH_PRIORITY','FAMILIES_INFORMATIVE_ONLY']})
    priority.sort(key=lambda x:(-x['fixture_sport_priority'],x['fixture_id']))
    return {'schema_version':QUEUE_SCHEMA,'asof_madrid':discovery['asof_madrid'],
      'preselection_sha256':discovery['preselection_sha256'],'ranking_method':'FAMILY_SPECIFIC_DESCRIPTIVE_QUALITY_NOT_PROBABILITY_OR_VALUE',
      'shortlist':items[:30],'items':items,'fixture_sport_priority':priority,'counts':{'items':len(items),'fixtures':len({str(x['fixture_id']) for x in items}),
       'by_metric':dict(Counter(x['metric'] for x in items)),'by_status':dict(Counter(x['validation_status'] for x in items)),
       'by_fixture':dict(Counter(str(x['fixture_id']) for x in items))},
      'actionable_bet':False,'contains_odds':False}


def write_md(matrix,discovery,queue,out):
    lines=['# Matriz de cobertura observada V2','',f"Corte: `{matrix['asof_madrid']}` · preselection SHA: `{matrix['preselection_sha256']}`",'',
      '| Liga/temporada/género | Métrica/fuente | Histórico | Solicitado | Válido | Missing proveedor | No solicitado | Estado legacy N/V | Cobertura solicitada/efectiva |','|---|---|---:|---:|---:|---:|---:|---:|---|']
    for r in matrix['rows']:
        lines.append(f"| {r['league_id']}/{r['season']}/{r['gender']} | {r['metric']} / {r['source_type']} | {r['history_available_n']} | {r['details_requested_n']} | {r['valid_observations_n']} | {r['missing_provider_n']} | {r['not_requested_n']} | {r['request_status_unverifiable_n']} | {r['requested_coverage_pct']}/{r['effective_coverage_pct']} |")
    lines+=['','Cobertura observada en la muestra; no es whitelist del proveedor. Denominador cero produce `null`.','']
    (out/'coverage_matrix.md').write_text('\n'.join(lines),encoding='utf-8')
    lines=['# Descubrimiento híbrido de patrones V2','',f"Corte: `{discovery['asof_madrid']}` · preselection SHA: `{discovery['preselection_sha256']}`",'',
      f"Patrones descriptivos: **{discovery['counts']['patterns']}** · estados: `{json.dumps(discovery['counts']['by_status'],ensure_ascii=False,sort_keys=True)}`.",'',
      '| Fixture | Lado | Métrica/segmento | Modo | Estado | n | Efecto descriptivo |','|---:|---|---|---|---|---:|---|']
    for p in discovery['patterns'][:50]:lines.append(f"| {p['fixture_id']} | {p['team_side']} | {p['metric']} / {p['segment']} | {p['discovery_mode']} | {p['validation_status']} | {p['n_own']}/{p['n_opponent_conceded']} | `{json.dumps(p['effect_descriptive'],ensure_ascii=False)}` |")
    lines+=['','La exploración de asociaciones múltiples queda `EXPLORATORY_UNVALIDATED`; correlación no implica causalidad. Jugadores, periodos, entrenadores, árbitros y PPDA no se fabrican.','']
    (out/'pattern_discovery.md').write_text('\n'.join(lines),encoding='utf-8')
    lines=['# Cola deportiva de investigación V2','',f"Corte: `{queue['asof_madrid']}`",'',
      'Orden deportivo previo a Bwin. No es cola comercial, probabilidad, EV, cuota justa ni stake.','',
      '| # | Fixture | Familia/métrica | Segmento | Estado | Prioridad descriptiva |','|---:|---:|---|---|---|---:|']
    for i,p in enumerate(queue['shortlist'],1):lines.append(f"| {i} | {p['fixture_id']} | {p['family']} / {p['metric']} | {p['segment']} | {p['validation_status']} | {p['sport_research_priority']} |")
    (out/'research_queue_sport.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def generate(preselection,output_dir,asof=None,min_n=4,min_effective=40.0):
    prepath=Path(preselection);pre=read(prepath);pre_sha=sha(prepath)
    asof=asof or parse_asof(pre.get('generated_at_utc'))
    if not asof:raise DiscoveryError('Preselection sin corte temporal verificable')
    if any(dt(f.get('kickoff_madrid')) and dt(f['kickoff_madrid'])<=asof for f in pre.get('matches',[])):
        mode='RETROSPECTIVE_NON_OPERATIONAL'
    else: mode='PREMATCH_OPERATIONAL'
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    matrix=build_coverage(pre,pre_sha,asof);discovery=build_discovery(pre,pre_sha,asof,min_n,min_effective);queue=build_queue(discovery)
    for doc in (matrix,discovery,queue):doc['temporal_mode']=mode;doc['operational_status']=mode
    dump(out/'coverage_matrix.json',matrix);dump(out/'pattern_discovery.json',discovery);dump(out/'research_queue_sport.json',queue)
    trial=prepath.parent.parent/'budget_assignment.json'
    original=[]
    if trial.is_file(): original=read(trial).get('bwin_order_fixture_ids',[])
    proposed=[x['fixture_id'] for x in queue['fixture_sport_priority']]
    comparison={'schema_version':'1.0-selector-order-comparison','original_bwin_order_fixture_ids':original,'proposed_fixture_sport_priority':proposed,
                'reasons':['SPORT_QUEUE_INDEPENDENT_FROM_BWIN','FAMILY_DIVERSIFICATION','OPERATIONAL_BWIN_ORDER_UNCHANGED']}
    dump(out/'selector_order_comparison.json',comparison)
    (out/'selector_order_comparison.md').write_text('# Comparación de orden del selector\n\n'+json.dumps(comparison,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_md(matrix,discovery,queue,out)
    return matrix,discovery,queue


def commercial_link(queue_path,scanner_path,bwin_path,output_dir):
    queue=read(queue_path);scanner=read(scanner_path) if scanner_path and Path(scanner_path).is_file() else {}
    bwin=read(bwin_path) if bwin_path and Path(bwin_path).is_file() else {}
    audits={str(x.get('fixture_id')):x for x in scanner.get('audits',[]) if isinstance(x,dict)}
    reviews=defaultdict(list)
    for row in scanner.get('selection_reviews',[]):reviews[str(row.get('fixture_id'))].append(row)
    discoveries={str(x.get('fixture_id')):x for x in bwin.get('matches',[]) if isinstance(x,dict)}
    scanner_asof=dt(scanner.get('asof_madrid'))
    queue_pre_sha=queue.get('preselection_sha256'); source_pre_sha=(scanner.get('sources') or {}).get('preselection_sha256')
    bwin_source_sha=bwin.get('source_input_sha256')
    links=[]
    for item in queue.get('items',[]):
        fid=str(item['fixture_id']);audit=audits.get(fid);event=discoveries.get(fid);metric=item.get('metric')
        # Exact team/metric linkage only. xG remains auxiliary and on_target is
        # not silently mapped to generic/player shots.
        audit_event=audit.get('bwin_event_id') if audit else None
        discovery_event=event.get('bwin_event_id') if event else None
        review_events={str(x.get('bwin_event_id')) for x in reviews[fid] if x.get('bwin_event_id')}
        expected_event=audit_event or discovery_event
        kickoff_values=[dt((audit or {}).get('kickoff_madrid')),dt((event or {}).get('kickoff_madrid'))]
        kickoff=kickoff_values[0] if all(x is not None for x in kickoff_values) and kickoff_values[0]==kickoff_values[1] else None
        coherent_ids=bool(audit_event and discovery_event and str(audit_event)==str(discovery_event) and
                          (not review_events or review_events=={str(audit_event)}))
        sha_complete=bool(queue_pre_sha and source_pre_sha and bwin_source_sha and
                          queue_pre_sha==source_pre_sha==bwin_source_sha)
        matching=[x for x in reviews[fid] if x.get('metric')==metric and x.get('team_side')==item.get('team_side') and
                  x.get('status') not in ('DIAGNOSTIC_LOW_PRICE','LIVE_EXCLUDED') and x.get('market') and x.get('selection') and finite(x.get('odd')) and
                  x.get('capture_madrid') and x.get('bwin_event_id') and str(x.get('fixture_id'))==fid and str(x.get('bwin_event_id'))==str(expected_event) and
                  scanner_asof and kickoff and dt(x.get('capture_madrid')) and dt(x.get('capture_madrid'))<=scanner_asof and dt(x.get('capture_madrid'))<kickoff]
        valid_event=bool(audit and audit.get('status')=='VERIFIED' and coherent_ids and sha_complete and scanner_asof and kickoff and scanner_asof<kickoff and matching)
        reasons=set()
        present_ids={str(x) for x in (audit_event,discovery_event) if x} | review_events
        if len(present_ids)>1:reasons.add('EVENT_ID_CONFLICT')
        elif not coherent_ids:reasons.add('EVENT_ID_MISSING')
        if not sha_complete:reasons.add('SHA_MISSING_OR_CONFLICT')
        if kickoff is None:reasons.add('KICKOFF_MISSING_OR_CONFLICT')
        if not (scanner_asof and kickoff and scanner_asof<kickoff):reasons.add('TEMPORAL_CUTOFF_INVALID')
        rejected_captures=[]
        for row in reviews[fid]:
            if row.get('metric')!=metric or row.get('team_side')!=item.get('team_side'):continue
            captured=dt(row.get('capture_madrid'))
            if not (captured and scanner_asof and kickoff and captured<=scanner_asof and captured<kickoff):
                rejected_captures.append({'market':row.get('market'),'selection':row.get('selection'),'capture_madrid':row.get('capture_madrid'),'reason':'CAPTURE_TIME_INVALID'})
        if rejected_captures and not matching:reasons.add('CAPTURE_TIME_INVALID')
        if audit and audit.get('status')!='VERIFIED':status='PREFLIGHT_INVALID'
        elif valid_event:status='BWIN_VERIFIED'
        elif audit and audit.get('status')=='VERIFIED' and (reviews[fid] or event):status='COMMERCIAL_SOURCE_NOT_VERIFIABLE'
        elif audit and audit.get('status')=='VERIFIED' and reviews[fid]:status='MARKET_NOT_SUPPORTED'
        elif event and event.get('bwin_event_id'):status='NO_PRICE'
        else:status='BWIN_NOT_FOUND'
        links.append({**item,'commercial_status':status,'scanner_diagnostic_status':(audit or {}).get('status','NOT_SCANNED'),
                      'pattern_validation_status':item.get('validation_status'),'commercial_availability':status,
                      'verified_quote_link':bool(valid_event),'betting_value_status':'NOT_ESTABLISHED',
                      'bwin_event_id':(audit or event or {}).get('bwin_event_id'),
                      'matching_selection_count':len(matching),'matching_selections':matching,
                      'commercial_validation_reasons':sorted(reasons),'rejected_capture_diagnostics':rejected_captures,
                      'actionable_bet':False,'sport_signal_preserved':True})
    counts=Counter(x['commercial_status'] for x in links)
    doc={'schema_version':LINK_SCHEMA,'asof_madrid':scanner.get('asof_madrid') or queue.get('asof_madrid'),
      'preselection_sha256':queue.get('preselection_sha256'),'sport_queue_sha256':sha(queue_path),
      'scanner_sha256':sha(scanner_path) if scanner_path and Path(scanner_path).is_file() else None,
      'bwin_discovery_sha256':sha(bwin_path) if bwin_path and Path(bwin_path).is_file() else None,
      'links':links,'counts':dict(counts),'actionable_bet':False,
      'scanner_asof_madrid':scanner.get('asof_madrid'),'queue_preselection_sha256':queue_pre_sha,
      'warning':'BWIN_VERIFIED acredita vínculo observado, no valor financiero ni pick.'}
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True);dump(out/'commercial_link_report.json',doc)
    lines=['# Vinculación comercial separada V2','',f"Corte: `{doc['asof_madrid']}`",'',
           f"Estados: `{json.dumps(doc['counts'],ensure_ascii=False,sort_keys=True)}`",'',
           '| Fixture | Familia/métrica | Señal deportiva | Estado comercial | Selecciones |','|---:|---|---|---|---:|']
    for x in links[:80]:lines.append(f"| {x['fixture_id']} | {x['family']} / {x['metric']} | {x['validation_status']} | {x['commercial_status']} | {x['matching_selection_count']} |")
    lines+=['','Las señales sin Bwin se conservan, pero ninguna fila es apuesta ejecutable.','']
    (out/'commercial_link_report.md').write_text('\n'.join(lines),encoding='utf-8')
    return doc


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--preselection',type=Path);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--asof');p.add_argument('--min-sample',type=int,default=4);p.add_argument('--min-effective-coverage',type=float,default=40.0)
    p.add_argument('--link-only',action='store_true');p.add_argument('--queue',type=Path);p.add_argument('--scanner',type=Path);p.add_argument('--bwin-discovery',type=Path)
    a=p.parse_args(argv)
    try:
        if a.link_only:
            if not a.queue:p.error('--link-only requiere --queue')
            doc=commercial_link(a.queue,a.scanner,a.bwin_discovery,a.output_dir);print(f"Vínculos comerciales: {len(doc['links'])} -> {a.output_dir}")
        else:
            if not a.preselection:p.error('se requiere --preselection')
            matrix,discovery,queue=generate(a.preselection,a.output_dir,parse_asof(a.asof) if a.asof else None,a.min_sample,a.min_effective_coverage)
            print(f"Cobertura: {len(matrix['rows'])} filas | patrones: {len(discovery['patterns'])} | cola: {len(queue['items'])}")
        return 0
    except (DiscoveryError,OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
