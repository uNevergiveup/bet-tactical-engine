"""Offline API-Football observations, with availability and fixture cutoffs.

No network, inference of team IDs, or use of a cache downloaded after asof.
"""
from pathlib import Path
from datetime import timedelta
import json
from preselect_matches import read_stat_block, numeric
import prematch_prospective_trial as t


def observations_from_games(games, team_id, details, requested_ids=None):
    rows=[]
    requested=set(details if requested_ids is None else requested_ids)
    for g in games:
        other=g['away_id'] if g['home_id']==team_id else g['home_id']
        raw=details.get(g['id'],{})
        own=read_stat_block(raw,team_id);opp=read_stat_block(raw,other)
        # Preserve observed blocked shots without altering old readiness weights.
        for target,tid in ((own,team_id),(opp,other)):
            blocks=[v for v in raw.get('statistics',[]) if v.get('team',{}).get('id')==tid]
            if len(blocks)==1:
                for item in blocks[0].get('statistics',[]):
                    if item.get('type')=='Blocked Shots' and numeric(item.get('value')) is not None:
                        target['blocked_shots']=numeric(item['value'])
                    elif item.get('type')=='Ball Possession' and item.get('value') is not None:
                        possession=numeric(str(item['value']).rstrip('%'))
                        if possession is not None and 0<=possession<=100:target['possession_pct']=possession
        # Only regular-time FT games enter the tactical sample.
        if g.get('status')!='FT':continue
        own['goals']=g['team_goals'];opp['goals']=g['opponent_goals']
        owner='home' if g['home_id']==team_id else 'away';other_side='away' if owner=='home' else 'home'
        half=(raw.get('score') or {}).get('halftime') or {}
        for target,label in ((own,owner),(opp,other_side)):
            h=numeric(half.get(label))
            if h is not None and target['goals'] is not None and h<=target['goals']:
                target['goals_h1']=h;target['goals_h2']=target['goals']-h
        request_status=('NOT_REQUESTED' if g['id'] not in requested else
                        'VALID' if bool(raw.get('statistics')) and (own or opp) else 'REQUESTED_MISSING')
        rows.append({'fixture_id':g['id'],'team_id':team_id,'opponent_id':other,
                     'kickoff_utc':g['date_utc'],'league_id':g['league_id'],'season':g['season'],
                     'venue':g['venue'],'status':'FT','statistics_requested':g['id'] in requested,
                     'statistics_request_status':request_status,'own':own,'conceded':opp})
    return rows


class LocalHistoryCache:
    def __init__(self,root,asof):
        self.asof=asof;self.records={};self.sources={};self.loaded_sha={}
        for p in sorted((Path(root)/'outputs/data/cache/preselect_api').glob('*.json')):
            try:
                data=p.read_bytes();doc=json.loads(data);self.loaded_sha[str(p)]=__import__('hashlib').sha256(data).hexdigest();seen=t.dt(doc.get('retrieved_at_utc'))
                if seen is None or seen.timestamp()>asof.timestamp():continue
                for raw in doc.get('response',[]):
                    f=raw.get('fixture',{});fid=f.get('id');at=t.dt(f.get('date'))
                    if not isinstance(fid,int) or not at or at>=asof:continue
                    if f.get('status',{}).get('short')!='FT':continue
                    previous=self.records.get(fid)
                    # Prefer a complete statistics block over a later basic history row.
                    key=(bool(raw.get('statistics')),seen.timestamp())
                    if previous is None or key>previous[0]:
                        self.records[fid]=(key,raw,p)
            except (ValueError,OSError,TypeError):continue

    def for_team(self,team_id,league_id,cutoff,limit=20,season=None):
        from preselect_matches import prepare_history
        chosen=[];details={};refs={}
        for _,raw,path in self.records.values():
            teams=raw.get('teams',{});ids=[teams.get(s,{}).get('id') for s in ('home','away')]
            at=t.dt(raw['fixture']['date'])
            if team_id not in ids or raw.get('league',{}).get('id')!=league_id or not at or at>=cutoff:continue
            if season is not None and raw.get('league',{}).get('season')!=season:continue
            chosen.append(raw);details[raw['fixture']['id']]=raw
            refs[raw['fixture']['id']]=path
        games=prepare_history(chosen,team_id,cutoff,limit)
        # prepare_history now preserves FT status; backward compatibility with old snapshots is explicit.
        for g in games:g['status']='FT'
        requested={fid for fid,raw in details.items() if isinstance(raw.get('statistics'),list)}
        observations=observations_from_games(games,team_id,details,requested)
        used=sorted({refs[g['fixture_id']] for g in observations})
        for path in used:self.sources[str(path)]={'path':str(path),'sha256':self.loaded_sha[str(path)]}
        return observations


