"""Offline international exploration. Frozen baseline is read-only."""
import argparse, copy, hashlib, json, math, random
from collections import Counter,defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean,pstdev
from prematch_statistical_baseline import finite,dt,sha,write,summaries
from prematch_advanced_discovery_v2 import by_adjust

IDS=(1528958,1528959,1528960,1528961,1528962,1528963,1528964,1545613)
METRICS=('goals','corners','yellow','fouls','shots','on_target')
COMPETITIVE={1,5,32} # Named and verified from the local fixture payloads.
POLICY={'half_life_days':365,'stratum_prior_weight':4,'min_train':6,'min_evaluation':8,
        'validation':'No VALIDADO promotion without independent untouched data',
        'primary_competitions':[1,5,32],'friendlies':'sensitivity_only','unknown_competitions':'audit_only'}

def interval(values,seed=61):
    if len(values)<2:return None
    rng=random.Random(seed)
    # Resample fixtures, never the two team views as independent observations.
    draws=sorted(mean(rng.choices(values,k=len(values))) for _ in range(1000))
    return [draws[24],draws[974]]

def fit(rows,metric,at,league,season,venue,model,uncertainty=True):
    rows=[r for r in rows if dt(r['available_at_utc'])<at and finite(r['metrics'].get(metric))]
    if not rows:return None
    vals=[r['metrics'][metric] for r in rows]
    weights=[2**(-max(0,(at-dt(r['kickoff_utc'])).total_seconds()/86400)/365) for r in rows]
    if model=='recent5':rows=rows[-5:];vals=vals[-5:];weights=weights[-5:]
    if model=='venue':
        ix=[i for i,r in enumerate(rows) if r['venue']==venue]
        if len(ix)<3:return None
        rows=[rows[i] for i in ix];vals=[vals[i] for i in ix];weights=[weights[i] for i in ix]
    if model=='baseline':weights=[1.]*len(rows)
    total=sum(weights);mu=sum(w*v for w,v in zip(weights,vals))/total
    local_w=0
    if model=='hierarchical':
        ix=[i for i,r in enumerate(rows) if (r['league_id'],r['season'])==(league,season)]
        local_w=sum(weights[i] for i in ix)
        mu=(sum(weights[i]*vals[i] for i in ix)+4*mu)/(local_w+4)
    variance=sum(w*(v-mu)**2 for w,v in zip(weights,vals))/total
    ess=total**2/sum(w*w for w in weights)
    return {'mean':mu,'n':len(rows),'effective_n':ess,'variance':variance,
        'overdispersion':variance/mu if mu>0 else None,'descriptive_mean_interval':interval(vals) if uncertainty else None,
        'current_stratum_weight':local_w if model=='hierarchical' else None,
        'pooling_prior_weight':4 if model=='hierarchical' else None,
        'interval_limit':'Fixture bootstrap unweighted; not a calibrated prediction interval',
        'fixture_ids':[r['fixture_id'] for r in rows],'weights':weights,'commercial_use':False}

