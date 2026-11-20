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
from prematch_tactical_report_renderer import (
    enrich_series_denominators,
    build_window_slice,
    build_period_and_support_metrics,
    enrich_observations_from_raw,
    build_coach_regime_summary,
    enrich_signal_diagnostics,
    build_matchup_cross_matrix,
    build_tactical_scenarios,
    build_pattern_library_diagnostics,
    build_matchup_diagnostics,
    render_research_profiles_md,
    render_pattern_library_summary_md,
)

VERSION='2.1-robust-tactical-signals'
METRICS=('goals','corners','shots','on_target','fouls','yellow','offsides','xg')
FAMILY={'goals':'goals','corners':'corners','shots':'shots','on_target':'shots','fouls':'fouls','yellow':'cards','offsides':'offsides','xg':'goals'}
# Pesos orientados a robustez de señal (sin bonificación por volumen bruto de métricas o familias):
# sample_precision aplica contracción empírica Bayesiana n/(n+6) para evitar empates artificiales
# entre muestras pequeñas (n=4) y muestras sólidas (n>=12).
WEIGHTS={'sample_precision':.20,'stability':.25,'agreement':.20,'sensitivity':.20,'venue':.10,'profile':.05}
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
    base={**summary,'history_n':all_n,'history_not_detailed_n':all_n-len(detailed_records),'expected_n':len(detailed_records),'missing_n':len(detailed_records)-len(valid),
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
    return enrich_series_denominators(base,records,detailed_records,valid,venue,metric)


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
            'zero_n':None,'positive_n':None,'metric_observations_zero':None,'metric_observations_positive':None,
            'unobserved_total_n':available-n,'zero_fixture_ids':[],'missing_fixture_ids':[],'not_detailed_fixture_ids':[],
            'denominator_rule':'AGGREGATED_PRESELECTION',
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
    from prematch_cache_record import historical_available, acquisition_before
    output=[];seen=set();rejected=[];tid=fixture.get(side+'_id')
    for r in sorted(rows,key=lambda r:str(r.get('kickoff_utc','')),reverse=True):
        fid=r.get('fixture_id');at=t.dt(r.get('kickoff_utc'))
        why=None
        if tid is None or str(r.get('team_id'))!=str(tid):why='OBSERVATION_TEAM_ID_MISMATCH'
        elif not at or at>=cutoff or str(fid)==str(fixture['fixture_id']):why='POST_CUTOFF_OBSERVATION'
        elif not historical_available(r,cutoff):why='RESULT_NOT_AVAILABLE_AT_CUTOFF'
        elif r.get('acquired_at_utc') and not acquisition_before(r['acquired_at_utc'],cutoff):why='ACQUISITION_AFTER_CUTOFF'
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
    # Cobertura efectiva y solicitada se auditan como puerta de elegibilidad y diagnóstico,
    # nunca como bonificación para inflar muestras pequeñas (ej. 4/4 = 100%).
    coverage=mean([(s['historical_effective_coverage_pct'] or 0)/100 for s in (own,conceded)])
    min_effective_cov=min(((s['historical_effective_coverage_pct'] or 0)/100 for s in (own,conceded)), default=0.0)
    min_requested_cov=min(((s['requested_data_coverage_pct'] or 0)/100 for s in (own,conceded)), default=0.0)
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
    window_resilience=[]
    for s in (own,conceded):
        v=s['leave_one_out_mean_range']
        loo_s=1/(1+(v[1]-v[0])/max(s['mean'],1)) if v else 0
        rec_mean=(s.get('recent') or {}).get('mean')
        if rec_mean is not None and s.get('mean') is not None and (s.get('recent') or {}).get('n',0)>=3:
            win_s=1/(1+abs(rec_mean-s['mean'])/max(s['mean'],1))
            window_resilience.append(win_s)
            sensitivity.append(0.7*loo_s+0.3*win_s)
        else:
            sensitivity.append(loo_s)
    sample_precision=min(n/10,1.0)*(n/(n+6)) if n>0 else 0.0
    trend_consistency=1.0
    if a is not None and b is not None and a*b<0:
        trend_consistency=max(0.5, 1.0 - min(0.5, min(abs(a),abs(b))))
    raw_stability=(1/(1+cv))*trend_consistency if cv is not None else 0.0
    parts={'sample':min(n/10,1),'sample_precision':round(sample_precision,4),'coverage':coverage,
           'stability':round(raw_stability,4),'agreement':round(agreement,4),
           'sensitivity':round(min(sensitivity),4),'venue':min(venue_n/5,1),'profile':1 if confirmed else 0}
    score=max(0,100*sum(WEIGHTS[k]*parts[k] for k in WEIGHTS)-min(20,10*len(contradictions)))
    min_mean=min([s['mean'] for s in (own,conceded) if s.get('mean') is not None], default=1.0)
    # Conteos raros (goles/tarjetas) tienen CV natural Poisson 1/sqrt(mu); no marcar <1.56 como inestable por un umbral plano 0.8
    cv_limit=min(1.65,max(0.85,1.35/math.sqrt(max(0.25,min_mean)))) if metric in ('goals','yellow') else 0.90
    if n==0:status='INSUFFICIENT_DATA'
    elif n<4:status='SMALL_SAMPLE'
    elif cv is None:status='VARIABILITY_UNAVAILABLE'
    elif cv>cv_limit:status='UNSTABLE_PATTERN'
    elif contradictions:status='CONTRADICTORY_SIGNAL'
    elif score<55 or min_effective_cov<.40 or min_requested_cov<.50:status='PARTIAL_EVIDENCE'
    else:status='SUPPORTED_DESCRIPTIVE_SIGNAL'
    sos_context=fixture.get('sos_context') or {}
    sos_status='APPLIED' if sos_context.get('applied') else 'NOT_AVAILABLE_IN_PRESELECTION_SNAPSHOT'
    uncertainty={
        'effective_n':n,
        'sample_precision_weight':round(n/(n+6),4) if n>0 else 0.0,
        'uncertainty_se_scale':round(1.0/math.sqrt(max(1,n)),4) if n>0 else None,
        'laplace_shrunk_ceiling_rate':round((n+1)/(n+2),4) if n>0 else None,
        'window_resilience':round(min(window_resilience),4) if window_resilience else None,
        'sos_adjustment_status':sos_status,
        'score_interpretation':'DESCRIPTIVE_ROBUSTNESS_SCORE_NOT_CALIBRATED_PROBABILITY',
    }
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
    sig={'volume_associations':volume,'signal_id':f'{fixture["fixture_id"]}:{side}:{metric}','metric':metric,'family':FAMILY[metric],
         'team_side':side,'team_id':fixture.get(side+'_id'),'status':status,'priority_score':round(score,2),
         'components':parts,'uncertainty':uncertainty,'own':own,'opponent_conceded':conceded,'source_mode':source,
         'contradictions':contradictions,'profile_claim_ids':[p['claim_id'] for p in relevant],
         'confidence_level':'MODERATE_DESCRIPTIVE' if status=='SUPPORTED_DESCRIPTIVE_SIGNAL' and n>=10 and source=='FIXTURE_OBSERVATIONS' else 'LIMITED_DESCRIPTIVE',
         'hypothesis':f'Investigar la distribución de {metric} de {fixture[side]} confrontando producción propia y concesión rival; mecanismo táctico no confirmado.',
         'limitations':caveats,'market_status':'PENDIENTE_MERCADO','markets':[]}
    return enrich_signal_diagnostics(sig,fixture)


_GENERIC_CLUB_TOKENS = frozenset({'fc', 'cf', 'cd', 'ca', 'sc', 'ac', 'club', 'deportivo', 'atletico', 'real', 'de', 'la', 'los', 'las', 'el'})


def _team_side_from_market_prefix(prefix: str, fixture: dict[str, Any]) -> str | None:
    """Resuelve propietario en cabeceras tipo 'Montevideo Wanderers FC - Total de goles' sin falso emparejamiento."""
    from bwin_discovery import resolve_verified_team_side

    resolved = resolve_verified_team_side(prefix, fixture)
    if resolved in ("home", "away"):
        return resolved

    p_raw = str(prefix or "")
    observed_female = bool(re.search(r"\b(?:w|women|f|femenina|femenino|frauen)\b|\(f\)", p_raw, re.I))
    fixture_gender = str(fixture.get("gender") or "").upper()
    if fixture_gender not in ("F", "M") and re.search(
        r"\b(?:w|women|femenina)\b|\(f\)",
        f"{fixture.get('home', '')} {fixture.get('away', '')}",
        re.I,
    ):
        fixture_gender = "F"
    if (fixture_gender == "F" and not observed_female) or (fixture_gender == "M" and observed_female):
        return None

    p_norm = norm(prefix)
    p_tokens = set(p_norm.split()) - _GENERIC_CLUB_TOKENS
    matches: list[str] = []
    for s in ('home', 'away'):
        cand_names = [fixture.get(s), fixture.get(f'{s}_bwin')]
        matched = False
        for cname in cand_names:
            c_norm = norm(cname or '')
            if not c_norm:
                continue
            if p_norm == c_norm:
                matched = True
                break
            c_tokens = set(c_norm.split()) - _GENERIC_CLUB_TOKENS
            if c_tokens and p_tokens and (c_tokens <= p_tokens or p_tokens <= c_tokens):
                matched = True
                break
        if matched:
            matches.append(s)
    return matches[0] if len(matches) == 1 else None


def classify_market(market,fixture):
    value=norm(market)
    if any(x in value for x in ('live','en vivo','en directo','resto del','marcador actual','combinad','crea tus','jugador','goleador','descanso','parte','prorroga')):return None
    if not value.endswith(' | tiempo reglamentario'):return None
    base=value.split(' | tiempo reglamentario')[0].strip();side=None
    if ' - ' in base:
        prefix, rest = base.split(' - ', 1)
        resolved = _team_side_from_market_prefix(prefix, fixture)
        if resolved:
            side = resolved
            base = rest.strip()
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
            fixture_names=dict(fixture,home_bwin=events[0].get('bwin_home') or fixture.get('home_bwin'),away_bwin=events[0].get('bwin_away') or fixture.get('away_bwin'))
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append(str(exc));fixture_names=fixture
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
            spec=classify_market(q['market'],fixture_names)
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
    """Separa familias utilizables (con observaciones > 0) del mínimo global entre todas las métricas."""
    pairs=[series for signal in signals for series in (signal.get('own',{}),signal.get('opponent_conceded',{}))]
    usable=[row for row in pairs if isinstance(row.get('metric_observations_valid'),(int,float)) and row.get('metric_observations_valid',0)>0]
    active=usable or pairs
    def minimum(field, target_rows=active):
        values=[row[field] for row in target_rows if isinstance(row.get(field),(int,float))]
        return min(values) if values else None
    usable_families=sorted({s.get('family') for s in signals if min((s.get('own',{}).get('n') or 0),(s.get('opponent_conceded',{}).get('n') or 0))>=4})
    return {'basis':'USABLE_FAMILIES_WITH_ALL_SERIES_AUDIT',
            'usable_series_count':len(usable),
            'usable_families':usable_families,
            'historical_matches_available':minimum('historical_matches_available'),
            'historical_matches_detailed':minimum('historical_matches_detailed'),
            'historical_matches_not_detailed':minimum('historical_matches_not_detailed'),
            'metric_observations_valid':minimum('metric_observations_valid'),
            'metric_observations_missing':minimum('metric_observations_missing'),
            'minimum_across_all_families_valid':minimum('metric_observations_valid', pairs),
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
    usable=[
        s for s in signals
        if s['metric']!='xg'
        and s['status'] in ('SUPPORTED_DESCRIPTIVE_SIGNAL','PARTIAL_EVIDENCE','CONTRADICTORY_SIGNAL','UNSTABLE_PATTERN')
        and min(s.get('own',{}).get('n',0),s.get('opponent_conceded',{}).get('n',0))>=4
        and min(s.get('own',{}).get('historical_effective_coverage_pct') or 0,
                s.get('opponent_conceded',{}).get('historical_effective_coverage_pct') or 0)>=40
        and min(s.get('own',{}).get('requested_data_coverage_pct') or 0,
                s.get('opponent_conceded',{}).get('requested_data_coverage_pct') or 0)>=50
    ]
    # xG supports goal hypotheses; it is not a separate market family.
    best=sorted(supported,key=lambda s:-s['priority_score'])
    best_usable=sorted(usable,key=lambda s:-s['priority_score'])
    score=mean([s['priority_score'] for s in best[:3]]) if best else (mean([s['priority_score'] for s in best_usable[:3]]) if best_usable else 0)
    usable_cov=[s['components']['coverage']*100 for s in usable]
    coverage=mean(usable_cov) if usable_cov else (mean([s['components']['coverage']*100 for s in signals]) if signals else 0)
    return {'fixture_id':fixture['fixture_id'],'home':fixture['home'],'away':fixture['away'],
            'kickoff_madrid':fixture.get('kickoff_madrid'),'status':'BLOCKED' if blocked else 'EVALUATED',
            'block_reasons':blocked,'signals':signals,'signal_count':len(supported),
            'usable_signal_count':len(usable),
            'signal_families':sorted({s['family'] for s in supported}),
            'usable_signal_families':sorted({s['family'] for s in usable}),
            'confidence_level':'MODERATE_DESCRIPTIVE' if any(s['confidence_level']=='MODERATE_DESCRIPTIVE' for s in supported) else 'LIMITED_DESCRIPTIVE',
            'data_coverage':round(coverage,2),'coverage_summary':coverage_summary(signals),
            # This is deliberately independent of Bwin discovery and quote volume.
            'tactical_research_score':round(score,2),
            'profile_status':profile_status,'profile_rejections':profile_rejections,
            'profile_claims':evidence,'contradictions':sorted({x for s in signals for x in s['contradictions']}),
            'statistical_priority':fixture.get('statistical_priority'),
            'declared_coverage_bonus_applied':0.0,
            'score_interpretation':'DESCRIPTIVE_ROBUSTNESS_SCORE_NOT_CALIBRATED_PROBABILITY',
            'legacy_priority_score':round(score,2),
            'priority_score':round(score,2),'reason':'DESCRIPTIVE_SIGNALS_REQUIRE_MARKET_AND_MANUAL_RESEARCH' if best else ('CONDITIONAL_SIGNALS_WITH_LIMITATIONS' if best_usable else 'NO_ROBUST_FAMILY_SIGNAL'),
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


def load_expanded_statistical_dossiers(root, fixtures, asof, directory=None):
    """Reuse Objective 6/6.1 evidence without changing commercial readiness."""
    folder=Path(directory or Path(root)/'outputs/control/runs/objective6_1_international_final_v1')
    if not (folder/'manifest.json').is_file():
        return {}, {'status':'NOT_AVAILABLE','path':str(folder),'fixtures_reused':0}
    manifest=t.read(folder/'manifest.json');inputs=manifest.get('inputs') or {};loaded={};rejected=[]
    for fixture in fixtures:
        fid=str(fixture['fixture_id']);path=folder/f'{fid}.json'
        if not path.is_file():continue
        try:
            dossier=t.read(path);cut=t.dt(dossier.get('cutoff'));current=t.dt(fixture.get('analysis_cutoff_utc'))
            kickoff=t.dt(fixture.get('kickoff_madrid'));baseline=Path(root)/str(dossier.get('baseline_ref',''))
            expected=inputs.get(str(dossier.get('baseline_ref')))
            baseline_doc=t.read(baseline) if baseline.is_file() else {}
            identity=baseline_doc.get('fixture') or {}
            if (str(dossier.get('fixture_id'))!=fid or not cut or not current or not kickoff
                    or cut>current or current>asof or cut>=kickoff
                    or any(str(identity.get(k))!=str(fixture.get(k)) for k in ('fixture_id','home_id','away_id','league_id','season'))
                    or not expected or t.sha(baseline)!=expected or dossier.get('baseline_sha256')!=expected):
                raise ValueError('DOSSIER_IDENTITY_CUTOFF_OR_HASH_MISMATCH')
            signals=dossier.get('signals') or []
            primary=[s for s in signals if s.get('scope')=='competitive']
            counts=lambda rows:{name:sum(s.get('classification')==name for s in rows)
                                for name in ('VALIDADO','EXPLORATORIO','FRAGIL','INSUFFICIENT_SAMPLE','DESCARTADO')}
            effective=[(s.get('estimate') or {}).get('effective_n') for s in primary]
            effective=[v for v in effective if isinstance(v,(int,float)) and math.isfinite(v)]
            loaded[fid]={'status':'REUSED_CUTOFF_COMPATIBLE','source':str(path.resolve()),'sha256':t.sha(path),
                         'cutoff':cut.isoformat(),'current_analysis_cutoff':current.isoformat(),
                         'primary_scope':'competitive','primary_signal_count':len(primary),
                         'primary_classifications':counts(primary),'all_classifications':counts(signals),
                         'families':sorted({s.get('market_family') for s in primary if s.get('market_family')}),
                         'max_effective_n':round(max(effective),4) if effective else None,
                         'qualitative_only':True,'commercial_use':False,
                         'limitations':dossier.get('limitations',[])}
        except (OSError,ValueError,TypeError,KeyError) as exc:
            rejected.append({'fixture_id':fixture['fixture_id'],'reason':str(exc),'path':str(path)})
    source={'status':'REUSED' if loaded else 'NO_COMPATIBLE_FIXTURE','path':str(folder.resolve()),
            'manifest_sha256':t.sha(folder/'manifest.json'),'fixtures_reused':len(loaded),'rejected':rejected}
    return loaded,source


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
        # Penalizar solo contradicciones en las señales principales evaluadas (evita castigar
        # partidos con más familias medidas frente a partidos que solo tienen 1 familia).
        top_signals = sorted(
            [s for s in x.get('signals', []) if s.get('metric') != 'xg' and s.get('status') not in ('INSUFFICIENT_DATA', 'SMALL_SAMPLE')],
            key=lambda s: (-s.get('priority_score', 0), s.get('signal_id', '')),
        )[:3]
        relevant_contradictions = sorted({c for s in top_signals for c in (s.get('contradictions') or [])})
        # Joint ordering is an operational research queue, not a probability,
        # estimate of value or substitute for a model.
        score=.60*x.get('tactical_research_score',x.get('priority_score',0))+.40*readiness
        score=max(0,score-min(15,3*len(relevant_contradictions))+opportunity_adj)
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
            and s.get('status') in ('SUPPORTED_DESCRIPTIVE_SIGNAL','PARTIAL_EVIDENCE','CONTRADICTORY_SIGNAL','UNSTABLE_PATTERN')
            and min(s.get('own',{}).get('n',0),s.get('opponent_conceded',{}).get('n',0))>=4
            and min(s.get('own',{}).get('historical_effective_coverage_pct') or 0,
                    s.get('opponent_conceded',{}).get('historical_effective_coverage_pct') or 0)>=40
            and min(s.get('own',{}).get('requested_data_coverage_pct') or 0,
                    s.get('opponent_conceded',{}).get('requested_data_coverage_pct') or 0)>=50),
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
                      -min(15,3*len(relevant_contradictions))+opportunity_adj)
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


def run_engine(preselection,pattern,out,root=None,preflights=(),scanner=None,profiles=None,asof=None,historical=False,use_cache=True,statistical_dossiers=None):
    started=time.perf_counter();prepath=Path(preselection);out=Path(out);root=Path(root or prepath.parents[3]);asof=asof or t.now_madrid()
    pre=t.read(prepath);gen=t.dt(pre.get('generated_at_utc'))
    if not gen or gen>asof:raise ValueError('PRESELECTION_UNDATED_OR_AFTER_ASOF')
    from preselect_matches import validate_temporal_sources
    validate_temporal_sources(pre)
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
    expanded,expanded_source=load_expanded_statistical_dossiers(root,fixtures,asof,statistical_dossiers)
    for fid,evidence in expanded.items():
        results[fid]['expanded_statistical_evidence']=evidence
    for fid,x in results.items():
        x.setdefault('expanded_statistical_evidence',{'status':'NOT_AVAILABLE_FOR_FIXTURE','commercial_use':False})
    ranking=selection_rows(fixtures,results,scan,asof);eligible=[r for r in ranking if r['research_eligible']]
    for row in ranking:
        row['expanded_statistical_evidence']=results[str(row['fixture_id'])]['expanded_statistical_evidence']
    statistical_ranking=sorted([
        {'fixture_id':f['fixture_id'],'home':f['home'],'away':f['away'],
         'statistical_basis':'CURRENT_PRESELECTION_DESCRIPTIVE_ENGINE',
         'statistical_status':results[str(f['fixture_id'])]['status'],
         'statistical_research_score':results[str(f['fixture_id'])].get('tactical_research_score',0),
         'signal_count':results[str(f['fixture_id'])].get('signal_count',0),
         'signal_families':results[str(f['fixture_id'])].get('signal_families',[]),
         'coverage_summary':results[str(f['fixture_id'])].get('coverage_summary',{}),
         'contradictions':results[str(f['fixture_id'])].get('contradictions',[]),
         'expanded_6_1_evidence':results[str(f['fixture_id'])]['expanded_statistical_evidence'],
         # Compatibility alias; explicitly describes only the optional 6.1 dossier.
         'evidence':results[str(f['fixture_id'])]['expanded_statistical_evidence'],
         'commercial_readiness_not_used':True,'actionable_bet':False}
        for f in fixtures],key=lambda row:(
            -row['statistical_research_score'],-row['signal_count'],row['fixture_id']))
    for position,row in enumerate(statistical_ranking,1):row['position']=position
    doc={'schema_version':VERSION,'asof_madrid':asof.isoformat(),'mode':'RETROSPECTIVE_AUDIT' if historical else 'PREMATCH_RESEARCH_SCREEN',
         'sources':{'preselection':str(prepath.resolve()),'preselection_sha256':t.sha(prepath),'pattern_sha256':t.sha(Path(pattern)) if pattern else None,
                    'profiles_sha256':t.sha(Path(profiles)) if profiles and Path(profiles).is_file() else None,
                    'raw_cache_sources':list(cache.sources.values()) if cache else [],
                    'expanded_statistical_dossiers':expanded_source},
         'fixtures':results,'ranking':ranking,'statistical_ranking':statistical_ranking,
         'ranking_separation':{'statistical':'CURRENT_PRESELECTION_DESCRIPTIVE_EVIDENCE',
                               'expanded_6_1':'OPTIONAL_VERIFIED_FIXTURE_DOSSIER',
                               'commercial':'PREFLIGHT_SCANNER_READINESS',
                               'expanded_signals_do_not_create_picks':True},
         'selected_fixture_id':eligible[0]['fixture_id'] if eligible and not historical else None,
         'historical_comparison_first':eligible[0]['fixture_id'] if eligible and historical else None,
         'weights':WEIGHTS,'value_opportunity_validated':False,'actionable_bet':False,
         'runtime_seconds':round(time.perf_counter()-started,4),'api_calls':0}
    for x in results.values():
        t.dump(Path(x['detail_json']),x)
        x['detail_sha256']=t.sha(Path(x['detail_json']))
    write_report(doc,out);return doc


def research_context_category_matches(expected, observed):
    """Normalize explicit provider codes only; unknown is never corroboration."""
    canonical = {'M':'M', 'male':'M', 'F':'F', 'female':'F'}
    return expected in ('M','F') and canonical.get(observed) == expected


def research_profiles(preselection, output, asof, interpretation=None, contexts=(), retrospective=True, library=None):
    """Offline research using verified histories; immutable, resumable artifacts."""
    from datetime import timedelta
    from prematch_tactical_history import longitudinal_profile,contextual_segments,evolve_tactical_library,tactical_rule_value
    from prematch_predictive_evidence_ranking_v1 import write_once
    def persist(path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        raw=value if isinstance(value,bytes) else (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        if path.exists():
            if path.read_bytes()!=raw:raise ValueError('RESEARCH_IMMUTABLE_ARTIFACT_CONFLICT: '+str(path))
            return
        import os,tempfile
        fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.research-')
        try:
            with os.fdopen(fd,'wb') as fh:fh.write(raw);fh.flush();os.fsync(fh.fileno())
            try:os.link(tmp,path)
            except FileExistsError:
                if path.read_bytes()!=raw:raise ValueError('RESEARCH_CONCURRENT_CONFLICT')
        finally:Path(tmp).unlink(missing_ok=True)
    when=t.dt(asof)
    if not when:raise ValueError('ASOF_REQUIRES_EXPLICIT_OFFSET')
    started=time.perf_counter()
    library=Path(library) if library else Path(output)/'pattern_library'
    source=Path(preselection);pre=t.read(source);source_hash=t.sha(source);teams={};matches=[];ledger=[];sources=[];context_map={}
    cache_root=next((p for p in [source.resolve(),*source.resolve().parents] if (p/'outputs/data/cache/preselect_api').is_dir()),None)
    local_cache=LocalHistoryCache(cache_root,when) if cache_root else None
    if t.dt(pre.get('generated_at_utc')) and t.dt(pre['generated_at_utc'])>when:raise ValueError('RESEARCH_SOURCE_AFTER_ASOF')
    for path in contexts:
        path=Path(path);d=t.read(path);stamp=t.dt(d.get('generated_at'))
        if not stamp or stamp>when:raise ValueError('RESEARCH_CONTEXT_AFTER_ASOF')
        f=d['fixture'];fid=f['fixture_id']
        if fid in context_map:raise ValueError('RESEARCH_DUPLICATE_CONTEXT')
        if str(d.get('match',{}).get('verified_fixture_id'))!=str(fid):raise ValueError('RESEARCH_CONTEXT_IDENTITY')
        context_map[fid]=d;sources.append({'path':str(path.resolve()),'sha256':t.sha(path),'acquired_at':stamp.isoformat()})
    seen_fixtures=set()
    for original in pre['matches']:
        if original.get('fixture_id') in seen_fixtures:raise ValueError('RESEARCH_DUPLICATE_FIXTURE')
        seen_fixtures.add(original.get('fixture_id'))
        if any(not isinstance(original.get(k),int) or isinstance(original.get(k),bool) or original[k]<=0 for k in ('fixture_id','home_id','away_id','league_id','season')) or original['home_id']==original['away_id']:raise ValueError('RESEARCH_INVALID_IDENTITY')
        f=dict(original);kickoff=t.dt(f.get('kickoff_madrid'));cut=t.dt(f.get('analysis_cutoff_utc'))
        if not kickoff or not cut or cut>when or cut>=kickoff:raise ValueError('INVALID_RESEARCH_CUTOFF')
        if when>=kickoff:
            ledger.append({'fixture_id':f['fixture_id'],'status':'EXCLUDED','reason':'INVALID_CUTOFF_OR_STARTED'});continue
        observations={side:list((f.get('statistical_observations') or {}).get(side,[])) for side in ('home','away')};raw_by_team={};context=context_map.get(f['fixture_id'])
        if context:
            cf=context['fixture']
            if any(cf.get(k)!=f.get(k) for k in ('home_id','away_id','league_id','season')) or t.dt(cf.get('date'))!=kickoff:raise ValueError('RESEARCH_CONTEXT_IDENTITY')
            expected=f.get('gender') if f.get('gender') in ('M','F') else None
            if expected and any(not research_context_category_matches(expected,context['match'].get(k)) for k in ('home_gender','away_gender')):raise ValueError('RESEARCH_CONTEXT_CATEGORY')
            for side in ('home','away'):
                rows=context['context'].get(side+'_history_deep',{}).get('detailed_fixtures',[]);valid={}
                for r in rows:
                    rf=r.get('fixture',{});at=t.dt(rf.get('date'));fid=rf.get('id');ids=[r.get('teams',{}).get(x,{}).get('id') for x in ('home','away')]
                    if f[side+'_id'] not in ids or ids[0]==ids[1]:raise ValueError('RESEARCH_HISTORY_IDENTITY')
                    if fid==f['fixture_id'] or not at or at+timedelta(hours=3)>=when or rf.get('status',{}).get('short')!='FT':continue
                    if fid in valid and valid[fid]!=r:raise ValueError('RESEARCH_CONFLICTING_HISTORY')
                    valid[fid]=r
                raw_by_team[side]=list(valid.values())
                for r in valid.values():
                    home=r['teams']['home']['id']==f[side+'_id'];owner='home' if home else 'away';opp='away' if home else 'home'
                    def block(tid):
                        mapping={'Total Shots':'shots','Shots on Goal':'on_target','Corner Kicks':'corners','Fouls':'fouls','Yellow Cards':'yellow','Offsides':'offsides','expected_goals':'xg','Ball Possession':'possession_pct'}
                        return {mapping[x['type']]:float(str(x['value']).rstrip('%')) for group in r.get('statistics',[]) if group.get('team',{}).get('id')==tid for x in group.get('statistics',[]) if x.get('type') in mapping and x.get('value') is not None and str(x['value']).rstrip('%').replace('.','',1).isdigit()}
                    own=block(f[side+'_id']);con=block(r['teams'][opp]['id']);own['goals']=r['goals'].get(owner);con['goals']=r['goals'].get(opp)
                    new={'fixture_id':r['fixture']['id'],'team_id':f[side+'_id'],'opponent_id':r['teams'][opp]['id'],'kickoff_utc':r['fixture']['date'],'league_id':r['league']['id'],'season':r['league']['season'],'venue':'HOME' if home else 'AWAY','status':'FT','statistics_requested':True,'own':own,'conceded':con}
                    lineups=[l for l in r.get('lineups',[]) if l.get('team',{}).get('id')==f[side+'_id']]
                    if len(lineups)==1:
                        coach=lineups[0].get('coach') or {};cid=coach.get('id');cname=coach.get('name')
                        new.update(formation=lineups[0].get('formation'),coach_id=cid if isinstance(cid,int) and not isinstance(cid,bool) else None)
                        if cname:new['coach_name']=cname
                    observations[side]=[x for x in observations[side] if x['fixture_id']!=new['fixture_id']]+[new]
        f['analysis_cutoff_utc']=when.isoformat();f['statistical_observations']=observations;profiles=[];side_profiles={}
        for side in ('home','away'):
            rows=sorted(observations[side],key=lambda r:str(r.get('kickoff_utc','')),reverse=True)
            rows=[r for r in rows if t.dt(r.get('kickoff_utc')) and t.dt(r['kickoff_utc'])<when and r.get('team_id')==f[side+'_id'] and r.get('status')=='FT' and r['fixture_id']!=f['fixture_id']]
            unique={}
            for r in rows:
                if r['fixture_id'] in unique and r!=unique[r['fixture_id']]:raise ValueError('RESEARCH_DUPLICATE_CONFLICT')
                unique[r['fixture_id']]=r
            old_datasets=[]
            for path in (library/'team_history'/str(f[side+'_id'])).glob('*.json'):
                cached=t.read(path)
                if t.dt(cached['observed_at'])<=when and cached['season']==f['season']:
                    if hashlib.sha256(json.dumps(cached,sort_keys=True).encode()).hexdigest()!=path.stem:raise ValueError('TEAM_LIBRARY_CORRUPT')
                    old_datasets.append(cached)
            older={}
            for cached in sorted(old_datasets,key=lambda x:x['observed_at']):
                for r in cached['observations']:
                    if t.dt(r['kickoff_utc'])<when and r['fixture_id']!=f['fixture_id']:older[r['fixture_id']]=r
            older.update(unique);rows=sorted(older.values(),key=lambda r:r['kickoff_utc'],reverse=True)
            raw_history={}
            for cached in sorted(old_datasets,key=lambda x:x['observed_at']):
                for raw in cached.get('raw_rows',[]):
                    if raw['fixture']['id']!=f['fixture_id'] and t.dt(raw['fixture']['date'])<when:raw_history[raw['fixture']['id']]=raw
            if local_cache:
                for r in rows:
                    fid=r['fixture_id']
                    if fid in local_cache.records and fid not in raw_history:
                        _,raw,_=local_cache.records[fid]
                        pair=raw.get('teams') or {};ids={pair.get('home',{}).get('id'),pair.get('away',{}).get('id')}
                        if ids=={r.get('team_id'),r.get('opponent_id')} and t.dt(raw.get('fixture',{}).get('date'))==t.dt(r.get('kickoff_utc')):
                            raw_history[fid]=raw
            for raw in raw_by_team.get(side,[]):raw_history[raw['fixture']['id']]=raw
            raw_by_team[side]=sorted(raw_history.values(),key=lambda r:r['fixture']['id'])
            enrich_observations_from_raw(rows,raw_by_team[side],f[side+'_id'])
            observations[side]=rows
            dataset={'team_id':f[side+'_id'],'season':f['season'],'observed_at':when.isoformat(),'observations':rows,'raw_rows':raw_by_team[side]}

            dataset_key=hashlib.sha256(json.dumps(dataset,sort_keys=True).encode()).hexdigest()
            persist(library/'team_history'/str(f[side+'_id'])/(dataset_key+'.json'),dataset)
            same,rejected=filtered_observations(rows,f,side,when)
            venue_side='HOME' if side=='home' else 'AWAY'
            period_metrics,support_metrics=build_period_and_support_metrics(same,venue_side,stats,finite)
            longi=longitudinal_profile(f[side+'_id'],rows,raw_by_team.get(side,[]))
            coach_summary=build_coach_regime_summary(f[side+'_id'],same,rows,longi.get('coaches',[]),stats,finite)
            longi['coach_regime_summary']=coach_summary
            profile={'team_id':f[side+'_id'],'team_name':f.get(side),'venue_in_target_fixture':venue_side,
                'season':f['season'],'league_id':f['league_id'],
                'fixture_ids':[r['fixture_id'] for r in rows],'observations':rows,'rejections':rejected,
                'source_refs':[{'path':str(source.resolve()),'sha256':source_hash}]+sources,
                'metrics':{m:{k:summarize_series(same,m,k,venue_side) for k in ('own','conceded')} for m in METRICS},
                'period_metrics':period_metrics,'support_metrics':support_metrics,
                'coach_regime_summary':coach_summary,
                'windows':{},'longitudinal':longi,
                'validation':'DESCRIPTIVE_ONLY','actionable_bet':False}
            target_comp_key=f"{f['league_id']}:{f['season']}"
            window_samples=[
                ('GLOBAL',rows),
                ('HOME',[r for r in rows if r['venue']=='HOME']),
                ('AWAY',[r for r in rows if r['venue']=='AWAY']),
                (f"{target_comp_key}:HOME",[r for r in same if r['venue']=='HOME']),
                (f"{target_comp_key}:AWAY",[r for r in same if r['venue']=='AWAY']),
            ]+[(k,[r for r in rows if str(r['league_id'])+':'+str(r['season'])==k]) for k in profile['longitudinal']['competition_season_samples']]
            for label,sample in window_samples:
                profile['windows'][label]={str(n):build_window_slice(sample[:n],METRICS,stats,finite) for n in (5,10,20)}
                profile['windows'][label]['PREVIOUS_L5']=build_window_slice(sample[5:10],METRICS,stats,finite)
            profile['contextual']=contextual_segments(profile)
            key=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest();teams[key]=profile;profiles.append(key);side_profiles[side]=profile
            persist(Path(output)/'team_store'/str(profile['team_id'])/(key+'.json'),profile)
        x=analyze_fixture(f,when,observations)
        cross_matrix=build_matchup_cross_matrix(f,side_profiles['home'],side_profiles['away'],x['signals'],stats,finite)
        tactical_scenarios=build_tactical_scenarios(f,side_profiles['home'],side_profiles['away'],x['signals'],cross_matrix)
        matches.append({'fixture_id':f['fixture_id'],'teams':[f['home_id'],f['away_id']],'team_names':{'home':f.get('home'),'away':f.get('away')},'league_id':f['league_id'],'season':f['season'],'kickoff':kickoff.isoformat(),'profile_ids':profiles,'data_quality':x['data_coverage'],'pattern_strength':x['tactical_research_score'],'tactical_compatibility':None,'risk':'UNASSESSED','target_lineups':context['context'].get('lineups') if context else None,'lineup_status':'API_STRUCTURED_NOT_OFFICIAL_CERTIFICATION' if context else 'UNAVAILABLE','signals':x['signals'],'matchup_cross_matrix':cross_matrix,'tactical_scenarios':tactical_scenarios,'potential_market_families':x['signal_families'],'actionable_bet':False})
    if set(context_map)-{m['fixture_id'] for m in matches}:raise ValueError('UNUSED_OR_WRONG_CONTEXT')
    doc={'schema_version':'1.1-research-exchange','mode':'RETROSPECTIVE_RECONSTRUCTED' if retrospective else 'PREMATCH_RESEARCH',
         'asof':when.isoformat(),'source':str(source.resolve()),'source_sha256':source_hash,'code_sha256':t.sha(Path(__file__)),
         'history_code_sha256':t.sha(Path(__file__).with_name('prematch_tactical_history.py')),'context_sources':sources,'teams':teams,'matches':matches,'ledger':ledger,'api_calls':0}
    if interpretation:
        llm=t.read(Path(interpretation));seen=set()
        if llm.get('source_sha256')!=doc['source_sha256'] or llm.get('context_sources')!=sources:raise ValueError('LLM_SOURCE_SHA_MISMATCH')
        for claim in llm.get('claims',[]):
            match=next((m for m in matches if m['fixture_id']==claim.get('fixture_id')),None)
            if not match or claim.get('status') not in ('CONFIRMADO','INFERIDO','HIPÓTESIS','INSUFFICIENT_DATA'):raise ValueError('INVALID_LLM_CLAIM')
            allowed={i for k in match['profile_ids'] for i in teams[k]['fixture_ids']}
            if not claim.get('fixture_ids') or not set(claim['fixture_ids'])<=allowed:raise ValueError('LLM_EVIDENCE_UNBOUND')
            if not claim.get('counter_scenario') or not claim.get('falsifier') or not claim.get('mechanism'):raise ValueError('LLM_COUNTEREVIDENCE_REQUIRED')
            if not retrospective and t.dt(llm.get('generated_at')) is None:raise ValueError('LLM_GENERATION_TIME_REQUIRED')
            if t.dt(llm.get('generated_at')) and t.dt(llm['generated_at'])>when:raise ValueError('LLM_GENERATED_AFTER_ASOF')
            if claim['status']=='CONFIRMADO':
                ref=claim.get('verified_observation_source') or {}
                if not isinstance(ref,dict):raise ValueError('LLM_OBSERVATION_REQUIRED')
                path=Path(ref.get('path',''));at=t.dt(ref.get('acquired_at'))
                if not path.is_file() or t.sha(path)!=ref.get('sha256') or not at or at>when or not ref.get('excerpt') or ref['excerpt'] not in path.read_text(errors='replace'):raise ValueError('LLM_OBSERVATION_REQUIRED')
            if any(k in claim for k in ('EV','stake','p_model','odds')):raise ValueError('COMMERCIAL_OUTPUT_FORBIDDEN')
            for check in claim.get('quantitative_checks',[]):
                profiles=[teams[k] for k in match['profile_ids'] if teams[k]['team_id']==check.get('team_id')]
                if len(profiles)!=1:raise ValueError('LLM_QUANTITATIVE_IDENTITY')
                rows=[r for r in profiles[0]['observations'] if r['fixture_id'] in claim['fixture_ids']]
                vals=[r.get(check.get('kind','own'),{}).get(check['metric']) for r in rows]
                actual=stats([v for v in vals if finite(v)])
                if any(actual.get(k)!=check.get(k) for k in ('n','mean')):raise ValueError('LLM_QUANTITATIVE_MISMATCH')
            token=hashlib.sha256(json.dumps(claim,sort_keys=True).encode()).hexdigest()
            if token in seen:raise ValueError('LLM_DUPLICATE_CLAIM')
            seen.add(token)
        doc['interpretation']=llm
    library=Path(library) if library else Path(output)/'pattern_library'
    proposals=doc.get('interpretation',{}).get('team_patterns',[])
    if proposals and not retrospective and (not t.dt(doc['interpretation'].get('generated_at')) or t.dt(doc['interpretation']['generated_at'])>when):raise ValueError('PATTERN_IMPORT_TIME_UNVERIFIED')
    doc['pattern_library']=evolve_tactical_library(list(teams.values()),proposals,library,when,persist,retrospective)
    doc['pattern_library']['diagnostics']=build_pattern_library_diagnostics(list(teams.values()),proposals,doc['pattern_library'],bool(interpretation),tactical_rule_value)
    for match in matches:
        byside={side:[e for e in doc['pattern_library']['evaluations'] if e['team_id']==tid] for side,tid in zip(('home','away'),match['teams'])}
        match['pattern_matchup']={'home':byside['home'],'away':byside['away'],
            'contrasts':[{'home_pattern_id':h['pattern_id'],'away_pattern_id':a['pattern_id'],
                'status':'COEXISTENCE_REQUIRES_TACTICAL_REVIEW','home_mechanism':h['mechanism'],'away_mechanism':a['mechanism']} for h in byside['home'] for a in byside['away']],
            'missing':'No automatic claim of superiority; verify roles, lineups, coach and activation conditions',
            'actionable_bet':False}
    for review in doc.get('interpretation',{}).get('matchup_reviews',[]):
        match=next((m for m in matches if m['fixture_id']==review.get('fixture_id')),None)
        if not match:raise ValueError('MATCHUP_FIXTURE_UNBOUND')
        required={e['pattern_id'] for side in ('home','away') for e in match['pattern_matchup'][side]}
        references=set(review.get('pattern_ids',[]))
        if not references or not references<=required:raise ValueError('MATCHUP_PATTERN_UNBOUND')
        for side in ('home','away'):
            available={e['pattern_id'] for e in match['pattern_matchup'][side]}
            if available and not references&available:raise ValueError('MATCHUP_SIDE_NOT_ANALYZED')
        if not review.get('scenarios') or not review.get('counter_scenario') or not review.get('falsifier'):raise ValueError('MATCHUP_REASONING_REQUIRED')
        match['pattern_matchup']['codex_review']=review
        match['pattern_matchup']['status']='CONDITIONAL_SCOUT_REVIEW_NOT_VALIDATED'
    for match in matches:
        match['pattern_matchup']['diagnostics']=build_matchup_diagnostics(match,doc['pattern_library'],bool(interpretation))
    packets=[]
    for profile in teams.values():
        payload={'team_id':profile['team_id'],'season':profile['season'],'observations':profile['observations'],
                 'longitudinal':profile['longitudinal'],'contextual':profile['contextual']}
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest();path=library/'interpretation_packets'/str(profile['team_id'])/(digest+'.json')
        fresh=not path.exists();persist(path,payload)
        packets.append({'team_id':profile['team_id'],'data_hash':digest,'path':str(path.resolve()),'new_relevant_data':fresh})
    # Packet readiness is operational telemetry, not part of immutable result identity.
    doc['interpretation_packets']=[{k:v for k,v in packet.items() if k!='new_relevant_data'} for packet in packets]
    doc['ranking']=sorted([{'fixture_id':m['fixture_id'],'pattern_strength':m['pattern_strength'],'data_quality':m['data_quality'],'tactical_compatibility':None,'risk':m['risk']} for m in matches],key=lambda m:(-m['pattern_strength'],-m['data_quality'],m['fixture_id']))
    token=hashlib.sha256(json.dumps(doc,sort_keys=True).encode()).hexdigest()[:20];out=Path(output)/token
    persist(out/'research_profiles.json',doc)
    persist(out/'research_profiles.md',render_research_profiles_md(doc).encode('utf-8'))
    persist(out/'pattern_library_summary.md',render_pattern_library_summary_md(doc).encode('utf-8'))
    for ref in sources:
        if t.sha(Path(ref['path']))!=ref['sha256']:raise ValueError('SOURCE_CHANGED_DURING_RESEARCH')
    if t.sha(source)!=source_hash:raise ValueError('SOURCE_CHANGED_DURING_RESEARCH')
    from datetime import datetime,timezone
    persist(out/('execution_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'),{'api_calls':0,'new_interpretation_packets':sum(p['new_relevant_data'] for p in packets),'runtime_seconds':time.perf_counter()-started,'result_sha256':t.sha(out/'research_profiles.json')})
    return out,doc


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--preselection',type=Path,required=True);p.add_argument('--pattern',type=Path)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--preflight',type=Path,action='append',default=[]);p.add_argument('--scanner',type=Path)
    p.add_argument('--pattern-library',type=Path);p.add_argument('--research-context',type=Path,action='append',default=[]);p.add_argument('--research-profiles',action='store_true');p.add_argument('--interpretation',type=Path);p.add_argument('--profiles',type=Path);p.add_argument('--asof',help='Auditoría retrospectiva explícita; nunca genera selección accionable')
    a=p.parse_args(argv)
    try:
        when=t.dt(a.asof) if a.asof else None
        if a.asof and not when:raise ValueError('ASOF_REQUIRES_EXPLICIT_OFFSET')
        if a.research_profiles:
            out,doc=research_profiles(a.preselection,a.output_dir,a.asof or t.now_madrid().isoformat(),a.interpretation,a.research_context,bool(a.asof),a.pattern_library)
            print(f'RESEARCH_PROFILES: {out}; fixtures={len(doc["matches"])}; API=0');return 0
        d=run_engine(a.preselection,a.pattern,a.output_dir,a.root,a.preflight,a.scanner,a.profiles,when,bool(a.asof))
        print(f"{d['mode']}: {len(d['fixtures'])} fixtures; API=0; selección={d['selected_fixture_id']}; {a.output_dir}")
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:print(f'ERROR: {exc}');return 2

if __name__=='__main__':raise SystemExit(main())