def longitudinal_profile(team_id, observations, raw_rows):
    """Verified nominal roles and metric-specific exposure, never inferred tactics."""
    from collections import Counter
    from statistics import mean
    metrics={'shots':('shots','total'),'on_target':('shots','on'),
             'key_passes':('passes','key'),'tackles':('tackles','total'),
             'goals':('goals','total'),'assists':('goals','assists')}
    players={};formations=[];coaches=[]
    for raw in raw_rows:
        fid=raw['fixture']['id'];at=raw['fixture']['date']
        for lu in raw.get('lineups',[]):
            if lu.get('team',{}).get('id')!=team_id:continue
            formations.append({'fixture_id':fid,'kickoff':at,'formation':lu.get('formation')})
            coaches.append({'fixture_id':fid,'kickoff':at,'coach':lu.get('coach'),
                            'status':'PROVIDER_RECORDED_NOT_CONFIRMED_CURRENT'})
        seen=set()
        for group in raw.get('players',[]):
            if group.get('team',{}).get('id')!=team_id:continue
            for pl in group.get('players',[]):
                pid=pl.get('player',{}).get('id');stats=pl.get('statistics',[])
                if not isinstance(pid,int) or isinstance(pid,bool) or pid<=0 or pid in seen or len(stats)!=1:continue
                seen.add(pid);s=stats[0];minutes=numeric(s.get('games',{}).get('minutes'))
                if minutes is None or minutes<=0:continue
                entry=players.setdefault(pid,{'player_id':pid,'name':pl['player'].get('name'),'observations':[]})
                starters=[x.get('player',{}).get('id') for l in raw.get('lineups',[]) if l.get('team',{}).get('id')==team_id for x in l.get('startXI',[])]
                entry['observations'].append({'fixture_id':fid,'minutes':minutes,'kickoff':at,
                    'nominal_position':s.get('games',{}).get('position'),'starter':pid in starters if starters else None,
                    'metrics':{m:numeric((s.get(b) or {}).get(k)) for m,(b,k) in metrics.items()}})
    for entry in players.values():
        entry['observations'].sort(key=lambda r:r['kickoff'],reverse=True);entry['roles']='NOMINAL_POSITIONS_ONLY_NO_FUNCTIONAL_ROLE_VERIFIED'
        entry['metrics']={}
        for m in metrics:
            valid=[r for r in entry['observations'] if r['metrics'][m] is not None];minutes=sum(r['minutes'] for r in valid);total=sum(r['metrics'][m] for r in valid)
            usable=len(valid)>=3 and minutes>=180
            entry['metrics'][m]={'metric':m,'unit':'count','observed_matches':len(valid),'eligible_minutes':minutes,
                'fixture_ids':[r['fixture_id'] for r in valid],'total':total if valid else None,
                'per90':round(90*total/minutes,4) if usable else None,'formula':'90 * sum(observed metric) / sum(minutes in same observations)',
                'status':'DESCRIPTIVE_EXPOSURE_ONLY' if usable else 'INSUFFICIENT_DATA'}
    groups={}
    for row in observations:
        key=f"{row['league_id']}:{row['season']}";groups.setdefault(key,[]).append(row)
    return {'competition_season_samples':{k:[r['fixture_id'] for r in sorted(v,key=lambda r:r['kickoff_utc'],reverse=True)] for k,v in groups.items()},
            'players':sorted(players.values(),key=lambda p:p['player_id']),
            'formations':sorted(formations,key=lambda r:r['kickoff'],reverse=True),
            'coaches':sorted(coaches,key=lambda r:r['kickoff'],reverse=True),
            'coach_changes':'UNVERIFIED_DO_NOT_INFER_FROM_PROVIDER_ID_CHANGES',
            'functional_roles':'INSUFFICIENT_OBSERVATIONAL_EVIDENCE'}


