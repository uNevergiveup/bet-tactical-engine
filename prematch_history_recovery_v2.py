#!/usr/bin/env python3
"""Offline-first historical recovery. Online acquisition requires --online.

Uses the existing Phase1ApiClient (budgets/retries/negative TTL) and writes only
new derived cache/report versions. Original provider snapshots stay untouched.
"""
import argparse,hashlib,json,math
from collections import defaultdict,Counter
from datetime import datetime,timezone,timedelta
from pathlib import Path

import prematch_advanced_discovery_v2 as discovery
from prematch_phase1 import Phase1ApiClient,Phase1Error,BudgetExhausted,atomic_json
from prematch_predictive_evidence_ranking_v1 import write_once

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def ref(p,doc):return {'path':str(p.resolve()),'sha256':sha(p),'fetched_at_utc':doc.get('fetched_at_utc',doc.get('retrieved_at_utc'))}

def identity_matches(record,row,final=True):
    f=record.get('fixture',{});l=record.get('league',{});t=record.get('teams',{})
    h=t.get('home',{}).get('id');a=t.get('away',{}).get('id')
    expected=(row['team_id'],row['opponent_id']) if row['venue']=='HOME' else (row['opponent_id'],row['team_id'])
    return (f.get('id')==row['fixture_id'] and (h,a)==expected and l.get('id')==row['league_id'] and l.get('season')==row['season'] and discovery.dt(f.get('date'))==discovery.dt(row['kickoff_utc']) and (not final or f.get('status',{}).get('short')=='FT'))

def legacy_index(root,wanted):
    index=defaultdict(list);errors=[]
    for p in sorted((root/'outputs/data/cache').rglob('*.json')):
        try:
            doc=read(p);payload=doc.get('payload',doc)
            rows=payload.get('response',[])
            if payload.get('errors'):continue
            if not isinstance(rows,list):continue
            for r in rows:
                if isinstance(r,dict) and isinstance(r.get('fixture'),dict) and r['fixture'].get('id') in wanted and r.get('teams') and r.get('league'):
                    index[r['fixture']['id']].append((r,ref(p,doc)))
        except (OSError,ValueError,TypeError) as exc:errors.append({'path':str(p),'reason':str(exc)})
    return index,errors

