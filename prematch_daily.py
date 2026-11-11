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
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import prematch_prospective_trial as trial
from prematch_runtime import reusable_preparation, scan, stamp, verified_preflight

# Existing discovery/preparation remains conservative (T-45).  Revalidation
# of an already prepared handoff is a lighter operation and may run until T-5;
# it still requires fresh quotes and all identity/integrity checks.
REVIEW_MIN_LEAD_MINUTES = 5


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
    if trial.sha(Path(doc['odds_txt'])) != doc['odds_sha256']:
        raise trial.TrialError('HANDOFF_SNAPSHOT_SHA_MISMATCH')
    if trial.sha(Path(doc['context_paths'][0])) != doc['context_sha256']:
        raise trial.TrialError('HANDOFF_CONTEXT_SHA_MISMATCH')
    context = trial.read(Path(doc['context_paths'][0]))
    fixture = context['fixture']
    if str(fixture['fixture_id']) != str(doc['fixture_id']):
        raise trial.TrialError('HANDOFF_FIXTURE_IDENTITY_MISMATCH')
    project = root or Path(__file__).resolve().parent
    pf = trial.read(Path(doc['preflight']))
    verified = verified_preflight(project, Path(doc['preflight']), fixture,
                Path(pf['source_preselection']), SimpleNamespace(min_lead=configured_review_min,
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
        elif any(token in str(inspection.get('identity_status','')) for token in ('MISMATCH','CONFLICT')) or row.get('team_order_status') in ('AWAY_HOME_CONFLICT','AWAY_HOME_TOKEN_CONFLICT'):
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
            competition_ok = competition_similarity(api.get('league',''), inspection.get('page_title',''))>=.82
            order_ok = row.get('team_order_status') in ('HOME_AWAY','HOME_AWAY_TOKEN')
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
    eligible = [r for r in ranked if r.get('bwin',{}).get('status')=='VERIFIED' and
                (not args.fixture_id or r['fixture_id']==args.fixture_id)]
    if not eligible: raise ValueError('NO_VERIFIED_BWIN_FIXTURE: conservar candidaturas; ver ranking.json')
    chosen = eligible[0];fid=chosen['fixture_id'];event=str(chosen['bwin']['event_id'])
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
        if target.is_file() and (not captured or not 0 <= (now-captured).total_seconds() <= args.max_odds_age*60):
            raise ValueError('EXISTING_ODDS_STALE: actualizar mediante refresh-odds; no sobrescribir snapshot')
        run('bwin_market_preflight.py',['--discovery',bwin_path,'--preselection',prepath,'--fixture-id',fid,
            '--root',root,'--output-dir',folder/'preflight','--min-lead-minutes',args.min_lead,
            '--reuse-existing' if captured else '--run-scraper'],'preflight.log')
        preflight=folder/'preflight/market_preflight.json'
    fixture=next(r for r in trial.read(prepath)['matches'] if r['fixture_id']==fid)
    verified_preflight(root,preflight,fixture,prepath,args)
    run('select_single_match.py',['--preselection',prepath,'--discovery',bwin_path,'--preflight',preflight,
        '--output-dir',folder,'--root',root,'--min-comparable-matches','1',
        '--min-lead-minutes',args.min_lead,'--max-snapshot-age-minutes',args.max_odds_age],'selector.log')
    selected=folder/'selected_match.json'
    doc=trial.read(selected)
    if doc['fixture_id']!=fid or str(doc['bwin_event_id'])!=event:raise ValueError('SELECTOR_CHANGED_FIXTURE_OR_EVENT')
    run('prematch_single.py',['--root',root,'--selected',selected,'check'],'check.log')
    return {'status':'SELECTED_AND_CHECKED_FOR_RESEARCH','fixture_id':fid,'bwin_event_id':event,
        'selected_match':str(selected),'selection_decision':str(folder/'selection_decision.json'),
        'preflight':str(preflight.resolve()),'check_log':str(folder/'check.log'),'actionable_bet':False,
        'comparison_scope':'ONE_VERIFIED_FIXTURE_EXPLICIT_SINGLE_EVENT_PILOT'}


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
        prepath = trial.window_path(root, day, start, end) / 'prospective_trial/enriched/preselection.json'
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
        warnings.append('PRESELECTION_OLDER_THAN_120_MINUTES: cached history only; current context requires refresh')
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
        rests = {}
        for side in ('home','away'):
            rows = patterns.discovery.clean_observations(fixture, side, when)
            rests[side] = round((ko-patterns._dt(rows[0]['kickoff_utc'])).total_seconds()/3600, 2) if rows else None
        components['short_rest_penalty'] = 3 if any(v is not None and v < 72 for v in rests.values()) else 0
        score = round(components['tactical_quality'] + components['player_evidence_bonus'] - sum(v for k,v in components.items() if k.endswith('penalty')), 4)
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
        result = subprocess.run([sys.executable, str(root/'bwin_discovery.py'), '--input', str(canonical_path),
            '--top', '0', '--output-dir', str(destination), '--headless', '--max-country-pages', '8',*catalog_args], cwd=root)
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
    source=(args.preselection or trial.window_path(root,day,start,end)/'prospective_trial/enriched/preselection.json').resolve()
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',type=trial.parse_clock)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--tactical-research',action='store_true');parser.add_argument('--research-context',type=Path,action='append',default=[]);parser.add_argument('--interpretation',type=Path);parser.add_argument('--pattern-library',type=Path)
    mode.add_argument('--rank', action='store_true')
    mode.add_argument('--prepare-fixture', action='store_true')
    parser.add_argument('--fixture-id', type=int)
    parser.add_argument('--preselection', type=Path)
    parser.add_argument('--fixtures-input', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--asof')
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--max-api-calls', type=int, default=0)
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--reconcile-schedule',action='store_true',help='Revalida calendario API actual en lotes; requiere --rank --online y presupuesto; no repite históricos')
    parser.add_argument('--sync-bwin', action='store_true', help='Descubrimiento ligero Bwin sin cuotas profundas ni API-Football')
    parser.add_argument('--bwin-catalog', type=Path, help='Catálogo Bwin existente; conserva fecha y SHA')
    parser.add_argument('--select-verified', action='store_true', help='Tras ranking: un evento verificado -> preflight -> selector -> check, sin finish')
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
    parser.add_argument('--competition-scope',choices=['adult-open','domestic-top-tier'],default='adult-open',
                        help='adult-open: todas las competiciones adultas; domestic-top-tier: filtro legado')
    parser.add_argument('--dry-run', '--show-config', dest='dry_run', action='store_true')
    parser.add_argument('--next-window', action='store_true', help='Preparar la siguiente franja si la actual ya no tiene candidatos útiles')
    parser.add_argument('--review', type=Path, help='Revalidar el handoff antes de continuar manualmente')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--max-matches', type=int, default=5,help='Tamaño del lote de evaluación API; todos los candidatos del horizonte se evalúan')
    parser.add_argument('--history', type=int, default=20, help='Históricos por equipo (4..50; default 20)')
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
    parser.add_argument('--discovery-retries', type=int, default=2)
    parser.add_argument('--min-lead', type=int, default=45)
    parser.add_argument('--max-odds-age', type=int, default=120)
    args = parser.parse_args(argv)
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
            folder = trial.window_path(root, trial.parse_date_relative(args.date), start, end)
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
    if args.evaluation_budget<0 or args.discovery_budget<0 or not __import__("math").isfinite(args.horizon_hours) or args.horizon_hours<0 or args.bwin_top<1 or args.max_scrapes<0 or min(args.max_matches,args.max_odds_age)<1 or args.min_lead<45 or args.review_min_lead<REVIEW_MIN_LEAD_MINUTES or args.discovery_retries<0 or not 4 <= args.history <= 50 or not 1 <= args.details <= args.history:
        parser.error('Límites inválidos; history 4..50, details 1..history, preparación >=45 y review >=5 minutos')
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
        day = trial.parse_date_relative(args.date) if args.date else automatic_day(trial.now_madrid(), args.start, args.end)
        if args.next_window:
            day += timedelta(days=1)
        folder = trial.window_path(root, day, args.start, args.end)
        begin, finish = trial.local_window(day, args.start, args.end)
        print(f'PERFIL {profile}: {begin.isoformat()} → {finish.isoformat()} | HORIZONTE {args.horizon_hours} h (0=toda la ventana)\nSALIDA {folder}', flush=True)
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
            try:
                major = folder / 'fixtures_major.json'
                if major.is_file():
                    existing_policy = trial.read(major).get('policy',{}).get('competition_tier')
                    expected_policy = 'ADULT_OPEN' if args.competition_scope == 'adult-open' else 'DOMESTIC_TOP_TIER'
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
                    status = 'NO_VALID_CANDIDATE' if str(exc).startswith('No hay partidos futuros') else 'INSUFFICIENT_EVIDENCE'
                    path = folder / 'prospective_trial/handoff_prematch.json'
                    trial.dump(path, {'schema_version':'1.0-prematch-handoff','status':status,
                               'generated_madrid':trial.now_madrid().isoformat(), 'actionable_bet':False,
                               'discard_reasons':[{'phase':'prepare','reason':str(exc)}]})
                    print(f'{status}: {path}', flush=True)
                    return 0 if status=='NO_VALID_CANDIDATE' else 2
            predictive_out = run_optional_predictor(root, folder, args)
            if predictive_out:
                print(f'Predictor offline congelado: {predictive_out}', flush=True)
            transcript = folder / 'prospective_trial/logs' / f'daily_scan_{stamp()}.log'
            transcript.parent.mkdir(parents=True, exist_ok=True)
            print(f'Verificando capturas y scanner; registro: {transcript}', flush=True)
            with transcript.open('w', encoding='utf-8') as output, contextlib.redirect_stdout(output):
                doc = scan(root, folder, day, args)
            mapping_out = run_optional_market_mapping(root, folder, args, predictive_out)
            if mapping_out:
                print(f'Market mapping offline: {mapping_out[0]} ({mapping_out[1]})', flush=True)
            from prematch_daily_csv import export_daily
            rows=export_daily(folder/'prospective_trial')
            print(f"{doc['status']}: {folder / 'prospective_trial/handoff_prematch.json'}", flush=True)
            print(f'CSV: {folder / "prospective_trial/partidos_diarios.csv"} | filas={len(rows)}',flush=True)
            return 0 if doc['status'] in ('READY_FOR_MANUAL_RESEARCH','NO_VALID_CANDIDATE') else 2
    except (trial.TrialError, OSError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    finally:
        if 'folder' in locals() and not args.dry_run:
            from prematch_daily_csv import export_daily
            export_daily(folder/'prospective_trial')


if __name__ == '__main__':
    raise SystemExit(main())