TACTICAL_CATALOG={
    'version':'1.1','extensible':True,
    'families':['offensive_organization','defensive_organization','pressing_recovery',
                'transitions','width_crosses','finishing_area','set_pieces','corners',
                'duels_discipline','player_roles','coach_adaptations','formation_relationships'],
    'rule_language':'JSON predicates over observations; no executable LLM code',
    'unobserved_mechanisms':['pressing_height','off_ball_runs','rehearsed_set_pieces'],
    'statistical_status':'DESCRIPTIVE_NOT_CALIBRATED'}


def contextual_segments(profile):
    """Independent team dossier; outcomes describe historical games, not prior strength."""
    from collections import defaultdict
    from prematch_tactical_signal_engine import stats,finite
    groups=defaultdict(list)
    for r in profile['observations']:
        competition=f"{r['league_id']}:{r['season']}";groups[competition+':ALL'].append(r)
        groups[competition+':'+r['venue']].append(r)
        own=r.get('own',{});opp=r.get('conceded',{})
        if finite(own.get('goals')) and finite(opp.get('goals')):
            result='WON' if own['goals']>opp['goals'] else 'LOST' if own['goals']<opp['goals'] else 'DRAW'
            groups[competition+':FINAL_RESULT_'+result].append(r)
        if finite(own.get('possession_pct')):
            groups[competition+(':POSSESSION_LOW' if own['possession_pct']<45 else ':POSSESSION_HIGH' if own['possession_pct']>55 else ':POSSESSION_BALANCED')].append(r)
        h1=own.get('goals_h1');a1=opp.get('goals_h1')
        if finite(h1) and finite(a1):groups[competition+(':HT_LEADING' if h1>a1 else ':HT_TRAILING' if h1<a1 else ':HT_DRAWING')].append(r)
        for key in ('formation','coach_id'):
            if r.get(key) is not None:groups[competition+':'+key+'='+str(r[key])].append(r)
    return {'segments':{key:{'fixture_ids':[r['fixture_id'] for r in rows],
        'metrics':{metric:{kind:stats([r.get(kind,{}).get(metric) for r in rows if finite(r.get(kind,{}).get(metric))]) for kind in ('own','conceded')} for metric in ('goals','shots','on_target','corners','fouls','yellow','possession_pct','goals_h1','goals_h2')}} for key,rows in sorted(groups.items())},
        'unavailable':{'favorite_status':'Requires archived prematch odds or documented prior assessment; final result forbidden as proxy',
                       'opponent_strength':'Requires opponent references strictly before each historical kickoff',
                       'score_state':'FT totals cannot be attributed to periods leading/drawing/trailing',
                       'H1_H2':'Goals by half included when recorded; shots/corners/fouls by half require period-specific metrics',
                       'availability':'Absence from historical statistics is not verified injury'},
        'selection_bias_warning':'Result and possession segments are post-match descriptive conditions, not known predictors before that match'}


