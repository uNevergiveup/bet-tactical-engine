#!/usr/bin/env python3
"""Descriptive market-specific research signals. No probabilities, EV or bets.

See docs/TACTICAL_SIGNAL_ENGINE.md for scales, cutoffs and unsupported mechanisms.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
import time
from pathlib import Path
from statistics import mean,pstdev
from collections import defaultdict
from types import SimpleNamespace
import prematch_prospective_trial as t
from prematch_pattern_engine import source_sides
from prematch_opportunity_scanner import norm,match_identity,validate_snapshot,parse_line
from prematch_tactical_history import LocalHistoryCache
from market_quality_gate import market_kind, is_period, is_player_market

VERSION='2.0-descriptive-tactical-signals'
METRICS=('goals','corners','shots','on_target','fouls','yellow','offsides','xg')
FAMILY={'goals':'goals','corners':'corners','shots':'shots','on_target':'shots','fouls':'fouls','yellow':'cards','offsides':'offsides','xg':'goals'}
WEIGHTS={'sample':.20,'coverage':.20,'stability':.25,'agreement':.15,'sensitivity':.10,'venue':.05,'profile':.05}
LIMITATIONS=['DESCRIPTIVE_NOT_CALIBRATED_NO_EV','NO_PRESSING_CROSSES_OR_COACH_STRATEGY_INFERRED',
             'NO_CORNER_SCORING_EFFICIENCY_INFERRED','NO_SCORE_STATE_TIMELINE','PLAYER_MINUTES_AND_MATCHUPS_UNAVAILABLE',
             'REFEREE_HISTORY_UNAVAILABLE','OPPONENT_STRENGTH_AND_COACH_REGIMES_NOT_ADJUSTED']


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0


def stats(values):
    if not values:return {'n':0,'mean':None,'cv':None}
    avg=mean(values);sd=pstdev(values)
    return {'n':len(values),'mean':round(avg,4),'stdev':round(sd,4),
            'cv':round(sd/avg,4) if avg>0 else (0.0 if sd==0 else None)}


def summarize_series(records,metric,kind,venue):
    # Goals are available in a finished fixture even when the detailed statistics
    # endpoint was not requested. All other metrics must expose both denominators:
    # detailed-request coverage and coverage over the historical fixtures known.
    all_n=len(records)
    detailed_records=records if metric=='goals' else [r for r in records if r.get('statistics_requested',True)]
    vals=[(r,r.get(kind,{}).get(metric)) for r in detailed_records]
    valid=[(r,float(v)) for r,v in vals if finite(v)]
    values=[v for _,v in valid];summary=stats(values)
    venue_values=[v for r,v in valid if r['venue']==venue]
    recent=stats(values[:5]);previous=stats(values[5:10])
    trend=(recent['mean']-previous['mean'])/max(previous['mean'],1) if recent['n']>=3 and previous['n']>=3 else None
    loo=[]
    if len(values)>=4:
        loo=[mean(values[:i]+values[i+1:]) for i in range(len(values))]
    requested_coverage=round(100*len(valid)/len(detailed_records),2) if detailed_records else None
    effective_coverage=round(100*len(valid)/all_n,2) if all_n else None
    return {**summary,'history_n':all_n,'history_not_detailed_n':all_n-len(detailed_records),'expected_n':len(detailed_records),'missing_n':len(detailed_records)-len(valid),
            # Backwards-compatible alias: it always meant coverage within details requested.
            'coverage_pct':requested_coverage,
            'historical_matches_available':all_n,
            'historical_matches_detailed':len(detailed_records),
            'historical_matches_not_detailed':all_n-len(detailed_records),
            'metric_observations_valid':len(valid),
            'metric_observations_missing':len(detailed_records)-len(valid),
            'requested_data_coverage_pct':requested_coverage,
            'historical_effective_coverage_pct':effective_coverage,
            'venue':stats(venue_values),'recent':recent,'previous':previous,'trend_relative':trend,
            'leave_one_out_mean_range':[round(min(loo),4),round(max(loo),4)] if loo else None,
            'fixture_ids':[r['fixture_id'] for r,_ in valid]}


def profile_evidence(path,fixture,asof):
    """Explicit entity binding and validity, with coach-regime invalidation."""
    if not path or not Path(path).is_file():return [],'TACTICAL_PROFILE_UNAVAILABLE',[]
    try:doc=t.read(Path(path))
    except (ValueError,OSError):return [],'TACTICAL_PROFILE_INVALID_SCHEMA',[]
    accepted=[];rejected=[]
    if not isinstance(doc,dict) or not isinstance(doc.get('claims'),list) or doc.get('schema_version')!='1.0-tactical-profiles':return [],'TACTICAL_PROFILE_INVALID_SCHEMA',[]
    team_ids={str(fixture.get('home_id')),str(fixture.get('away_id'))}-{'None'}
    for p in doc.get('claims',[]):
        if not isinstance(p,dict):
            rejected.append({'claim_id':None,'reason':'PROFILE_REQUIRED_FIELDS_MISSING'});continue
        if str(p.get('team_id')) not in team_ids:continue
        reason=None;observed=t.dt(p.get('observed_at'));begin=t.dt(p.get('valid_from'));end=t.dt(p.get('valid_until'))
        if not p.get('claim_id') or not p.get('entity_id') or not p.get('entity_type') or not p.get('claim_type') or not p.get('source') or not p.get('markets'):
            reason='PROFILE_REQUIRED_FIELDS_MISSING'
        elif not observed or not begin or not end or not observed<=asof or not begin<=asof<end:
            reason='PROFILE_EXPIRED_OR_FUTURE'
        elif p.get('evidence_level') not in ('CONFIRMADO','PROBABLE','INFERIDO','NO_VERIFICADO'):
            reason='PROFILE_EVIDENCE_LEVEL_INVALID'
        elif p['entity_type'] not in ('team','coach','player'):reason='PROFILE_ENTITY_TYPE_INVALID'
        elif p['entity_type']=='team' and str(p['entity_id'])!=str(p['team_id']):reason='PROFILE_ENTITY_ID_MISMATCH'
        elif p['entity_type']=='player' and str(p['entity_id']) not in {str(i) for i in fixture.get('verified_player_ids',{}).get(str(p['team_id']),[])}:reason='PLAYER_ID_OR_EXPOSURE_UNVERIFIED'
        elif p['entity_type']=='coach' and str(fixture.get('coach_ids',{}).get(str(p['team_id'])))!=str(p['entity_id']):reason='COACH_ENTITY_UNVERIFIED'
        elif p.get('coach_dependent'):
            current=fixture.get('coach_ids',{}).get(str(p['team_id']))
            if current is None:reason='COACH_REGIME_UNVERIFIED'
            elif str(current)!=str(p.get('coach_id')):reason='COACH_CHANGED_PROFILE_INVALIDATED'
        if reason:rejected.append({'claim_id':p.get('claim_id'),'reason':reason})
        else:accepted.append(p)
    return accepted,'TACTICAL_PROFILE_AVAILABLE' if accepted else 'TACTICAL_PROFILE_UNAVAILABLE',rejected


def aggregate_series(fixture,side,metric,kind):
    series=source_sides(fixture,metric)
    raw=series[f'{side}_{"own" if kind=="own" else "conceded"}']
    hist=fixture.get(side+'_evidence',{}).get('history',{})
    available=hist.get('same_competition_total',0) or 0
    detailed=available if metric=='goals' else ((fixture.get(side+'_evidence',{}).get('details') or {}).get('fixture_details_available',0) or 0)
    n=raw['n'];avg=raw['mean'];cv=raw['cv']
    if not isinstance(n,int) or n<0 or not finite(avg):n=0;avg=None;cv=None
    if not finite(cv):cv=None
    available=max(n,available)
    detailed=max(n,detailed)
    requested_coverage=round(100*n/detailed,2) if detailed else None
    effective_coverage=round(100*n/available,2) if available else None
    return {'n':n,'mean':avg,'cv':cv,'expected_n':detailed,'missing_n':detailed-n,
            'coverage_pct':requested_coverage,
            'history_n':available,'history_not_detailed_n':available-detailed,
            'historical_matches_available':available,'historical_matches_detailed':detailed,
            'historical_matches_not_detailed':available-detailed,
            'metric_observations_valid':n,'metric_observations_missing':detailed-n,
            'requested_data_coverage_pct':requested_coverage,
            'historical_effective_coverage_pct':effective_coverage,'venue':{'n':0,'mean':None},
            'recent':{'n':0,'mean':None},'previous':{'n':0,'mean':None},'trend_relative':None,
            'leave_one_out_mean_range':None,'fixture_ids':[],
            'limitation':'AGGREGATES_ONLY_NO_TREND_LOCALITY_POINT_SENSITIVITY_OR_SEASON_REGIME_SPLIT'}


def filtered_observations(rows,fixture,side,cutoff):
    output=[];seen=set();rejected=[];tid=fixture.get(side+'_id')
    for r in sorted(rows,key=lambda r:str(r.get('kickoff_utc','')),reverse=True):
        fid=r.get('fixture_id');at=t.dt(r.get('kickoff_utc'))
        why=None
        if tid is None or str(r.get('team_id'))!=str(tid):why='OBSERVATION_TEAM_ID_MISMATCH'
        elif not at or at>=cutoff or str(fid)==str(fixture['fixture_id']):why='POST_CUTOFF_OBSERVATION'
        elif r.get('status')!='FT':why='NON_FT_PERIOD_NOT_COMPARABLE'
        elif r.get('league_id')!=fixture.get('league_id'):why='DIFFERENT_COMPETITION'
        elif r.get('season')!=fixture.get('season'):why='DIFFERENT_SEASON'
        elif r.get('venue') not in ('HOME','AWAY'):why='VENUE_UNKNOWN'
        elif fid in seen:why='DUPLICATE_HISTORICAL_FIXTURE'
        if why:rejected.append({'fixture_id':fid,'reason':why});continue
        seen.add(fid);output.append(r)
    return output[:30],rejected


def build_signal(fixture,side,metric,observations,profiles):
    opposite='away' if side=='home' else 'home';venue='HOME' if side=='home' else 'AWAY'
    own=summarize_series(observations[side],metric,'own',venue)
    conceded=summarize_series(observations[opposite],metric,'conceded','AWAY' if venue=='HOME' else 'HOME')
    source='FIXTURE_OBSERVATIONS'
    if not own['n'] and not conceded['n']:
        own=aggregate_series(fixture,side,metric,'own');conceded=aggregate_series(fixture,opposite,metric,'conceded');source='AGGREGATED_PRESELECTION'
    n=min(own['n'],conceded['n'])
    # Research quality must use the effective historical denominator, never only
    # the subset for which detailed statistics happened to be requested.
    coverage=mean([(s['historical_effective_coverage_pct'] or 0)/100 for s in (own,conceded)])
    cvs=[s['cv'] for s in (own,conceded) if finite(s.get('cv'))]
    cv=max(cvs) if len(cvs)==2 else None
    agreement=1-abs(own['mean']-conceded['mean'])/max(own['mean'],conceded['mean'],1) if n else 0
    contradictions=[]
    if n and agreement<.6:contradictions.append('OWN_PRODUCTION_VS_RIVAL_CONCESSION_DISAGREE')
    a,b=own['trend_relative'],conceded['trend_relative']
    if a is not None and b is not None and a*b<0 and min(abs(a),abs(b))>.25:
        contradictions.append('OPPOSITE_RECENT_TRENDS')
    venue_n=min(own['venue']['n'],conceded['venue']['n'])
    for s in (own,conceded):
        if s['venue']['n']>=3 and s['mean'] is not None and abs(s['venue']['mean']-s['mean'])/max(s['mean'],1)>.4:
            contradictions.append('VENUE_VS_GLOBAL_DISAGREE');break
    relevant=[p for p in profiles if FAMILY[metric] in p['markets'] and str(p['team_id'])==str(fixture.get(side+'_id'))]
    production_claims={'corners':'CORNER_GENERATION_VOLUME','shots':'SHOT_VOLUME','on_target':'SHOTS_ON_TARGET_VOLUME',
                       'fouls':'FOULS_COMMITTED','yellow':'YELLOW_CARD_COUNTS','offsides':'OFFSIDES_COMMITTED',
                       'goals':'GOALS_PRODUCTION','xg':'XG_PRODUCTION'}
    confirmed=[p for p in relevant if p['evidence_level']=='CONFIRMADO' and p.get('target_metric')==metric
               and p.get('claim_type')==production_claims[metric] and p.get('effect_scope')=='PRODUCTION'
               and p['entity_type']!='player']
    sensitivity=[]
    for s in (own,conceded):
        v=s['leave_one_out_mean_range']
        sensitivity.append(1/(1+(v[1]-v[0])/max(s['mean'],1)) if v else 0)
    parts={'sample':min(n/10,1),'coverage':coverage,'stability':1/(1+cv) if cv is not None else 0,
           'agreement':agreement,'sensitivity':min(sensitivity),'venue':min(venue_n/5,1),'profile':1 if confirmed else 0}
    score=max(0,100*sum(WEIGHTS[k]*v for k,v in parts.items())-min(20,10*len(contradictions)))
    if n==0:status='INSUFFICIENT_DATA'
    elif n<4:status='SMALL_SAMPLE'
    elif cv is None:status='VARIABILITY_UNAVAILABLE'
    elif cv>.8:status='UNSTABLE_PATTERN'
    elif contradictions:status='CONTRADICTORY_SIGNAL'
    elif score<55 or coverage<.5:status='PARTIAL_EVIDENCE'
    else:status='SUPPORTED_DESCRIPTIVE_SIGNAL'
    caveats=list(LIMITATIONS)
    if any(s['historical_matches_not_detailed'] for s in (own,conceded)):
        caveats.append('HISTORICAL_COVERAGE_PARTIAL_DETAILED_SUBSET_NOT_TREATED_AS_FULL_HISTORY')
    if source=='AGGREGATED_PRESELECTION':caveats.append('TREND_AND_LOCALITY_UNAVAILABLE')
    if n<10:caveats.append('SAMPLE_BELOW_TEN_NOT_AUTOMATIC_REJECTION')
    if metric=='fouls':caveats.append('CONCEDED_IS_OPPONENT_FOULS_COMMITTED_NOT_PLAYER_FOULS_DRAWN')
    if metric=='offsides':caveats.append('PROVOKED_OFFSIDES_RECONSTRUCTED_FROM_OPPONENT_NOT_DEFENSIVE_LINE_HEIGHT')
    if metric=='yellow':caveats.append('YELLOW_CARDS_NOT_ALL_CARD_POINTS_OR_REFEREE_MODEL')
    volume=[]
    if metric=='corners':
        for support in ('shots','blocked_shots'):
            pairs=[(r['own'].get('corners'),r['own'].get(support)) for r in observations[side]]
            pairs=[(a,b) for a,b in pairs if finite(a) and finite(b)]
            corr=None
            if len(pairs)>=4:
                a,b=zip(*pairs);ma,mb=mean(a),mean(b)
                denom=math.sqrt(sum((v-ma)**2 for v in a)*sum((v-mb)**2 for v in b))
                if denom:corr=sum((x-ma)*(y-mb) for x,y in pairs)/denom
            volume.append({'metric':support,'paired_n':len(pairs),'pearson_descriptive':round(corr,4) if corr is not None else None,
                           'limitation':'ASSOCIATION_NOT_PRESSING_OR_CAUSATION'})
    return {'volume_associations':volume,'signal_id':f'{fixture["fixture_id"]}:{side}:{metric}','metric':metric,'family':FAMILY[metric],
            'team_side':side,'team_id':fixture.get(side+'_id'),'status':status,'priority_score':round(score,2),
            'components':parts,'own':own,'opponent_conceded':conceded,'source_mode':source,
            'contradictions':contradictions,'profile_claim_ids':[p['claim_id'] for p in relevant],
            'confidence_level':'MODERATE_DESCRIPTIVE' if status=='SUPPORTED_DESCRIPTIVE_SIGNAL' and n>=10 and source=='FIXTURE_OBSERVATIONS' else 'LIMITED_DESCRIPTIVE',
            'hypothesis':f'Investigar la distribución de {metric} de {fixture[side]} confrontando producción propia y concesión rival; mecanismo táctico no confirmado.',
            'limitations':caveats,'market_status':'PENDIENTE_MERCADO','markets':[]}


def classify_market(market,fixture):
    value=norm(market)
    if any(x in value for x in ('live','en vivo','en directo','resto del','marcador actual','combinad','crea tus','jugador','goleador','descanso','parte','prorroga')):return None
    if not value.endswith(' | tiempo reglamentario'):return None
    base=value.split(' | tiempo reglamentario')[0].strip();side=None
    for s in ('home','away'):
        prefix=norm(fixture[s])+' - '
        if base.startswith(prefix):side=s;base=base[len(prefix):];break
    mapping={'total de goles':'goals','total de corners':'corners','total de tiros':'shots',
             'total de tiros a puerta':'on_target','total de remates a puerta':'on_target',
             'total de faltas':'fouls','total de fueras de juego':'offsides','total de tarjetas amarillas':'yellow'}
    return (mapping[base],side) if base in mapping else None


def market_links(fixture,signals,prepath,paths,asof,age_limit):
    from bwin_market_preflight import validate_match
    reasons=[];verified=0;direct=0;indirect=0;player_markets=0;usable_markets=0;commercial_shortlist=0
    for p in paths:
        doc=t.read(Path(p));matches=[m for m in doc.get('matches',[]) if str(m.get('fixture_id'))==str(fixture['fixture_id'])]
        if not matches:continue
        if len(matches)!=1:reasons.append('DUPLICATE_PREFLIGHT_FIXTURE');continue
        m=matches[0]
        errors,quotes,_=validate_snapshot(fixture,m,doc,Path(p),Path(prepath),None,asof,age_limit,45,False)
        try:
            source=Path(doc['source_discovery'])
            if t.sha(source)!=doc['source_sha256']:raise ValueError('DISCOVERY_SHA_MISMATCH')
            disc=t.read(source);events=[e for e in disc['matches'] if str(e['fixture_id'])==str(fixture['fixture_id'])]
            if len(events)!=1 or match_identity(fixture,events[0]) or str(events[0].get('bwin_event_id'))!=str(m.get('bwin_event_id')) or events[0].get('bwin_url')!=m.get('bwin_url'):
                raise ValueError('DISCOVERY_IDENTITY_CONFLICT')
            checked=validate_match(events[0],disc.get('catalog_observed',[]),asof,45,180)
            errors+=checked['reasons']
            if m.get('quality_gate',{}).get('join_status')!='MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF':errors.append('STATISTICAL_JOIN_INVALID')
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append(str(exc))
        if errors:reasons+=errors;continue
        verified+=1
        commercial_shortlist+=m.get('quality_gate',{}).get('researchable_after_data_gate',0)
        for q in quotes:
            kind=market_kind(q['market'])
            label=norm(q['market'])
            if (kind not in ('other','combined_or_builder','goalscorers','assists','half_full_time')
                    and not is_period(q['market']) and not is_player_market(q['market'],kind)
                    and not any(token in label for token in ('live','en vivo','en directo','resto del partido','marcador actual','prorroga'))):
                usable_markets+=1
            if any(token in norm(q['market']) for token in ('jugador','goleador')):
                # Deliberately record the observed market but never connect a
                # team signal to it: player ID, minutes, role and XI are absent.
                player_markets+=1
                continue
            spec=classify_market(q['market'],fixture)
            line=parse_line(q['selection'])
            if not spec or not line:continue
            metric,side=spec
            for sig in signals:
                if sig['metric']!=metric or side not in (None,sig['team_side']):continue
                relation='DIRECT_TEAM_MARKET' if side else 'INDIRECT_TEAM_TO_MATCH_TOTAL'
                link={**q,'line':line['line'],'direction':line['direction'],'bwin_event_id':m['bwin_event_id'],
                      'fixture_id':fixture['fixture_id'],'capture_madrid':m['snapshot']['capture_declared_madrid'],
                      'relation':relation,'association_category':relation,
                      'execution_status':'MARKET_VERIFIED_NO_CALIBRATED_MODEL',
                      'status':'VERIFIED_QUOTE_NO_CALIBRATED_MODEL','preflight':str(p)}
                if link not in sig['markets']:
                    sig['markets'].append(link)
                    if relation=='DIRECT_TEAM_MARKET':direct+=1
                    else:indirect+=1
                sig['market_status']='MARKET_VERIFIED_NO_CALIBRATED_MODEL'
    no_verified=0
    for sig in signals:
        if sig['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL' and not sig['markets']:
            if verified:
                sig['market_status']='NO_VERIFIED_MARKET'
            no_verified+=1
    return {'verified_preflights':verified,'block_reasons':sorted(set(reasons)),
            'usable_verified_market_selections':usable_markets,
            'commercial_researchable_selections':commercial_shortlist,
            'direct_team_market_hypotheses':direct,
            'indirect_team_to_match_total_hypotheses':indirect,
            'player_market_requires_exposure_and_xi':player_markets,
            'market_verified_no_calibrated_model':direct+indirect,
            'no_verified_market_hypotheses':no_verified}


def coverage_summary(signals):
    """Conservative fixture-level projection; exact per-series values stay in JSON."""
    pairs=[series for signal in signals for series in (signal.get('own',{}),signal.get('opponent_conceded',{}))]
    def minimum(field):
        values=[row[field] for row in pairs if isinstance(row.get(field),(int,float))]
        return min(values) if values else None
    return {'basis':'MINIMUM_ACROSS_PRODUCTION_AND_CONCESSION_SERIES',
            'historical_matches_available':minimum('historical_matches_available'),
            'historical_matches_detailed':minimum('historical_matches_detailed'),
            'historical_matches_not_detailed':minimum('historical_matches_not_detailed'),
            'metric_observations_valid':minimum('metric_observations_valid'),
            'metric_observations_missing':minimum('metric_observations_missing'),
            'requested_data_coverage_pct':minimum('requested_data_coverage_pct'),
            'historical_effective_coverage_pct':minimum('historical_effective_coverage_pct')}


def analyze_fixture(fixture,asof,observations,profiles_path=None):
    ko=t.dt(fixture.get('kickoff_madrid'));cutoff=t.dt(fixture.get('analysis_cutoff_utc'))
    blocked=[]
    if not ko or asof>=ko:blocked.append('MATCH_STARTED')
    if not cutoff or cutoff>asof or ko and cutoff>=ko:blocked.append('INVALID_ANALYSIS_CUTOFF')
    evidence,profile_status,profile_rejections=profile_evidence(profiles_path,fixture,min(asof,ko) if ko else asof)
    clean={};rejected=[]
    for side in ('home','away'):
        clean[side],bad=filtered_observations(observations.get(side,[]),fixture,side,min(cutoff or asof,ko or asof))
        rejected+=bad
    signals=[] if blocked else [build_signal(fixture,side,metric,clean,evidence) for metric in METRICS for side in ('home','away')]
    supported=[s for s in signals if s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL' and s['metric']!='xg']
    # xG supports goal hypotheses; it is not a separate market family.
    best=sorted(supported,key=lambda s:-s['priority_score'])
    score=mean([s['priority_score'] for s in best[:3]]) if best else 0
    coverage=mean([s['components']['coverage']*100 for s in signals]) if signals else 0
    return {'fixture_id':fixture['fixture_id'],'home':fixture['home'],'away':fixture['away'],
            'kickoff_madrid':fixture.get('kickoff_madrid'),'status':'BLOCKED' if blocked else 'EVALUATED',
            'block_reasons':blocked,'signals':signals,'signal_count':len(supported),'signal_families':sorted({s['family'] for s in supported}),
            'confidence_level':'MODERATE_DESCRIPTIVE' if any(s['confidence_level']=='MODERATE_DESCRIPTIVE' for s in supported) else 'LIMITED_DESCRIPTIVE',
            'data_coverage':round(coverage,2),'coverage_summary':coverage_summary(signals),
            # This is deliberately independent of Bwin discovery and quote volume.
            'tactical_research_score':round(score,2),
            'profile_status':profile_status,'profile_rejections':profile_rejections,
            'profile_claims':evidence,'contradictions':sorted({x for s in signals for x in s['contradictions']}),
            'priority_score':round(score,2),'reason':'DESCRIPTIVE_SIGNALS_REQUIRE_MARKET_AND_MANUAL_RESEARCH' if best else 'NO_ROBUST_FAMILY_SIGNAL',
            'observation_rejections':rejected,'player_status':'INSUFFICIENT_PLAYER_EXPOSURE_AND_MINUTES',
            'limitations':LIMITATIONS}


def diversified_order(fixtures,score=None):
    """Round-robin 2-hour kickoff buckets; no silent truncation or coverage guess."""
    buckets=defaultdict(list)
    for f in fixtures:
        ko=t.dt(f.get('kickoff_madrid'))
        key=(ko.date().isoformat(),ko.hour//2) if ko else ('',0)
        buckets[key].append(f)
    for rows in buckets.values():
        rows.sort(key=lambda f:(-(score or {}).get(str(f['fixture_id']),f.get('research_priority_score') or 0),str(f['fixture_id'])))
    result=[]
    while any(buckets.values()):
        for key in sorted(buckets):
            if buckets[key]:result.append(buckets[key].pop(0))
    return result


def market_link_counts(signals):
    """Compatibility view of signal-to-market links; it does not rank them."""
    def key(m):
        return (m.get('bwin_event_id'),m.get('market'),m.get('selection'),m.get('line'),m.get('odd'))
    raw=[m for s in signals for m in s.get('markets',[])]
    supported=[m for s in signals if s.get('status')=='SUPPORTED_DESCRIPTIVE_SIGNAL' and s.get('metric')!='xg' for m in s.get('markets',[])]
    return {'raw_signal_market_links':len(raw),'unique_market_selections':len({key(m) for m in raw}),
            'direct_raw_unique':len({key(m) for m in raw if m.get('relation')=='DIRECT_TEAM_MARKET'}),
            'indirect_raw_unique':len({key(m) for m in raw if m.get('relation')=='INDIRECT_TEAM_TO_MATCH_TOTAL'}),
            'supported_signal_market_links':len(supported),'unique_supported_selections':len({key(m) for m in supported}),
            'direct_supported':len({key(m) for m in supported if m.get('relation')=='DIRECT_TEAM_MARKET'}),
            'indirect_supported':len({key(m) for m in supported if m.get('relation')=='INDIRECT_TEAM_TO_MATCH_TOTAL'})}


def capture_age_score(capture,asof,limit=120):
    """Compatibility helper for the unchanged capture-freshness calculation."""
    age=(asof-capture).total_seconds()/60 if capture else None
    return 10 if age is not None and 0<=age<=limit else 0


def selection_rows(fixtures,tactical,scanner,asof):
    rows=[];audits={str(a['fixture_id']):a for a in (scanner or {}).get('audits',[])}
    reviews_by_fid=defaultdict(list)
    for r in (scanner or {}).get('selection_reviews',[]):
        if r.get('fixture_id') is not None:
            reviews_by_fid[str(r['fixture_id'])].append(r)
    for f in fixtures:
        fid=str(f['fixture_id']);x=tactical.get(fid,{})
        supported=[s for s in x.get('signals',[]) if s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL' and s['metric']!='xg']
        with_market=[s for s in supported if s['markets']]
        families={s['family'] for s in with_market}
        verified=audits.get(fid,{}).get('status')=='VERIFIED' and x.get('market_validation',{}).get('verified_preflights',0)>0
        lead=(t.dt(f['kickoff_madrid']).timestamp()-asof.timestamp())/60
        links=[m for s in with_market for m in s['markets']]
        direct=sum(m.get('association_category',m.get('relation'))=='DIRECT_TEAM_MARKET' for m in links)
        indirect=sum(m.get('association_category',m.get('relation')) in ('INDIRECT_TEAM_TO_MATCH_TOTAL','INDIRECT_TEAM_TO_MATCH_TOTAL_NEEDS_JOINT_ANALYSIS') for m in links)
        # Old synthetic fixtures deliberately only carry a verified quote marker;
        # treat that as an unspecified, not fabricated, executable correspondence.
        unclassified=max(0,len(links)-direct-indirect)
        validation=x.get('market_validation',{})
        direct=validation.get('direct_team_market_hypotheses',direct)
        indirect=validation.get('indirect_team_to_match_total_hypotheses',indirect)
        unmodelled=validation.get('market_verified_no_calibrated_model',direct+indirect+unclassified)
        capture_times=[t.dt(m.get('capture_madrid')) for m in links]
        capture_times=[v for v in capture_times if v]
        capture=max(capture_times,key=lambda value:value.timestamp()) if capture_times else None
        # Operational readiness is independent from the historical score and does
        # not grow with the number of quoted selections.
        readiness_parts={'identity_and_preflight':25 if validation.get('verified_preflights',0)>0 else 0,
                         'scanner':20 if audits.get(fid,{}).get('status')=='VERIFIED' else 0,
                         'statistical_join':15 if verified else 0,
                         'market_correspondence':20 if direct else 12 if indirect else 8 if unclassified else 0,
                         'capture_age':capture_age_score(capture,asof),
                         'temporal_margin':10 if lead>=45 else 0}
        readiness=round(sum(readiness_parts.values()),2)
        reason=[]
        if x.get('status')!='EVALUATED':reason+=['TACTICAL_ENGINE_NOT_VALID']
        if not supported:reason+=['NO_SUPPORTED_MARKET_FAMILY']
        if not with_market:reason+=['NO_VERIFIED_MARKET_FOR_SUPPORTED_SIGNAL']
        if not verified:reason+=['SCANNER_OR_BWIN_NOT_VERIFIED']
        if lead<45:reason+=['INSUFFICIENT_PREMATCH_MARGIN']
        # Ajuste por discrepancia exploratoria frente a la línea real de Bwin (si existe en scanner)
        fx_reviews=reviews_by_fid.get(fid,[])
        supported_metrics={s['metric'] for s in with_market}
        discrepancies=[r for r in fx_reviews if r.get('status')=='DISCREPANCIA_EXPLORATORIA_REVISAR'
                       and (not supported_metrics or r.get('metric') in supported_metrics)]
        if not discrepancies:
            discrepancies=[r for r in fx_reviews if r.get('status')=='DISCREPANCIA_EXPLORATORIA_REVISAR']
        best_gap_pp=max((float(r['gap_worst_pp']) for r in discrepancies if isinstance(r.get('gap_worst_pp'),(int,float))),default=None)
        signal_groups=len({(r.get('metric'),r.get('team_side')) for r in discrepancies})
        evaluated_lines=[r for r in fx_reviews if r.get('p_poisson_exploratory') is not None]
        all_negative=bool(evaluated_lines and all(r.get('status')=='SIN_DISCREPANCIA_BAJO_ESCENARIOS' for r in evaluated_lines))
        if discrepancies and best_gap_pp is not None:
            opportunity_adj=round(min(10.0,max(0.0,best_gap_pp))+min(5.0,2.5*signal_groups),2)
        elif all_negative:
            opportunity_adj=-10.0
        else:
            opportunity_adj=0.0
        # Joint ordering is an operational research queue, not a probability,
        # estimate of value or substitute for a model.
        score=.60*x.get('tactical_research_score',x.get('priority_score',0))+.40*readiness
        score=max(0,score-min(15,3*len(x.get('contradictions',[])))+opportunity_adj)
        if x.get('status')!='EVALUATED' or not supported:
            completeness='CANDIDATO_DEBIL_SEGUN_EVIDENCIA'
        elif not validation.get('verified_preflights',0):
            completeness='CANDIDATO_PROMETEDOR_NO_INVESTIGADO'
        elif not with_market:
            completeness='MERCADO_CAPTURADO_SIN_ASOCIACION_VERIFICADA'
        elif not verified:
            completeness='MERCADO_CAPTURADO_PENDIENTE_SCANNER'
        else:
            completeness='COMPLETAMENTE_EVALUADO_PARA_INVESTIGACION'
        research_signals=sorted((s for s in x.get('signals',[]) if s.get('metric')!='xg'
            and s.get('status') in ('SUPPORTED_DESCRIPTIVE_SIGNAL','PARTIAL_EVIDENCE','UNSTABLE_PATTERN')
            and min(s.get('own',{}).get('n',0),s.get('opponent_conceded',{}).get('n',0))>=4
            and min(s.get('own',{}).get('historical_effective_coverage_pct') or 0,
                    s.get('opponent_conceded',{}).get('historical_effective_coverage_pct') or 0)>=50),
            key=lambda s:(-s['priority_score'],s['signal_id']))
        validation_blocks=validation.get('block_reasons',[])
        hard=list(x.get('block_reasons',[]))
        if x.get('status')!='EVALUATED' and not hard:hard.append('TACTICAL_ENGINE_NOT_VALID')
        if lead<45:hard.append('INSUFFICIENT_PREMATCH_MARGIN')
        if not verified:hard.append('SCANNER_OR_BWIN_NOT_VERIFIED')
        if validation_blocks:hard.append('MARKET_SOURCE_INTEGRITY_ERROR')
        if validation.get('usable_verified_market_selections',0)<1:hard.append('NO_USABLE_VERIFIED_MARKET')
        if not research_signals:hard.append('INSUFFICIENT_HISTORICAL_RESEARCH_EVIDENCE')
        eligible=not reason and not hard
        missing=[] if eligible else [r for r in reason if r not in hard]
        if not eligible and not validation.get('commercial_researchable_selections',0):
            missing.append('NO_COMMERCIAL_SHORTLIST')
        if not eligible and not validation.get('direct_team_market_hypotheses',0):missing.append('NO_DIRECT_TEAM_MARKET_HYPOTHESIS')
        if research_signals and research_signals[0]['status']!='SUPPORTED_DESCRIPTIVE_SIGNAL':
            missing.append('HISTORICAL_SIGNAL_NOT_FULLY_SUPPORTED')
        tier='A' if eligible else 'B' if not hard else 'C'
        conditional_score=round(mean(s['priority_score'] for s in research_signals[:3]),2) if research_signals else 0.0
        if tier=='B':
            score=max(0,.60*conditional_score+.40*readiness
                      -min(15,3*len(x.get('contradictions',[])))+opportunity_adj)
        evidence=[{'signal_id':s['signal_id'],'status':s['status'],'score':s['priority_score'],
                   'own_n':s['own']['n'],'opponent_n':s['opponent_conceded']['n'],
                   'own_effective_coverage_pct':s['own']['historical_effective_coverage_pct'],
                   'opponent_effective_coverage_pct':s['opponent_conceded']['historical_effective_coverage_pct']}
                  for s in research_signals[:3]]
        rows.append({'fixture_id':f['fixture_id'],'research_priority':round(score,2),'joint_research_priority':round(score,2),'components':readiness_parts,
                     'eligible':eligible,'research_eligible':tier in ('A','B'),'selection_tier':tier,
                     'conditional_research_score':conditional_score,
                     'research_missing_evidence':sorted(set(missing)),'hard_block_reasons':sorted(set(hard)),
                     'research_evidence':evidence,'reasons':reason,'matched_families':sorted(families),
                     'tactical_priority':x.get('priority_score',0),'tactical_research_score':x.get('tactical_research_score',x.get('priority_score',0)),
                     'market_execution_readiness':readiness,'direct_market_hypotheses':direct,
                     'indirect_market_hypotheses':indirect,'unmodelled_market_count':unmodelled,
                     'player_market_requires_exposure_and_xi':validation.get('player_market_requires_exposure_and_xi',0),
                     'opportunity_discrepancies':len(discrepancies),'best_exploratory_gap_worst_pp':best_gap_pp,
                     'opportunity_score_adjustment':opportunity_adj,
                     'scanner_status':audits.get(fid,{}).get('status','NO_EVALUADO'),
                     'evaluation_completeness':completeness,'lead_minutes':round(lead,2)})
    return sorted(rows,key=lambda r:({'A':0,'B':1,'C':2}[r['selection_tier']],-r['research_priority'],str(r['fixture_id'])))


def write_report(doc,out):
    lines=['# TACTICAL SELECTION REPORT','',f"Modo: {doc['mode']} | corte: {doc['asof_madrid']}",
           'Señales descriptivas y prioridades de investigación separadas; sin probabilidades calibradas, EV ni apuestas.',
           '',f"Selección de investigación: {doc.get('selected_fixture_id') or 'NINGUNA'}",'']
    for r in doc['ranking']:
        x=doc['fixtures'][str(r['fixture_id'])]
        lines += [f"## {x['home']}–{x['away']} ({r['fixture_id']})",f"Nivel: {r['selection_tier']}; research_eligible: {r['research_eligible']}; Tactical Research Score: {r['tactical_research_score']}; Market Execution Readiness: {r['market_execution_readiness']}; Ajuste Oportunidad: {r.get('opportunity_score_adjustment', 0.0)}; orden conjunto: {r['research_priority']}; elegible comercial: {r['eligible']}; carencias: {r['research_missing_evidence']}; bloqueos: {r['hard_block_reasons']}",
                  f"Señales: {x['signal_count']}; familias: {x['signal_families']}; perfiles: {x['profile_status']}",
                  f"Cobertura mínima por serie: {x['coverage_summary']}; contradicciones: {x['contradictions']}; mercados: directos={r['direct_market_hypotheses']}, indirectos={r['indirect_market_hypotheses']}, sin modelo={r['unmodelled_market_count']}, player/XI={r['player_market_requires_exposure_and_xi']}."]
        best=sorted((s for s in x['signals'] if s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL'),key=lambda s:-s['priority_score'])[:3]
        for sig in best:
            lines.append(f"- {sig['metric']} / {sig['team_side']}: {sig['hypothesis']} n={sig['own']['n']}/{sig['opponent_conceded']['n']}; media={sig['own']['mean']}/{sig['opponent_conceded']['mean']}; CV={sig['own']['cv']}/{sig['opponent_conceded']['cv']}; prioridad={sig['priority_score']}; fuente={sig['source_mode']}; mercado={sig['market_status']}.")
        lines += ['- Debilidades: '+', '.join(x['limitations']), '']
    eligible=[r for r in doc['ranking'] if r['research_eligible']]
    if eligible:
        lines += ['## Justificación de prioridad',f"Primero: {eligible[0]['fixture_id']}, orden conjunto {eligible[0]['research_priority']}. Componentes operativos: {eligible[0]['components']}.",
                  'Comparación siguiente: '+str([{'fixture_id':r['fixture_id'],'score':r['research_priority'],'reasons':r['reasons']} for r in doc['ranking'][1:4]]),
                  'Una mayor prioridad descriptiva no demuestra mayor valor económico.']
    t.dump(out/'tactical_signal_engine.json',doc)
    (out/'TACTICAL_SELECTION_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def run_engine(preselection,pattern,out,root=None,preflights=(),scanner=None,profiles=None,asof=None,historical=False,use_cache=True):
    started=time.perf_counter();prepath=Path(preselection);out=Path(out);root=Path(root or prepath.parents[3]);asof=asof or t.now_madrid()
    pre=t.read(prepath);gen=t.dt(pre.get('generated_at_utc'))
    if not gen or gen>asof:raise ValueError('PRESELECTION_UNDATED_OR_AFTER_ASOF')
    if pattern:
        pat=t.read(Path(pattern))
        if pat.get('data_sources',{}).get('preselection_sha256')!=t.sha(prepath):raise ValueError('PATTERN_PRESELECTION_SHA_MISMATCH')
        if not t.dt(pat.get('asof_madrid')) or t.dt(pat['asof_madrid'])>asof:raise ValueError('PATTERN_AFTER_ASOF')
    identity_path=prepath.parent.parent/'fixtures_major_test.json'
    identities={str(f['fixture_id']):f for f in t.read(identity_path).get('fixtures',[])} if identity_path.is_file() else {}
    if identities and pre.get('source_filter_sha256')!=t.sha(identity_path):raise ValueError('CANONICAL_IDENTITY_SOURCE_SHA_MISMATCH')
    cache=LocalHistoryCache(root,asof) if use_cache else None;results={};fixtures=[]
    for original in pre.get('matches',[]):
        f=dict(original);identity=identities.get(str(f['fixture_id']))
        if identity:
            if match_identity(f,identity):raise ValueError('CANONICAL_FIXTURE_IDENTITY_MISMATCH')
            for key in ('home_id','away_id'):
                if key in f and f[key]!=identity.get(key):raise ValueError('CANONICAL_TEAM_ID_MISMATCH')
                f[key]=identity.get(key)
        observations=f.get('statistical_observations',{}) or {}
        if cache:
            cutoff=min(t.dt(f.get('analysis_cutoff_utc')) or asof,t.dt(f['kickoff_madrid']))
            cap=int(pre.get('methodology',{}).get('sample_history_cap') or 20)
            merged={}
            for side in ('home','away'):
                existing=observations.get(side,[]) if isinstance(observations,dict) else []
                cached=cache.for_team(f.get(side+'_id'),f['league_id'],cutoff,cap,f.get('season')) if f.get(side+'_id') is not None else []
                byid={str(row.get('fixture_id')):row for row in existing if isinstance(row,dict)}
                for row in cached:
                    key=str(row.get('fixture_id'));previous=byid.get(key)
                    quality=lambda x:sum(v is not None for block in (x.get('own',{}),x.get('conceded',{})) for v in block.values())
                    if previous is None or quality(row)>quality(previous):byid[key]=row
                merged[side]=sorted(byid.values(),key=lambda x:str(x.get('kickoff_utc','')),reverse=True)[:cap]
            observations=merged
        x=analyze_fixture(f,asof,observations,profiles)
        x['market_validation']=market_links(f,x['signals'],prepath,preflights,asof,120) if x['status']=='EVALUATED' else {'verified_preflights':0,'block_reasons':x['block_reasons']}
        x['market_hypotheses_count']=sum(s['metric']!='xg' and s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL' for s in x['signals'])
        x['market_hypotheses_verified']=sum(s['metric']!='xg' and s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL' and bool(s['markets']) for s in x['signals'])
        validation=x['market_validation']
        x['direct_market_hypotheses']=validation.get('direct_team_market_hypotheses',0)
        x['indirect_market_hypotheses']=validation.get('indirect_team_to_match_total_hypotheses',0)
        x['unmodelled_market_count']=validation.get('market_verified_no_calibrated_model',0)
        x['player_market_requires_exposure_and_xi']=validation.get('player_market_requires_exposure_and_xi',0)
        x['no_verified_market_hypotheses']=validation.get('no_verified_market_hypotheses',x['market_hypotheses_count'])
        path=out/'fixtures'/f"{f['fixture_id']}.json";x['detail_json']=str(path.resolve())
        results[str(f['fixture_id'])]=x;fixtures.append(f)
    scan=t.read(Path(scanner)) if scanner else {}
    if scan:
        if scan.get('sources',{}).get('preselection_sha256')!=t.sha(prepath):raise ValueError('SCANNER_PRESELECTION_SHA_MISMATCH')
        if not t.dt(scan.get('asof_madrid')) or t.dt(scan['asof_madrid'])>asof:raise ValueError('SCANNER_AFTER_ASOF')
        if not {str(Path(p).resolve()) for p in preflights}.issubset({str(Path(p).resolve()) for p in scan.get('sources',{}).get('preflights',[])}):raise ValueError('SCANNER_PREFLIGHT_SOURCES_MISMATCH')
        if scan.get('mode')!='PREMATCH_SNAPSHOT_SCREEN' and not historical:raise ValueError('RETROSPECTIVE_SCANNER_NOT_ACTIONABLE')
    if cache:
        for ref in cache.sources.values():
            if t.sha(Path(ref['path']))!=ref['sha256']:raise ValueError('RAW_CACHE_CHANGED_DURING_RUN')
    ranking=selection_rows(fixtures,results,scan,asof);eligible=[r for r in ranking if r['research_eligible']]
    doc={'schema_version':VERSION,'asof_madrid':asof.isoformat(),'mode':'RETROSPECTIVE_AUDIT' if historical else 'PREMATCH_RESEARCH_SCREEN',
         'sources':{'preselection':str(prepath.resolve()),'preselection_sha256':t.sha(prepath),'pattern_sha256':t.sha(Path(pattern)) if pattern else None,
                    'profiles_sha256':t.sha(Path(profiles)) if profiles and Path(profiles).is_file() else None,
                    'raw_cache_sources':list(cache.sources.values()) if cache else []},
         'fixtures':results,'ranking':ranking,'selected_fixture_id':eligible[0]['fixture_id'] if eligible and not historical else None,
         'historical_comparison_first':eligible[0]['fixture_id'] if eligible and historical else None,
         'weights':WEIGHTS,'value_opportunity_validated':False,'actionable_bet':False,
         'runtime_seconds':round(time.perf_counter()-started,4),'api_calls':0}
    for x in results.values():
        t.dump(Path(x['detail_json']),x)
        x['detail_sha256']=t.sha(Path(x['detail_json']))
    write_report(doc,out);return doc


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--preselection',type=Path,required=True);p.add_argument('--pattern',type=Path)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--preflight',type=Path,action='append',default=[]);p.add_argument('--scanner',type=Path)
    p.add_argument('--profiles',type=Path);p.add_argument('--asof',help='Auditoría retrospectiva explícita; nunca genera selección accionable')
    a=p.parse_args(argv)
    try:
        when=t.dt(a.asof) if a.asof else None
        if a.asof and not when:raise ValueError('ASOF_REQUIRES_EXPLICIT_OFFSET')
        d=run_engine(a.preselection,a.pattern,a.output_dir,a.root,a.preflight,a.scanner,a.profiles,when,bool(a.asof))
        print(f"{d['mode']}: {len(d['fixtures'])} fixtures; API=0; selección={d['selected_fixture_id']}; {a.output_dir}")
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:print(f'ERROR: {exc}');return 2

if __name__=='__main__':raise SystemExit(main())
