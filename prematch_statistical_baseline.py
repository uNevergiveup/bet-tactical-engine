"""Objective 6: immutable offline statistical dossiers from frozen observations."""
import argparse
import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median, pstdev
from datetime import datetime
from prematch_advanced_discovery_v2 import discover, by_adjust

VERSION='1.0-STATISTICAL_BASELINE_PREMATCH'
FAMILIES=('goals','corners','yellow','red','fouls','shots','on_target')
def finite(v):return isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dt(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
def write(p,d):
    with Path(p).open('x') as f:json.dump(d,f,ensure_ascii=False,indent=2,allow_nan=False)
def summaries(rows):
    result=[]
    for metric in sorted({k for r in rows for k in r['metrics'] if k.startswith(('own.','conceded.','derived.')) and 'offsides' not in k}):
        valid=[r for r in rows if finite(r['metrics'].get(metric))]
        vals=[r['metrics'][metric] for r in valid]
        if not vals:continue
        recent=vals[-5:];prior=vals[:-5]
        result.append({'metric':metric,'n':len(vals),'missing':len(rows)-len(vals),'mean':mean(vals),
            'median':median(vals),'sd':pstdev(vals),'variance_to_mean':pstdev(vals)**2/mean(vals) if mean(vals)>0 else None,
            'range':[min(vals),max(vals)],'recent5_mean':mean(recent),'previous_mean':mean(prior) if prior else None,
            'positive_frequency':sum(v>0 for v in vals)/len(vals),'fixture_ids':[r['fixture_id'] for r in valid],
            'status':'EXPLORATORIO' if len(vals)>=8 else 'INSUFFICIENT_SAMPLE'})
    return result

def classify(p):
    t=p['train'];h=p['holdout'];a,b,c,d=h['counts']
    if min(t['counts'][0]+t['counts'][1],t['counts'][2]+t['counts'][3],a+b,c+d)<3:return 'INSUFFICIENT_SAMPLE','TEMPORAL_GROUP_SUPPORT_LT3'
    if p['stability']['train_effect'] and p['stability']['holdout_effect'] and p['stability']['train_effect']*p['stability']['holdout_effect']<0:return 'DESCARTADO','HOLDOUT_DIRECTION_REVERSAL'
    if p['validation_status']=='TEMPORALLY_VALIDATED_ASSOCIATION':return 'VALIDADO','TEMPORAL_ASSOCIATION_ONLY_NOT_PREDICTIVE'
    return 'EXPLORATORIO','NO_MULTIPLICITY_CORRECTED_TEMPORAL_SUPPORT'

def validate_hypothesis(rows,h,reference_rows=()):
    """Rolling-origin forecast: previous observations only, fixed 5-game window."""
    metric=h['outcome']; predictions=[]
    for i,r in enumerate(rows):
        y=r['metrics'].get(metric)
        if not finite(y):continue
        prior=[x for x in rows[:i] if dt(x['available_at_utc'])<dt(r['kickoff_utc'])]
        history=[x['metrics'].get(metric) for x in prior if finite(x['metrics'].get(metric))]
        if len(history)<8:continue
        if h['transform']=='recent5':values=history[-5:]
        elif h['transform']=='venue':values=[x['metrics'][metric] for x in prior if x.get('venue')==r.get('venue') and finite(x['metrics'].get(metric))]
        elif h['transform']=='pressure':
            values=[x['metrics'][metric] for x in prior if finite(x['metrics'].get('own.blocked_shots')) and x['metrics']['own.blocked_shots']>median([z['metrics']['own.blocked_shots'] for z in prior if finite(z['metrics'].get('own.blocked_shots'))]) and finite(x['metrics'].get(metric))]
        elif h['transform']=='matchup':
            rival=[x['metrics'].get('conceded.goals') for x in reference_rows if x['entity_type']=='TEAM' and x['team_id']==r['opponent_id'] and x['league_id']==r['league_id'] and x['season']==r['season'] and dt(x['available_at_utc'])<dt(r['kickoff_utc']) and finite(x['metrics'].get('conceded.goals'))]
            if len(rival)<3:continue
            values=[(mean(history)+mean(rival))/2]*3
        else:raise ValueError('UNKNOWN_TRANSFORM')
        if len(values)<3:continue
        predictions.append({'fixture_id':r['fixture_id'],'kickoff':r['kickoff_utc'],'observed':y,'candidate':mean(values),
                            'baseline':mean(history),'training_fixture_ids':[x['fixture_id'] for x in prior]})
    losses=[(x['candidate']-x['observed'])**2-(x['baseline']-x['observed'])**2 for x in predictions]
    improvement=-mean(losses) if losses else None
    # Fixed split: no tuning of hypotheses on this evaluation subset.
    return {'status':'INSUFFICIENT_SAMPLE' if len(losses)<20 else 'EXPLORATORIO' if improvement>0 else 'DESCARTADO',
        'reason':'NO_INDEPENDENT_UNSEEN_HOLDOUT_CODEX_SAW_FROZEN_RESULTS',
        'n':len(losses),'loss':'SQUARED_ERROR_BINARY_BRIER_WHEN_BTTS',
        'candidate_mse':mean((x['candidate']-x['observed'])**2 for x in predictions) if losses else None,
        'baseline_mse':mean((x['baseline']-x['observed'])**2 for x in predictions) if losses else None,
        'improvement':improvement,'predictions':predictions,'commercial_probability_authorized':False}

def run(a):
    root=a.root;pre_path=root/'preselection.json';obs_path=root/'advanced/historical_observations.json'
    pre=json.loads(pre_path.read_text());frozen=json.loads(obs_path.read_text());rec=json.loads(a.reconciliation.read_text())
    selected=[r for r in rec['fixtures'] if r.get('identity_verified')]
    targets={r['fixture_id']:r for r in pre['matches']};out=a.output_dir;out.mkdir(parents=True,exist_ok=False)
    original=json.loads((root/'advanced/advanced_patterns.json').read_text())['patterns']
    hypotheses=json.loads(a.hypotheses.read_text()) if a.hypotheses else []
    dossiers=[];all_patterns=[];tests=[];audit=[];verified_hashes={}
    for identity in selected:
        fid=identity['fixture_id'];target=targets.get(fid)
        if not target:raise ValueError('VERIFIED_FIXTURE_MISSING_STATISTICAL_INPUT:'+str(fid))
        eligible=target.get('category_status')=='ELIGIBLE';cut=dt(target['analysis_cutoff_utc']);ko=dt(target['kickoff_utc'])
        source=[r for r in frozen['observations'] if fid in r.get('target_fixture_ids',[])]
        rows=[];seen={};excluded=Counter()
        for raw in source:
            if raw['team_id'] not in (target['home_id'],target['away_id']):excluded['WRONG_TEAM']+=1;continue
            if raw['fixture_id']==fid or dt(raw['kickoff_utc'])>=cut or dt(raw['available_at_utc'])>cut or cut>=ko:
                excluded['TEMPORAL_EXCLUSION']+=1;continue
            if raw.get('status') not in ('FT','AET','PEN'):excluded['NOT_COMPLETED']+=1;continue
            refs=raw.get('provenance',[])
            if not refs:excluded['NO_PROVENANCE']+=1;continue
            for ref in refs:
                path=ref.get('path');expected=ref.get('sha256')
                if not path or not expected:raise ValueError('PROVENANCE_INCOMPLETE')
                if path not in verified_hashes:verified_hashes[path]=sha(path)
                if verified_hashes[path]!=expected:raise ValueError('PROVENANCE_HASH_MISMATCH:'+path)
                if ref.get('fetched_at_utc') and dt(ref['fetched_at_utc'])>cut:raise ValueError('SOURCE_ACQUIRED_AFTER_CUTOFF')
            if raw['entity_type']=='PLAYER' and (not finite(raw.get('minutes')) or raw['minutes']<30 or not raw.get('position') or raw.get('starter') is None):excluded['PLAYER_EXPOSURE_ROLE']+=1;continue
            r=copy.deepcopy(raw);r['metrics']={k:v for k,v in r['metrics'].items() if 'offsides' not in k}
            if r['entity_type']=='TEAM':
                for metric in FAMILIES:
                    own=r['metrics'].get('own.'+metric);opp=r['metrics'].get('conceded.'+metric)
                    if finite(own) and finite(opp):r['metrics']['derived.total_'+metric]=own+opp
                own=r['metrics'].get('own.goals');opp=r['metrics'].get('conceded.goals')
                if finite(own) and finite(opp):r['metrics']['derived.btts']=int(own>0 and opp>0)
            key=(r['entity_type'],r['team_id'],r.get('player_id'),r['fixture_id'],r['period'])
            if key in seen:
                excluded['DUPLICATE']+=1
                if seen[key]['metrics']!=r['metrics']:raise ValueError('CONFLICTING_DUPLICATE')
                continue
            seen[key]=r;rows.append(r)
        rows.sort(key=lambda r:(r['kickoff_utc'],r['fixture_id']))
        player_groups=defaultdict(list)
        for r in rows:
            if r['entity_type']=='PLAYER':player_groups[(r['team_id'],r['player_id'],r['league_id'],r['season'])].append(r)
        allowed_players={k for k,rs in player_groups.items() if len(rs)>=5 and sum(r['minutes'] for r in rs)>=300}
        rows=[r for r in rows if r['entity_type']=='TEAM' or (r['team_id'],r['player_id'],r['league_id'],r['season']) in allowed_players]
        # Keep exact season/competition/category groups; no implicit pooling in discovery.
        patterns,budget=discover(rows,[target],max_tests=64) if eligible else ([],[])
        for p in patterns:
            p['classification'],p['classification_reason']=classify(p)
            p['fixture_id']=fid
        team_summaries=[]
        for tid in (target['home_id'],target['away_id']):
            grouped=defaultdict(list)
            for r in rows:
                if r['entity_type']=='TEAM' and r['team_id']==tid:grouped[(r['league_id'],r['season'],r['period'])].append(r)
            for group,rs in grouped.items():team_summaries.append({'team_id':tid,'league_id':group[0],'season':group[1],'period':group[2],
                'n_matches':len(rs),'venue_counts':dict(Counter(x['venue'] for x in rs)),'metrics':summaries(rs)})
        main_coverage={}
        for tid in (target['home_id'],target['away_id']):
            rs=[r for r in rows if r['entity_type']=='TEAM' and r['team_id']==tid]
            main_coverage[str(tid)]={m:sum(finite(r['metrics'].get('own.'+m)) for r in rs) for m in FAMILIES}
        current={str(tid):{m:sum(finite(r['metrics'].get('own.'+m)) for r in rows if r['entity_type']=='TEAM' and r['team_id']==tid and r['league_id']==target['league_id'] and r['season']==target['season']) for m in FAMILIES} for tid in (target['home_id'],target['away_id'])}
        sufficient=all(min(v[m] for m in ('goals','corners','yellow','fouls','shots','on_target'))>=8 for v in current.values())
        validations=[]
        for h in hypotheses:
            for group in team_summaries:
                rs=[r for r in rows if r['entity_type']=='TEAM' and r['team_id']==group['team_id'] and r['league_id']==group['league_id'] and r['season']==group['season'] and r['period']==group['period']]
                v=validate_hypothesis(rs,h,rows);validations.append({'hypothesis_id':h['id'],'team_id':group['team_id'],'league_id':group['league_id'],'season':group['season'],**v})
        previous=[p for p in original if any(x['fixture_id']==fid for x in p.get('next_match_applicability',[]))]
        d={'schema_version':VERSION,'fixture':target|{'statistical_observations':None},'identity':identity,'eligible':eligible,
           'status':'BLOCKED' if not eligible else 'DATA_READY' if sufficient else 'PARTIAL',
           'cutoff':cut.isoformat(),'coverage_by_team':main_coverage,'current_competition_season_coverage':current,'exclusions':dict(excluded),
           'historical_groups':team_summaries,'original_python_patterns_n':len(previous),
           'original_python_patterns':previous,
           'original_by_market_metric':dict(Counter(p['variables']['outcome'] for p in previous)),
           'original_offsides_excluded_from_candidates':sum('offsides' in json.dumps(p['variables']) for p in previous),
           'original_python_statuses':dict(Counter(p['validation_status'] for p in previous)),
           'python_patterns':patterns,'test_budget':budget,'codex_hypotheses':hypotheses,'python_hypothesis_validation':validations,
           'classification_counts':dict(Counter(p['classification'] for p in patterns)),
           'statistical_risk':5 if not sufficient else 4 if not any(p['classification']=='VALIDADO' for p in patterns) else 3,
           'questions_for_future_qualitative':['¿Existe cambio acreditado de entrenador o plantilla que invalide el régimen histórico?',
               '¿Qué roles y exposición esperada podrían sostener los props individuales?',
               '¿La competición y fortaleza del rival permiten trasladar la muestra histórica?'],
           'limitations':['Coverage across competitions is availability only; estimates remain stratified.',
             'Shared historical fixtures imply dependence; fixture cluster is the effective unit.',
             'Codex hypotheses are post-selection exploration; require new untouched observations.',
             'BTTS and total descriptive frequencies are not commercial probabilities.',
             'Player target XI unavailable; no player prediction authorized.'],
           'commercial_probability_authorized':False,'actionable_bet':False}
        write(out/f'{fid}.json',d)
        lines=[f'# {fid}: {target["home"]} – {target["away"]}',f'Estado: {d["status"]}. Corte: {d["cutoff"]}. Riesgo estadístico: {d["statistical_risk"]}/5.',
               f'Cobertura propia por equipo: {main_coverage}',f'Patrones previos: {len(previous)}. Nuevos resultados: {d["classification_counts"]}.',
               '## Evidencia por patrón','']
        for p in patterns:lines.append(f'- {p["pattern_id"]}: {p["variables"]}; {p["classification"]} ({p["classification_reason"]}); train n={p["train"]["n"]}, prueba n={p["holdout"]["n"]}; q={p["uncertainty"]}; contraejemplos={p["counterevidence_fixture_ids"]}.')
        lines+=['','## Hipótesis Codex y contraste',json.dumps(validations,ensure_ascii=False,indent=2),'','## Limitaciones',*d['limitations'],'','## Preguntas futuras',*d['questions_for_future_qualitative']]
        (out/f'{fid}.md').write_text('\n'.join(lines))
        dossiers.append(d);all_patterns+=patterns;tests+=validations
    # Global multiplicity across fixture dossiers (conservative with shared histories).
    q=by_adjust([p['train']['p_value'] for p in all_patterns]);qh=by_adjust([p['holdout']['p_value'] for p in all_patterns])
    # Existing discover uses global within-dossier BY; global promotion is forbidden here.
    ranking=sorted([{'fixture_id':d['fixture']['fixture_id'],'match':d['fixture']['home']+'–'+d['fixture']['away'],
        'status':d['status'],'coverage_floor':min(v[m] for v in d['current_competition_season_coverage'].values() for m in ('goals','corners','yellow','fouls','shots','on_target')),
        'patterns':d['classification_counts'],'risk':d['statistical_risk']} for d in dossiers],key=lambda x:(-x['coverage_floor'],x['risk'],x['fixture_id']))
    write(out/'ranking.json',{'method':'coverage_floor descending, statistical risk ascending, fixture ID tie-break; no financial score','fixtures':ranking,
        'patterns':sorted([{'fixture_id':p['fixture_id'],'pattern_id':p['pattern_id'],'classification':p['classification'],'q_global':qt,'holdout_q_global':ht} for p,qt,ht in zip(all_patterns,q,qh)],key=lambda x:(x['holdout_q_global'],x['q_global'],x['pattern_id']))})
    write(out/'summary.json',{'schema_version':VERSION,'examined':len(selected),'eligible':sum(d['eligible'] for d in dossiers),
        'sufficient':sum(d['status']=='DATA_READY' for d in dossiers),'patterns':len(all_patterns),'classifications':dict(Counter(p['classification'] for p in all_patterns)),
        'hypotheses':len(hypotheses),'hypothesis_tests':len(tests),'hypothesis_statuses':dict(Counter(v['status'] for v in tests)),
        'original_patterns_total':sum(d['original_python_patterns_n'] for d in dossiers),
        'original_unique_pattern_ids':len({p['pattern_id'] for d in dossiers for p in d['original_python_patterns']}),
        'criteria':{'availability_min_each_main_metric_each_team':8,'association_train_and_holdout_min_group':3,'BY_alpha':.05,
                    'availability_scope':'CURRENT_COMPETITION_AND_SEASON_PER_TEAM',
                    'prediction_validation':'NONE_AUTHORIZED','Codex_validation':'requires independent future holdout'}})
    inputs=[pre_path,obs_path,root/'advanced/advanced_patterns.json',a.reconciliation,Path(__file__)]
    if a.hypotheses:inputs.append(a.hypotheses)
    write(out/'manifest.json',{'status':VERSION,'inputs':{str(p.resolve()):sha(p) for p in inputs},'verified_provenance':verified_hashes,
          'files':{p.name:sha(p) for p in sorted(out.iterdir()) if p.is_file()},'network_calls':0})
    print((out/'summary.json').read_text())

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--reconciliation',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--hypotheses',type=Path);run(p.parse_args())