def tactical_rule_value(expression,row):
    """Tri-state predicate. Missing metric never becomes a false observation or zero."""
    if not isinstance(expression,dict):raise ValueError('INVALID_PATTERN_RULE')
    if 'all' in expression or 'any' in expression:
        key='all' if 'all' in expression else 'any'
        if set(expression)!={key} or not isinstance(expression[key],list):raise ValueError('INVALID_PATTERN_RULE')
        values=[tactical_rule_value(x,row) for x in expression[key]]
        if not values:raise ValueError('EMPTY_PATTERN_RULE')
        if key=='all':return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if set(expression)!={'field','op','value'} or expression['op'] not in ('gt','ge','lt','le','eq','ne'):raise ValueError('INVALID_PATTERN_RULE')
    field=expression['field']
    allowed=('own.','conceded.','venue','league_id','season','formation','coach_id')
    if not isinstance(field,str) or not any(field==x or x.endswith('.') and field.startswith(x) for x in allowed):raise ValueError('UNSUPPORTED_PATTERN_FIELD')
    value=row
    for bit in field.split('.'):
        value=value.get(bit) if isinstance(value,dict) else None
    expected=expression['value']
    referenced=False
    if isinstance(expected,dict) and set(expected)=={'field'}:
        rhs=expected['field']
        if not isinstance(rhs,str) or not rhs.startswith(('own.','conceded.')):raise ValueError('INVALID_PATTERN_REFERENCE')
        expected=row
        for bit in rhs.split('.'):expected=expected.get(bit) if isinstance(expected,dict) else None
        referenced=True
    if referenced and expected is None:return None
    if isinstance(expected,(dict,list,bool)) or expected is None:raise ValueError('INVALID_PATTERN_THRESHOLD')
    import math
    if isinstance(expected,(int,float)) and not math.isfinite(expected):raise ValueError('INVALID_PATTERN_THRESHOLD')
    if value is None or isinstance(value,bool) or isinstance(value,(int,float)) and not math.isfinite(value):return None
    try:return {'gt':lambda:value>expected,'ge':lambda:value>=expected,'lt':lambda:value<expected,'le':lambda:value<=expected,'eq':lambda:value==expected,'ne':lambda:value!=expected}[expression['op']]()
    except TypeError:raise ValueError('INCOMPARABLE_PATTERN_VALUES')


def normalize_tactical_rule(rule):
    if not isinstance(rule,dict):raise ValueError('INVALID_PATTERN_RULE')
    if 'all' in rule or 'any' in rule:
        key='all' if 'all' in rule else 'any'
        items={json.dumps(normalize_tactical_rule(r),sort_keys=True):normalize_tactical_rule(r) for r in rule[key]}
        ordered=[items[k] for k in sorted(items)]
        return ordered[0] if len(ordered)==1 else {key:ordered}
    result=dict(rule)
    counts={'goals','corners','shots','on_target','fouls','yellow','goals_h1','goals_h2'}
    if str(result.get('field')).split('.')[-1] in counts and isinstance(result.get('value'),int) and not isinstance(result['value'],bool):
        if result.get('op')=='gt':result.update(op='ge',value=result['value']+1)
        elif result.get('op')=='lt':result.update(op='le',value=result['value']-1)
    return result


