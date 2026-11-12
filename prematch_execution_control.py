"""Manual control for the existing PREMATCH stages; preparation never acquires data."""
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def require_external_confirmation(confirmed=False):
    import re
    if not confirmed and not re.fullmatch(r'[a-f0-9]{64}',os.getenv('PREMATCH_EXTERNAL_CONFIRMATION','')):
        raise ValueError('EXTERNAL_CONFIRMATION_REQUIRED: use --confirm-external explicitly')


def confirmed_cli(function):
    """Propagate an explicit CLI approval to children, restoring the caller env."""
    from functools import wraps
    @wraps(function)
    def wrapped(argv=None):
        arguments=list(sys.argv[1:] if argv is None else argv)
        previous=os.environ.get('PREMATCH_EXTERNAL_CONFIRMATION')
        if '--confirm-external' in arguments:
            os.environ['PREMATCH_EXTERNAL_CONFIRMATION']=digest({'explicit_cli':arguments})
        try:return function(arguments)
        finally:
            if previous is None:os.environ.pop('PREMATCH_EXTERNAL_CONFIRMATION',None)
            else:os.environ['PREMATCH_EXTERNAL_CONFIRMATION']=previous
    return wrapped


def reference(path):
    path=Path(path).resolve()
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def write_once(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as handle:json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)


def read_plan(path):
    plan=json.loads(Path(path).read_text());claimed=plan.pop('approval_sha256')
    if digest(plan)!=claimed:raise ValueError('PLAN_HASH_MISMATCH')
    for ref in plan['inputs'].values():
        if ref and reference(ref['path'])!=ref:raise ValueError('PLAN_INPUT_CHANGED')
    for ref in plan.get('preflights',[]):
        if reference(ref['path'])!=ref:raise ValueError('PLAN_INPUT_CHANGED')
    plan['approval_sha256']=claimed
    return plan


def prepare(args):
    from prematch_phase1 import parse_clock,window_bounds
    begin,end=window_bounds(date.fromisoformat(args.date),parse_clock(args.start),parse_clock(args.end))
    if args.fixtures_input:
        fixtures=json.loads(args.fixtures_input.read_text())
        from prematch_phase1 import parse_dt
        if any(not (ko:=parse_dt(row.get('kickoff_utc') or row.get('kickoff_madrid'))) or not begin<=ko<=end
               for row in fixtures.get('fixtures',[])):
            raise ValueError('FIXTURE_INPUT_OUTSIDE_PLANNED_WINDOW')
    if not 0<=args.api_budget<=7500 or not 0<=args.bwin_events<=100 or not 1<=args.llm_max_iterations<=10 or not 1<=args.history<=20:
        raise ValueError('INVALID_EXPLICIT_BUDGET')
    runtime=json.loads(args.llm_runtime_command_json) if args.llm_runtime_command_json else None
    if args.browser_executable and not Path(args.browser_executable).is_file():raise ValueError('BROWSER_EXECUTABLE_NOT_FOUND')
    if runtime is not None and (not isinstance(runtime,list) or not runtime or any(not isinstance(v,str) for v in runtime)):
        raise ValueError('RUNTIME_ARGV_ARRAY_REQUIRED')
    plan={'schema_version':'1.0-manual-prematch-plan','created_at_utc':datetime.now(timezone.utc).isoformat(),
          'date':args.date,'start':args.start,'end':args.end,'window':{'start':begin.isoformat(),'end':end.isoformat()},
          'root':str(args.root.resolve()),'output_dir':str(args.output_dir.resolve()),'history':args.history,
          'cache_root':str((args.cache_root or args.root/'outputs/.api_football_history_cache').resolve()),
          'api_budget':args.api_budget,'bwin_events':args.bwin_events,'llm_max_iterations':args.llm_max_iterations,
          'runtime_command':runtime,'browser_executable':args.browser_executable,
          'inputs':{'fixtures':reference(args.fixtures_input) if args.fixtures_input else None,
                    'bwin':reference(args.bwin_input) if args.bwin_input else None},
          'preflights':[reference(path) for path in args.preflight],
          'code':{name:reference(args.root/name)['sha256'] for name in (
              'prematch_phase1.py','prematch_tactical_history.py','prematch_phase2.py',
              'prematch_advanced_discovery_v2.py','prematch_predictive_engine.py',
              'prematch_llm_research.py','prematch_execution_control.py','bwin_discovery.py',
              'scraper_bwin.py','bwin_market_preflight.py','prematch_market_mapping_v1.py',
              'prematch_predictive_evidence_ranking_v1.py','prematch_daily.py','preselect_matches.py',
              'prematch_single.py','prematch_multi.py','generate_context.py','config/constants.py')},
          'actionable_bet':False,'preparation_network_calls':0}
    plan['approval_sha256']=digest(plan);write_once(args.plan,plan)
    return {'status':'PREPARED_NO_EXTERNAL_EXECUTION','plan':str(args.plan.resolve()),
            'approval_sha256':plan['approval_sha256'],'external_calls':0}


