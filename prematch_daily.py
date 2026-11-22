#!/usr/bin/env python3
"""Un comando diario hasta la investigación manual PREMATCH; nunca ejecuta finish."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import time as performance_clock
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import prematch_prospective_trial as trial
from prematch_runtime import reusable_preparation, scan, stamp, verified_preflight

# Existing discovery/preparation remains conservative (T-45).  Revalidation
# of an already prepared handoff is a lighter operation and may run until T-5;
# it still requires fresh quotes and all identity/integrity checks.
REVIEW_MIN_LEAD_MINUTES = 5
MANUAL_BWIN_MIN_LEAD_MINUTES = 5


def subprocess_component_seconds(base):
    """Nested subprocess diagnostics; never added again to daily totals."""
    groups = {
        'api_fixture_and_history': {'discover_fixtures.py', 'preselect_matches.py'},
        'statistical_processing': {'filter_major_leagues.py', 'prematch_pattern_discovery.py',
                                   'prematch_pattern_engine.py'},
        'bwin_discovery': {'bwin_discovery.py'},
        'bwin_preflight': {'bwin_market_preflight.py'},
        'commercial_scanner': {'prematch_opportunity_scanner.py'},
    }
    totals={key:0.0 for key in groups};totals['other']=0.0
    path=Path(base)/'logs/pipeline_stage_timings.jsonl'
    if not path.is_file():return totals
    for line in path.read_text(encoding='utf-8').splitlines():
        try:row=json.loads(line);elapsed=float(row.get('elapsed_seconds',0) or 0)
        except (ValueError,TypeError,json.JSONDecodeError):continue
        group=next((name for name,scripts in groups.items() if row.get('script') in scripts),'other')
        totals[group]+=elapsed
    return {key:round(value,4) for key,value in totals.items()}


def complete_daily_metrics(base, doc, daily_started, exclusive, ended=None):
    """Publish non-overlapping daily totals plus clearly nested diagnostics."""
    ended=performance_clock.perf_counter() if ended is None else ended
    daily_total=max(0.0,ended-daily_started)
    exclusive={key:round(max(0.0,float(value)),4) for key,value in exclusive.items()}
    accounted=sum(exclusive.values())
    exclusive['unattributed_orchestration']=round(max(0.0,daily_total-accounted),4)
    scan_internal=dict(doc.get('phase_metrics') or {})
    metrics={
        'schema_version':'2.0-daily-complete-timings',
        'scope':'ONE_PREMATCH_DAILY_COMMAND',
        'daily_total_elapsed_seconds':round(daily_total,4),
        'exclusive_phase_seconds':exclusive,
        'exclusive_phase_sum_seconds':round(sum(exclusive.values()),4),
        'scan_internal_phase_seconds':scan_internal,
        'subprocess_component_seconds_nested':subprocess_component_seconds(base),
        'accounting':'exclusive phases sum to daily total; scan/subprocess breakdowns are nested diagnostics and must not be added again',
    }
    path=Path(doc.get('phase_metrics_path') or Path(base)/'phase_metrics.json')
    trial.dump(path,metrics)
    doc['phase_metrics']=metrics;doc['phase_metrics_path']=str(path)
    if doc.get('run_handoff'):
        trial.dump(Path(doc['run_handoff']),doc)
    trial.dump(Path(base)/'handoff_prematch.json',doc)
    return metrics


def run_optional_predictor(root, folder, args):
    """Ejecuta el predictor offline solo con opt-in; no toca selección ni finish."""
    if not getattr(args, 'predictive_engine', False):
        return None
    import prematch_predictive_engine as predictor
    source = folder / 'prospective_trial/enriched/preselection.json'
    if not source.is_file():
        raise trial.TrialError(f'PREDICTIVE_PRESELECTION_MISSING:{source}')
    try:
        out, *_ = predictor.run(
            source, folder / 'prospective_trial/predictive',
            fixture_id=getattr(args, 'fixture_id', None))
    except predictor.PredictiveError as exc:
        raise trial.TrialError(
            f'PREDICTIVE_ENGINE_BLOCKED: {exc}'
        ) from exc
    return out


def run_optional_market_mapping(root, folder, args, predictive_out=None):
    """Mapea patrones a mercados sólo con snapshots/preflights locales."""
    if not getattr(args, 'market_mapping', False):
        return None
    import prematch_market_mapping_v1 as mapping
    patterns = getattr(args, 'market_mapping_patterns', None)
    if patterns is None and predictive_out:
        patterns = Path(predictive_out) / 'predictive_patterns.json'
    if patterns is None:
        # Reuse only a uniquely identified frozen predictor from this exact
        # preselection. Never silently fall back to the legacy detector.
        source = folder / 'prospective_trial/enriched/preselection.json'
        candidates = []
        for manifest in (folder / 'prospective_trial/predictive').rglob('run_manifest.json'):
            doc = trial.read(manifest)
            target = manifest.parent / 'predictive_patterns.json'
            expected = (doc.get('artifacts', {}).get('predictive_patterns.json') or {}).get('sha256')
            if source.is_file() and doc.get('source_preselection_sha256') == trial.sha(source):
                if not target.is_file() or not expected or trial.sha(target) != expected:
                    raise trial.TrialError(f'MARKET_MAPPING_PREDICTOR_SHA_MISMATCH:{target}')
                candidates.append(target)
        if len(candidates) != 1:
            raise trial.TrialError('MARKET_MAPPING_REQUIRE_EXPLICIT_FROZEN_PATTERNS')
        patterns = candidates[0]
    patterns = Path(patterns)
    if not patterns.is_file():
        raise trial.TrialError(f'MARKET_MAPPING_PATTERNS_MISSING:{patterns}')
    paths = getattr(args, 'market_mapping_preflight', None) or []
    if not paths:
        # Audit copies are not additional captures. Keep the latest original
        # per fixture; conflicting event assignments require explicit review.
        base = folder / 'prospective_trial'
        found = sorted(set((base / 'preflight').rglob('market_preflight.json')) |
                       set((base / 'daily_runs').glob('*/preflight/*/market_preflight.json')))
        by_fixture = {}
        for path in found:
            document = trial.read(path)
            for match in document.get('matches', []):
                fid = str(match.get('fixture_id'))
                eid = str(match.get('bwin_event_id'))
                previous = by_fixture.get(fid)
                if previous and previous[1] != eid:
                    raise trial.TrialError(f'MARKET_MAPPING_CONFLICTING_EVENTS:{fid}')
                timestamp = document.get('generated_at_utc') or ''
                if previous is None or timestamp > previous[2]:
                    by_fixture[fid] = (path, eid, timestamp)
        paths = sorted({entry[0] for entry in by_fixture.values()})
    # Content-addressed derived runs leave every earlier mapping untouched.
    import hashlib
    inputs = [(str(patterns.resolve()), trial.sha(patterns))]
    for path in paths:
        review = path.parent / 'market_selection_review.csv'
        inputs.append((str(path.resolve()), trial.sha(path), trial.sha(review) if review.is_file() else None))
    inputs.append(('mapping_code', mapping.code_sha()))
    ledger = folder / 'prospective_trial/candidate_ledger.json'
    minimum_odd = getattr(args,'min_market_odd',1.20)
    inputs.append(('minimum_odd',minimum_odd))
    if ledger.is_file(): inputs.append(('candidate_ledger',trial.sha(ledger)))
    run_id = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:20]
    output = folder / 'prospective_trial/market_mapping_v1/runs' / run_id
    result = mapping.map_markets(patterns, paths, output_dir=output,min_odd=minimum_odd,
                                 candidate_ledger=ledger if ledger.is_file() else None)
    return output, result['counts']


def review_minimum_lead(doc, default=REVIEW_MIN_LEAD_MINUTES):
    """Return the safe T-5 gate for an already prepared handoff.

    ``minimum_lead_minutes`` remains the historical preparation gate and is
    intentionally not consulted here.  Malformed/legacy values never relax
    the floor.
    """
    try:
        return max(REVIEW_MIN_LEAD_MINUTES, int(doc.get('review_minimum_lead_minutes', default)))
    except (TypeError, ValueError):
        return REVIEW_MIN_LEAD_MINUTES


def automatic_day(now, start, end):
    return trial.automatic_day(now,start,end)


def final_odds_check(doc, now):
    kickoff=trial.dt(doc.get('kickoff_madrid'));capture=trial.dt(doc.get('capture_madrid'))
    refresh_before=doc.get('final_odds_policy',{}).get('capture_within_kickoff_minutes',60)
    age_limit=doc.get('final_odds_policy',{}).get('max_age_minutes',15)
    if not kickoff or not capture or not 0 < kickoff.timestamp()-now.timestamp() <= refresh_before*60:
        raise trial.TrialError('FINAL_ODDS_REFRESH_REQUIRED: revisión final solo cerca del kickoff')
    if capture.timestamp() < kickoff.timestamp()-refresh_before*60 or not 0 <= now.timestamp()-capture.timestamp() <= age_limit*60:
        raise trial.TrialError('FINAL_ODDS_REFRESH_REQUIRED: recapturar y revalidar cuotas antes del análisis final')


def review(path, root=None, check_final_odds=False, review_min_lead=REVIEW_MIN_LEAD_MINUTES):
    doc = trial.read(path)
    now = trial.now_madrid()
    kickoff = trial.dt(doc.get('kickoff_madrid'))
    capture = trial.dt(doc.get('capture_madrid'))
    if doc.get('status') != 'READY_FOR_MANUAL_RESEARCH':
        raise trial.TrialError(doc.get('status', 'HANDOFF_INVALID'))
    if doc.get('selection_tier'):
        rank=next((r for r in doc.get('tactical_selection_ranking',[])
                   if str(r.get('fixture_id'))==str(doc.get('fixture_id'))),None)
        if (not rank or rank.get('selection_tier')!=doc['selection_tier']
                or not rank.get('research_eligible') or rank.get('hard_block_reasons')
                or rank.get('research_missing_evidence')!=doc.get('research_missing_evidence')
                or bool(rank.get('eligible'))!=bool(doc.get('eligible'))):
            raise trial.TrialError('HANDOFF_SELECTION_TIER_CONFLICT')
    # ``minimum_lead_minutes`` records the original preparation gate (T-45).
    # Reviewing/revalidating an already prepared handoff is a separate
    # operation and only needs the T-5 safety margin.  Old handoffs without the
    # new field therefore get the safe default, never a relaxed zero margin.
    configured_review_min = review_minimum_lead(doc, review_min_lead)
    if not kickoff or (kickoff-now).total_seconds() < configured_review_min*60:
        raise trial.TrialError(f'HANDOFF_EXPIRED: partido iniciado o sin margen PREMATCH para review (T-{configured_review_min} min)')
    if not capture or not timedelta(0) <= now-capture <= timedelta(minutes=doc.get('max_odds_age_minutes',120)):
        raise trial.TrialError('HANDOFF_EXPIRED: snapshot caducado')
    project = root or Path(__file__).resolve().parent
    from prematch_runtime import resolve_source
    try:
        doc['odds_txt'] = str(resolve_source(project, doc['odds_txt'], doc['odds_sha256']))
    except trial.TrialError as exc:
        raise trial.TrialError('HANDOFF_SNAPSHOT_SHA_OR_PATH_INVALID: ' + str(exc)) from exc
    try:
        doc['context_paths'][0] = str(resolve_source(project, doc['context_paths'][0], doc['context_sha256']))
    except trial.TrialError as exc:
        raise trial.TrialError('HANDOFF_CONTEXT_SHA_OR_PATH_INVALID: ' + str(exc)) from exc
    if trial.sha(Path(doc['odds_txt'])) != doc['odds_sha256']:
        raise trial.TrialError('HANDOFF_SNAPSHOT_SHA_MISMATCH')
    if trial.sha(Path(doc['context_paths'][0])) != doc['context_sha256']:
        raise trial.TrialError('HANDOFF_CONTEXT_SHA_MISMATCH')
    context = trial.read(Path(doc['context_paths'][0]))
    fixture = context['fixture']
    if str(fixture['fixture_id']) != str(doc['fixture_id']):
        raise trial.TrialError('HANDOFF_FIXTURE_IDENTITY_MISMATCH')
    doc['preflight'] = str(resolve_source(project, doc['preflight'], doc.get('preflight_sha256')))
    pf = trial.read(Path(doc['preflight']))
    verified = verified_preflight(project, Path(doc['preflight']), fixture,
                resolve_source(project, pf['source_preselection'], pf.get('source_preselection_sha256')), SimpleNamespace(min_lead=configured_review_min,
                                                               max_odds_age=doc['max_odds_age_minutes']))
    if any(str(verified[k]) != str(doc[k]) for k in ('bwin_event_id','bwin_url','home','away','kickoff_madrid')):
        raise trial.TrialError('HANDOFF_EVENT_IDENTITY_MISMATCH')
    if check_final_odds:
        final_odds_check(doc,now)
    doc['actionable_bet'] = False
    doc['ready_to_bet'] = False
    doc['requires_pre_kickoff_odds_refresh'] = True
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def bwin_ranking_links(canonical, catalog, when, source, min_lead=45):
    """Reutiliza identidad y preflight; no confunde catálogo con cuotas."""
    from prematch_phase1 import identity_map, api_fixture_verification
    from bwin_discovery import competition_similarity
    from bwin_market_preflight import validate_match
    mapped = identity_map(canonical, catalog)
    associations = {str(r['api_fixture_id']): r for group in ('verified','pending','not_found','excluded') for r in mapped.get(group, [])}
    rows = catalog.get('matches', [])
    by_fixture = {}
    event_fixtures = {}
    for row in rows:
        fid = str(row.get('fixture_id')); eid = str(row.get('bwin_event_id') or '')
        by_fixture.setdefault(fid, []).append(row)
        if eid: event_fixtures.setdefault(eid,set()).add(fid)
    links = {}
    for fixture in canonical.get('fixtures', []):
        fid = str(fixture['fixture_id']); candidates = by_fixture.get(fid, [])
        link = associations.get(fid, {}); row = candidates[0] if len(candidates)==1 else {}
        inspection = row.get('inspection') or {}; raw = link.get('identity_status')
        state = 'CANDIDATE_UNVERIFIED'; reason = link.get('reason','FIXTURE_NOT_EVALUATED_IN_CATALOG')
        evidence = None
        order_ok=category_ok=competition_ok=False
        if len(candidates)>1 or len(event_fixtures.get(str(row.get('bwin_event_id')),set()))>1:
            state='CONFLICT';reason='DUPLICATE_FIXTURE_OR_EVENT_ASSOCIATION'
        elif raw=='AMBIGUOUS': state='AMBIGUOUS'
        elif any(token in str(inspection.get('identity_status','')) for token in ('MISMATCH','CONFLICT')) or row.get('team_order_status') in ('AWAY_HOME_CONFLICT','AWAY_HOME_TOKEN_CONFLICT','AWAY_HOME_ALIAS_CONFLICT'):
            state='CONFLICT';reason='BWIN_API_IDENTITY_OR_TIME_CONFLICT'
        elif raw=='NOT_FOUND_IN_SCANNED_PAGES': state='NOT_FOUND'
        elif row:
            if any(row.get(k)!=fixture.get(k) for k in ('home_id','away_id','league_id','season')) or trial.dt(row.get('kickoff_madrid'))!=trial.dt(fixture.get('kickoff_madrid')):
                links[fid]={'status':'CONFLICT','event_id':row.get('bwin_event_id'),'reason':'CATALOG_API_FIXTURE_FIELDS_CONFLICT','can_continue_deep_analysis':False}
                continue
            evidence=validate_match(row,catalog.get('catalog_observed',[]),when,min_lead,120)
            api = dict(fixture);api.setdefault('category_status',api.get('filter_status'))
            api_ok = api_fixture_verification(api,now=when)[0]=='API_FIXTURE_VERIFIED'
            category_ok = (api.get('gender')=='M' and row.get('category_compatibility')=='COMPATIBLE_OR_UNKNOWN') or (api.get('gender')=='F' and row.get('category_compatibility')=='FEMALE_CORROBORATED')
            competition_ok = competition_similarity(api.get('league',''), inspection.get('page_title',''),
                league_id=api.get('league_id'),country=api.get('country'),season=api.get('season'))>=.82
            order_ok = row.get('team_order_status') in ('HOME_AWAY','HOME_AWAY_TOKEN','HOME_AWAY_ALIAS')
            timezone_ok = evidence.get('time_evidence',{}).get('status')=='ABSOLUTE_TIME_VERIFIED' or catalog.get('user_time_reference')=='Europe/Madrid'
            if api_ok and category_ok and competition_ok and order_ok and timezone_ok and evidence['status']=='READY_FOR_SCRAPER':
                state='VERIFIED';reason='API_BWIN_IDENTITY_AND_ORIGINAL_TIME_EVIDENCE_REVALIDATED'
            elif evidence['reasons']:
                reason=','.join(evidence['reasons'])
                if any(r in evidence['reasons'] for r in ('BWIN_API_KICKOFF_CONFLICT','BWIN_EVENT_LIVE')):state='CONFLICT'
            elif not competition_ok: reason='EVENT_PAGE_COMPETITION_NOT_CORROBORATED'
            elif not category_ok: reason='EVENT_CATEGORY_NOT_CORROBORATED'
            elif not order_ok: reason='EVENT_HOME_AWAY_ORDER_NOT_CORROBORATED'
            elif not timezone_ok: reason='BWIN_DISPLAY_TIMEZONE_NOT_VERIFIED'
        schedule_state=('SCHEDULE_VERIFIED' if state=='VERIFIED' else 'SCHEDULE_CONFLICT' if evidence and any(r in evidence.get('reasons',[]) for r in ('BWIN_API_KICKOFF_CONFLICT','BWIN_EVENT_LIVE')) else 'TIME_NOT_CONFIRMED')
        identity_state=('IDENTITY_AMBIGUOUS' if state=='AMBIGUOUS' else 'IDENTITY_VERIFIED' if row and inspection.get('title_teams_match') and order_ok and category_ok and competition_ok else 'IDENTITY_CANDIDATE')
        reason_code=('VERIFIED' if state=='VERIFIED' else 'NOT_OBSERVED_IN_CATALOG' if state=='NOT_FOUND' else 'IDENTITY_NOT_CONFIRMED' if identity_state!='IDENTITY_VERIFIED' else 'TIME_CONFLICT' if schedule_state=='SCHEDULE_CONFLICT' else 'TIME_NOT_CONFIRMED')
        links[fid]={'binding_reason_code':reason_code,'identity_state':identity_state,'schedule_state':schedule_state,'market_state':'MARKETS_NOT_CAPTURED','status':state,'event_id':row.get('bwin_event_id'),'url':row.get('bwin_url'),
            'reason':reason,'evidence':evidence,'inspection':inspection,
            'captured_at':catalog.get('generated_at_utc'),'source':str(Path(source).resolve()),
            'source_sha256':trial.sha(source),'commercial_information':'HEADINGS_ONLY_NOT_QUOTES' if row.get('markets') else 'NOT_CAPTURED',
            'can_continue_deep_analysis':state=='VERIFIED','actionable_bet':False}
    return links, mapped


def select_verified_bwin(args, root, out, ranked, prepath, bwin_path):
    """Conecta los CLIs validados, sin reconstruir sus contratos JSON."""
    import subprocess
    from bwin_market_preflight import declared_capture_time
    if args.asof: raise ValueError('RETROSPECTIVE_RUN_CANNOT_SELECT_OPERATIONALLY')
    prepare = getattr(args, 'prepare_selected', False)
    if prepare:
        from prematch_execution_control import require_external_confirmation
        require_external_confirmation(getattr(args, 'confirm_external', False))
    eligible = [r for r in ranked if r.get('bwin',{}).get('status')=='VERIFIED' and
                (not args.fixture_id or r['fixture_id']==args.fixture_id)]
    if not eligible: raise ValueError('NO_VERIFIED_BWIN_FIXTURE: conservar candidaturas; ver ranking.json')
    chosen = eligible[0];fid=chosen['fixture_id'];event=str(chosen['bwin']['event_id'])
    if prepare and (root/'outputs'/'matches'/event/'manifest.json').exists():
        raise ValueError('CANONICAL_MANIFEST_EXISTS: continuar con el expediente existente')
    folder=out/'fixtures'/str(fid)/'selection_pipeline';folder.mkdir(parents=True)
    def run(script, parameters, name):
        with (folder/name).open('w',encoding='utf-8') as log:
            result=subprocess.run([sys.executable,str(root/script),*map(str,parameters)],cwd=root,stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:raise ValueError(f'{script} BLOCKED: {folder/name}')
    preflight=args.preflight
    if preflight is None:
        target=root/'outputs/matches'/event/'inputs'/f'cuotas_bwin_{event}.txt'
        captured=declared_capture_time(target) if target.is_file() else None
        now=trial.now_madrid()
        refresh_existing = bool(getattr(args, 'refresh_existing_odds', False))
        has_manifest = (target.parent.parent / 'manifest.json').exists()
        if target.is_file() and not refresh_existing and (not captured or not 0 <= (now-captured).total_seconds() <= args.max_odds_age*60):
            raise ValueError('EXISTING_ODDS_STALE: actualizar mediante refresh-odds; no sobrescribir snapshot')
        if target.is_file() and refresh_existing:
            refresh_flags = ['--run-scraper', '--refresh-existing']
            if has_manifest:
                refresh_flags.append('--immutable-capture')
            needs_external = True
        elif captured:
            refresh_flags = ['--reuse-existing']
            needs_external = False
        else:
            refresh_flags = ['--run-scraper']
            needs_external = True
        cmd_pre = ['--discovery',bwin_path,'--preselection',prepath,'--fixture-id',fid,
            '--root',root,'--output-dir',folder/'preflight','--min-lead-minutes',args.min_lead,
            *refresh_flags]
        if needs_external and getattr(args, 'confirm_external', False):
            cmd_pre.append('--confirm-external')
        run('bwin_market_preflight.py', cmd_pre, 'preflight.log')
        preflight=folder/'preflight/market_preflight.json'
    fixture=next(r for r in trial.read(prepath)['matches'] if r['fixture_id']==fid)
    verified_preflight(root,preflight,fixture,prepath,args)
    run('select_single_match.py',['--preselection',prepath,'--discovery',bwin_path,'--preflight',preflight,
        '--output-dir',folder,'--root',root,'--min-comparable-matches','1',
        '--min-lead-minutes',args.min_lead,'--max-snapshot-age-minutes',args.max_odds_age],'selector.log')
    selected=folder/'selected_match.json'
    doc=trial.read(selected)
    if doc['fixture_id']!=fid or str(doc['bwin_event_id'])!=event:raise ValueError('SELECTOR_CHANGED_FIXTURE_OR_EVENT')
    single = ['--root', root, '--selected', selected, '--min-lead-minutes', args.min_lead,
              '--max-odds-age-minutes', min(args.max_odds_age, 120)]
    run('prematch_single.py', [*single, 'check', *(['--reuse-odds'] if prepare else [])], 'check.log')
    result = {'status':'SELECTED_AND_CHECKED_FOR_RESEARCH','fixture_id':fid,'bwin_event_id':event,
        'selected_match':str(selected),'selection_decision':str(folder/'selection_decision.json'),
        'preflight':str(preflight.resolve()),'check_log':str(folder/'check.log'),'actionable_bet':False,
        'comparison_scope':'ONE_VERIFIED_FIXTURE_EXPLICIT_SINGLE_EVENT_PILOT'}
    if prepare:
        run('prematch_single.py', [*single, '--confirm-external', 'prepare', '--reuse-odds'], 'prepare.log')
        manifest = root / 'outputs' / 'matches' / event / 'manifest.json'
        result.update(status='PREPARED_FOR_RESEARCH', manifest=str(manifest),
                      manifest_sha256=trial.sha(manifest), prepare_log=str(folder/'prepare.log'))
    return result


def operational_research(args, now=None):
    """Fase 3: ranking local y preparación aislada con los motores existentes."""
    import prematch_phase1 as foundation
    import prematch_phase2 as patterns
    import prematch_tactical_signal_engine as tactical
    root = args.root.resolve()
    when = patterns._dt(args.asof) if args.asof else (now or trial.now_madrid())
    if when is None: raise ValueError('ASOF_INVALID')
    if args.online and args.asof: raise ValueError('SIMULATED_CLOCK_CANNOT_ACQUIRE_OPERATIONAL_DATA')
    if args.max_api_calls < 0 or args.max_api_calls > 25: raise ValueError('API_BUDGET_REQUIRES_0_TO_25')
    config, _, _ = trial.load_window_config(root)
    _, start, end = trial.resolve_window(config, args.window, args.start, args.end)
    day = trial.parse_date_relative(args.date, today=when.astimezone(trial.TZ).date()) if args.date else automatic_day(when, start, end)
    begin, finish = trial.local_window(day, start, end)
    prepath = args.preselection
    if prepath is None:
        window_folder = trial.window_path(root, day, start, end)
        prepath = trial.latest_evaluation(window_folder) / 'prospective_trial/enriched/preselection.json'
    prepath = prepath.resolve()
    # Never search yesterday's directories as a fallback for a missing day.
    for parent in prepath.parents:
        label = re.fullmatch(r'(\d{4}-\d{2}-\d{2})_(\d{4})_(\d{4})', parent.name)
        if label:
            expected = f'{day.isoformat()}_{start:%H%M}_{end:%H%M}'
            if parent.name != expected:
                raise ValueError(f'PRESELECTION_WINDOW_MISMATCH: source={parent.name}, requested={expected}; specify its matching date/window explicitly')
            break
    pre = trial.read(prepath)
    warnings = []
    generated = patterns._dt(pre.get('generated_at_utc'))
    if generated is None:
        warnings.append('PRESELECTION_GENERATION_TIME_UNKNOWN')
    elif generated > when:
        warnings.append('PRESELECTION_GENERATED_AFTER_ASOF: retrospective reconstruction, not availability proven at cutoff')
    elif (when-generated).total_seconds() > 7200:
        age_min = round((when - generated).total_seconds() / 60.0, 1)
        warnings.append(
            f'PRESELECTION_OLDER_THAN_120_MINUTES (age={age_min} min): '
            'historical_baseline_reusable=True; current_evidence_requires_refresh (schedule/lineups/odds)'
        )
    if not any(patterns._dt(m.get('kickoff_madrid')) and begin <= patterns._dt(m['kickoff_madrid']) < finish for m in pre.get('matches', [])):
        warnings.append('NO_FIXTURES_IN_REQUESTED_WINDOW: no substitution with another matchday')
    for warning in warnings:
        print('AVISO: '+warning, file=sys.stderr)
    canonical_path = args.fixtures_input or prepath.parent.parent / 'fixtures_major_test.json'
    canonical = trial.read(canonical_path)
    if pre.get('source_filter_sha256') and trial.sha(canonical_path) != pre['source_filter_sha256']:
        raise ValueError('CANONICAL_IDENTITY_SHA_MISMATCH')
    if getattr(args,'reconcile_schedule',False):
        if args.asof or not args.online or args.max_api_calls<1:
            raise ValueError('SCHEDULE_RECONCILIATION_REQUIRES_LIVE_ONLINE_AND_BUDGET')
        from config.constants import API_KEY
        import copy
        recdir=((args.output_dir or root/'outputs/phase3')/'schedule_reconciliation'/stamp()).resolve()
        recdir.mkdir(parents=True,exist_ok=False)
        client=foundation.Phase1ApiClient(recdir/'api',offline=False,max_new_calls=args.max_api_calls,key=API_KEY)
        raw=[];errors=[];ids=[f['fixture_id'] for f in canonical['fixtures']]
        for offset in range(0,len(ids),20):
            try:raw.extend(client.get('fixtures',ids='-'.join(map(str,ids[offset:offset+20]))).response)
            except (RuntimeError,ValueError,OSError) as exc:errors.append(str(exc))
        trial.dump(recdir/'current_fixtures.json',raw)
        canonical,audit=foundation.reconcile_fixture_schedules(canonical,raw,begin,finish,trial.now_madrid())
        trial.dump(recdir/'schedule_audit.json',{'rows':audit,'api':client.statistics(),'errors':errors,'original_canonical':str(canonical_path),'original_sha256':trial.sha(canonical_path)})
        canonical_path=recdir/'fixtures_reconciled.json';trial.dump(canonical_path,canonical)
        original_prepath=prepath;pre=copy.deepcopy(pre);updated={f['fixture_id']:f for f in canonical['fixtures']}
        for f in pre['matches']:
            current=updated.get(f['fixture_id'],{})
            for key in ('kickoff_utc','kickoff_madrid','status','schedule_reconciliation'):
                if key in current:f[key]=current[key]
        pre.update(parent_source=str(original_prepath),parent_sha256=trial.sha(original_prepath),source_filter_sha256=trial.sha(canonical_path))
        prepath=recdir/'preselection_reconciled.json';trial.dump(prepath,pre)
    byid = {}
    for row in canonical.get('fixtures', []):
        if row['fixture_id'] in byid: raise ValueError('CANONICAL_FIXTURE_DUPLICATED')
        byid[row['fixture_id']] = row
    ledger = []; ranked = []; matches = {}
    for fixture in pre.get('matches', []):
        fid = fixture['fixture_id']
        if fid in matches: raise ValueError('PRESELECTION_FIXTURE_DUPLICATED')
        matches[fid] = fixture
        identity = dict(byid.get(fid) or {})
        identity.setdefault('category_status', identity.get('filter_status'))
        status, reason = foundation.api_fixture_verification(identity, now=when)
        ko = patterns._dt(fixture.get('kickoff_madrid'))
        cutoff = patterns._dt(fixture.get('analysis_cutoff_utc'))
        reasons = []
        reconciliation=identity.get('schedule_reconciliation',{})
        if reconciliation and reconciliation['status']!='SCHEDULE_VERIFIED':reasons.append(reconciliation['reason'])
        if reconciliation.get('reason')=='RESCHEDULED_OUT_OF_WINDOW':reasons.append('RESCHEDULED_OUT_OF_WINDOW')
        if status != 'API_FIXTURE_VERIFIED': reasons.append(reason)
        for key in ('home_id', 'away_id', 'league_id', 'season', 'gender'):
            if fixture.get(key) != identity.get(key): reasons.append('IDENTITY_CONFLICT_' + key.upper())
        if ko != patterns._dt(identity.get('kickoff_utc') or identity.get('kickoff_madrid')): reasons.append('KICKOFF_CONFLICT')
        if not cutoff or cutoff > when or ko and cutoff >= ko: reasons.append('INVALID_ANALYSIS_CUTOFF')
        if not ko or (ko-when).total_seconds() < args.min_lead*60: reasons.append('PREPARATION_TEMPORAL_GATE')
        manual = args.fixture_id == fid
        if ko and not begin <= ko < finish and not manual: reasons.append('OUTSIDE_OPERATIONAL_WINDOW')
        if reasons:
            ledger.append({'fixture_id': fid, 'status': 'BLOCKED', 'reasons': sorted(set(reasons))}); continue
        evaluated = tactical.analyze_fixture(fixture, when, fixture.get('statistical_observations') or {})
        signals = evaluated['signals']
        components = {'tactical_quality': evaluated['tactical_research_score'],
                      'contradiction_penalty': min(20, 5*len(evaluated['contradictions'])),
                      'small_sample_penalty': 10 if not evaluated['signal_count'] else 0}
        player_evidence = {'status': 'NOT_AVAILABLE', 'bonus': 0}
        player_path = root/'outputs/phase2'/str(fid)/'longitudinal'/f'player_patterns_{fid}.json'
        if player_path.is_file():
            pd = trial.read(player_path)
            refs = {r['path']: r['sha256'] for obs in pd.get('observations', []) for r in obs.get('provenance', [])}
            if (pd.get('fixture_id') == fid and patterns._dt(pd.get('cutoff_utc')) == cutoff and refs and
                    all(Path(path).is_file() and trial.sha(Path(path)) == digest for path,digest in refs.items()) and
                    pd.get('integrity_validation', {}).get('status') == 'PASS'):
                supported_players = sum(any(v.get('per90_usable') for v in pl['metrics'].values()) and pl.get('performance_appearances',0)>=3 for pl in pd.get('patterns', []))
                player_evidence = {'status': 'VERIFIED_CACHED_EVIDENCE', 'players_with_exposure': supported_players,
                                   'source_sha256': trial.sha(player_path), 'bonus': min(3, supported_players*.25)}
        components['player_evidence_bonus'] = player_evidence['bonus']
        components['declared_coverage_bonus'] = 0.0
        rests = {}
        for side in ('home','away'):
            rows = patterns.discovery.clean_observations(fixture, side, when)
            rests[side] = round((ko-patterns._dt(rows[0]['kickoff_utc'])).total_seconds()/3600, 2) if rows else None
        components['short_rest_penalty'] = 3 if any(v is not None and v < 72 for v in rests.values()) else 0
        score = round(components['tactical_quality'] + components['player_evidence_bonus'] + components['declared_coverage_bonus'] - sum(v for k,v in components.items() if k.endswith('penalty')), 4)
        entry = {'fixture_id': fid, 'home': fixture['home'], 'away': fixture['away'], 'kickoff_madrid': ko.astimezone(trial.TZ).isoformat(),
                 'research_priority': score, 'components': components, 'coverage': evaluated['coverage_summary'],
                 'supported_signals': evaluated['signal_count'], 'contradictions': evaluated['contradictions'],
                 'rest_hours': rests, 'status': 'RANKED', 'actionable_bet': False}
        entry['player_evidence'] = player_evidence
        entry['signal_evidence'] = [{k:s.get(k) for k in ('signal_id','metric','team_side','status','source_mode','own','opponent_conceded','contradictions')} for s in signals if s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL']
        ranked.append(entry); ledger.append(entry)
    ranked.sort(key=lambda r: (-r['research_priority'], r['kickoff_madrid'], r['fixture_id']))
    out = ((args.output_dir or root/'outputs/phase3'/f'{day.isoformat()}_{start:%H%M}_{end:%H%M}') / 'runs' / stamp()).resolve()
    out.mkdir(parents=True, exist_ok=False)
    bwin_path = getattr(args, 'bwin_catalog', None)
    if getattr(args, 'sync_bwin', False):
        import subprocess
        destination = out/'bwin_catalog'
        archived=sorted((root/'outputs/discovery'/f'{day.isoformat()}_{start:%H%M}_{end:%H%M}'/'prospective_trial/bwin').rglob('bwin_discovery.json'))
        catalog_args=[part for path in archived for part in ('--catalog-snapshot',str(path.resolve()))]
        cmd_disc = [sys.executable, str(root/'bwin_discovery.py'), '--input', str(canonical_path),
            '--top', '0', '--output-dir', str(destination), '--headless', '--max-country-pages', '8',*catalog_args]
        if getattr(args, 'confirm_external', False):
            cmd_disc.append('--confirm-external')
        result = subprocess.run(cmd_disc, cwd=root)
        if result.returncode != 0: raise ValueError('BWIN_DISCOVERY_FAILED: '+str(destination))
        if not args.asof: when = trial.now_madrid()
        bwin_path = destination/'bwin_discovery.json'
    if bwin_path:
        catalog = trial.read(bwin_path)
        associations, identity_doc = bwin_ranking_links(canonical,catalog,when,bwin_path,args.min_lead)
        trial.dump(out/'bwin_identity_map.json', identity_doc)
        inventory=[]
        for event in catalog.get('catalog_observed',[]):
            linked=[r for r in catalog.get('matches',[]) if str(r.get('bwin_event_id'))==str(event.get('bwin_event_id'))]
            inventory.append({**event,'browser_time_reference':catalog.get('user_time_reference'),
                'api_fixture_ids':[r['fixture_id'] for r in linked],
                'event_page_titles':[(r.get('inspection') or {}).get('page_title') for r in linked],
                'binding_states':[associations.get(str(r['fixture_id']),{}).get('status') for r in linked]})
        trial.dump(out/'bwin_event_inventory.json',{'catalog':str(Path(bwin_path).resolve()),'catalog_sha256':trial.sha(bwin_path),'events':inventory})
        for entry in ledger:
            entry['bwin'] = associations.get(str(entry['fixture_id']), {'status':'CANDIDATE_UNVERIFIED','reason':'FIXTURE_NOT_EVALUATED'})
    else:
        for entry in ledger:
            entry['bwin'] = {'status': 'CANDIDATE_UNVERIFIED', 'event_id': None,
                             'reason': 'CATALOG_NOT_PROVIDED_OR_SYNCED'}
    for entry in ledger:
        if 'RESCHEDULED_OUT_OF_WINDOW' in entry.get('reasons',[]):
            entry['bwin']={**entry['bwin'],'status':'RESCHEDULED_OUT_OF_WINDOW','reason':'RESCHEDULED_OUT_OF_WINDOW','binding_reason_code':'RESCHEDULED_OUT_OF_WINDOW','market_state':'NOT_APPLICABLE_OUT_OF_WINDOW','can_continue_deep_analysis':False}
    ranking = {'asof_madrid': when.astimezone(trial.TZ).isoformat(), 'source_preselection_sha256': trial.sha(prepath),
               'source_preselection': str(prepath), 'warnings': warnings,
               'window': {'start': begin.isoformat(), 'end': finish.isoformat()}, 'shortlist': ranked[:5],
               'ranking': ranked, 'ledger': ledger, 'api_calls': 0, 'actionable_bet': False}
    from collections import Counter
    ranking['counts']={'api_fixtures_received':len(matches),'ranked':len(ranked),'blocked':len(ledger)-len(ranked),
        'bwin_events_discovered':len({str(r.get('bwin_event_id')) for r in catalog.get('catalog_observed',[]) if r.get('bwin_event_id')}) if bwin_path else 0,
        'by_bwin_status':dict(Counter(r['bwin']['status'] for r in ledger))}
    if bwin_path:
        ranking['counts']['bwin_events_without_api_candidate']=sum(not e['api_fixture_ids'] for e in inventory)
        ranking['counts']['bwin_fixtures_searched']=sum(not str(r.get('status','')).startswith('SKIPPED') for r in catalog.get('matches',[]))
    for position, entry in enumerate(ranked,1):entry['position']=position
    trial.dump(out/'ranking.json', ranking)
    lines=['# Ranking deportivo y vinculación Bwin','',
        '| Posición | Puntuación | Fixture | Partido | Evento Bwin | Estado | Motivo | Información comercial | Continuar profundo |',
        '|---:|---:|---:|---|---|---|---|---|---|']
    for row in ranked:
        link=row['bwin']
        lines.append(f"| {row['position']} | {row['research_priority']} | {row['fixture_id']} | {row['home']}–{row['away']} | {link.get('event_id') or '—'} | {link['status']} | {link.get('reason','')} | {link.get('commercial_information','NOT_CAPTURED')} | {link.get('can_continue_deep_analysis',False)} |")
    (out/'ranking_bwin.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if getattr(args,'select_verified',False):
        result=select_verified_bwin(args,root,out,ranked,prepath,bwin_path)
        trial.dump(out/'selection_result.json',result)
        return result
    if args.rank:
        visible=[{k:r[k] for k in ('position','research_priority','fixture_id','home','away','kickoff_madrid','bwin')} for r in ranked[:5]]
        return {'status': 'RANKING_READY', 'path': str(out), 'counts':ranking['counts'], 'shortlist': visible, 'warnings': warnings, 'source_preselection': str(prepath)}
    selected = next((r for r in ranked if r['fixture_id'] == args.fixture_id), None) if args.fixture_id else next(iter(ranked), None)
    if selected is None: raise ValueError('NO_ELIGIBLE_FIXTURE: consultar ranking.json en '+str(out))
    fid = selected['fixture_id']; fixture = dict(matches[fid]); fixture['market_identity_ready'] = False
    event_out = out/'fixtures'/str(fid); event_out.mkdir(parents=True)
    isolated = {**pre, 'matches': [fixture]}
    isolated.pop('source_filter_sha256', None)
    trial.dump(event_out/'preselection.json', isolated)
    if args.online:
        from config.constants import FOOTBALL_BASE_URL
        client = foundation.Phase1ApiClient(root/'outputs/.api_football_history_cache', offline=False,
                                           max_new_calls=args.max_api_calls, base_url=FOOTBALL_BASE_URL)
        acquisition = foundation.collect_longitudinal(client, event_out/'preselection.json', fid, when, event_out, 5)
    else: acquisition = {'api_calls': 0, 'mode': 'CACHE_ONLY'}
    patterns.run(event_out/'preselection.json', event_out, [fid], when, cache_root=root/'outputs/.api_football_history_cache')
    tactical_doc = tactical.analyze_fixture(fixture, when, fixture.get('statistical_observations') or {})
    trial.dump(event_out/'tactical_signals.json', tactical_doc)
    player_doc = trial.read(event_out/f'player_patterns_{fid}.json')
    market = {'verified': False, 'reason': 'BWIN_ORIGINAL_PREFLIGHT_NOT_PROVIDED', 'event_id': None}
    if args.preflight:
        if args.asof: market['reason'] = 'RETROSPECTIVE_RUN_NOT_OPERATIONAL'
        elif not patterns._dt(canonical.get('generated_at_utc')) or not 0 <= (when-patterns._dt(canonical.get('generated_at_utc'))).total_seconds() <= 7200:
            market['reason'] = 'API_IDENTITY_SNAPSHOT_STALE_OR_UNDATED_REQUIRES_REFRESH'
        else:
            try:
                verified = verified_preflight(root, args.preflight, fixture, prepath, args)
                market.update(verified=True, reason='ORIGINAL_PREFLIGHT_REVALIDATED', event_id=verified.get('bwin_event_id'))
            except (ValueError, OSError, KeyError, trial.TrialError) as exc: market['reason'] = str(exc)
    package = {'status': 'READY_FOR_QUALITATIVE_RESEARCH', 'fixture': fixture, 'selection': selected,
        'warnings': warnings,
        'context_age_minutes': round((when-patterns._dt(fixture['analysis_cutoff_utc'])).total_seconds()/60, 2),
        'asof_madrid': when.astimezone(trial.TZ).isoformat(), 'mode': 'RETROSPECTIVE_REPLAY' if args.asof else 'PREMATCH_RESEARCH',
        'sources': {'preselection': str(prepath), 'sha256': trial.sha(prepath), 'canonical': str(canonical_path.resolve()), 'canonical_sha256': trial.sha(canonical_path)},
        'player_summary': player_doc['summary'], 'acquisition': acquisition, 'bwin_identity': market,
        'market_identity_ready': market['verified'], 'actionable_bet': False,
        'missing_information': ['XI y bajas actuales', 'Noticias y entrenadores fechados', 'Contexto táctico verificable',
                                'Cuotas frescas y validación final obligatoria'],
        'artifacts': [p.name for p in sorted(event_out.glob('*.json'))]}
    trial.dump(event_out/'research_package.json', package)
    (event_out/'research_brief.md').write_text('# Investigación PREMATCH\n\n'+
        f"Fixture {fid}: {fixture['home']}–{fixture['away']}. Corte {fixture['analysis_cutoff_utc']}.\n\n"+
        'Consultar research_package.json y patrones colectivos/individuales adjuntos. Verificar fuentes fechadas, evidencia favorable y contraria; separar hechos de inferencias.\n\n'+
        'Investigar XI, bajas, entrenadores, cambios recientes y mecanismos compatibles con producción/concesión. No inferir tácticas de la formación nominal.\n\n'+
        'Vinculación Bwin: '+market['reason']+'. Evaluación comercial pendiente.\n', encoding='utf-8')
    return {'status': package['status'], 'fixture_id': fid, 'path': str(event_out), 'bwin_identity': market}


def daily_tactical_research(args):
    """One offline research command for the configured window, independent of Bwin."""
    from prematch_tactical_signal_engine import research_profiles
    when=trial.dt(args.asof) if args.asof else trial.now_madrid()
    if not when:raise ValueError('ASOF_INVALID')
    if args.online or args.sync_bwin or args.select_verified:raise ValueError('TACTICAL_RESEARCH_OFFLINE_ONLY')
    root=args.root.resolve();config,_,_=trial.load_window_config(root)
    _,start,end=trial.resolve_window(config,args.window,args.start,args.end)
    day=trial.parse_date_relative(args.date,today=when.astimezone(trial.TZ).date()) if args.date else automatic_day(when,start,end)
    begin,finish=trial.local_window(day,start,end)
    window_folder=trial.window_path(root,day,start,end)
    source=(args.preselection or trial.latest_evaluation(window_folder)/'prospective_trial/enriched/preselection.json').resolve()
    if not source.is_file():
        from prematch_tactical_history import prepare_research_from_cache
        prepare_research_from_cache(root,source,when)
    pre=trial.read(source)
    selected=[f for f in pre.get('matches',[]) if trial.dt(f.get('kickoff_madrid')) and begin<=trial.dt(f['kickoff_madrid'])<finish and (not args.fixture_id or args.fixture_id==f['fixture_id'])]
    if not selected:raise ValueError('RESEARCH_NO_FIXTURES_IN_REQUESTED_WINDOW')
    # Preserve the source: a derived immutable window view is recorded separately.
    from prematch_predictive_evidence_ranking_v1 import write_once
    import hashlib
    base=args.output_dir or root/'outputs/tactical_research'/f'{day.isoformat()}_{start:%H%M}_{end:%H%M}'
    base.mkdir(parents=True,exist_ok=True)
    view={**pre,'matches':selected,'parent_source':str(source),'parent_sha256':trial.sha(source)}
    raw=json.dumps(view,ensure_ascii=False,sort_keys=True,indent=2).encode();viewpath=base/('window_'+hashlib.sha256(raw).hexdigest()[:20]+'.json');write_once(viewpath,raw)
    out,doc=research_profiles(viewpath,base,when.isoformat(),args.interpretation,args.research_context,bool(args.asof),args.pattern_library or root/'outputs/tactical_pattern_library_v11')
    return {'status':doc['mode'],'window_start':begin.isoformat(),'window_end':finish.isoformat(),
            'fixtures_evaluated':len(doc['matches']),'excluded':doc['ledger'],'api_calls':0,'output':str(out),
            'ranking':doc['ranking'],'next_action':'Revisar perfiles y completar interpretación Codex con referencias, contraescenarios y falsadores; repetir con --interpretation. Bwin no es requisito de investigación.',
            'actionable_bet':False}


def discovery_status(root, day, window=None):
    """Read-only state of one daily window. Never calls API, Bwin or multi manifests."""
    root = Path(root).expanduser().resolve()
    config, _, _ = trial.load_window_config(root)
    profile, start, end = trial.resolve_window(config, window, None, None)
    day = trial.parse_date_relative(day) if not hasattr(day, 'isoformat') or isinstance(day, str) else day
    window_folder = trial.window_path(root, day, start, end)
    folder = trial.latest_evaluation(window_folder)
    phase1 = root / 'outputs' / 'phase1' / folder.name
    tracked = {
        'handoff': folder / 'prospective_trial' / 'handoff_prematch.json',
        'preselection': folder / 'prospective_trial' / 'enriched' / 'preselection.json',
        'phase1_summary': phase1 / 'phase1_summary.json',
        'phase1_progress': phase1 / 'phase1_progress.json',
    }
    report = {
        'schema_version': '1.0-prematch-daily-status',
        'flow': 'prematch_daily',
        'profile': profile,
        'date': day.isoformat(),
        'folder': str(folder),
        'window_folder': str(window_folder),
        'reads_only': True,
        'network': False,
        'actionable_bet': False,
        'multi_manifest_note': (
            'outputs/matches/<event_id>/manifest.json pertenece a prematch_multi prepare. '
            'Este flujo no lo crea.'
        ),
        'artifacts': {name: {'path': str(path), 'present': path.is_file()} for name, path in tracked.items()},
    }
    handoff = tracked['handoff']
    if not handoff.is_file():
        report['status'] = 'NO_DAILY_HANDOFF'
        return report
    try:
        doc = trial.read(handoff)
    except (OSError, ValueError, trial.TrialError) as exc:
        report['status'] = 'HANDOFF_UNREADABLE'
        report['error'] = str(exc)
        return report
    report['status'] = doc.get('status') or 'HANDOFF_PRESENT'
    report['actionable_bet'] = bool(doc.get('actionable_bet'))
    for key in ('fixture_id', 'kickoff_madrid', 'generated_madrid', 'minimum_lead_minutes',
                'review_minimum_lead_minutes', 'requires_pre_kickoff_odds_refresh',
                'final_odds_policy', 'ready_to_bet'):
        if key in doc:
            report[key] = doc[key]
    report['bwin_event_id'] = doc.get('bwin_event_id') or doc.get('event_id')
    return report


def _latest_daily_run(base: Path) -> Path | None:
    runs = sorted(p for p in (base / 'daily_runs').glob('*') if p.is_dir()) if (base / 'daily_runs').is_dir() else []
    return runs[-1] if runs else None


def diagnose_fixture(root, day, fixture_id: int, window=None, eval_dir: Path | None = None) -> dict:
    """Strictly read-only diagnostic per fixture_id on frozen artifacts. Zero API/Bwin."""
    root = Path(root).expanduser().resolve()
    config, _, _ = trial.load_window_config(root)
    profile, start, end = trial.resolve_window(config, window, None, None)
    parsed_day = trial.parse_date_relative(day) if day and (not hasattr(day, 'isoformat') or isinstance(day, str)) else day
    if eval_dir is not None:
        folder = Path(eval_dir).expanduser().resolve()
        window_folder = folder.parent.parent if folder.parent.name == 'evaluations' else folder.parent
    else:
        if parsed_day is None:
            raise ValueError('Se requiere --date YYYY-MM-DD o --eval-dir')
        window_folder = trial.window_path(root, parsed_day, start, end)
        folder = trial.latest_evaluation(window_folder)
    base = folder / 'prospective_trial'
    if not base.is_dir():
        raise FileNotFoundError(f'No existe evaluación congelada en {folder}')

    fid_int = int(fixture_id)
    fid_str = str(fid_int)
    evidence_files: dict[str, str] = {}

    def _read_optional(name: str, path: Path):
        if path.is_file():
            evidence_files[name] = str(path)
            return trial.read(path)
        return None

    major_doc = _read_optional('fixtures_major', folder / 'fixtures_major.json') or {}
    pre_doc = _read_optional('preselection', base / 'enriched/preselection.json') or {}
    tac_doc = _read_optional('tactical_signal_engine', base / 'tactical/tactical_signal_engine.json') or {}
    disc_doc = _read_optional('bwin_discovery', base / 'bwin/shared/bwin_discovery.json') or {}
    audit_doc = _read_optional('identity_audit', base / 'bwin/identity_audit/identity_audit.json') or {}
    handoff_doc = _read_optional('handoff_prematch', base / 'handoff_prematch.json') or {}
    run_dir = _latest_daily_run(base)
    ledger_doc = _read_optional('candidate_ledger', run_dir / 'candidate_ledger.json') if run_dir else None
    if ledger_doc is None:
        ledger_doc = _read_optional('candidate_ledger', base / 'candidate_ledger.json') or {}
    scanner_doc = _read_optional('opportunity_scan', run_dir / 'opportunity/opportunity_scan.json') if run_dir else {}

    pre_row = next((m for m in pre_doc.get('matches', []) if str(m.get('fixture_id')) == fid_str), None)
    major_rows = major_doc.get('fixtures', []) + major_doc.get('excluded', []) + major_doc.get('review', [])
    major_row = next((m for m in major_rows if str(m.get('fixture_id')) == fid_str), None)
    match_meta = pre_row or major_row
    if match_meta is None:
        raise LookupError(f'Fixture {fid_str} no encontrado en {folder}')

    stat_row = next((r for r in tac_doc.get('statistical_ranking', []) if str(r.get('fixture_id')) == fid_str), None)
    tactical_row = next((r for r in tac_doc.get('ranking', []) if str(r.get('fixture_id')) == fid_str), None)
    tac_fixture = (tac_doc.get('fixtures') or {}).get(fid_str) or {}
    disc_row = next((m for m in disc_doc.get('matches', []) if str(m.get('fixture_id')) == fid_str), None)
    audit_row = next((f for f in audit_doc.get('fixtures', []) if str(f.get('fixture_id')) == fid_str), None)
    ledger_row = next((r for r in ledger_doc.get('fixtures', []) if str(r.get('fixture_id')) == fid_str), None)
    scan_row = next((a for a in (scanner_doc or {}).get('audits', []) if str(a.get('fixture_id')) == fid_str), None)

    preflight_paths = []
    if run_dir and (run_dir / 'preflight').is_dir():
        preflight_paths.extend(sorted((run_dir / 'preflight').glob(f'{fid_str}_*/market_preflight.json')))
    if (base / 'preflight').is_dir():
        preflight_paths.extend(sorted((base / 'preflight').glob(f'{fid_str}_*/market_preflight.json')))
    preflight_match = None
    if preflight_paths:
        evidence_files['market_preflight'] = str(preflight_paths[-1])
        pf_doc = trial.read(preflight_paths[-1])
        preflight_match = next((m for m in pf_doc.get('matches', []) if str(m.get('fixture_id')) == fid_str), None)

    bwin_eid = (
        (preflight_match or {}).get('bwin_event_id')
        or (disc_row or {}).get('bwin_event_id')
        or (audit_row or {}).get('bwin_event_id')
    )
    manifest_path = root / 'outputs/matches' / str(bwin_eid) / 'manifest.json' if bwin_eid else None
    manifest_present = bool(manifest_path and manifest_path.is_file())
    if manifest_present:
        evidence_files['canonical_manifest'] = str(manifest_path)

    # Preview offline local catalog rematch with current rules (purely read-only, 0 network).
    local_rematch = []
    catalog_observed = disc_doc.get('catalog_observed', [])
    if catalog_observed and pre_row:
        from bwin_discovery import match_catalog
        local_rematch = [
            {
                'bwin_event_id': c.get('bwin_event_id'),
                'bwin_url': c.get('bwin_url'),
                'name_score': c.get('name_score'),
                'home_similarity': c.get('home_similarity'),
                'away_similarity': c.get('away_similarity'),
                'team_order_status': c.get('team_order_status'),
                'category_compatibility': c.get('category_compatibility'),
                'competition_similarity': c.get('competition_similarity'),
            }
            for c in match_catalog(pre_row, catalog_observed)[:3]
        ]

    # Exact blocking phase & root cause diagnosis
    scraper_info = (preflight_match or {}).get('scraper') or {}
    scraper_err = str(scraper_info.get('error') or '')
    ledger_state = (ledger_row or {}).get('state')
    ledger_reason = str((ledger_row or {}).get('reason') or '')
    if major_row and any(m.get('fixture_id') == fid_int for m in major_doc.get('excluded', [])):
        blocking_phase = 'league_filter'
        root_cause = str(major_row.get('filter_reason') or 'EXCLUDED_BY_LEAGUE_POLICY')
        recommended_action = 'No elegible por política actual de competiciones; no forzar manualmente.'
    elif pre_row is None or pre_row.get('data_readiness_status') in ('EVALUACION_INCOMPLETA', 'PENDIENTE_DETALLE'):
        blocking_phase = 'api_coverage'
        root_cause = f"Cobertura insuficiente/incompleta en preselection: {pre_row.get('data_readiness_status') if pre_row else 'AUSENTE_EN_PRESELECTION'}"
        recommended_action = 'Revisar cobertura histórica API antes de usar para análisis táctico.'
    elif (preflight_match or {}).get('status') == 'SCRAPER_FAILED_OR_UNVERIFIED' or 'refresh-odds' in scraper_err or 'SCRAPER_CAPTURE_FAILED' in ledger_reason:
        blocking_phase = 'capture_or_preflight'
        root_cause = scraper_err or ledger_reason or 'SCRAPER_FAILED_OR_UNVERIFIED'
        if 'refresh-odds' in scraper_err or manifest_present:
            recommended_action = (
                'Evento ya preparado (manifest.json presente): ejecutar corrida diaria con --refresh-existing-odds '
                '(usará --immutable-capture sin alterar el manifest) o usar prematch_single.py refresh-odds.'
            )
        else:
            recommended_action = 'Reintentar captura Bwin acotada para este evento.'
    elif (disc_row or {}).get('status') == 'EVENT_REQUIRES_REVIEW' or (audit_row or {}).get('link_status') in {'NAME_MISMATCH', 'TIME_CONFLICT', 'CATEGORY_CONFLICT', 'AMBIGUOUS_LINK'}:
        blocking_phase = 'bwin_identity_validation'
        root_cause = f"{(audit_row or {}).get('link_status') or (disc_row or {}).get('status')}: {','.join((audit_row or {}).get('reason_codes') or [])}"
        recommended_action = 'No relajar umbrales: verificar manualmente si Bwin corresponde al mismo torneo, categoría y hora exacta.'
    elif (disc_row or {}).get('status') == 'NOT_FOUND_IN_SCANNED_PAGES':
        blocking_phase = 'bwin_discovery'
        if local_rematch:
            root_cause = (
                f"Evento PRESENTE en catalog_observed.json ({local_rematch[0]['bwin_event_id']}) "
                f"pero no coincidió en la corrida congelada por alias/nombre de equipo."
            )
            recommended_action = 'Ejecutar nueva evaluación diaria para inspeccionar la página del evento ya recuperado y capturar cuotas.'
        else:
            root_cause = f"{(disc_row or {}).get('discovery_diagnostic') or 'NOT_FOUND_IN_SCANNED_PAGES'}: no observado en {len(disc_doc.get('pages', []))} páginas inspeccionadas."
            recommended_action = 'Comprobar manualmente si el evento/liga está listado en Bwin o pendiente de publicación.'
    elif scan_row and scan_row.get('status') != 'VERIFIED':
        blocking_phase = 'opportunity_scanner'
        root_cause = f"{scan_row.get('status')}: {','.join(scan_row.get('reasons') or [])}"
        recommended_action = 'Corregir validez temporal o integridad SHA del snapshot Bwin antes de operar.'
    elif scan_row and scan_row.get('status') == 'VERIFIED':
        blocking_phase = 'NONE'
        root_cause = 'IDENTIDAD_Y_CUOTAS_VERIFICADAS'
        recommended_action = 'Candidato verificado para investigación manual (requiere cuotas finales T-60/T-15 y revisión de XI).'
    else:
        blocking_phase = ledger_state or 'UNRESOLVED'
        root_cause = ledger_reason or 'SIN_DIAGNOSTICO_ESPECIFICO'
        recommended_action = 'Revisar candidate_ledger y logs de preflight.'

    pending_checks = ['Ninguna ventaja económica ni probabilidad calibrada demostrada']
    if not (preflight_match or {}).get('snapshot'):
        pending_checks.append('Falta captura Bwin verificada')
    pending_checks.append('Confirmación de XI oficial (22 titulares) y cuotas finales (<15m antes de KO<=60m)')

    return {
        'schema_version': '1.0-fixture-diagnosis',
        'reads_only': True,
        'network': False,
        'actionable_bet': False,
        'profile': profile,
        'date': parsed_day.isoformat() if parsed_day else None,
        'evaluation_dir': str(folder),
        'match': {
            'fixture_id': fid_int,
            'home': match_meta.get('home'),
            'away': match_meta.get('away'),
            'country': match_meta.get('country'),
            'league': match_meta.get('league'),
            'league_id': match_meta.get('league_id'),
            'kickoff_madrid': match_meta.get('kickoff_madrid'),
        },
        'api_coverage': {
            'data_readiness_score': pre_row.get('data_readiness_score') if pre_row else None,
            'data_readiness_status': pre_row.get('data_readiness_status') if pre_row else None,
            'coverage_summary': tac_fixture.get('coverage_summary'),
            'research_missing_evidence': (tactical_row or {}).get('research_missing_evidence'),
        },
        'independent_statistical_score': {
            'statistical_ranking_position': (stat_row or {}).get('position'),
            'statistical_research_score': (stat_row or {}).get('statistical_research_score'),
            'signal_count': (stat_row or {}).get('signal_count'),
            'signal_families': (stat_row or {}).get('signal_families'),
            'contradictions': (stat_row or {}).get('contradictions'),
            'tactical_selection_tier': (tactical_row or {}).get('selection_tier'),
            'tactical_priority_score': (tactical_row or {}).get('priority_score'),
            'tactical_eligible': (tactical_row or {}).get('eligible'),
        },
        'bwin_identity': {
            'discovery_status': (disc_row or {}).get('status'),
            'discovery_diagnostic': (disc_row or {}).get('discovery_diagnostic'),
            'audit_link_status': (audit_row or {}).get('link_status'),
            'audit_reason_codes': (audit_row or {}).get('reason_codes'),
            'bwin_event_id': bwin_eid,
            'bwin_url': (disc_row or {}).get('bwin_url') or (audit_row or {}).get('bwin_url'),
            'canonical_manifest_present': manifest_present,
            'local_catalog_rematch_preview': local_rematch,
        },
        'odds_preflight_scanner': {
            'ledger_state': ledger_state,
            'ledger_reason': ledger_reason,
            'preflight_status': (preflight_match or {}).get('status'),
            'scraper_error': scraper_err or None,
            'snapshot_path': ((preflight_match or {}).get('snapshot') or {}).get('snapshot_path'),
            'selection_triplets_verified': ((preflight_match or {}).get('snapshot') or {}).get('selection_triplets_verified'),
            'capture_declared_madrid': ((preflight_match or {}).get('snapshot') or {}).get('capture_declared_madrid'),
            'scanner_status': (scan_row or {}).get('status'),
            'scanner_reasons': (scan_row or {}).get('reasons'),
            'selected_in_handoff': str(handoff_doc.get('fixture_id')) == fid_str,
        },
        'blocking_phase': blocking_phase,
        'root_cause': root_cause,
        'evidence_files': evidence_files,
        'recommended_action': recommended_action,
        'pending_checks': pending_checks,
    }


def main(argv=None):
    daily_started=performance_clock.perf_counter()
    arguments=list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in ('prepare','dry-run','run'):
        from prematch_execution_control import main as controlled_main
        return controlled_main(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',type=trial.parse_clock)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--tactical-research',action='store_true');parser.add_argument('--research-context',type=Path,action='append',default=[]);parser.add_argument('--interpretation',type=Path);parser.add_argument('--pattern-library',type=Path)
    mode.add_argument('--rank', action='store_true')
    mode.add_argument('--prepare-fixture', action='store_true')
    mode.add_argument('--status', action='store_true',
                      help='Solo lectura del handoff de --date. No usa manifest.json ni red')
    mode.add_argument('--diagnose-fixture', action='store_true',
                      help='Diagnóstico en solo lectura por --fixture-id en evaluación existente. 0 API / 0 Bwin')
    mode.add_argument('--bind-bwin-url', action='store_true',
                      help='Registra y valida URL(s) Bwin manuales asociadas a --fixture-id sin saltar controles PREMATCH')
    parser.add_argument('--manual-bwin-url', action='append', default=[],
                        help='Vinculación manual <FIXTURE_ID>=<BWIN_URL> (repetible)')
    parser.add_argument('--manual-bwin-file', type=Path,
                        help='Archivo JSON/CSV con pares fixture_id y bwin_url')
    parser.add_argument('--verify-manual-bwin', action='store_true',
                        help='Revalida en vivo la página del evento Bwin para URLs manuales (requiere --confirm-external)')
    parser.add_argument('--scrape-manual-bwin', action='store_true',
                        help='Tras verificar identidad manual, captura/reintenta cuotas con scraper (requiere --confirm-external)')
    parser.add_argument('--fixture-id', type=int)
    parser.add_argument('--eval-dir', type=Path, help='Directorio explícito de evaluación para --diagnose-fixture')
    parser.add_argument('--preselection', type=Path)
    parser.add_argument('--fixtures-input', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--asof')
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--confirm-external',action='store_true')
    parser.add_argument('--max-api-calls', type=int, default=0)
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--reconcile-schedule',action='store_true',help='Revalida calendario API actual en lotes; requiere --rank --online y presupuesto; no repite históricos')
    parser.add_argument('--sync-bwin', action='store_true', help='Descubrimiento ligero Bwin sin cuotas profundas ni API-Football')
    parser.add_argument('--bwin-catalog', type=Path, help='Catálogo Bwin existente; conserva fecha y SHA')
    parser.add_argument('--select-verified', action='store_true', help='Tras ranking: un evento verificado -> preflight -> selector -> check, sin finish')
    parser.add_argument('--prepare-selected', action='store_true',
                        help='Con --rank --select-verified: prepara el expediente canónico reutilizando las cuotas verificadas')
    parser.add_argument('--end',type=trial.parse_clock)
    parser.add_argument('--evaluation-budget',type=int,default=0,help='0=todos; límite explícito con filas pendientes')
    parser.add_argument('--discovery-budget',type=int,default=0,help='0=todos; límite explícito con filas pendientes')
    parser.add_argument('--date', help='Forzar fecha inicial Madrid: YYYY-MM-DD, hoy o mañana')
    parser.add_argument('--horizon-hours',type=float,default=0,help='Próximas N horas; 0 permite preparar toda la ventana con antelación')
    parser.add_argument('--bwin-top',type=int,default=5,help='Tamaño de lote Bwin; sin recorte global')
    parser.add_argument('--check-final-odds',action='store_true',help='Con --review: exigir una captura cercana al kickoff y fresca')
    parser.add_argument('--review-min-lead',type=int,default=REVIEW_MIN_LEAD_MINUTES,
                        help='Margen mínimo para revalidar un handoff ya preparado (>=5; no cambia la preparación T-45)')
    parser.add_argument('--window', choices=['normal','ampliada'])
    parser.add_argument('--competition-scope',choices=['adult-open','domestic-top-tier','top-tier-and-senior-cups'],default='adult-open',
                        help='adult-open: todas las competiciones adultas; top-tier-and-senior-cups: 1a division M/F y copas absolutas; domestic-top-tier: filtro legado')
    parser.add_argument('--dry-run', '--show-config', dest='dry_run', action='store_true')
    parser.add_argument('--next-window', action='store_true', help='Preparar la siguiente franja si la actual ya no tiene candidatos útiles')
    parser.add_argument('--review', type=Path, help='Revalidar el handoff antes de continuar manualmente')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--max-matches', type=int, default=5,help='Tamaño del lote de evaluación API; todos los candidatos del horizonte se evalúan')
    parser.add_argument('--history', type=int, default=20, help='Históricos por equipo (4..50; default 20)')
    parser.add_argument('--coverage-rules',type=Path,help='Reglas JSON de prioridad estadística y planificación')
    parser.add_argument('--details', type=int, default=8, help='Detalles por equipo (1..history; default 8)')
    parser.add_argument('--max-scrapes', type=int, default=0, help='Presupuesto de capturas verificadas; 0 = todos los candidatos del horizonte')
    parser.add_argument('--refresh-existing-odds',action='store_true',help='Autoriza recaptura con archivo SHA de snapshots caducados del mismo evento')
    parser.add_argument('--predictive-engine', action='store_true',
                        help='Opt-in: predictor offline sobre la preselección congelada; no altera Bwin, selección ni finish')
    parser.add_argument('--market-mapping', action='store_true',
                        help='Opt-in: mapea patrones descriptivos a mercados Bwin ya capturados; sin red ni picks')
    parser.add_argument('--market-mapping-patterns', type=Path)
    parser.add_argument('--min-market-odd',type=float,default=1.20,help='Filtro comercial configurable; no representa valor esperado')
    parser.add_argument('--market-mapping-preflight', action='append', type=Path, default=[])
    parser.add_argument('--market-mapping-only', action='store_true',
                        help='Reprocesa sólo snapshots locales; nunca llama prepare, scan, API ni Bwin')
    parser.add_argument('--discovery-retries', type=int, default=1,
                        help='Máximo de descubrimientos completos por fixture sin identidad persistida (default 1)')
    parser.add_argument('--bwin-identity-catalog', type=Path,
                        help='Reconciliación Fixture ID/Event ID persistida; se reinspecciona la URL directa')
    parser.add_argument('--min-lead', type=int, default=45)
    parser.add_argument('--manual-bwin-min-lead', type=int, default=MANUAL_BWIN_MIN_LEAD_MINUTES,
                        help='Margen mínimo para vincular/verificar/capturar URL manual Bwin (>=5; no cambia preparación T-45)')
    parser.add_argument('--max-odds-age', type=int, default=120)
    parser.add_argument('--shadow-mode', action='store_true',
                        help='Opt-in: ejecuta Bet Engine en Shadow Mode sobre snapshots locales sin alterar salidas operativas')
    mode.add_argument('--shadow-preanalyze', action='store_true',
                      help='Ciclo T-24h offline: preanaliza preselection y persiste ranking sin picks definitivos')
    mode.add_argument('--shadow-status', action='store_true',
                      help='Planificador secuencial: muestra cola por proximidad y disponibilidad (default 13:00 Europe/Madrid)')
    mode.add_argument('--shadow-finalize', action='store_true',
                      help='Finaliza 1 partido en Shadow Mode reutilizando T-24h y recalculando la cola')
    parser.add_argument('--shadow-dir', type=Path, help='Directorio persistente para ciclo T-24h Shadow')
    parser.add_argument('--available-from', default='13:00', help='Hora local de disponibilidad (HH:MM, default 13:00)')
    parser.add_argument('--now-utc', help='Reloj UTC explícito para pruebas reproducibles T-24h/proximidad')
    parser.add_argument('--manual-select', help='Event ID manual para desempates de calidad equivalente')
    parser.add_argument('--event-id', help='Event ID explícito para --shadow-finalize')
    parser.add_argument('--odds-json', type=Path, help='Snapshot de cuotas frescas JSON para --shadow-finalize offline')
    parser.add_argument('--manual-lineups', type=Path, help='Archivo JSON estructurado de XI manuales confirmados para --shadow-finalize')
    parser.add_argument('--lineup-cutoff-minutes', type=int, default=15, help='Corte prepartido para exigir 22 titulares confirmados (default 15)')
    parser.add_argument('--fallback-on-missing-lineups', action='store_true', help='Si faltan 22 titulares confirmados en el corte, descartar para auditoría y avanzar al siguiente candidato')
    parser.add_argument('--user-manual-absent', action='store_true', help='Simula/marca ausencia de incorporación manual de XI por el usuario')
    parser.add_argument('--auto-continue-next', action='store_true', help='Tras descartar por falta de XI, ejecuta automáticamente el siguiente candidato elegible del plan')
    parser.add_argument('--next-odds-json', type=Path, help='Cuotas frescas JSON para el candidato de continuación automática')
    parser.add_argument('--next-manual-lineups', type=Path, help='XI manuales JSON para el candidato de continuación automática')
    args = parser.parse_args(argv)
    if args.prepare_selected and not (args.rank and args.select_verified):
        parser.error('--prepare-selected requiere --rank --select-verified')
    if args.dry_run and any((args.rank,args.prepare_fixture,args.sync_bwin,args.reconcile_schedule,
                            args.tactical_research,args.market_mapping_only,args.online,args.bind_bwin_url)):
        print(json.dumps({'status':'DRY_RUN_NO_EXTERNAL_EXECUTION','date':args.date,
                          'online_requested':args.online,'external_calls':0,'stages_executed':0},indent=2))
        return 0
    manual_bwin_offline_only = (args.bind_bwin_url or bool(args.manual_bwin_url or args.manual_bwin_file)) and not (
        args.online or args.sync_bwin or args.prepare_fixture or args.verify_manual_bwin or args.scrape_manual_bwin
    )
    if not args.dry_run and (args.online or args.sync_bwin or args.prepare_fixture or args.verify_manual_bwin or args.scrape_manual_bwin or (not manual_bwin_offline_only and not any((args.status,args.diagnose_fixture,args.tactical_research,args.market_mapping_only,args.rank,args.review,args.shadow_preanalyze,args.shadow_status,args.shadow_finalize)))):
        from prematch_execution_control import require_external_confirmation
        try:require_external_confirmation(args.confirm_external)
        except ValueError as error:parser.error(str(error))
    if args.shadow_preanalyze or args.shadow_status or args.shadow_finalize:
        if not args.date:
            parser.error('--shadow-preanalyze/--shadow-status/--shadow-finalize requieren --date YYYY-MM-DD')
        root = args.root.expanduser().resolve()
        action = 'preanalyze' if args.shadow_preanalyze else 'status' if args.shadow_status else 'finalize'
        try:
            from bet_engine.orchestration import run_daily_shadow_lifecycle
            res = run_daily_shadow_lifecycle(
                root,
                action=action,
                date_str=args.date,
                preselection_path=args.preselection,
                shadow_dir=args.shadow_dir,
                available_from=args.available_from,
                now_utc=args.now_utc,
                manual_select_event_id=args.manual_select,
                event_id=args.event_id,
                odds_json_path=args.odds_json,
                max_odds_age_minutes=args.max_odds_age,
                min_lead_minutes=args.min_lead,
                manual_lineup_path=args.manual_lineups,
                lineup_cutoff_minutes=args.lineup_cutoff_minutes,
                fallback_on_missing_lineups=args.fallback_on_missing_lineups,
                user_manual_absent=args.user_manual_absent,
                auto_continue_next=args.auto_continue_next,
                next_odds_json_path=args.next_odds_json,
                next_manual_lineup_path=args.next_manual_lineups,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError, KeyError, FileNotFoundError) as exc:
            print('ERROR: ' + str(exc), file=sys.stderr)
            return 2
    if args.bind_bwin_url or ((args.manual_bwin_url or args.manual_bwin_file) and not args.online):
        if not args.date and not args.eval_dir:
            parser.error('--bind-bwin-url requiere --date YYYY-MM-DD o --eval-dir <PATH>')
        if not args.manual_bwin_url and not args.manual_bwin_file:
            parser.error('--bind-bwin-url requiere --manual-bwin-url <FID>=<URL> o --manual-bwin-file <PATH>')
        try:
            from bwin_manual_links import (
                ManualBwinLinkError,
                load_manual_bwin_file,
                load_manual_links,
                manual_links_path,
                parse_manual_bwin_spec,
                register_manual_bwin_urls,
                verify_manual_bwin_links_online,
            )
            from prematch_runtime import classify_existing_snapshot, stamp as _runtime_stamp, verified_preflight
            from prematch_daily_csv import export_daily

            root = args.root.expanduser().resolve()
            if args.eval_dir:
                eval_dir = args.eval_dir.expanduser().resolve()
            else:
                config, _, _ = trial.load_window_config(root)
                _, start, end = trial.resolve_window(config, args.window, args.start, args.end)
                window_folder = trial.window_path(root, trial.parse_date_relative(args.date), start, end)
                eval_dir = trial.latest_evaluation(window_folder)

            bindings: list[tuple[int, str]] = [parse_manual_bwin_spec(s) for s in args.manual_bwin_url]
            if args.manual_bwin_file:
                bindings.extend(load_manual_bwin_file(args.manual_bwin_file))

            manual_min_lead = int(getattr(args, 'manual_bwin_min_lead', MANUAL_BWIN_MIN_LEAD_MINUTES))
            if manual_min_lead < MANUAL_BWIN_MIN_LEAD_MINUTES:
                raise ManualBwinLinkError(
                    f'MANUAL_BWIN_MINIMUM_5_MINUTES:{manual_min_lead}<{MANUAL_BWIN_MIN_LEAD_MINUTES}'
                )

            now_dt = trial.dt(args.now_utc) if args.now_utc else None
            if args.now_utc and now_dt is None:
                raise ManualBwinLinkError(f'INVALID_NOW_UTC:{args.now_utc}')
            manual_now_madrid = now_dt.astimezone(trial.TZ) if now_dt is not None else trial.now_madrid()
            reg = register_manual_bwin_urls(
                eval_dir,
                bindings,
                now_utc=now_dt,
                min_lead_minutes=manual_min_lead,
            )
            ver = None
            scraped = []
            if args.verify_manual_bwin or args.scrape_manual_bwin:
                ver = verify_manual_bwin_links_online(
                    eval_dir,
                    [fid for fid, _ in bindings],
                    min_lead_minutes=manual_min_lead,
                    now_utc=now_dt,
                )
            if args.scrape_manual_bwin:
                base = eval_dir / 'prospective_trial'
                prepath = base / 'enriched/preselection.json'
                if not prepath.is_file():
                    raise ManualBwinLinkError(f'PRESELECTION_REQUIRED_FOR_SCRAPE:{prepath}')
                pre_doc = trial.read(prepath)
                pre_by_fid = {int(m['fixture_id']): m for m in pre_doc.get('matches', []) if m.get('fixture_id') is not None}
                store = load_manual_links(eval_dir)
                manual_args = SimpleNamespace(**vars(args))
                manual_args.min_lead = manual_min_lead
                for fid, _ in bindings:
                    entry = (store.get('links') or {}).get(str(fid)) or {}
                    op_block = entry.get('operational_block') or {}
                    if op_block.get('blocked'):
                        scraped.append({
                            'fixture_id': fid,
                            'status': entry.get('operational_status') or 'OPERATIONAL_BLOCK_TEMPORAL',
                            'identity_status': entry.get('identity_status'),
                            'operational_block': op_block,
                        })
                        continue
                    if entry.get('identity_status') != 'BWIN_MANUAL_LINK_VERIFIED':
                        scraped.append({'fixture_id': fid, 'status': 'SKIPPED_NOT_VERIFIED', 'identity_status': entry.get('identity_status')})
                        continue
                    fixture = pre_by_fid.get(fid)
                    if not fixture:
                        scraped.append({'fixture_id': fid, 'status': 'SKIPPED_NOT_IN_PRESELECTION'})
                        continue
                    disc_path = Path((entry.get('verification') or {}).get('discovery_json') or '')
                    eid = str(entry.get('extracted_event_id') or '')
                    dest = base / 'daily_runs' / f'manual_{_runtime_stamp()}' / 'preflight' / f'{fid}_manual'
                    target = root / 'outputs/matches' / eid / 'inputs' / f'cuotas_bwin_{eid}.txt'
                    has_manifest = bool(eid) and (root / 'outputs/matches' / eid / 'manifest.json').exists()
                    refresh_args = []
                    if target.exists():
                        if getattr(args, 'refresh_existing_odds', False):
                            refresh_args = ['--refresh-existing', '--immutable-capture'] if has_manifest else ['--refresh-existing']
                        else:
                            cls, detail = classify_existing_snapshot(target, eid, entry.get('bwin_url', ''), manual_now_madrid, args.max_odds_age)
                            if cls == 'REUSABLE_EXISTING_SNAPSHOT':
                                refresh_args = ['--reuse-existing']
                    cmd_pre = [
                        '--discovery', str(disc_path), '--preselection', str(prepath),
                        '--fixture-id', str(fid), '--root', str(root),
                        '--min-lead-minutes', str(manual_min_lead), '--output-dir', str(dest),
                    ]
                    if args.now_utc:
                        cmd_pre.extend(['--now-utc', str(args.now_utc)])
                    if '--reuse-existing' not in refresh_args:
                        cmd_pre.append('--run-scraper')
                    cmd_pre.extend(refresh_args)
                    if args.confirm_external:
                        cmd_pre.append('--confirm-external')
                    try:
                        trial.run(root, 'bwin_market_preflight.py', cmd_pre, base / 'logs')
                        verified_preflight(root, dest / 'market_preflight.json', fixture, prepath, manual_args)
                        entry['quote_capture_status'] = 'CAPTURED_VERIFIED'
                        entry['operational_status'] = 'CLEAR'
                        entry['operational_block'] = None
                        entry['last_preflight'] = str((dest / 'market_preflight.json').resolve())
                        scraped.append({'fixture_id': fid, 'status': 'CAPTURED_VERIFIED', 'preflight': entry['last_preflight']})
                    except (trial.TrialError, OSError, ValueError, KeyError) as exc:
                        err_text = str(exc)
                        is_temporal_err = 'ALREADY_STARTED_OR_INSUFFICIENT_PREMATCH_MARGIN' in err_text or 'POST_KICKOFF' in err_text
                        if is_temporal_err:
                            entry['quote_capture_status'] = 'BLOCKED_TEMPORAL_WINDOW'
                            entry['operational_status'] = 'OPERATIONAL_BLOCK_TEMPORAL'
                            entry['operational_block'] = {
                                'blocked': True,
                                'block_type': 'TEMPORAL_WINDOW_BLOCK',
                                'operation': 'scrape_manual_bwin',
                                'reason': err_text,
                                'min_lead_minutes': manual_min_lead,
                                'checked_at_utc': manual_now_madrid.astimezone(timezone.utc).isoformat(),
                            }
                            scraped.append({
                                'fixture_id': fid,
                                'status': 'BLOCKED_TEMPORAL_WINDOW',
                                'identity_status': entry.get('identity_status'),
                                'error': err_text,
                            })
                        else:
                            entry['quote_capture_status'] = 'CAPTURE_FAILED'
                            entry['last_scrape_error'] = err_text
                            scraped.append({'fixture_id': fid, 'status': 'CAPTURE_FAILED', 'error': err_text})
                trial.dump(manual_links_path(eval_dir), store)

            if (eval_dir / 'fixtures.json').is_file() and (eval_dir / 'prospective_trial/candidate_ledger.json').is_file():
                export_daily(eval_dir / 'prospective_trial')

            print(json.dumps({
                'registration': reg,
                'live_verification': ver,
                'scrape_results': scraped,
                'operation_windows': {
                    'preparation_min_lead_minutes': args.min_lead,
                    'manual_bwin_min_lead_minutes': manual_min_lead,
                    'review_min_lead_minutes': REVIEW_MIN_LEAD_MINUTES,
                },
            }, ensure_ascii=False, indent=2))
            has_live_conflict = bool(ver and ver.get('failed'))
            has_temporal_block = bool(reg.get('temporal_blocked_count') or (ver and ver.get('temporal_blocked')))
            has_scrape_block = any(
                str(item.get('status') or '').startswith(('OPERATIONAL_BLOCK_', 'BLOCKED_', 'CAPTURE_FAILED', 'SKIPPED_NOT_VERIFIED'))
                for item in scraped
            )
            return 0 if (reg['conflict_count'] == 0 and not has_live_conflict and not has_temporal_block and not has_scrape_block) else 2
        except (ManualBwinLinkError, ValueError, OSError, KeyError, trial.TrialError) as exc:
            print('ERROR: ' + str(exc), file=sys.stderr)
            return 2

    if args.diagnose_fixture:
        if not args.fixture_id:
            parser.error('--diagnose-fixture requiere --fixture-id <FID>')
        if not args.date and not args.eval_dir:
            parser.error('--diagnose-fixture requiere --date YYYY-MM-DD o --eval-dir <PATH>')
        try:
            report = diagnose_fixture(args.root, args.date, args.fixture_id, args.window, args.eval_dir)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError, LookupError, KeyError, trial.TrialError) as exc:
            print('ERROR: ' + str(exc), file=sys.stderr)
            return 2
    if args.status:
        if not args.date:
            parser.error('--status requiere --date YYYY-MM-DD')
        report = discovery_status(args.root, args.date, args.window)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        ready = report.get('artifacts', {}).get('handoff', {}).get('present') and report.get('status') not in (
            'NO_DAILY_HANDOFF', 'HANDOFF_UNREADABLE')
        return 0 if ready else 2
    if args.tactical_research:
        try:
            print(json.dumps(daily_tactical_research(args),ensure_ascii=False,indent=2));return 0
        except (ValueError,OSError,KeyError,TypeError) as exc:
            print('ERROR: '+str(exc),file=sys.stderr);return 2
    if args.market_mapping_only:
        if args.rank or args.prepare_fixture or args.predictive_engine or args.sync_bwin or args.online:
            parser.error('--market-mapping-only no permite etapas de adquisición ni predictor')
        if not args.date:
            parser.error('--market-mapping-only requiere --date explícita')
        args.market_mapping = True
        root = args.root.expanduser().resolve()
        try:
            config, _, _ = trial.load_window_config(root)
            _, start, end = trial.resolve_window(config, args.window, args.start, args.end)
            window_folder = trial.window_path(root, trial.parse_date_relative(args.date), start, end)
            folder = trial.latest_evaluation(window_folder)
            output, counts = run_optional_market_mapping(root, folder, args)
            print(json.dumps({'status':'OFFLINE_SNAPSHOT_MAPPING', 'path':str(output), 'counts':counts,
                              'actionable_bet':False,'next_action':'Revisar market_mapping.md: resolver causas pendientes y comprobar identidad/cuotas antes del uso actual',
                              'required_inputs':'Cuotas frescas; XI oficial si condiciona la tesis; investigación externa opcional',
                              'next_command':f'python prematch_daily.py --date {args.date} --market-mapping-only',
                              'next_command_purpose':'Repetir tras incorporar preflights locales; no realiza adquisición'}, ensure_ascii=False, indent=2))
            return 0
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            print('ERROR: '+str(exc), file=sys.stderr)
            return 2
    if args.rank or args.prepare_fixture or args.sync_bwin or args.reconcile_schedule:
        try:
            if args.min_lead < 45: raise ValueError('PREPARATION_MINIMUM_45_MINUTES')
            print(json.dumps(operational_research(args), ensure_ascii=False, indent=2)); return 0
        except (ValueError, OSError, KeyError, TypeError, trial.TrialError) as exc:
            print('ERROR: '+str(exc), file=sys.stderr); return 2
    if args.evaluation_budget<0 or args.discovery_budget<0 or not __import__("math").isfinite(args.horizon_hours) or args.horizon_hours<0 or args.bwin_top<1 or args.max_scrapes<0 or min(args.max_matches,args.max_odds_age)<1 or args.min_lead<45 or args.review_min_lead<REVIEW_MIN_LEAD_MINUTES or not 0 <= args.discovery_retries <= 3 or not 4 <= args.history <= 50 or not 1 <= args.details <= args.history:
        parser.error('Límites inválidos; history 4..50, details 1..history, discovery-retries 0..3, preparación >=45 y review >=5 minutos')
    if args.date and args.next_window:
        parser.error('--date y --next-window son alternativas')
    if args.check_final_odds and not args.review:
        parser.error('--check-final-odds requiere --review')
    try:
        if args.review:
            return review(args.review, args.root.expanduser().resolve(),args.check_final_odds,args.review_min_lead)
        root = args.root.expanduser().resolve()
        config, _, _ = trial.load_window_config(root)
        profile, args.start, args.end = trial.resolve_window(config, args.window,args.start,args.end)
        execution_reference = trial.now_madrid()
        args.execution_reference = execution_reference
        day = trial.parse_date_relative(args.date, execution_reference.date()) if args.date else automatic_day(execution_reference, args.start, args.end)
        if args.next_window:
            day += timedelta(days=1)
        window_folder = trial.window_path(root, day, args.start, args.end)
        begin, finish = trial.local_window(day, args.start, args.end)
        print(f'PERFIL {profile}: {begin.isoformat()} → {finish.isoformat()} | HORIZONTE {args.horizon_hours} h (0=toda la ventana)\nVENTANA {window_folder}', flush=True)
        if args.dry_run:
            return 0
        args.team=[]
        # This lock is shared with trial scan and remains as an inert file after closing.
        lock_path = root / 'outputs/.prematch_daily.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise trial.TrialError('PREMATCH_ALREADY_RUNNING') from None
            exclusive={};phase_started=performance_clock.perf_counter()
            folder, evaluation_mode = trial.resolve_evaluation(
                window_folder, day, args, execution_reference)
            if evaluation_mode == 'RESUME_FROZEN_EVALUATION':
                frozen_reference = trial.dt(trial.evaluation_manifest(folder).get('reference_madrid'))
                if frozen_reference is None:
                    raise trial.TrialError('RESUME_EVALUATION_REFERENCE_INVALID')
                args.execution_reference = frozen_reference
            exclusive['evaluation_resolution_and_lock']=performance_clock.perf_counter()-phase_started
            print(f'EVALUACIÓN {evaluation_mode}: {folder}', flush=True)
            phase_started=performance_clock.perf_counter()
            try:
                major = folder / 'fixtures_major.json'
                if major.is_file():
                    existing_policy = trial.read(major).get('policy',{}).get('competition_tier')
                    expected_policy = {'adult-open': 'ADULT_OPEN', 'domestic-top-tier': 'DOMESTIC_TOP_TIER', 'top-tier-and-senior-cups': 'TOP_TIER_AND_SENIOR_CUPS'}[args.competition_scope]
                    if existing_policy != expected_policy:
                        raise trial.TrialError('COMPETITION_SCOPE_CHANGED: preparacion anterior no reutilizable')
                reusable_preparation(root, folder, day, args)
                print('Preparación existente íntegra: reutilizada sin consultas API.', flush=True)
            except (trial.TrialError, OSError, ValueError, KeyError) as exc:
                print(f'Preparación necesaria: {exc}', flush=True)
                try:
                    transcript = folder / 'prospective_trial/logs' / f'daily_prepare_{stamp()}.log'
                    transcript.parent.mkdir(parents=True, exist_ok=True)
                    print(f'Preparando API, patrones y Bwin; registro: {transcript}', flush=True)
                    with transcript.open('w', encoding='utf-8') as output, contextlib.redirect_stdout(output):
                        previous_scope = os.environ.get('PREMATCH_COMPETITION_SCOPE')
                        os.environ['PREMATCH_COMPETITION_SCOPE'] = args.competition_scope
                        try:
                            trial.prepare(root, folder, day, args)
                        finally:
                            if previous_scope is None:
                                os.environ.pop('PREMATCH_COMPETITION_SCOPE', None)
                            else:
                                os.environ['PREMATCH_COMPETITION_SCOPE'] = previous_scope
                except trial.TrialError as exc:
                    # An empty candidate set is a normal, explicit terminal state.
                    status = 'NO_VALID_CANDIDATE' if str(exc).startswith('No hay partidos futuros') else 'TECHNICAL_FAILURE'
                    path = folder / 'prospective_trial/handoff_prematch.json'
                    failure_doc = {'schema_version':'1.0-prematch-handoff','status':status,
                               'generated_madrid':trial.now_madrid().isoformat(), 'actionable_bet':False,
                               'discard_reasons':[{'phase':'prepare','reason':str(exc)}]}
                    evaluation_path=folder/'evaluation_manifest.json'
                    if evaluation_path.is_file():
                        evaluation=trial.evaluation_manifest(folder)
                        failure_doc.update(evaluation_id=evaluation['evaluation_id'],
                            evaluation_manifest=str(evaluation_path),
                            evaluation_manifest_sha256=trial.sha(evaluation_path),
                            execution_reference_madrid=evaluation['reference_madrid'])
                    trial.dump(path, failure_doc)
                    exclusive['preparation_total']=performance_clock.perf_counter()-phase_started
                    complete_daily_metrics(folder/'prospective_trial',failure_doc,daily_started,exclusive)
                    print(f'{status}: {path}', flush=True)
                    return 0 if status=='NO_VALID_CANDIDATE' else 2
            exclusive['preparation_total']=performance_clock.perf_counter()-phase_started
            phase_started=performance_clock.perf_counter()
            predictive_out = run_optional_predictor(root, folder, args)
            exclusive['optional_predictor']=performance_clock.perf_counter()-phase_started
            if predictive_out:
                print(f'Predictor offline congelado: {predictive_out}', flush=True)
            transcript = folder / 'prospective_trial/logs' / f'daily_scan_{stamp()}.log'
            transcript.parent.mkdir(parents=True, exist_ok=True)
            print(f'Verificando capturas y scanner; registro: {transcript}', flush=True)
            phase_started=performance_clock.perf_counter()
            with transcript.open('w', encoding='utf-8') as output, contextlib.redirect_stdout(output):
                doc = scan(root, folder, day, args)
            exclusive['scan_total']=performance_clock.perf_counter()-phase_started
            phase_started=performance_clock.perf_counter()
            mapping_out = run_optional_market_mapping(root, folder, args, predictive_out)
            exclusive['optional_market_mapping']=performance_clock.perf_counter()-phase_started
            if mapping_out:
                print(f'Market mapping offline: {mapping_out[0]} ({mapping_out[1]})', flush=True)
            if getattr(args, 'shadow_mode', False):
                try:
                    from bet_engine.orchestration import run_shadow_daily_optin
                    sh_daily = run_shadow_daily_optin(root, folder, day.isoformat())
                    print(f'Shadow Mode diario (opt-in): {sh_daily}', flush=True)
                except Exception as exc:
                    print(f'[SHADOW_WARN] fallo no bloqueante en daily: {exc}', flush=True)
            from prematch_daily_csv import export_daily
            phase_started=performance_clock.perf_counter()
            rows=export_daily(folder/'prospective_trial')
            csv_exported=True
            exclusive['csv_export']=performance_clock.perf_counter()-phase_started
            complete_daily_metrics(folder/'prospective_trial',doc,daily_started,exclusive)
            print(f"{doc['status']}: {folder / 'prospective_trial/handoff_prematch.json'}", flush=True)
            print(f'CSV: {folder / "prospective_trial/partidos_diarios.csv"} | filas={len(rows)}',flush=True)
            return 0 if doc['status'] in ('READY_FOR_MANUAL_RESEARCH','NO_VALID_CANDIDATE','INSUFFICIENT_EVIDENCE') else 2
    except (trial.TrialError, OSError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    finally:
        if ('folder' in locals() and not args.dry_run and
                (folder/'prospective_trial').is_dir() and not locals().get('csv_exported',False)):
            from prematch_daily_csv import export_daily
            export_daily(folder/'prospective_trial')


from prematch_execution_control import confirmed_cli
main = confirmed_cli(main)

if __name__ == '__main__':
    raise SystemExit(main())