def evolve_tactical_library(profiles,proposals,library,asof,persist,retrospective=False):
    """Append-only independent patterns and evaluations; no target fixture in pattern ID."""
    import hashlib
    library=Path(library);catalog={**TACTICAL_CATALOG};persist(library/'catalog.json',catalog)
    merged={}
    for profile in profiles:
        key=(profile['team_id'],profile['season']);target=merged.setdefault(key,{**profile,'observations':[],'fixture_ids':[]})
        known={r['fixture_id']:r for r in target['observations']}
        for r in profile['observations']:
            if r['fixture_id'] in known and known[r['fixture_id']]!=r:raise ValueError('PATTERN_PROFILE_CONFLICT')
            known[r['fixture_id']]=r
        target['observations']=sorted(known.values(),key=lambda r:r['kickoff_utc'],reverse=True);target['fixture_ids']=list(known)
    profiles=list(merged.values())
    existing={}
    for path in sorted((library/'patterns').glob('*/*.json')):
        d=t.read(path)
        if hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest()!=path.stem:raise ValueError('PATTERN_LIBRARY_CORRUPT')
        if hashlib.sha256(json.dumps(d['definition'],sort_keys=True).encode()).hexdigest()!=d['pattern_id']:raise ValueError('PATTERN_LIBRARY_CORRUPT')
        if t.dt(d['evidence_asof'])<=asof and (d['pattern_id'] not in existing or t.dt(d['evidence_asof'])>t.dt(existing[d['pattern_id']]['evidence_asof'])):existing[d['pattern_id']]=d
    accepted=[];rejected=[]
    for p in proposals:
        if p.get('status') not in ('INFERIDO','HIPÓTESIS','CONFIRMADO','INSUFFICIENT_DATA'):raise ValueError('INVALID_PATTERN_STATUS')
        if not p.get('mechanism') or not p.get('counter_scenario') or not p.get('falsifier'):raise ValueError('PATTERN_REASONING_REQUIRED')
        if p['status']=='CONFIRMADO':raise ValueError('DYNAMIC_MECHANISM_REQUIRES_OBSERVATIONAL_REVIEW_NOT_AUTO_CONFIRMATION')
        definition={'team_id':p['team_id'],'season':p['season'],'condition':normalize_tactical_rule(p['condition']),'outcome':normalize_tactical_rule(p['outcome'])}
        key=hashlib.sha256(json.dumps(definition,sort_keys=True).encode()).hexdigest()
        profile=next((x for x in profiles if x['team_id']==p['team_id'] and x['season']==p['season']),None)
        if profile is None:raise ValueError('PATTERN_TEAM_UNBOUND')
        if not p.get('fixture_ids') or not set(p['fixture_ids'])<=set(profile['fixture_ids']):raise ValueError('PATTERN_FIXTURES_UNBOUND')
        tactical_rule_value(definition['condition'],{});tactical_rule_value(definition['outcome'],{})
        if p.get('equivalent_to') and p['equivalent_to']!=key:raise ValueError('PATTERN_EQUIVALENCE_NOT_VERIFIED')
        existing[key]={'pattern_id':key,'definition':definition,'title':p.get('title'),'family':p.get('family','custom'),
            'mechanism':p['mechanism'],'counter_scenario':p['counter_scenario'],'falsifier':p['falsifier'],
            'interpretation_status':p['status'],'evidence_asof':asof.isoformat(),'retrospective':retrospective,'evidence_sources':profile.get('source_refs',[])}
        accepted.append(key)
    evaluations=[]
    for key,p in sorted(existing.items()):
        profile=next((x for x in profiles if x['team_id']==p['definition']['team_id'] and x['season']==p['definition']['season']),None)
        if profile is None:
            if any(x['team_id']==p['definition']['team_id'] for x in profiles):
                stale={'pattern_id':key,'team_id':p['definition']['team_id'],'asof':asof.isoformat(),'n':0,'successes':0,'frequency':None,'fixture_ids':[],
                    'counterexamples':[],'state':'OBSOLETE_SEASON','evolution':'INAPPLICABLE_TO_CURRENT_SEASON','predictive_use_authorized':False}
                token=hashlib.sha256(json.dumps(stale,sort_keys=True).encode()).hexdigest();persist(library/'evaluations'/key/(token+'.json'),stale)
                evaluations.append({**stale,'title':p.get('title'),'family':p.get('family'),'mechanism':p['mechanism']})
            continue
        selected=[];success=[];counter=[];missing=[];competitions={}
        for row in profile['observations']:
            condition=tactical_rule_value(p['definition']['condition'],row)
            if condition is not True:continue
            value=tactical_rule_value(p['definition']['outcome'],row)
            if value is None:missing.append(row['fixture_id']);continue
            selected.append(row['fixture_id']);(success if value else counter).append(row['fixture_id'])
            group=f"{row['league_id']}:{row['season']}";competitions.setdefault(group,{'n':0,'successes':0});competitions[group]['n']+=1;competitions[group]['successes']+=int(value)
        n=len(selected);rate=len(success)/n if n else None
        previous=[]
        for path in sorted((library/'evaluations'/key).glob('*.json')):
            v=t.read(path)
            if hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest()!=path.stem:raise ValueError('PATTERN_EVALUATION_CORRUPT')
            if v['pattern_id']!=key:raise ValueError('PATTERN_EVALUATION_CORRUPT')
            if t.dt(v['asof'])<asof:previous.append(v)
        old=max(previous,key=lambda x:t.dt(x['asof'])) if previous else None
        state='INSUFFICIENT_DATA' if n<6 else 'CONTRADICTED' if rate<.5 else 'DESCRIPTIVE_REGULARITY' if rate>=.75 else 'MIXED_EVIDENCE'
        if state=='DESCRIPTIVE_REGULARITY' and len(competitions)>1:state='REVIEW_REQUIRED_COMPETITION_MIX'
        change='INITIAL' if not old else 'UNCHANGED'
        if old and set(selected)-set(old['fixture_ids']):
            change='NEW_EVIDENCE' if old['frequency'] is None or rate==old['frequency'] else 'STRENGTHENED' if rate>old['frequency'] else 'WEAKENED'
        if old and not set(old['fixture_ids'])<=set(selected):
            change='INCOMPLETE_CURRENT_SAMPLE';state='REVIEW_REQUIRED'
        declared=next((p.get('claimed_statistics') for p in proposals if p.get('team_id')==profile['team_id'] and normalize_tactical_rule(p['condition'])==existing[key]['definition']['condition'] and normalize_tactical_rule(p['outcome'])==existing[key]['definition']['outcome']),None)
        if declared and (declared.get('n')!=n or declared.get('successes')!=len(success)):raise ValueError('PATTERN_DECLARED_COUNTS_MISMATCH')
        evaluation={'pattern_id':key,'team_id':profile['team_id'],'asof':asof.isoformat(),'n':n,'successes':len(success),'frequency':rate,
            'fixture_ids':selected,'counterexamples':counter,'missing_outcomes':missing,'by_competition':competitions,
            'state':state,'evolution':change,'statistical_significance':'NOT_TESTED_MULTIPLE_DISCOVERY_UNCALIBRATED',
            'mechanism_status':p['interpretation_status'],'predictive_use_authorized':False}
        evidence_hash=hashlib.sha256(json.dumps(evaluation,sort_keys=True).encode()).hexdigest()
        persist(library/'patterns'/key/(hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()+'.json'),p)
        persist(library/'evaluations'/key/(evidence_hash+'.json'),evaluation);evaluations.append({**evaluation,'title':p.get('title'),'family':p.get('family'),'mechanism':p['mechanism']})
    families=sorted(set(catalog['families'])|{p['family'] for p in existing.values()})
    persist(library/'catalog_versions'/(hashlib.sha256(json.dumps(families).encode()).hexdigest()+'.json'),{'families':families,'extensible':True})
    return {'catalog':catalog,'accepted_pattern_ids':sorted(set(accepted)),'evaluations':evaluations,'rejected':rejected}