def evaluate(rows,metric,model):
    predictions=[]
    for r in rows:
        prior=[x for x in rows if dt(x['available_at_utc'])<dt(r['kickoff_utc'])]
        if sum(finite(x['metrics'].get(metric)) for x in prior)<6 or not finite(r['metrics'].get(metric)):continue
        at=dt(r['kickoff_utc']);args=(prior,metric,at,r['league_id'],r['season'],r['venue'])
        candidate=fit(*args,model,uncertainty=False);base=fit(*args,'baseline',uncertainty=False)
        if not candidate:continue
        y=r['metrics'][metric]
        predictions.append({'fixture_id':r['fixture_id'],'at':r['kickoff_utc'],'y':y,'candidate':candidate['mean'],
            'baseline':base['mean'],'training_ids':candidate['fixture_ids'],
            'loss_difference':(candidate['mean']-y)**2-(base['mean']-y)**2})
    loss=[r['loss_difference'] for r in predictions];ci=interval(loss)
    rng=random.Random(61)
    p=(1+sum(mean(v*(1 if rng.random()<.5 else -1) for v in loss)<=mean(loss) for _ in range(1999)))/2000 if loss else 1.
    return {'n':len(loss),'mse':mean((r['candidate']-r['y'])**2 for r in predictions) if loss else None,
        'baseline_mse':mean((r['baseline']-r['y'])**2 for r in predictions) if loss else None,
        'mean_loss_difference':mean(loss) if loss else None,'loss_difference_interval':ci,
        'first_half_difference':mean(loss[:len(loss)//2]) if len(loss)>=2 else None,
        'second_half_difference':mean(loss[len(loss)//2:]) if len(loss)>=2 else None,
        'exploratory_sign_randomization_p':p,'predictions':predictions,
        'counterexample_fixture_ids':[r['fixture_id'] for r in predictions if r['loss_difference']>0]}

def classification(estimate,evaluation):
    if not estimate or estimate['n']<6:return 'INSUFFICIENT_SAMPLE','FEWER_THAN_6_VALID_HISTORICAL_FIXTURES'
    if evaluation['n']<8:return 'FRAGIL','FEWER_THAN_8_WALK_FORWARD_EVALUATIONS'
    if evaluation['mean_loss_difference']>=0:return 'DESCARTADO','DOES_NOT_BEAT_EXPANDING_BASELINE'
    if estimate['effective_n']<8 or evaluation['first_half_difference']>=0 or evaluation['second_half_difference']>=0:return 'FRAGIL','LOW_EFFECTIVE_SAMPLE_OR_TEMPORAL_INSTABILITY'
    return 'EXPLORATORIO','IMPROVEMENT_REQUIRES_INDEPENDENT_VALIDATION'

def run(a):
    baseline=a.baseline;frozen=json.loads(a.observations.read_text());out=a.output_dir;out.mkdir(parents=True,exist_ok=False)
    hashes={};league_names={};dossiers=[];signals=[];inputs={str(a.observations):sha(a.observations)}
    hypotheses=json.loads(a.hypotheses.read_text()) if a.hypotheses else []
    if a.hypotheses:inputs[str(a.hypotheses)]=sha(a.hypotheses)
    for fid in IDS:
        bpath=baseline/f'{fid}.json';b=json.loads(bpath.read_text());inputs[str(bpath)]=sha(bpath)
        t=b['fixture'];cut=dt(b['cutoff']);rs=[];seen=set();excluded=Counter()
        for raw in frozen['observations']:
            if raw['entity_type']!='TEAM' or fid not in raw.get('target_fixture_ids',[]):continue
            if raw['team_id'] not in (t['home_id'],t['away_id']) or raw['fixture_id']==fid:continue
            if dt(raw['available_at_utc'])>cut or dt(raw['kickoff_utc'])>=cut or raw['status']!='FT':excluded['TEMPORAL_OR_NON_REGULATION']+=1;continue
            for ref in raw['provenance']:
                p=ref['path']
                if p not in hashes:
                    hashes[p]=sha(p)
                    payload=json.loads(Path(p).read_text())
                    for x in payload.get('response',[]) if isinstance(payload.get('response'),list) else []:
                        if isinstance(x,dict) and isinstance(x.get('league'),dict):league_names[x['league']['id']]=x['league']['name']
                if hashes[p]!=ref['sha256']:raise ValueError('PROVENANCE_HASH_CONFLICT')
                if ref.get('fetched_at_utc') and dt(ref['fetched_at_utc'])>cut:raise ValueError('ACQUIRED_AFTER_CUTOFF')
            key=(raw['team_id'],raw['fixture_id'])
            if key in seen:excluded['DUPLICATE']+=1;continue
            seen.add(key);rs.append(raw)
        rs.sort(key=lambda r:(r['kickoff_utc'],r['fixture_id']))
        strata=defaultdict(list)
        for r in rs:
            age=(cut-dt(r['kickoff_utc'])).days
            strata[(r['team_id'],r['league_id'],r['season'],r['venue'],'0_180' if age<=180 else '181_365' if age<=365 else 'over365')].append(r)
        audit=[{'team_id':k[0],'league_id':k[1],'league':league_names.get(k[1]),'season':k[2],'venue':k[3],
                'age_days_band':k[4],'n':len(v),'fixture_ids':[x['fixture_id'] for x in v],
                'metrics':summaries(v)} for k,v in sorted(strata.items())]
        results=[]
        for side in ('home','away'):
            tid=t[side+'_id'];rival=t['away_id' if side=='home' else 'home_id'];venue='HOME' if side=='home' else 'AWAY'
            for family in METRICS:
                for direction in ('own','conceded'):
                    metric=direction+'.'+family
                    for scope in ('competitive','with_friendlies'):
                        pool=[r for r in rs if r['team_id']==tid and r['league_id'] in (COMPETITIVE if scope=='competitive' else COMPETITIVE|{10})]
                        for model in ('weighted','hierarchical'):
                            estimate=fit(pool,metric,cut,t['league_id'],t['season'],venue,model)
                            ev=evaluate(pool,metric,model);status,reason=classification(estimate,ev)
                            rival_metric=('conceded.' if direction=='own' else 'own.')+family
                            rival_rows=[r for r in rs if r['team_id']==rival and r['league_id'] in COMPETITIVE]
                            rival_fit=fit(rival_rows,rival_metric,cut,t['league_id'],t['season'],'AWAY' if venue=='HOME' else 'HOME',model)
                            result={'id':f'{fid}:{tid}:{metric}:{scope}:{model}','fixture_id':fid,'team_id':tid,'metric':metric,'scope':scope,
                                'model':model,'estimate':estimate,'rival_complement':rival_fit,'validation':ev,'classification':status,'reason':reason,
                                'market_family':family,'market_status':'FUTURE_EXACT_BWIN_CONTRACT_VERIFICATION_REQUIRED',
                                'qualitative_only':True,'commercial_use':False}
                            results.append(result)
        additional=[]
        for h in hypotheses:
            for side in ('home','away'):
                pool=[r for r in rs if r['team_id']==t[side+'_id'] and r['league_id'] in COMPETITIVE]
                metric=h['metric'];model=h['model'];est=fit(pool,metric,cut,t['league_id'],t['season'],'HOME' if side=='home' else 'AWAY',model)
                ev=evaluate(pool,metric,model);status,reason=classification(est,ev)
                additional.append({'hypothesis':h,'team_id':t[side+'_id'],'estimate':est,'validation':ev,
                                   'classification':status,'reason':reason,'independent_validation':False})
        d={'version':'6.1-INTERNATIONAL_EXPLORATION','fixture_id':fid,'home':t['home'],'away':t['away'],'cutoff':b['cutoff'],
           'baseline_ref':str(bpath),'baseline_sha256':sha(bpath),'baseline_status':b['status'],
           'baseline_current_coverage':b['current_competition_season_coverage'],'policy':POLICY,'strata':audit,
           'excluded':dict(excluded),'signals':results,'codex_additional':additional,
           'questions':['¿Qué diferencias de rival y régimen explican los contraejemplos?','¿La señal cambia con incentivos competitivos frente a amistosos?'],
           'limitations':['Selection capped at local acquired history; not full career.',
             'Hierarchical model is empirical partial pooling of team competition-season strata, not calibrated Bayesian inference.',
             'Age weighting does not correct opponent quality or coach regime.',
             'Temporal losses overlap across targets/models; global BY is conservative, not independent replication.',
             'Intervals resample fixture blocks but do not model serial dependence; qualitative hypotheses only.']}
        dossiers.append(d);signals+=results
    q=by_adjust([x['validation']['exploratory_sign_randomization_p'] for x in signals])
    for x,v in zip(signals,q):x['validation']['global_BY_q']=v
    for d in dossiers:
        write(out/f'{d["fixture_id"]}.json',d)
        lines=[f'# {d["home"]} – {d["away"]} ({d["fixture_id"]})',f'Corte: {d["cutoff"]}; baseline: {d["baseline_status"]}.',
            f'Clasificación: {dict(Counter(x["classification"] for x in d["signals"]))}',
            '## Estratos disponibles',*[f'- {x["team_id"]}: {x["league"]}, {x["season"]}, {x["venue"]}, antigüedad {x["age_days_band"]} días: n={x["n"]}.' for x in d['strata']],
            '## Señales y contraejemplos']
        for x in d['signals']:lines.append(f'- {x["id"]}: {x["classification"]} ({x["reason"]}); estimación={x["estimate"]}; validación={x["validation"]}.')
        lines+=['## Hipótesis Codex',json.dumps(d['codex_additional'],ensure_ascii=False,indent=2),'## Limitaciones',*d['limitations']]
        (out/f'{d["fixture_id"]}.md').write_text('\n'.join(lines))
    write(out/'summary.json',{'fixtures':len(dossiers),'signals':len(signals),'classifications':dict(Counter(x['classification'] for x in signals)),
          'codex_hypotheses':len(hypotheses),'codex_tests':sum(len(d['codex_additional']) for d in dossiers),
          'codex_statuses':dict(Counter(x['classification'] for d in dossiers for x in d['codex_additional'])),
          'policy':POLICY,'league_names':league_names})
    write(out/'manifest.json',{'inputs':inputs,'provenance':hashes,'script_sha256':sha(__file__),
          'files':{p.name:sha(p) for p in out.iterdir()},'network_calls':0,'baseline_preserved':True})
    print((out/'summary.json').read_text())

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--observations',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--hypotheses',type=Path);run(p.parse_args())
