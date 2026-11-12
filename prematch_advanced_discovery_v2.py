#!/usr/bin/env python3
"""Offline interpretable association discovery over frozen PREMATCH histories.

No provider client, Bwin or LLM is instantiated. Thresholds are learned on the
older training partition. Fisher tests and global BY adjustments are descriptive
association checks, never calibrated match probabilities.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import combinations
from pathlib import Path
from statistics import median

import prematch_phase2 as players
from prematch_predictive_engine import dt, result_available_at
from prematch_predictive_evidence_ranking_v1 import write_once

VERSION='2.1-methodological-review'
STAT_FIELDS={'Total Shots':'shots','Shots on Goal':'on_target','Blocked Shots':'blocked_shots',
             'Corner Kicks':'corners','Fouls':'fouls','Yellow Cards':'yellow','Red Cards':'red',
             'Ball Possession':'possession_pct','Total passes':'passes','Passes accurate':'passes_accurate',
             'Passes %':'passes_accuracy_pct','expected_goals':'xg'}

def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def finite(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def valid_id(x):return isinstance(x,int) and not isinstance(x,bool) and x>0

def numeric(x):
    if isinstance(x,str):
        try:x=float(x.rstrip('%'))
        except ValueError:return None
    return x if finite(x) and x>=0 else None

def fisher(a,b,c,d):
    """Two-sided conditional exact test; fixed margins, includes ties."""
    n=a+b+c+d
    if not n:return 1.0
    r=a+b; col=a+c
    lo=max(0,r-(n-col)); hi=min(r,col)
    def logchoose(n,k):return math.lgamma(n+1)-math.lgamma(k+1)-math.lgamma(n-k+1)
    def prob(x):return math.exp(logchoose(col,x)+logchoose(n-col,r-x)-logchoose(n,r))
    observed=prob(a)
    return min(1.0,sum(prob(x) for x in range(lo,hi+1) if prob(x)<=observed+1e-12))

def bh(values):
    """Benjamini-Hochberg, deterministic ties; includes ALL attempted tests."""
    n=len(values); result=[1.0]*n; bound=1.0
    for rank,index in reversed(list(enumerate(sorted(range(n),key=lambda i:(values[i],i)),1))):
        bound=min(bound,values[index]*n/rank);result[index]=bound
    return result

def by_adjust(values):
    # BY controls FDR under arbitrary dependence between the tested hypotheses.
    correction=sum(1/i for i in range(1,len(values)+1))
    return [min(1.0,q*correction) for q in bh(values)]

def allowed_ref(ref,cutoff,retrospective):
    acquired=dt((ref or {}).get('fetched_at_utc'))
    return bool(ref and acquired and (acquired<=cutoff or retrospective))

class EnrichedCache(players.PlayerCache):
    """Also reuse API /fixtures?ids=... embedded detail, retaining its SHA."""
    def get(self,endpoint,fid,cutoff):
        response,ref=super().get(endpoint,fid,cutoff)
        if response is not None or not endpoint.startswith('fixtures/'):return response,ref
        fixture,fixture_ref=super().get('fixtures',fid,cutoff)
        field=endpoint.split('/',1)[1]
        embedded=(fixture or {}).get(field)
        if isinstance(embedded,list):return embedded,{**fixture_ref,'embedded_field':field}
        return None,None

def cache_identity(cache,row,cutoff,retrospective):
    doc,ref=cache.get('fixtures',row['fixture_id'],cutoff)
    if not doc or not allowed_ref(ref,cutoff,retrospective):return None,None
    f=doc.get('fixture',{});t=doc.get('teams',{});l=doc.get('league',{})
    h=t.get('home',{}).get('id');a=t.get('away',{}).get('id')
    expected=(row['team_id'],row['opponent_id']) if row['venue']=='HOME' else (row['opponent_id'],row['team_id'])
    identities={(x.get('teams',{}).get('home',{}).get('id'),x.get('teams',{}).get('away',{}).get('id'),x.get('league',{}).get('id'),x.get('league',{}).get('season'),x.get('fixture',{}).get('date')) for x,_ in cache.candidates('fixtures',row['fixture_id'])}
    if len(identities)!=1 or (h,a)!=expected or f.get('id')!=row['fixture_id'] or f.get('status',{}).get('short')!='FT' or dt(f.get('date'))!=dt(row['kickoff_utc']) or l.get('id')!=row['league_id'] or l.get('season')!=row['season']:
        return None,None
    return doc,ref

def normalize(pre,source_ref,cache=None,retrospective=False):
    cutoff=dt(pre.get('generated_at_utc'))
    if not cutoff:raise ValueError('SOURCE_CUTOFF_MISSING')
    # Never combine histories beyond an earlier target's original analysis cut.
    cutoff=min([cutoff]+[value for m in pre.get('matches',[]) if (value:=dt(m.get('analysis_cutoff_utc'))) is not None])
    rows={}; targets=[]; rejected=[]; conflicts=set(); player_rows={}; cache_audits=[]
    for target in pre.get('matches',[]):
        kickoff=dt(target.get('kickoff_madrid')); local_cut=dt(target.get('analysis_cutoff_utc'))
        if not kickoff or not local_cut or kickoff<=cutoff or not all(valid_id(target.get(k)) for k in ('fixture_id','home_id','away_id')) or target['home_id']==target['away_id']:
            rejected.append({'fixture_id':target.get('fixture_id'),'reason':'TARGET_CUTOFF_INVALID'});continue
        cut=min(cutoff,local_cut,kickoff)
        targets.append({k:target.get(k) for k in ('fixture_id','home_id','away_id','home','away','league_id','season','gender','kickoff_madrid')})
        for side in ('home','away'):
            for original in target.get('statistical_observations',{}).get(side,[]):
                available,policy=result_available_at(original)
                when=dt(original.get('kickoff_utc'))
                reason=None
                if not valid_id(original.get('fixture_id')):reason='FIXTURE_ID_INVALID'
                elif original.get('team_id')!=target.get(side+'_id'):reason='TEAM_ID_CONFLICT'
                elif original.get('fixture_id')==target['fixture_id']:reason='TARGET_IN_HISTORY'
                elif not when or not available or available>=cut or original.get('status')!='FT':reason='NOT_FINAL_BEFORE_CUTOFF'
                elif original.get('venue') not in ('HOME','AWAY') or not valid_id(original.get('opponent_id')) or original.get('opponent_id')==original.get('team_id'):reason='INVALID_HOME_AWAY_IDENTITY'
                elif not valid_id(original.get('league_id')) or not valid_id(original.get('season')):reason='COMPETITION_SEGMENT_UNVERIFIED'
                if reason:
                    rejected.append({'fixture_id':original.get('fixture_id'),'reason':reason});continue
                identity=(original['fixture_id'],original['team_id'],target.get('gender','UNKNOWN'))
                rec={k:original.get(k) for k in ('fixture_id','team_id','opponent_id','kickoff_utc','league_id','season','venue','status')}
                rec.update(entity_type='TEAM',player_id=None,gender=target.get('gender','UNKNOWN'),team_kind=target.get('team_kind','UNKNOWN'),
                           period='FULL_TIME',metrics={},conditions={'venue':original['venue']},
                           provenance=[source_ref],completion_policy=policy,available_at_utc=available.isoformat(),
                           eligible_cutoff_utc=cut.isoformat(),
                           acquisition_status='FROZEN_PRESELECTION',target_fixture_ids=[target['fixture_id']])
                for block in ('own','conceded'):
                    for metric,value in (original.get(block) or {}).items():
                        rec['metrics'][f'{block}.{metric}']=numeric(value)
                previous=rows.get(identity)
                if previous:
                    # Partial detail requests can coexist. Merge only equal or
                    # complementary observations; conflicting identities/values exclude.
                    fields=('opponent_id','kickoff_utc','league_id','season','venue')
                    bad=any(previous[k]!=rec[k] for k in fields) or any(v is not None and previous['metrics'].get(k) is not None and previous['metrics'][k]!=v for k,v in rec['metrics'].items())
                    if bad:conflicts.add(identity);rejected.append({'fixture_id':original['fixture_id'],'reason':'DUPLICATE_CONFLICT'});continue
                    previous['metrics'].update({k:v for k,v in rec['metrics'].items() if v is not None})
                    previous['target_fixture_ids']=sorted(set(previous['target_fixture_ids']+rec['target_fixture_ids']))
                    previous['eligible_cutoff_utc']=min(dt(previous['eligible_cutoff_utc']),cut).isoformat()
                else:rows[identity]=rec
        if cache:
            audit={}
            obs,_=players.collect_player_observations(target,cache.root,cut,audit,cache)
            cache_audits.append(audit)
            for p in obs:
                if not p['performance_eligible'] or not all(allowed_ref(ref,cut,retrospective) for ref in p['provenance']):continue
                key=(p['fixture_id'],p['team_id'],p['player_id'])
                rec={k:p.get(k) for k in ('fixture_id','team_id','player_id','player_name','opponent_id','kickoff_utc','league_id','season','venue','minutes','starter','position','provenance')}
                rec.update(entity_type='PLAYER',gender=target.get('gender','UNKNOWN'),team_kind=target.get('team_kind','UNKNOWN'),period='FULL_TIME',
                           conditions={k:p.get(k) for k in ('venue','starter','position')},
                           metrics={f'player.{k}':v if k not in p['metric_issues'] else None for k,v in p['metrics'].items()},
                           acquisition_status=p['availability_status'],target_fixture_ids=[target['fixture_id']])
                # Per90 uses only observed positive minutes; retain totals too.
                rec['per90']={k:90*v/p['minutes'] for k,v in rec['metrics'].items() if finite(v) and k!='player.minutes'}
                rec['metrics'].update({'rate90.'+k:v if p['minutes']>=30 else None for k,v in rec['per90'].items()})
                rec['raw_statistics']=p['raw_statistics']
                aliases=set(players.PLAYER_METRICS.values())
                for block in p['raw_statistics']:
                    for group,values in block.items():
                        if group=='games' or not isinstance(values,dict):continue
                        for field,value in values.items():
                            if (group,field) not in aliases:
                                rec['metrics'][f'player_raw.{group}.{field}']=numeric(value)
                if key in player_rows:
                    if player_rows[key]['metrics']!=rec['metrics']:raise ValueError('PLAYER_DUPLICATE_CONFLICT')
                    player_rows[key]['target_fixture_ids']=sorted(set(player_rows[key]['target_fixture_ids']+rec['target_fixture_ids']))
                else:player_rows[key]=rec
    team_rows=[r for key,r in rows.items() if key not in conflicts]
    for rec in team_rows:
        cut=dt(rec['eligible_cutoff_utc'])
        if cache:
            doc,ref=cache_identity(cache,rec,cut,retrospective)
            if doc:
                rec['provenance'].append(ref)
                for endpoint in ('fixtures/statistics','fixtures/lineups','fixtures/events'):
                    response,ref=cache.get(endpoint,rec['fixture_id'],cut)
                    if response is None or not allowed_ref(ref,cut,retrospective):continue
                    rec['provenance'].append(ref)
                    if endpoint=='fixtures/statistics':
                        blocks=[b for b in response if b.get('team',{}).get('id')==rec['team_id']]
                        if len(blocks)==1:
                            for item in blocks[0].get('statistics',[]):
                                name=STAT_FIELDS.get(item.get('type'))
                                if name and f'own.{name}' not in rec['metrics']:rec['metrics'][f'own.{name}']=numeric(item.get('value'))
                    elif endpoint=='fixtures/lineups':
                        blocks=[b for b in response if b.get('team',{}).get('id')==rec['team_id']]
                        if len(blocks)==1:rec['conditions']['formation']=blocks[0].get('formation')
                    else:
                        rec['events']=[e for e in response if e.get('team',{}).get('id') in (rec['team_id'],rec['opponent_id'])]
                # Official score periods only; never apportion FT shots by minute.
                score=doc.get('score',{});ft=score.get('fulltime',{});half=score.get('halftime',{})
                label='home' if rec['venue']=='HOME' else 'away'
                if finite(ft.get(label)) and finite(half.get(label)) and 0<=half[label]<=ft[label]:
                    rec['metrics']['own.goals_h1']=half[label];rec['metrics']['own.goals_h2']=ft[label]-half[label]
                if any(dt(r.get('fetched_at_utc')) and dt(r.get('fetched_at_utc'))>cut for r in rec['provenance']):rec['acquisition_status']='RETROSPECTIVE_RECONSTRUCTION'
    # Context is computed only from records already finished at each historical KO.
    # An observed gap is NOT a guaranteed rest interval in an incomplete schedule.
    for rec in team_rows:
        when=dt(rec['kickoff_utc'])
        same=[r for r in team_rows if r['team_id']==rec['team_id'] and r['gender']==rec['gender'] and dt(r['available_at_utc'])<when]
        rec['metrics']['context.observed_gap_days']=(when-max(dt(r['kickoff_utc']) for r in same)).total_seconds()/86400 if same else None
        comparable=[r for r in same if (r['league_id'],r['season'])==(rec['league_id'],rec['season'])]
        rec['lag_reference_fixture_ids']={}
        for metric in sorted(k for k in rec['metrics'] if k.startswith('own.')):
            valid=[r for r in comparable if finite(r['metrics'].get(metric))]
            prior=max(valid,key=lambda r:r['kickoff_utc']) if valid else None
            rec['metrics']['lag.'+metric]=prior['metrics'][metric] if prior else None
            rec['lag_reference_fixture_ids'][metric]=prior['fixture_id'] if prior else None
        rivals=[r for r in team_rows if r['team_id']==rec['opponent_id'] and (r['league_id'],r['season'],r['gender'])==(rec['league_id'],rec['season'],rec['gender']) and dt(r['available_at_utc'])<when and finite(r['metrics'].get('own.goals')) and finite(r['metrics'].get('conceded.goals'))]
        rec['opponent_reference_fixture_ids']=[r['fixture_id'] for r in rivals]
        rec['metrics']['context.opponent_prior_ppg']=sum(3 if r['metrics']['own.goals']>r['metrics']['conceded.goals'] else 1 if r['metrics']['own.goals']==r['metrics']['conceded.goals'] else 0 for r in rivals)/len(rivals) if len(rivals)>=4 else None
        rec['conditions']['opponent_strength']=('STRONG' if rec['metrics']['context.opponent_prior_ppg']>=2 else 'WEAK' if rec['metrics']['context.opponent_prior_ppg']<1 else 'MEDIUM') if rec['metrics']['context.opponent_prior_ppg'] is not None else None
    result=sorted(team_rows+list(player_rows.values()),key=lambda r:(r['kickoff_utc'],r['fixture_id'],r['team_id'],r.get('player_id') or 0))
    return result,targets,{'rejected':rejected,'player_audits':cache_audits,'cutoff_utc':cutoff.isoformat()}

def split_history(rows):
    rows=sorted(rows,key=lambda r:(r['kickoff_utc'],r['fixture_id']))
    cut=max(1,int(len(rows)*.7))
    boundary=rows[cut]['kickoff_utc'] if cut<len(rows) else rows[-1]['kickoff_utc']
    return [r for r in rows if r['kickoff_utc']<boundary],[r for r in rows if r['kickoff_utc']>=boundary]

def metric_period(metric):
    return 'FIRST_HALF' if metric.endswith('_h1') else 'SECOND_HALF' if metric.endswith('_h2') else 'FULL_TIME'

def methodological_review(pattern,observations,min_group=3):
    """Add explicit semantics without refitting thresholds or changing p/q."""
    p=pattern;outcome=p['variables']['outcome'];condition=p['variables']['condition']
    p['observation_period']=p.get('observation_period',p['period'])
    p['period']=metric_period(outcome)
    p['condition_period']=metric_period(condition['field']) if condition['operator']=='GT' else 'CONTEXT'
    p['commercial_adapter']['period']=p['period']
    if outcome.endswith(('_h1','_h2')):p['commercial_adapter']['metric']='goals'
    a,b,c,d=p['holdout']['counts'];effect=p['holdout']['risk_difference'];train=p['train']['risk_difference']
    direction='OPPOSITE' if effect is not None and train is not None and effect*train<0 else 'CONSISTENT' if effect and train and effect*train>0 else 'NO_DIRECTIONAL_EFFECT'
    status=('NO_VALID_OBSERVATIONS' if not a+b+c+d else 'NO_COMPARABLE_REFERENCE_GROUP' if not (a+b and c+d) else
            'NO_OUTCOME_VARIATION' if not (a+c and b+d) else 'INSUFFICIENT_COMPARABLE_SAMPLE' if min(a+b,c+d)<min_group else
            'CONTRARY_EFFECT' if direction=='OPPOSITE' else 'CONSISTENT_EFFECT' if direction=='CONSISTENT' else 'NO_OBSERVED_EFFECT')
    p['holdout_assessment']={'status':status,'descriptive_direction':direction,'comparison_possible':bool(a+b and c+d),'sufficient_group_support':min(a+b,c+d)>=min_group,'predictive_validation':False}
    p['methodology_version']=VERSION
    if outcome.startswith('rate90.'):
        base=outcome.removeprefix('rate90.')
        audit={}
        for label in ('train','holdout'):
            rows=[r for r in observations if r.get('player_id')==p['player_id'] and r['team_id']==p['team_id'] and r['fixture_id'] in p[label]['fixture_ids'] and finite(r['metrics'].get(outcome))]
            records=[]
            for r in rows:
                minutes=r.get('minutes');value=r['metrics'].get(base)
                valid=finite(minutes) and minutes>=30 and finite(value)
                expected=90*value/minutes if valid else None
                records.append({'fixture_id':r['fixture_id'],'minutes':minutes,'raw_metric':value,'rate90':r['metrics'][outcome],'formula_verified':bool(valid and math.isclose(expected,r['metrics'][outcome],rel_tol=1e-9)),'low_exposure':bool(finite(minutes) and minutes<60),'starter':r.get('starter'),'position':r.get('position')})
            audit[label]={'metric':base,'unit':'events_per_90_minutes','formula':'90 * observed_count / eligible_minutes','observed_rate_records':len(rows),'valid_matches':sum(r['formula_verified'] for r in records),'eligible_minutes':sum(r['minutes'] for r in records if r['formula_verified']),'records':records,'sensitivity_excluding_under60':table([r for r in rows if r.get('minutes',0)>=60],condition,outcome,p['variables']['training_threshold'])}
        p['per90_audit']=audit
        if any(not r['formula_verified'] for section in audit.values() for r in section['records']):
            p['predictive_use']={'authorized':False,'reason':'PER90_DENOMINATOR_UNVERIFIED'}
    p['predictive_use']['authorized']=False
    return p

def tactical_questions(p):
    who=f"team_id {p['team_id']}"+(f", player_id {p['player_id']}" if p['player_id'] else '')
    condition=p['variables']['condition'];rivals=[a['opponent_id'] for a in p['next_match_applicability']]
    counter=p['counterevidence_fixture_ids']
    questions=[f"For {who}, what dated evidence explains {condition} versus {p['variables']['outcome']} > {p['variables']['training_threshold']}? Distinguish recorded position/formation from demonstrated behaviour.",
        f"Compare counterexamples {counter} with supporting fixtures {p['evidence_fixture_ids']}; identify changes in minutes, roles, opponents and score without assuming a cause.",
        f"For upcoming opponent IDs {rivals}, is the same condition observable before kickoff? What official lineup or positional evidence would refute applicability?",
        f"Holdout assessment is {p['holdout_assessment']['status']}: what missing comparable matches or contrary evidence prevents treating this as validated?",
        'Supply independently traceable sources dated before the original cutoff; return UNVERIFIED for missing facts.']
    if p.get('per90_audit'):
        low=[r['fixture_id'] for section in p['per90_audit'].values() for r in section['records'] if r['low_exposure']]
        questions.append(f'For player {p["player_id"]}, review low-exposure fixtures {low}: do role, substitution time and team possession explain the rate? Contrast counts, eligible minutes and the fixed-threshold sensitivity; do not extrapolate to 90 actual minutes.')
    if p['period']!='FULL_TIME':questions.append(f'Outcome period is {p["period"]}; verify the period-specific event evidence and historical lag before discussing fixture {rivals}. Do not treat an H1 outcome as FT.')
    return questions

def reprocess_frozen(base,out,pattern_ids=(),preflights=()):
    """Re-label frozen findings without changing fitted values or old artifacts."""
    base=Path(base);out=Path(out)
    parent=json.loads((base/'manifest.json').read_text())
    for name,expected in parent['artifacts'].items():
        if hashlib.sha256((base/name).read_bytes()).hexdigest()!=expected:raise ValueError('FROZEN_ARTIFACT_SHA_CONFLICT')
    old=json.loads((base/'advanced_patterns.json').read_text())
    observations=json.loads((base/'historical_observations.json').read_text())
    dossiers=json.loads((base/'research_dossiers.json').read_text())
    selected=[p for p in old['patterns'] if not pattern_ids or any(p['pattern_id'].startswith(i) for i in pattern_ids)]
    if pattern_ids and (len(selected)!=len(pattern_ids) or any(sum(p['pattern_id'].startswith(i) for p in selected)!=1 for i in pattern_ids)):raise ValueError('PATTERN_SELECTION_NOT_UNIQUE')
    # JSON round-trip isolates objects from the original loaded snapshot.
    selected=json.loads(json.dumps(selected));before={p['pattern_id']:json.loads(json.dumps(p)) for p in selected}
    for p in selected:methodological_review(p,observations['observations'])
    kept=[]
    for d in dossiers['hypotheses']:
        p=next((p for p in selected if p['pattern_id']==d['pattern_id']),None)
        if p is None:continue
        d.update(entity=dict(d['entity'],period=p['period']),questions=tactical_questions(p),holdout_assessment=p['holdout_assessment'],per90_audit=p.get('per90_audit'),
                 explanation_status='PENDING_EXTERNAL_EVIDENCE',mechanism=None,predictive_use=p['predictive_use'],
                 knowledge_stages={'statistical_finding':p['validation_status'],'llm_hypothesis':'NOT_DOCUMENTED','sourced_explanation':'PENDING_EXTERNAL_EVIDENCE','predictive_pattern':'NOT_VALIDATED'})
        kept.append(d)
    comparison=[{'pattern_id':p['pattern_id'],'before_period':before[p['pattern_id']]['period'],'after_period':p['period'],
        'before_same_direction':before[p['pattern_id']]['stability']['same_direction'],'holdout_assessment':p['holdout_assessment'],
        'statistics_unchanged':all(before[p['pattern_id']][k]==p[k] for k in ('train','holdout','uncertainty','variables','validation_status'))} for p in selected]
    from prematch_history_recovery_v2 import diversification_proposal
    proposal=diversification_proposal(observations['observations'],observations['targets'],preflights)
    files={'advanced_patterns.json':{'schema_version':VERSION,'patterns':selected,'actionable_bet':False},
           'research_dossiers.json':{'schema_version':VERSION,'hypotheses':kept},'comparison.json':comparison,'acquisition_proposal.json':proposal}
    hashes={}
    for name,doc in files.items():
        raw=json.dumps(doc,ensure_ascii=False,sort_keys=True,indent=2)+'\n';write_once(out/name,raw);hashes[name]=hashlib.sha256(raw.encode()).hexdigest()
    manifest={'schema_version':VERSION,'parent_manifest':str((base/'manifest.json').resolve()),'parent_manifest_sha256':hashlib.sha256((base/'manifest.json').read_bytes()).hexdigest(),
              'artifacts':hashes,'mode':'FROZEN_REVIEW_NO_STATISTICAL_REFIT','selected_patterns':len(selected),'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_once(out/'manifest.json',json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    return manifest

def table(rows,condition,outcome,threshold):
    cells=[[],[],[],[]];valid=[]
    for r in rows:
        value=r['metrics'].get(outcome)
        if not finite(value):continue
        field=condition['field']; actual=r['conditions'].get(field) if condition['operator']=='EQ' else r['metrics'].get(field)
        if actual is None:continue
        yes=actual==condition['value'] if condition['operator']=='EQ' else actual>condition['value']
        high=value>threshold
        index=(0 if high else 1) if yes else (2 if high else 3)
        cells[index].append(r['fixture_id']);valid.append(r)
    a,b,c,d=map(len,cells);n=a+b+c+d
    effect=a/(a+b)-c/(c+d) if a+b and c+d else None
    def wilson(k,n):
        if not n:return [None,None]
        z=1.96; center=(k/n+z*z/(2*n))/(1+z*z/n)
        width=z*math.sqrt((k/n)*(1-k/n)/n+z*z/(4*n*n))/(1+z*z/n)
        return [max(0,center-width),min(1,center+width)]
    first=wilson(a,a+b);second=wilson(c,c+d)
    interval=[first[0]-second[1],first[1]-second[0]] if effect is not None else [None,None]
    leave_one=[]
    for cell,count in enumerate((a,b,c,d)):
        values=[a,b,c,d]
        if count:
            values[cell]-=1;aa,bb,cc,dd=values
            if aa+bb and cc+dd:leave_one.append(aa/(aa+bb)-cc/(cc+dd))
    return {'counts':[a,b,c,d],'n':n,'p_value':fisher(a,b,c,d),'risk_difference':effect,
            'descriptive_difference_interval':interval,'interval_method':'CONSERVATIVE_WILSON_BOUND_DIFFERENCE_NOT_PREDICTIVE',
            'leave_one_out_effect_range':[min(leave_one),max(leave_one)] if leave_one else None,
            'condition_true_high':cells[0],'condition_true_low':cells[1],
            'reference_high':cells[2],'reference_low':cells[3],
            'fixture_ids':sorted(set(r['fixture_id'] for r in valid))}

def discover(observations,targets,*,max_tests=64,min_group=3):
    groups=defaultdict(list)
    for r in observations:
        # Team kind/category and competitions are never pooled implicitly.
        key=(r['entity_type'],r['team_id'],r.get('player_id'),r['league_id'],r['season'],r['gender'],r['team_kind'],r['period'])
        groups[key].append(r)
    patterns=[];budget=[]
    for key,rows in sorted(groups.items(),key=lambda x:str(x[0])):
        train,test=split_history(rows)
        if len(train)<2*min_group:
            budget.append({'entity':key,'train_n':len(train),'holdout_n':len(test),'possible_tests':0,'executed':0,'untested_due_to_budget':0,'status':'INSUFFICIENT_TRAINING_SAMPLE'})
            continue
        metrics=sorted({k for r in train for k,v in r['metrics'].items() if finite(v)})
        outcomes=[k for k in metrics if k.startswith(('own.','player.','player_raw.','rate90.')) and k!='player.minutes']
        conditions=[]
        for field in sorted({k for r in train for k in r['conditions']}):
            values=sorted({str(r['conditions'][field]) for r in train if r['conditions'].get(field) is not None})
            if len(values)==2:values=values[:1]  # complementary binary tests are identical
            for value in values:
                actual=next(r['conditions'][field] for r in train if str(r['conditions'].get(field))==value)
                conditions.append({'field':field,'operator':'EQ','value':actual})
        for metric in metrics:
            vals=[r['metrics'][metric] for r in train if finite(r['metrics'].get(metric))]
            if len(set(vals))>1:conditions.append({'field':metric,'operator':'GT','value':median(vals)})
        tests=[]
        # Round-robin by outcome so the budget is not exhausted by one metric.
        for index in range(len(conditions)):
            for outcome in outcomes:
                cond=conditions[index]
                if cond['field']==outcome:continue
                if cond['field'].startswith('own.') and outcome.startswith('own.') and cond['field']>outcome:continue
                vals=[r['metrics'][outcome] for r in train if finite(r['metrics'].get(outcome))]
                if len(vals)<2*min_group or len(set(vals))<2:continue
                threshold=median(vals)
                t=table(train,cond,outcome,threshold)
                a,b,c,d=t['counts']
                if min(a+b,c+d)<min_group:continue
                tests.append((cond,outcome,threshold,t))
        budget.append({'entity':key,'possible_tests':len(tests),'executed':min(len(tests),max_tests),'untested_due_to_budget':max(0,len(tests)-max_tests)})
        buckets=defaultdict(list)
        for candidate in tests:
            cond=candidate[0]
            category='CONDITIONAL' if cond['operator']=='EQ' or cond['field'].startswith('context.') else 'CROSSED' if cond['field'].startswith('conceded.') else 'OWN'
            buckets[category].append(candidate)
        balanced=[]
        for index in range(max((len(v) for v in buckets.values()),default=0)):
            for category in ('OWN','CROSSED','CONDITIONAL'):
                if index<len(buckets[category]):balanced.append(buckets[category][index])
        for cond,outcome,threshold,t in balanced[:max_tests]:
            h=table(test,cond,outcome,threshold)
            category='CONDITIONAL' if cond['operator']=='EQ' or cond['field'].startswith('context.') else 'CROSSED' if cond['field'].startswith('conceded.') else 'OWN'
            identity={'entity':key,'condition':cond,'outcome':outcome,'threshold':threshold,'train_ids':t['fixture_ids'],'test_ids':h['fixture_ids']}
            pid=digest(identity)
            favorable=t['condition_true_high'] if (t['risk_difference'] or 0)>=0 else t['condition_true_low']
            contrary=t['condition_true_low'] if (t['risk_difference'] or 0)>=0 else t['condition_true_high']
            holdout_for=h['condition_true_high'] if (t['risk_difference'] or 0)>=0 else h['condition_true_low']
            holdout_against=h['condition_true_low'] if (t['risk_difference'] or 0)>=0 else h['condition_true_high']
            target_matches=[x for x in targets if key[1] in (x['home_id'],x['away_id']) and x['gender']==key[5]]
            applicability=[]
            for target in target_matches:
                opponent=target['away_id'] if target['home_id']==key[1] else target['home_id']
                rival=[r for r in observations if r['entity_type']=='TEAM' and r['team_id']==opponent and r['league_id']==key[3] and r['season']==key[4] and r['gender']==key[5]]
                conceded='conceded.'+outcome.split('.',1)[-1]
                rival_values=[r['metrics'][conceded] for r in rival if finite(r['metrics'].get(conceded))]
                applicability.append({'fixture_id':target['fixture_id'],'opponent_id':opponent,
                    'opponent_concession_metric':conceded,'opponent_concession_n':len(rival_values),
                    'opponent_concession_above_training_threshold_n':sum(v>threshold for v in rival_values),
                    'opponent_reference_fixture_ids':[r['fixture_id'] for r in rival if finite(r['metrics'].get(conceded))],
                    'status':'DESCRIPTIVE_MATCHUP_CONTEXT' if len(rival_values)>=min_group else 'INSUFFICIENT_RIVAL_REFERENCE',
                    'condition_status':('VENUE_CHECKABLE_PREMATCH' if cond['field']=='venue' else
                        'HISTORICAL_CONTEXT_REQUIRES_TARGET_EVALUATION' if cond['field'].startswith(('lag.','context.')) or cond['field']=='opponent_strength' else
                        'OFFICIAL_TARGET_LINEUP_REQUIRED' if cond['field'] in ('starter','position','formation') else
                        'POSTMATCH_ASSOCIATION_NOT_A_PREMATCH_TRIGGER'),
                    'lineup_required':key[0]=='PLAYER' or cond['field'] in ('starter','position','formation')})
            patterns.append({'pattern_id':pid,'category':category,'entity_type':key[0],'team_id':key[1],'player_id':key[2],
                'league_id':key[3],'season':key[4],'gender':key[5],'team_kind':key[6],'period':key[7],
                'variables':{'condition':cond,'outcome':outcome,'outcome_operator':'GT','training_threshold':threshold},
                'train':t,'holdout':h,'evidence_fixture_ids':sorted(set(favorable+holdout_for)),'counterevidence_fixture_ids':sorted(set(contrary+holdout_against)),
                'evidence_partitions':{'train_for':favorable,'train_against':contrary,'holdout_for':holdout_for,'holdout_against':holdout_against},
                'coverage':{'train_valid':t['n'],'train_total':len(train),'test_valid':h['n'],'test_total':len(test)},
                'uncertainty':{'method':'FISHER_TWO_SIDED_GLOBAL_BY','multiple_testing_q':None,'holdout_q':None},
                'stability':{'train_effect':t['risk_difference'],'holdout_effect':h['risk_difference'],'same_direction':bool(t['risk_difference'] and h['risk_difference'] and t['risk_difference']*h['risk_difference']>0)},
                'next_match_applicability':applicability,'validation_status':'ASSOCIATION_DISCOVERED',
                'limitations':['ASSOCIATION_NOT_CAUSATION','TRAINING_MEDIAN_THRESHOLD_NOT_A_MARKET_LINE','INCOMPLETE_HISTORY_NOT_ACTUAL_LAST_MATCHES','NO_BETTING_CALIBRATION','PLAYER_COUNTS_REQUIRE_MINUTE_EXPOSURE_REVIEW'] if key[0]=='PLAYER' else ['ASSOCIATION_NOT_CAUSATION','TRAINING_MEDIAN_THRESHOLD_NOT_A_MARKET_LINE','INCOMPLETE_HISTORY_NOT_ACTUAL_LAST_MATCHES','NO_BETTING_CALIBRATION'],
                'commercial_adapter':{'metric':outcome.split('.',1)[-1],'period':key[7],'market_status':'PENDING_INDEPENDENT_MARKET_MAPPING','actionable_bet':False}})
    q=by_adjust([p['train']['p_value'] for p in patterns]);qh=by_adjust([p['holdout']['p_value'] for p in patterns])
    for p,qt,qtest in zip(patterns,q,qh):
        p['uncertainty'].update(multiple_testing_q=qt,holdout_q=qtest)
        if qt<=.05:
            p['validation_status']='DESCRIPTIVELY_SUPPORTED_ASSOCIATION'
            a,b,c,d=p['holdout']['counts']
            if min(a+b,c+d)>=min_group and qtest<=.05 and p['stability']['same_direction']:
                p['validation_status']='TEMPORALLY_VALIDATED_ASSOCIATION'
        p['football_hypothesis_status']='PENDING_EXPLANATION_WITH_SOURCES'
        field=p['variables']['condition']['field']
        p['predictive_use']={'authorized':False,'feature_available_prematch':field.startswith(('lag.','context.')) or field=='venue',
            'reason':'SAME_MATCH_POST_KICKOFF_VARIABLE' if field.startswith(('own.','conceded.','player.','player_raw.','rate90.')) else 'TARGET_FEATURE_AND_OUT_OF_SAMPLE_MODEL_NOT_VALIDATED'}
        methodological_review(p,observations,min_group)
    return patterns,budget

def run(source,out,cache_root=None,retrospective=False,max_tests=64):
    source=Path(source).resolve();raw=source.read_bytes();pre=json.loads(raw)
    cache=EnrichedCache(cache_root) if cache_root else None
    ref={'path':str(source),'sha256':hashlib.sha256(raw).hexdigest(),'fetched_at_utc':pre.get('generated_at_utc')}
    obs,targets,audit=normalize(pre,ref,cache,retrospective)
    patterns,budget=discover(obs,targets,max_tests=max_tests)
    coverage=defaultdict(lambda:Counter())
    expected={}
    for kind in ('TEAM','PLAYER'):
        expected[kind]={k for row in obs if row['entity_type']==kind for k in row['metrics']}
    expected['TEAM'].update('own.'+k for k in ('goals','xg','shots','on_target','blocked_shots','corners','fouls','yellow','possession_pct','passes'))
    expected['PLAYER'].update('player.'+k for k in players.PLAYER_METRICS)
    for r in obs:
        group=f"{r['entity_type']}:{r['league_id']}:{r['season']}:{r['gender']}"
        coverage[group]['observations']+=1
        for metric in sorted(expected[r['entity_type']]):
            v=r['metrics'].get(metric)
            coverage[group][metric+':valid' if finite(v) else metric+':missing']+=1
        for field in ('venue','formation','starter','position','opponent_strength'):
            coverage[group][field+':observed' if r['conditions'].get(field) is not None else field+':missing']+=1
        coverage[group]['events:available' if 'events' in r else 'events:missing']+=1
    files={'historical_observations.json':{'schema_version':VERSION,'observations':obs,'targets':targets,'audit':audit},
           'advanced_patterns.json':{'schema_version':VERSION,'patterns':patterns,'counts':dict(Counter(p['validation_status'] for p in patterns)),'categories':dict(Counter(p['category'] for p in patterns)),'test_budget':budget,'actionable_bet':False},
           'coverage.json':{'schema_version':VERSION,'coverage':dict(coverage),'cache_rejections':cache.rejected if cache else [],
                'limitations':['No API call performed; endpoint entitlement cannot be established offline','Only snapshot histories; unobserved competitions and fixtures are not assumed absent','Events are retained; score-state tactical associations not reconstructed without a validated event timeline','No coach identity is inferred from team statistics','NATIONAL_TEAM and CLUB stay separate when explicitly provided; UNKNOWN is not guessed','Zero observations of PLAYER is insufficient individual coverage, never evidence of no player patterns']},
           'research_dossiers.json':{'schema_version':VERSION,'hypotheses':[{'pattern_id':p['pattern_id'],'statistics':p['variables'],'historical_fixture_ids':p['train']['fixture_ids'],'evidence_for':p['evidence_fixture_ids'],'evidence_against':p['counterevidence_fixture_ids'],'validation':p['validation_status'],'applicability':p['next_match_applicability'],'verification':'INFERIDO','mechanism':None,'questions':['What documented mechanism explains this association?','Do minutes, role, coach or red cards contradict it?','Is the upcoming opponent context comparable?'],'invalidation_conditions':['POST_CUTOFF_SOURCE','IDENTITY_CONFLICT','REGIME_CHANGE','HOLDOUT_FAILURE'],'missing_information':['Dated independent tactical sources','Verified coach and player roles'],'source_ref':ref} for p in patterns]}}
    manifest={'schema_version':VERSION,'source':ref,'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'dependency_sha256':{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (players.__file__,__import__('prematch_predictive_engine').__file__,__import__('prematch_predictive_evidence_ranking_v1').__file__)},'cutoff':pre.get('generated_at_utc'),'cache_root':str(Path(cache_root).resolve()) if cache_root else None,'allow_retrospective_cache':retrospective,'max_tests_per_entity_segment':max_tests,'observations':len(obs),'patterns':len(patterns),'actionable_bet':False,'artifacts':{}}
    manifest['effective_cutoff_utc']=audit['cutoff_utc']
    by_fixture=defaultdict(list)
    for row in obs:by_fixture[row['fixture_id']].append(row)
    for dossier,pattern in zip(files['research_dossiers.json']['hypotheses'],patterns):
        historical_ids=sorted(set(pattern['train']['fixture_ids']+pattern['holdout']['fixture_ids']))
        dossier['historical_fixture_ids']=historical_ids
        dossier['explanation_status']='PENDING_EXTERNAL_EVIDENCE'
        dossier['predictive_use']=pattern['predictive_use']
        dossier['statistical_evidence']={key:pattern[key] for key in ('train','holdout','uncertainty','coverage','stability','limitations')}
        dossier['entity']={key:pattern[key] for key in ('entity_type','team_id','player_id','league_id','season','gender','team_kind','period')}
        dossier['participants']={str(fid):[{'player_id':row['player_id'],'minutes':row['minutes'],'position':row.get('position'),'starter':row.get('starter')} for row in by_fixture[fid] if row['entity_type']=='PLAYER' and row['team_id']==pattern['team_id']] for fid in historical_ids}
        dossier['coach_status']='UNVERIFIED_NO_COACH_IDENTITY_IN_INPUT_CONTRACT'
        refs={item['sha256']:item for fid in historical_ids for row in by_fixture[fid] if row['team_id']==pattern['team_id'] for item in row['provenance']}
        dossier['sources']=list(refs.values())
        dossier['questions']=tactical_questions(pattern)
        dossier['holdout_assessment']=pattern['holdout_assessment']
        dossier['per90_audit']=pattern.get('per90_audit')
        dossier['knowledge_stages']={'statistical_finding':pattern['validation_status'],'llm_hypothesis':'NOT_DOCUMENTED','sourced_explanation':'PENDING_EXTERNAL_EVIDENCE','predictive_pattern':'NOT_VALIDATED'}
    references={(item['path'],item['sha256']) for row in obs for item in row['provenance']}
    for path,expected in references:
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:raise ValueError('INPUT_CHANGED_DURING_RUN')
    for name,doc in files.items():
        data=json.dumps(doc,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        write_once(Path(out)/name,data);manifest['artifacts'][name]=hashlib.sha256(data.encode()).hexdigest()
    write_once(Path(out)/'manifest.json',json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return manifest,files

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--preselection',type=Path);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--cache-root',type=Path);p.add_argument('--allow-retrospective-cache',action='store_true');p.add_argument('--max-tests-per-segment',type=int,default=64);p.add_argument('--reprocess-frozen-dir',type=Path);p.add_argument('--pattern-id',action='append',default=[]);p.add_argument('--preflight',action='append',type=Path,default=[]);a=p.parse_args(argv)
    if a.reprocess_frozen_dir:
        print(json.dumps(reprocess_frozen(a.reprocess_frozen_dir,a.output_dir,a.pattern_id,a.preflight)));return
    if not a.preselection:p.error('--preselection or --reprocess-frozen-dir required')
    if a.max_tests_per_segment<1:p.error('Test budget must be positive')
    m,_=run(a.preselection,a.output_dir,a.cache_root,a.allow_retrospective_cache,a.max_tests_per_segment)
    print(json.dumps(m,ensure_ascii=False));print('Next manual action: review coverage.json and research_dossiers.json; investigate mechanisms and counterevidence with dated sources. No picks or LLM calls generated.')
if __name__=='__main__':main()