def prepare_research_from_cache(root,source,when):
    """Use existing discovery and shared cache; never acquires a provider response."""
    import hashlib
    root=Path(root);source=Path(source)
    canonical=source.parent.parent.parent/'fixtures_major.json'
    if not canonical.is_file():raise ValueError('RESEARCH_DISCOVERY_MISSING: '+str(canonical))
    document=t.read(canonical)
    if not t.dt(document.get('generated_at_utc')) or t.dt(document['generated_at_utc'])>when:raise ValueError('DISCOVERY_AFTER_CUTOFF')
    cache=LocalHistoryCache(root,when);matches=[]
    for f in document['fixtures']:
        row={**f,'analysis_cutoff_utc':when.isoformat(),'statistical_observations':{}}
        for side in ('home','away'):row['statistical_observations'][side]=cache.for_team(f[side+'_id'],f['league_id'],when,20,f['season'])
        matches.append(row)
    result={'schema_version':'1.1-tactical-cache-preselection','generated_at_utc':when.isoformat(),'matches':matches,
            'source_filter_sha256':t.sha(canonical),'cache_sources':list(cache.sources.values()),'api_calls':0}
    from prematch_predictive_evidence_ranking_v1 import write_once
    write_once(source,json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2))
    return source


def audit_frozen_coverage(root, preselection):
    """Offline per-metric acquisition/extraction audit, preserving source cutoffs.

    Undated legacy caches are inventoried, not silently treated as point-in-time
    evidence. Only compatible FT identities and pre-cutoff dated responses can
    fill a missing observation. Conflicting values are reported, not averaged.
    """
    import copy,hashlib
    from collections import defaultdict,Counter
    from preselect_matches import METRICS,read_stat_block
    root=Path(root);pre=copy.deepcopy(preselection);ledgers=[];sources={};raw_index=defaultdict(list);requests=[]
    for batch in pre.get('source_batches',[]):
        path=Path(batch['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=batch['sha256']:raise ValueError('COVERAGE_BATCH_SHA_MISMATCH')
        doc=json.loads(path.read_text());ledgers.extend(doc.get('api_request_ledger',[]));sources[str(path)]=batch['sha256']
    seen=set()
    for r in ledgers:
        key=(r.get('request_id'),r.get('requested_at_utc'),r.get('status'))
        if key not in seen:requests.append(r);seen.add(key)
    # Shared extraction identities: details are joined only by fixture and team IDs.
    for path in sorted((root/'outputs/data/cache/preselect_api').glob('*.json')):
        try:
            doc=json.loads(path.read_text());at=t.dt(doc.get('retrieved_at_utc'))
            if doc.get('endpoint')!='fixtures' or not at:continue
            for raw in doc.get('response',[]):
                fid=(raw.get('fixture') or {}).get('id')
                if isinstance(fid,int):raw_index[fid].append((at,raw,path))
        except (ValueError,TypeError,OSError):continue
    legacy=defaultdict(list)
    for path in (root/'outputs/data/cache').glob('*.json'):
        try:
            doc=json.loads(path.read_text());params=doc.get('parameters') or {};fid=params.get('fixture')
            if doc.get('get')=='fixtures/statistics' and fid:legacy[int(fid)].append({'path':str(path),'metrics_present':bool(doc.get('response')),'errors':bool(doc.get('errors')),'acquisition_time':'UNKNOWN_NOT_RECOVERABLE_AT_FROZEN_CUTOFF'})
        except (ValueError,TypeError,OSError):continue
    request_summary=Counter(r.get('status','UNKNOWN') for r in requests)
    request_versions=[]
    for req in requests:
        path=Path(req.get('cache_path') or '__missing__')
        current=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        request_versions.append({'endpoint':req.get('endpoint'),'parameters':req.get('parameters'),'status':req.get('status'),'recorded_cache_sha256':req.get('cache_sha256'),'current_cache_sha256':current,'version_matches':current is not None and current==req.get('cache_sha256')})
    audit=[];recovered=[]
    for f in pre['matches']:
        cut=t.dt(f['analysis_cutoff_utc']);target=t.dt(f['kickoff_madrid']);cut=min(cut,target)
        for side in ('home','away'):
            tid=f[side+'_id'];rows=(f.get('statistical_observations') or {}).get(side,[])
            eligible=[r for r in rows if r.get('team_id')==tid and r.get('league_id')==f['league_id'] and r.get('season')==f['season'] and r.get('status')=='FT' and r['fixture_id']!=f['fixture_id'] and t.dt(r['kickoff_utc'])<cut]
            if len({r['fixture_id'] for r in eligible})!=len(eligible):raise ValueError('COVERAGE_DUPLICATE_FIXTURE')
            requested=set((f.get('detail_selection') or {}).get(side,{}).get('selected_fixture_ids',[]))
            req_for=defaultdict(list)
            for req in requests:
                params=req.get('parameters') or {};ids=str(params.get('ids','')).split('-')
                if params.get('id'):ids.append(str(params['id']))
                for row in eligible:
                    if str(row['fixture_id']) in ids:req_for[row['fixture_id']].append(req)
            for kind in ('own','conceded'):
                for metric in ('goals',*METRICS):
                    counts=Counter();items=[]
                    for row in eligible:
                        fid=row['fixture_id'];other=row['opponent_id'];wanted=tid if kind=='own' else other;old=row.get(kind,{}).get(metric)
                        candidates=[];blocks=[];future=0
                        for acquired,raw,path in raw_index.get(fid,[]):
                            pair=raw.get('teams') or {};ident={pair.get(s,{}).get('id') for s in ('home','away')};fx=raw.get('fixture') or {};lg=raw.get('league') or {}
                            if ident!={tid,other} or (pair.get('home',{}).get('id')==tid)!= (row['venue']=='HOME') or lg.get('id')!=f['league_id'] or lg.get('season')!=f['season'] or t.dt(fx.get('date'))!=t.dt(row['kickoff_utc']):continue
                            if acquired>cut:future+=1;continue
                            blocks.append(raw)
                            value=(raw.get('goals') or {}).get('home' if pair.get('home',{}).get('id')==wanted else 'away') if metric=='goals' else read_stat_block(raw,wanted).get(metric)
                            if numeric(value) is not None:candidates.append((value,path,acquired))
                        values={float(v) for v,_,_ in candidates};parser_fail=False
                        if metric!='goals':
                            for raw in blocks:
                                matches=[g for g in raw.get('statistics',[]) if g.get('team',{}).get('id')==wanted]
                                if len(matches)>1:parser_fail=True
                                if len(matches)==1:
                                    fields=matches[0].get('statistics') or [];keys=[v.get('type') for v in fields]
                                    if keys.count(METRICS[metric])>1:parser_fail=True
                                    if any(v.get('type')==METRICS[metric] and numeric(v.get('value')) is not None for v in fields) and read_stat_block(raw,wanted).get(metric) is None:parser_fail=True
                        state='VALID_EMBEDDED' if numeric(old) is not None else 'NOT_REQUESTED' if fid not in requested else 'REQUESTED_METRIC_MISSING'
                        if numeric(old) is None and len(values)==1 and not parser_fail:
                            value,path,acquired=candidates[0];row.setdefault(kind,{})[metric]=float(value)
                            if metric!='goals':row['statistics_requested']=True;row['statistics_request_status']='RECOVERED_CACHED_DETAIL'
                            state='RECOVERED_FROM_PRE_CUTOFF_CACHE';sources[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest();recovered.append({'fixture_id':f['fixture_id'],'historical_fixture_id':fid,'team_id':tid,'kind':kind,'metric':metric,'value':value,'path':str(path),'acquired_at':acquired.isoformat()})
                        if len(values)>1 or numeric(old) is not None and values and float(old) not in values:state='CACHE_VALUE_CONFLICT_NO_OVERWRITE'
                        if parser_fail:state='PARSER_OR_DUPLICATE_BLOCK_FAILURE'
                        counts[state]+=1
                        req=req_for[fid];fail=sum(x.get('status') not in ('CACHE_HIT','NETWORK_OK') for x in req)
                        counts['request_error_records']+=fail
                        counts['request_records']+=len(req)
                        counts['dated_responses_without_statistics_block']+=int(bool(blocks) and all(not r.get('statistics') for r in blocks)) if metric!='goals' else 0
                        counts['eligible_after_cutoff_cache_records_excluded']+=future
                        counts['usable_n']+=int(numeric(row.get(kind,{}).get(metric)) is not None)
                        items.append({'fixture_id':fid,'state':state,'requested_in_batch_plan':fid in requested,'request_records':len(req),'request_errors':fail,'dated_cache_responses':len(blocks),'responses_with_metric':len(candidates),'responses_statistics_empty':sum(raw.get('statistics')==[] for raw in blocks),'responses_statistics_field_absent':sum('statistics' not in raw for raw in blocks),'after_cutoff_responses_excluded':future,'undated_legacy_statistics':legacy.get(fid,[]),'source_embedded_value':old,'usable_value':row.get(kind,{}).get(metric),'parser_failure':parser_fail})
                    audit.append({'fixture_id':f['fixture_id'],'team_id':tid,'side':side,'kind':kind,'metric':metric,'eligible_history_n':len(eligible),'target_history_cap':20,'requested_detail_cap':(f.get('detail_selection') or {}).get(side,{}).get('requested_cap'),
                                  'requests_planned_n':sum(r['fixture_id'] in requested for r in eligible),'requests_omitted_n':sum(r['fixture_id'] not in requested for r in eligible),'counts':dict(counts),'coverage_effective_pct':100*counts['usable_n']/len(eligible) if eligible else None,'fixtures':items})
    return pre,{'series':audit,'recovered':recovered,'source_hashes':sources,'all_batch_request_records':len(requests),'request_status_counts':dict(request_summary),'request_versions':request_versions,'api_calls':0,'unknown_legacy_timestamp_policy':'INVENTORIED_NOT_USED','original_values_never_overwritten':True}