def import_response(cache,endpoint,params,response,source):
    if Path(source['path']).resolve().is_relative_to(cache.resolve()):return Path(source['path'])
    # Never overwrite a previous imported version, even if source changed.
    query='&'.join(f'{k}={v}' for k,v in sorted(params.items()))
    payload={'response':response}
    doc={'endpoint':endpoint,'query':query,'parameters':{k:str(v) for k,v in params.items()},'response':response,
         'fetched_at_utc':source.get('fetched_at_utc'),'imported_from':source,
         'import_status':'CONTENT_VERIFIED_LEGACY_IMPORT','raw_payload':payload,
         'payload_sha256':discovery.digest(payload)}
    token=discovery.digest({'endpoint':endpoint,'query':query,'source':source})
    path=cache/endpoint.replace('/','_')/(token+'.json')
    write_once(path,json.dumps(doc,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return path

def usable(response,endpoint):
    if not isinstance(response,list) or not response:return False
    if endpoint=='fixtures/statistics':return any(any(discovery.numeric(s.get('value')) is not None for s in r.get('statistics',[])) for r in response)
    if endpoint=='fixtures/players':return any(p.get('statistics') for r in response for p in r.get('players',[]))
    if endpoint=='fixtures/lineups':return any(r.get('startXI') for r in response)
    return True

def complete(response,endpoint):
    return usable(response,endpoint) and len({r.get('team',{}).get('id') for r in response})==2

def diversification_proposal(observations,targets,preflights):
    """Offline proposal, not an automatically executable acquisition queue."""
    observed=defaultdict(set);sources=[]
    for path in preflights:
        doc=read(path)
        for match in doc.get('matches',[]):
            snapshot=match.get('snapshot',{});txt=Path(snapshot.get('snapshot_path',''))
            target=next((t for t in targets if t['fixture_id']==match.get('fixture_id')),None)
            if not target or not txt.is_file() or sha(txt)!=snapshot.get('sha256') or match.get('quality_gate',{}).get('join_status')!='MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF':continue
            from bwin_market_preflight import snapshot_odds
            parsed=snapshot_odds(txt,match['bwin_event_id'])
            kinds={k for k,v in parsed['market_kinds'].items() if v>0}
            for tid in (target['home_id'],target['away_id']):observed[(tid,target['league_id'],target['season'])].update(kinds)
            sources.append({'path':str(path),'sha256':sha(path),'snapshot_sha256':snapshot['sha256'],'fixture_id':target['fixture_id'],'scope':'EVENT_MARKET_FAMILY_OBSERVED_NOT_TEAM_OR_PLAYER_SELECTION_VERIFIED'})
    teams=defaultdict(list);players_by_team=defaultdict(list)
    for row in observations:
        key=(row['team_id'],row['league_id'],row['season'],row['gender'])
        (teams if row['entity_type']=='TEAM' else players_by_team)[key].append(row)
    ranking=[]
    for key,history in teams.items():
        players=players_by_team[key];counts=Counter(r['player_id'] for r in players)
        available={r['fixture_id'] for r in players};train,_=discovery.split_history(history)
        pending=[r for r in train if r['fixture_id'] not in available]
        if not pending:continue
        ranking.append({'team_id':key[0],'league_id':key[1],'season':key[2],'gender':key[3],
            'existing_player_records':len(players),'players_with_6_to_8_total':sum(6<=n<=8 for n in counts.values()),
            'players_seen':len(counts),'candidate_fixture_ids':[r['fixture_id'] for r in pending],
            'observed_event_market_families':sorted(observed[(key[0],key[1],key[2])]),'per_player_market_availability':'NOT_VERIFIED',
            'estimated_calls_upper_bound':3*len(pending),'estimated_records_gain':None,
            'conditions':['CACHE_AUDIT_FIRST','VERIFY_IDENTITY','PLAYER_PARTICIPATION_UNKNOWN','NO_SIGNIFICANCE_GUARANTEE']})
    ranking.sort(key=lambda x:(-bool(x['observed_event_market_families']),-x['players_with_6_to_8_total'],-x['existing_player_records'],x['team_id']))
    diversified=[];used=set()
    for row in ranking:
        if row['league_id'] not in used:diversified.append(row);used.add(row['league_id'])
    diversified.extend(row for row in ranking if row not in diversified)
    for rank,row in enumerate(diversified,1):row['rank']=rank
    return {'status':'PROPOSAL_REQUIRES_REVIEW_NOT_EXECUTED','teams':diversified,'market_sources':sources,
        'fixture_priority':[{'team_rank':r['rank'],'team_id':r['team_id'],'league_id':r['league_id'],'fixture_id':fid,'priority_reason':'OLDER_TRAINING_GAP_IN_DIVERSIFIED_COMPETITION','endpoints':['fixtures/players','fixtures/lineups','fixtures/statistics']} for r in diversified for fid in r['candidate_fixture_ids']]}

class RecoveryApiClient(Phase1ApiClient):
    def get(self,endpoint,**params):
        result=super().get(endpoint,**params)
        if endpoint.startswith('fixtures/') and not complete(result.response,endpoint):
            envelope=read(result.cache_path);envelope['ttl_seconds']=300
            envelope['completeness']='PARTIAL_UNUSABLE_OR_TEAM_COVERAGE'
            atomic_json(result.cache_path,envelope)
        return result

def player_priority(discovery_dir,normalized,status):
    data=read(discovery_dir/'historical_observations.json')
    groups=defaultdict(list)
    for row in data['observations']:
        if row['entity_type']=='PLAYER':groups[(row['team_id'],row['player_id'],row['league_id'],row['season'],row['gender'],row['team_kind'],row['period'])].append(row)
    segments=[];benefits=defaultdict(list)
    for key,series in sorted(groups.items(),key=str):
        train,holdout=discovery.split_history(series)
        if not holdout:continue
        boundary=min(discovery.dt(r['kickoff_utc']) for r in holdout)
        seen={r['fixture_id'] for r in series}
        coverage={k:sum(discovery.finite(r['metrics'].get(k)) for r in series) for k in sorted({k for r in series for k in r['metrics'] if k.startswith('player.')})}
        needed=max(0,math.ceil(6/.7)-len(series))
        segment={'team_id':key[0],'player_id':key[1],'league_id':key[2],'season':key[3],'gender':key[4],'team_kind':key[5],'period':key[6],
                 'total':len(series),'training_n':len(train),'holdout_n':len(holdout),'original_holdout_boundary':boundary.isoformat(),
                 'metrics_valid':coverage,'additional_observations_estimated':needed,'qualification':'POTENTIAL_ONLY_PARTICIPATION_AND_METRICS_UNPROVEN'}
        candidates={r['fixture_id'] for r in normalized if (r['team_id'],r['league_id'],r['season'],r['gender'])==(key[0],key[2],key[3],key[4]) and r['fixture_id'] not in seen and discovery.dt(r['kickoff_utc'])<boundary and status[r['fixture_id']]['identity']=='PRESENT_VERIFIED_CONTENT' and status[r['fixture_id']]['endpoints']['fixtures/players']['state']!='CACHE_USABLE'}
        segment['pending_fixture_ids']=sorted(candidates)
        segments.append(segment)
        if len(train)<6:
            for fid in candidates:benefits[fid].append(segment)
    ranking=[]
    for fid,values in benefits.items():
        ranking.append({'fixture_id':fid,'near_completion_segments':sum(x['additional_observations_estimated']<=2 for x in values),
                        'potential_segments':len(values),'potential_players':len({x['player_id'] for x in values}),
                        'observed_metric_evidence':sum(sum(v>0 for v in x['metrics_valid'].values()) for x in values),
                        'segments':values,'cache':status[fid]['endpoints'],'bwin_individual_markets':'NOT_PROVEN_NO_COMMERCIAL_BONUS',
                        'risk':'PLAYER_PARTICIPATION_OR_PROVIDER_COVERAGE_NOT_GUARANTEED'})
    ranking.sort(key=lambda r:(-r['near_completion_segments'],-r['potential_players'],-r['observed_metric_evidence'],-status[r['fixture_id']]['perspectives'],r['fixture_id']))
    for index,row in enumerate(ranking,1):row['rank']=index
    return ranking,segments

def recover(source,root,out,*,online=False,max_calls=0,resume=False,client=None,player_discovery=None,fixture_ids=None):
    source=Path(source).resolve();root=Path(root).resolve();out=Path(out).resolve()
    doc=read(source);cutoff=discovery.dt(doc.get('generated_at_utc'))
    if cutoff is None:raise ValueError('CUTOFF_MISSING')
    config={'source_sha256':sha(source),'cutoff':cutoff.isoformat(),'version':'4a-1'}
    explicit=list(dict.fromkeys(fixture_ids or []))
    if any(not discovery.valid_id(fid) for fid in explicit):raise ValueError('FIXTURE_IDS_MUST_BE_POSITIVE_INTEGERS')
    if explicit:config['explicit_fixture_ids']=explicit
    if player_discovery:
        player_discovery=Path(player_discovery).resolve()
        manifest=read(player_discovery/'manifest.json')
        if manifest['source']['sha256']!=sha(source):raise ValueError('PLAYER_DISCOVERY_SOURCE_MISMATCH')
        if manifest.get('artifacts',{}).get('historical_observations.json')!=sha(player_discovery/'historical_observations.json'):raise ValueError('PLAYER_DISCOVERY_ARTIFACT_SHA_MISMATCH')
        config['player_discovery_sha256']=sha(player_discovery/'historical_observations.json')
    statefile=out/'recovery_identity.json'
    if statefile.exists():
        if read(statefile)!=config:raise ValueError('RESUME_INPUT_IDENTITY_CONFLICT')
        if online and not resume:raise ValueError('ONLINE_EXISTING_RUN_REQUIRES_RESUME')
    else:write_once(statefile,json.dumps(config,sort_keys=True,indent=2)+'\n')
    cache=out/'cache';shared=discovery.EnrichedCache(root/'outputs/.api_football_history_cache');prior=discovery.EnrichedCache(cache)
    upstream=discovery.EnrichedCache(manifest['cache_root']) if player_discovery and manifest.get('cache_root') else None
    normalized,_,audit=discovery.normalize(doc,{'path':str(source),'sha256':sha(source)},None)
    if explicit:normalized=[r for r in normalized if r['fixture_id'] in explicit]
    rows=defaultdict(list)
    for r in normalized:rows[r['fixture_id']].append(r)
    legacy,errors=legacy_index(root,set(explicit or rows));status={};coverage=[];priority=Counter();identity_missing=[]
    if explicit:
        for fid in explicit:
            if fid in rows:continue
            candidates=shared.candidates('fixtures',fid)+legacy.get(fid,[])+prior.candidates('fixtures',fid)
            if upstream:candidates+=upstream.candidates('fixtures',fid)
            signatures={(r.get('teams',{}).get('home',{}).get('id'),r.get('teams',{}).get('away',{}).get('id'),r.get('league',{}).get('id'),r.get('league',{}).get('season'),discovery.dt(r.get('fixture',{}).get('date'))) for r,_ in candidates}
            if len(signatures)!=1:raise ValueError(f'EXPLICIT_FIXTURE_IDENTITY_UNVERIFIED_OR_CONFLICT:{fid}')
            historical=[r for r,s in candidates if r.get('fixture',{}).get('status',{}).get('short')=='FT' and discovery.dt(s.get('fetched_at_utc'))]
            if not historical:raise ValueError(f'EXPLICIT_FIXTURE_NOT_VERIFIED_FINAL:{fid}')
            record=historical[0];home,away,league,season,kickoff=next(iter(signatures))
            if not all(discovery.valid_id(x) for x in (home,away,league,season)) or home==away or not kickoff or kickoff+timedelta(hours=3)>=cutoff:
                raise ValueError(f'EXPLICIT_FIXTURE_NOT_ELIGIBLE_BEFORE_CUTOFF:{fid}')
            for venue,tid,opponent in [('HOME',home,away),('AWAY',away,home)]:
                rows[fid].append({'fixture_id':fid,'team_id':tid,'opponent_id':opponent,'venue':venue,'league_id':league,'season':season,'gender':'UNKNOWN','kickoff_utc':kickoff.isoformat(),'metrics':{}})
        rows={fid:rows[fid] for fid in explicit}
    training=set()
    segments=defaultdict(list)
    for r in normalized:segments[(r['team_id'],r['league_id'],r['season'],r['gender'])].append(r)
    for key,series in segments.items():
        train,test=discovery.split_history(series)
        training.update((r['fixture_id'],r['team_id']) for r in train)
        metrics={}
        for metric in ('goals','shots','on_target','corners','fouls','yellow','xg'):
            metrics[metric]={'valid':sum(discovery.finite(r['metrics'].get('own.'+metric)) for r in series),'total':len(series),'training_valid':sum(discovery.finite(r['metrics'].get('own.'+metric)) for r in train),'training_total':len(train)}
        coverage.append({'team_id':key[0],'league_id':key[1],'season':key[2],'gender':key[3],'metrics_before':metrics})
    for fid,perspectives in rows.items():
        options=shared.candidates('fixtures',fid)+legacy.get(fid,[])
        if upstream:options+=upstream.candidates('fixtures',fid)
        if not options:options=prior.candidates('fixtures',fid)
        valid=[(r,s) for r,s in options if all(identity_matches(r,p) for p in perspectives) and discovery.dt(s.get('fetched_at_utc'))]
        valid.sort(key=lambda item:(0 if discovery.dt(item[1]['fetched_at_utc'])<=cutoff else 1,item[1]['fetched_at_utc']))
        # Quarantine contradicting body identities instead of guessing an alias.
        conflict=bool(options and any(not all(identity_matches(r,p,False) for p in perspectives) for r,_ in options))
        selected=valid[0] if valid and not conflict else None
        if explicit and not selected:raise ValueError(f'EXPLICIT_FIXTURE_IDENTITY_UNVERIFIED_OR_CONFLICT:{fid}')
        entry={'fixture_id':fid,'identity':'CONFLICT' if conflict else 'PRESENT_VERIFIED_CONTENT' if selected else 'NOT_ACQUIRED',
               'sources_found':len(options),'endpoints':{},'perspectives':len(perspectives)}
        if explicit:entry['historical_identity']={k:perspectives[0].get(k) for k in ('team_id','opponent_id','venue','league_id','season','kickoff_utc')}
        if conflict:
            entry['conflicting_evidence']=[{'source':s,'fixture':r.get('fixture'),'league':r.get('league'),'teams':r.get('teams')} for r,s in options if not all(identity_matches(r,p,False) for p in perspectives)]
        priority[fid]=sum(10*((fid,r['team_id']) in training)+sum(discovery.finite(v) for k,v in r['metrics'].items() if k.startswith('own.') and k!='own.goals') for r in perspectives)+len(perspectives)
        if selected:
            record,source_ref=selected;import_response(cache,'fixtures',{'id':fid},[record],source_ref)
            entry['identity_source']=source_ref
            entry['acquisition_status']='RECORDED_BY_CUTOFF' if discovery.dt(source_ref['fetched_at_utc'])<=cutoff else 'RETROSPECTIVE_RECONSTRUCTION'
        else:
            record={}
            if not conflict:identity_missing.append(fid)
        for endpoint in ('fixtures/statistics','fixtures/players','fixtures/lineups'):
            response,source_ref=shared.get(endpoint,fid,cutoff)
            if upstream:
                inherited,inherited_ref=upstream.get(endpoint,fid,cutoff)
                if usable(inherited,endpoint):response,source_ref=inherited,inherited_ref
            old_response,old_ref=prior.get(endpoint,fid,cutoff)
            if response is None and selected and isinstance(record.get(endpoint.split('/')[1]),list):response=record[endpoint.split('/')[1]];source_ref=selected[1]
            # Preselection caches omit endpoint metadata; only an exact query hash
            # can identify a standalone statistics/players/lineups response.
            if response is None:
                token=hashlib.sha256(f'{endpoint}?fixture={fid}'.encode()).hexdigest()
                p=root/'outputs/data/cache/preselect_api'/f'{token}.json'
                if p.exists():
                    raw=read(p);response=raw.get('response');source_ref=ref(p,raw)
            if not usable(response,endpoint) and usable(old_response,endpoint):response,source_ref=old_response,old_ref
            value=('CACHE_USABLE' if complete(response,endpoint) else 'CACHE_PARTIAL_USABLE') if usable(response,endpoint) and selected else 'CACHED_EMPTY_OR_UNUSABLE' if response is not None else 'NOT_QUERIED_IN_KNOWN_CACHE'
            if response and isinstance(response,list):
                observed={b.get('team',{}).get('id') for b in response}
                expected={r['team_id'] for r in perspectives}|{r['opponent_id'] for r in perspectives}
                if not observed.issubset(expected):value='CACHE_IDENTITY_CONFLICT'
            entry['endpoints'][endpoint]={'state':value,'source':source_ref}
            if value in ('CACHE_USABLE','CACHE_PARTIAL_USABLE'):import_response(cache,endpoint,{'fixture':fid},response,source_ref)
        status[fid]=entry
    ordered=explicit or sorted(rows,key=lambda fid:(-priority[fid],fid));batch_ids=[fid for fid in ordered if status[fid]['identity']!='CONFLICT' and (fid in identity_missing or status[fid]['endpoints']['fixtures/statistics']['state']!='CACHE_USABLE')]
    tasks=[{'endpoint':'fixtures','params':{'ids':'-'.join(map(str,batch_ids[i:i+20]))},'fixture_ids':batch_ids[i:i+20]} for i in range(0,len(batch_ids),20)]
    for endpoint in ('fixtures/statistics','fixtures/players','fixtures/lineups'):
        tasks.extend({'endpoint':endpoint,'params':{'fixture':fid},'fixture_ids':[fid]} for fid in ordered if status[fid]['identity']!='CONFLICT' and status[fid]['endpoints'][endpoint]['state']!='CACHE_USABLE')
    utility=None
    if player_discovery and not explicit:
        ranking,segments=player_priority(player_discovery,normalized,status)
        previous_tasks=tasks
        tasks=[{'endpoint':endpoint,'params':{'fixture':r['fixture_id']},'fixture_ids':[r['fixture_id']]} for r in ranking for endpoint in ('fixtures/players','fixtures/lineups','fixtures/statistics') if status[r['fixture_id']]['endpoints'][endpoint]['state']!='CACHE_USABLE']
        def budget_view(queue,n):
            fids={fid for task in queue[:n] for fid in task['fixture_ids']}
            return {'planned_requests':min(n,len(queue)),'fixtures_touched':len(fids),'potential_segments':len({(s['team_id'],s['player_id'],s['league_id'],s['season'],s['gender']) for r in ranking if r['fixture_id'] in fids for s in r['segments']}),'not_actual_recovered_records':True}
        utility={'ranking':ranking,'segments':segments,'method':'LEXICOGRAPHIC_COMPLETION_THEN_PLAYERS_THEN_OBSERVED_METRICS_THEN_SHARED_FIXTURE','budgets':{str(n):{'directed':budget_view(tasks,n),'previous':budget_view(previous_tasks,n)} for n in (10,20,30)}}
    plan={'config':config,'unique_fixtures':len(rows),'team_fixture_observations':len(normalized),'identities_missing':len(identity_missing),'identity_conflicts':sum(s['identity']=='CONFLICT' for s in status.values()),'identity_present':sum(s['identity']=='PRESENT_VERIFIED_CONTENT' for s in status.values()),'tasks':tasks,'priority':dict(priority),'coverage_before':coverage,'fixtures':list(status.values()),'errors':errors,'endpoint_entitlement':'NOT_VERIFIED_OFFLINE','estimate':{'requests_no_retry_upper_bound':len(tasks),'grouped_fixtures_requests':math.ceil(len(batch_ids)/20),'conditional_detail_requests':len(tasks)-math.ceil(len(batch_ids)/20),'retry_upper_bound':3*len(tasks)}}
    grouped=sum(t['endpoint']=='fixtures' for t in tasks)
    if explicit:
        tasks.sort(key=lambda task:min(explicit.index(fid) for fid in task['fixture_ids']))
        if any(fid not in explicit for task in tasks for fid in task['fixture_ids']):raise ValueError('EXPLICIT_SCOPE_VIOLATION')
        plan['tasks']=tasks
        plan['explicit_fixture_ids']=explicit
        plan['player_discovery_role']='CONTEXT_ONLY_NO_SCOPE_EXPANSION'
    plan['estimate'].update(grouped_fixtures_requests=grouped,conditional_detail_requests=len(tasks)-grouped)
    if utility:plan['player_utility_summary']={'ranked_fixtures':len(utility['ranking']),'benefited_segments':len({(s['team_id'],s['player_id'],s['league_id'],s['season'],s['gender']) for r in utility['ranking'] for s in r['segments']}),'budgets':utility['budgets']}
    version=discovery.digest(plan)[:16];write_once(out/f'plan_{version}.json',json.dumps(plan,sort_keys=True,indent=2)+'\n')
    if utility:write_once(out/f'player_utility_plan_{version}.json',json.dumps(utility,sort_keys=True,indent=2)+'\n')
    results=[];api=client
    if online:
        if client is None:
            from dotenv import load_dotenv
            load_dotenv(root/'config/.env')
        api=api or RecoveryApiClient(out/'acquired_cache',offline=False,max_new_calls=max_calls)
        if api.max_new_calls>max_calls:raise ValueError('CLIENT_BUDGET_EXCEEDS_AUTHORIZED_LIMIT')
        completed=set()
        # Scheduling alternates endpoint classes after each small fixture batch;
        # resume checks acquired evidence, not just a success flag.
        for task in tasks:
            fid=task['fixture_ids'][0];endpoint=task['endpoint']
            local=discovery.EnrichedCache(cache)
            if endpoint!='fixtures':
                identity,_=local.get('fixtures',fid,cutoff)
                if not identity:continue
                existing,_=local.get(endpoint,fid,cutoff)
                if complete(existing,endpoint):continue
            try:
                result=api.get(endpoint,**task['params']);payload=result.response
                archived=out/'raw'/f'{sha(result.cache_path)}.json'
                write_once(archived,result.cache_path.read_bytes())
                origin={'path':str(archived.resolve()),'sha256':sha(archived),'fetched_at_utc':result.fetched_at_utc}
                if endpoint=='fixtures':
                    for fid2 in task['fixture_ids']:
                        matches=[r for r in payload if r.get('fixture',{}).get('id')==fid2]
                        if len(matches)==1 and all(identity_matches(matches[0],r) for r in rows[fid2]):
                            import_response(cache,'fixtures',{'id':fid2},matches,origin)
                            for kind in ('statistics','players','lineups'):
                                if usable(matches[0].get(kind),f'fixtures/{kind}'):import_response(cache,f'fixtures/{kind}',{'fixture':fid2},matches[0][kind],origin)
                        else:results.append({'fixture_id':fid2,'state':'IDENTITY_UNVERIFIED_RESPONSE'})
                else:
                    ids={b.get('team',{}).get('id') for b in payload}
                    expected={r['team_id'] for r in rows[fid] }|{r['opponent_id'] for r in rows[fid]}
                    if not ids.issubset(expected):results.append({'fixture_id':fid,'state':'ENDPOINT_TEAM_ID_CONFLICT'});continue
                    import_response(cache,endpoint,{'fixture':fid},payload,origin)
                results.append({'endpoint':endpoint,'params':task['params'],'state':'RECOVERED_USABLE' if usable(payload,endpoint) else 'PENDING_EMPTY_OR_UNUSABLE','source':origin})
            except BudgetExhausted:
                results.append({'state':'BUDGET_EXHAUSTED'});break
            except (Phase1Error,OSError,ValueError) as exc:results.append({'endpoint':endpoint,'params':task['params'],'state':'ERROR','reason':str(exc)})
    if explicit:
        doc=json.loads(json.dumps(doc))
        for match in doc.get('matches',[]):
            for side in ('home','away'):
                match.get('statistical_observations',{})[side]=[r for r in match.get('statistical_observations',{}).get(side,[]) if r.get('fixture_id') in explicit]
    observations,_,_=discovery.normalize(doc,{'path':str(source),'sha256':sha(source)},discovery.EnrichedCache(cache),True)
    after=defaultdict(Counter)
    for row in observations:
        key=f"{row['league_id']}:{row['season']}:{row['gender']}:{row['team_id']}:{row['entity_type']}"
        after[key]['observations']+=1
        for k,v in row['metrics'].items():
            if discovery.finite(v):after[key][k]+=1
    final={'plan':f'plan_{version}.json','new_api_calls':api.new_calls if api else 0,'cache_hits':api.cache_hits if api else 0,'results':results,'query_ledger':api.ledger if api else [],'coverage_after':dict(Counter(k for r in observations for k,v in r['metrics'].items() if discovery.finite(v))),'player_observations_after':sum(r['entity_type']=='PLAYER' for r in observations),'mode':'ONLINE' if online else 'OFFLINE','historical_usage':'RETROSPECTIVE_RECONSTRUCTION_WHERE_ACQUIRED_AFTER_CUTOFF','pending_unique_fixtures':len({fid for task in tasks for fid in task['fixture_ids']}),'unexecuted_tasks_are_pending':True}
    final['coverage_after_by_segment']=dict(after)
    final_reader=discovery.EnrichedCache(cache)
    final['pending_unique_fixtures_after']=sum(any(not complete(final_reader.get(ep,fid,cutoff)[0],ep) for ep in ('fixtures/statistics','fixtures/players','fixtures/lineups')) for fid in rows if status[fid]['identity']!='CONFLICT')
    final['cache_usable_endpoints']=sum(e['state']=='CACHE_USABLE' for s in status.values() for e in s['endpoints'].values())
    token=discovery.digest(final)[:16];write_once(out/f'result_{token}.json',json.dumps(final,sort_keys=True,indent=2)+'\n')
    return plan,final

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--preselection',type=Path,required=True);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--online',action='store_true');p.add_argument('--resume',action='store_true');p.add_argument('--max-api-calls',type=int,default=0);p.add_argument('--player-discovery',type=Path);p.add_argument('--fixture-id',action='append',type=int,default=[]);a=p.parse_args()
    if any(fid<=0 for fid in a.fixture_id):p.error('--fixture-id requires positive integers')
    if a.online and a.max_api_calls<1:p.error('ONLINE requires a positive request budget')
    plan,result=recover(a.preselection,a.root,a.output_dir,online=a.online,max_calls=a.max_api_calls,resume=a.resume,player_discovery=a.player_discovery,fixture_ids=a.fixture_id)
    print(json.dumps({'unique':plan['unique_fixtures'],'identities_present':plan['identity_present'],'missing':plan['identities_missing'],'pending':result['pending_unique_fixtures'],'estimated_calls':plan['estimate'],'new_calls':result['new_api_calls'],'players_after':result['player_observations_after'],'files':str(a.output_dir)},ensure_ascii=False))
    directed=f' --player-discovery {a.player_discovery}' if a.player_discovery else ''
    directed+=''.join(f' --fixture-id {fid}' for fid in dict.fromkeys(a.fixture_id))
    if a.fixture_id:print(json.dumps({'selected':plan['explicit_fixture_ids'],'fixtures':plan['fixtures'],'tasks':plan['tasks']},ensure_ascii=False))
    print(f'Next acquisition (manual): python prematch_history_recovery_v2.py --preselection {a.preselection} --output-dir {a.output_dir}{directed} --online --resume --max-api-calls 10')
    print(f'After inspecting recovery results: python prematch_advanced_discovery_v2.py --preselection {a.preselection} --cache-root {a.output_dir}/cache --allow-retrospective-cache --output-dir {a.output_dir}/discovery_new_version')
if __name__=='__main__':main()