def describe(plan):
    import shutil
    return {'status':'DRY_RUN_NO_EXTERNAL_EXECUTION','date':plan['date'],'window':plan['window'],
            'approval_sha256':plan['approval_sha256'],'api_hard_cap':plan['api_budget'],
            'bwin_fixture_inspection_cap':plan['bwin_events'],'llm_request_cap':plan['llm_max_iterations'],
            'cache_root':plan['cache_root'],
            'runtime_status':'NOT_CONFIGURED' if not plan['runtime_command'] else
                'EXECUTABLE_AVAILABLE_NOT_EXECUTED' if shutil.which(plan['runtime_command'][0]) else 'EXECUTABLE_UNAVAILABLE',
            'default_mode':'OFFLINE','external_confirmation_required':True,
            'stages':['phase1','canonical_cache_bridge','phase2','advanced_discovery','predictive',
                      'evidence_ranking','llm_research_or_explicit_pending','market_mapping','explanations'],
            'inputs':plan['inputs'],'external_calls':0,'actionable_bet':False}


def execute(plan, *, external=False, confirmation=None, resume=False):
    """Reuse existing engines. Reserve budgets before children, including crashes."""
    if external and confirmation!=plan['approval_sha256']:
        raise ValueError('EXACT_PLAN_CONFIRMATION_REQUIRED')
    if external and plan['api_budget']<1 and not plan['bwin_events'] and not plan['runtime_command']:
        raise ValueError('NO_EXTERNAL_SERVICE_CONFIGURED')
    root=Path(plan['root']);out=Path(plan['output_dir'])
    for name,expected in plan['code'].items():
        if reference(root/name)['sha256']!=expected:raise ValueError('PLAN_CODE_CHANGED_REPREPARE')
    if out.exists() and not resume:raise ValueError('OUTPUT_EXISTS_USE_RESUME_OR_NEW_PLAN')
    out.mkdir(parents=True,exist_ok=True)
    from prematch_phase1 import atomic_json
    state_path=out/'controller_state.json'
    # A process lock covers the entire budget reservation and execution, not just writes.
    import fcntl
    lock=(out/'controller.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lock.close();raise ValueError('MANUAL_RUN_ALREADY_ACTIVE') from None
    state=json.loads(state_path.read_text()) if state_path.exists() else {
        'plan_sha256':plan['approval_sha256'],'api_reserved':0,'llm_reserved':0,'bwin_reserved':0,'completed':{}}
    if state['plan_sha256']!=plan['approval_sha256']:
        lock.close();raise ValueError('RESUME_PLAN_MISMATCH')
    def save(event):
        atomic_json(state_path,state)
        with (out/'controller_events.jsonl').open('a') as handle:
            handle.write(json.dumps({'at_utc':datetime.now(timezone.utc).isoformat(),'event':event,'state':state})+'\n')
    def child(command, log_name, partial_artifact=None):
        env=os.environ.copy()
        if external:env['PREMATCH_EXTERNAL_CONFIRMATION']=plan['approval_sha256']
        else:env.pop('PREMATCH_EXTERNAL_CONFIRMATION',None)
        if plan['browser_executable']:env['BWIN_BROWSER_EXECUTABLE']=plan['browser_executable']
        log=out/log_name
        attempt=1
        while log.exists():
            attempt+=1;log=out/(Path(log_name).stem+'_'+str(attempt)+Path(log_name).suffix)
        with log.open('x') as handle:
            result=subprocess.run([sys.executable,'-B',*map(str,command)],cwd=root,env=env,stdout=handle,stderr=subprocess.STDOUT)
        if result.returncode==2 and partial_artifact and partial_artifact.is_file():
            partial=json.loads(partial_artifact.read_text())
            if isinstance(partial.get('matches'),list):
                save('PARTIAL_MARKET_RESULT_'+log.name);return
        if result.returncode:raise ValueError('STAGE_FAILED: '+str(log))
    def stage(name, action, artifact):
        if name in state['completed']:
            if reference(state['completed'][name]['path'])!=state['completed'][name]:raise ValueError('RESUME_ARTIFACT_CHANGED:'+name)
            return
        if artifact.exists():raise ValueError('INTERRUPTED_STAGE_REQUIRES_NEW_RESULT_DIRECTORY:'+name)
        save('START_'+name);action();state['completed'][name]=reference(artifact);save('COMPLETE_'+name)
    try:
        save('RUN_EXTERNAL_CONFIRMED' if external else 'RUN_OFFLINE')
        acquisition=out/'phase1';canonical=acquisition/'fixtures_discovered.json'
        def acquire():
            online=external and plan['api_budget']>0
            budget=plan['api_budget']-state['api_reserved'] if online else 0
            if online and budget<=0:raise ValueError('API_BUDGET_RESERVED_OR_EXHAUSTED')
            if not online and not plan['inputs']['fixtures']:raise ValueError('OFFLINE_FIXTURE_INPUT_REQUIRED')
            if online:state['api_reserved']+=budget;save('RESERVE_API_BEFORE_START')
            command=[root/'prematch_phase1.py','--date',plan['date'],'--start',plan['start'],'--end',plan['end'],
                     '--cache-root',plan['cache_root'],
                     '--output-dir',acquisition,'--history',str(plan['history']),'--core-only','--data-all-competitions',
                     '--online' if online else '--offline','--max-api-calls',str(budget)]
            for key,flag in (('fixtures','--fixtures-input'),('bwin','--bwin-input')):
                if plan['inputs'][key]:command.extend([flag,plan['inputs'][key]['path']])
            child(command,'phase1.log')
            if online:
                summary=json.loads((acquisition/'phase1_summary.json').read_text())
                actual=summary['api_usage']['new_http_calls']
                if not 0<=actual<=budget:raise ValueError('API_ACCOUNTING_OUTSIDE_RESERVED_BUDGET')
                state['api_reserved']-=budget-actual;save('RECONCILE_API_COMPLETED')
        stage('acquisition',acquire,acquisition/'phase1_summary.json')
        bwin=Path(plan['inputs']['bwin']['path']) if plan['inputs']['bwin'] else None
        if external and plan['bwin_events']:
            bwin=out/'bwin/bwin_discovery.json'
            def discover_bwin():
                if state['bwin_reserved']:raise ValueError('BWIN_INSPECTION_BUDGET_ALREADY_RESERVED')
                state['bwin_reserved']=plan['bwin_events'];save('RESERVE_BWIN_BEFORE_START')
                command=[root/'bwin_discovery.py','--input',canonical,'--top',str(plan['bwin_events']),
                         '--output-dir',bwin.parent,'--headless']
                if plan['browser_executable']:command.extend(['--browser-executable',plan['browser_executable']])
                child(command,'bwin.log')
            stage('bwin',discover_bwin,bwin)
        pre=out/'preselection.json'
        def bridge():
            from prematch_tactical_history import phase1_preselection
            cutoff=datetime.now(timezone.utc)
            document=phase1_preselection(canonical,Path(plan['cache_root']),cutoff,history=plan['history'])
            if bwin:
                from prematch_phase1 import identity_map
                mapped=identity_map(json.loads(canonical.read_text()),json.loads(bwin.read_text()))
                verified={r['api_fixture_id']:r for r in mapped['verified']}
                for row in document['matches']:
                    bound=verified.get(row['fixture_id']);row['market_identity_ready']=bool(bound)
                    if bound:row.update(bwin_event_id=bound['bwin_event_id'],bwin_url=bound['bwin_url'])
                document['bwin_identity_source']=reference(bwin)
            write_once(pre,document)
        stage('bridge',bridge,pre)
        preflights=[Path(ref['path']) for ref in plan.get('preflights',[])]
        if external and plan['bwin_events'] and bwin:
            mapping=json.loads(bwin.read_text())
            verified_ids={r['fixture_id'] for r in json.loads(pre.read_text())['matches'] if r.get('market_identity_ready')}
            for row in mapping['matches']:
                if row.get('fixture_id') not in verified_ids:continue
                fid=row['fixture_id'];artifact=out/'markets'/str(fid)/'market_preflight.json'
                stage('markets_'+str(fid),lambda fid=fid,artifact=artifact:child([
                    root/'bwin_market_preflight.py','--discovery',bwin,'--preselection',pre,
                    '--fixture-id',str(fid),'--root',root,'--output-dir',artifact.parent,
                    '--run-scraper','--refresh-existing','--immutable-capture'], 'markets_'+str(fid)+'.log',partial_artifact=artifact),artifact)
                preflights.append(artifact)
        child_stages=[('phase2','prematch_phase2.py',['--preselection',pre,'--cache-root',plan['cache_root'],'--output-dir',out/'phase2'],out/'phase2/phase2_summary.json'),
                      ('advanced','prematch_advanced_discovery_v2.py',['--preselection',pre,'--cache-root',plan['cache_root'],'--output-dir',out/'advanced'],out/'advanced/manifest.json')]
        for name,script,flags,artifact in child_stages:
            stage(name,lambda script=script,flags=flags,name=name:child([root/script,*flags],name+'.log'),artifact)
        predictive=out/'predictive_pointer.json'
        def predict():
            from prematch_predictive_engine import run
            folder,*_=run(pre,out/'predictive');write_once(predictive,{'path':str(folder/'predictive_patterns.json')})
        stage('predictive',predict,predictive)
        base=out/'base_ranking.json'
        def rank():
            from prematch_predictive_evidence_ranking_v1 import build
            rows,_=build(Path(json.loads(predictive.read_text())['path']),out/'ranking',day=plan['date'],start=plan['start'],end=plan['end'],preselection=pre)
            for row in rows:row['fixture_id']=int(row['fixture_id'])
            expected={m['fixture_id'] for m in json.loads(pre.read_text())['matches']}
            if {r['fixture_id'] for r in rows}!=expected:raise ValueError('RANKING_DROPPED_FIXTURES')
            write_once(base,rows)
        stage('ranking',rank,base)
        market_artifact=out/'market_mapping/market_mapping.json'
        def market_mapping():
            from prematch_market_mapping_v1 import map_markets
            map_markets(Path(json.loads(predictive.read_text())['path']),preflights,output_dir=market_artifact.parent)
        stage('market_mapping',market_mapping,market_artifact)
        def llm():
            args=[root/'prematch_llm_research.py','--observations',out/'advanced/historical_observations.json',
                  '--base-ranking',base,'--cutoff',json.loads(pre.read_text())['generated_at_utc'],
                  '--output-dir',out/'llm','--max-iterations',str(plan['llm_max_iterations'])]
            import shutil
            if external and plan['runtime_command'] and shutil.which(plan['runtime_command'][0]):
                if state['llm_reserved']:raise ValueError('LLM_BUDGET_ALREADY_RESERVED')
                state['llm_reserved']=plan['llm_max_iterations'];save('RESERVE_LLM_BEFORE_START')
                args.extend(['--runtime-command-json',json.dumps(plan['runtime_command'])]);child(args,'llm.log')
            else:
                from prematch_llm_research import research_packet,write_pending_runtime
                data=json.loads((out/'advanced/historical_observations.json').read_text())
                packet,_=research_packet(data['observations'],data['targets'],datetime.fromisoformat(json.loads(pre.read_text())['generated_at_utc']),plan['llm_max_iterations'])
                write_pending_runtime(out/'llm',packet,json.loads(base.read_text()))
                write_once(out/'llm/runtime_dependency.json',{
                    'status':'RLM_PENDING','reason':'EXTERNAL_EXECUTION_NOT_AUTHORIZED' if not external else
                        'RUNTIME_NOT_CONFIGURED' if not plan['runtime_command'] else 'EXECUTABLE_UNAVAILABLE',
                    'runtime_executed':False,'max_iterations':plan['llm_max_iterations']})
        stage('llm',llm,out/'llm/ranking_enriched.json')
        def explanations():
            from prematch_market_mapping_v1 import explain_research
            result=explain_research(json.loads(pre.read_text()),json.loads(base.read_text()),
                json.loads(market_artifact.read_text()),json.loads((out/'llm/ranking_enriched.json').read_text())['status'])
            from prematch_market_mapping_v1 import integrate_advanced
            integrated=integrate_advanced(json.loads(base.read_text()),
                json.loads((out/'advanced/advanced_patterns.json').read_text()),
                json.loads(market_artifact.read_text()),result,
                json.loads(bwin.read_text()) if bwin else {'matches':[]})
            write_once(out/'integrated_research.json',integrated)
            write_once(out/'explanations.json',result)
            lines=['# Investigación PREMATCH','',
                   'Orden de investigación descriptivo. Valor financiero no demostrado. Sin picks ni stakes.','']
            for row in result['fixtures']:
                lines.extend([f"## {row['rank']}. {row['partido']} ({row['fixture_id']})",'',
                              row['prioridad']+'.',row['limitaciones']+'.',
                              f"Investigación LLM: {result['llm_status']}. Detalles por familia en explanations.json.",''])
            with (out/'explanations.md').open('x') as handle:handle.write('\n'.join(lines))
        stage('explanations',explanations,out/'explanations.json')
        return {'status':'OFFLINE_IMPLEMENTATION_VERIFIED' if not external else 'MANUAL_RUN_COMPLETED_WITH_EXPLICIT_APPROVAL',
                'output_dir':str(out),'api_reserved_or_consumed':state['api_reserved'],
                'llm_status':json.loads((out/'llm/ranking_enriched.json').read_text())['status'],
                'ranking_final':False,'actionable_bet':False}
    finally:
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--date',required=True);prep.add_argument('--start',default='15:00');prep.add_argument('--end',default='03:00')
    prep.add_argument('--plan',type=Path,required=True);prep.add_argument('--output-dir',type=Path,required=True)
    prep.add_argument('--root',type=Path,default=Path(__file__).resolve().parent);prep.add_argument('--history',type=int,default=20)
    prep.add_argument('--api-budget',type=int,default=0);prep.add_argument('--bwin-events',type=int,default=0)
    prep.add_argument('--llm-max-iterations',type=int,default=3);prep.add_argument('--llm-runtime-command-json')
    prep.add_argument('--fixtures-input',type=Path);prep.add_argument('--bwin-input',type=Path);prep.add_argument('--browser-executable')
    prep.add_argument('--preflight',type=Path,action='append',default=[])
    prep.add_argument('--cache-root',type=Path,help='Caché estadística local; se conserva, nunca se vacía')
    dry=sub.add_parser('dry-run');dry.add_argument('--plan',type=Path,required=True)
    run=sub.add_parser('run');run.add_argument('--plan',type=Path,required=True);run.add_argument('--external',action='store_true')
    run.add_argument('--confirm-external',help='Exact approval SHA256 printed by prepare/dry-run');run.add_argument('--resume',action='store_true')
    args=p.parse_args(argv)
    try:
        result=prepare(args) if args.action=='prepare' else describe(read_plan(args.plan)) if args.action=='dry-run' else execute(read_plan(args.plan),external=args.external,confirmation=args.confirm_external,resume=args.resume)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,KeyError,TypeError) as error:
        print('ERROR: '+str(error),file=sys.stderr);return 2
