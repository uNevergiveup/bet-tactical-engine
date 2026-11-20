"""Safe orchestration shared by the trial launcher and the daily entry point.

No final analysis, report fusion, validator finish or bets are executed here.
"""
from __future__ import annotations

import csv
import json
import shutil
import time
import zipfile
from datetime import timedelta, timezone
from pathlib import Path

import prematch_prospective_trial as trial
from bwin_market_preflight import validate_match
from market_quality_gate import is_period, is_player_market
from prematch_opportunity_scanner import match_identity, validate_snapshot


def classify_existing_snapshot(target: Path, event_id: str, expected_url: str, now_ref, max_odds_age_minutes: int) -> tuple[str, str]:
    """Classify a pre-existing Bwin TXT snapshot without relaxing or overwriting it."""
    from bwin_market_preflight import snapshot_odds
    try:
        snap = snapshot_odds(target, str(event_id))
    except (OSError, ValueError) as exc:
        return 'INCOMPATIBLE_EXISTING_SNAPSHOT', str(exc)
    src_url = snap.get('source_url')
    if expected_url and src_url and str(src_url).strip() != str(expected_url).strip():
        return 'INCOMPATIBLE_EXISTING_SNAPSHOT', f"URL_MISMATCH: {src_url}"
    cap_raw = snap.get('capture_declared_madrid')
    cap = trial.dt(cap_raw)
    if cap is None:
        return 'INCOMPATIBLE_EXISTING_SNAPSHOT', 'NO_VALID_DECLARED_CAPTURE_TIME'
    age_sec = now_ref.timestamp() - cap.timestamp()
    if age_sec < -120:
        return 'INCOMPATIBLE_EXISTING_SNAPSHOT', f'FUTURE_CAPTURE_TIMESTAMP: {cap.isoformat()}'
    if age_sec > max_odds_age_minutes * 60:
        return 'STALE_EXISTING_ODDS', f"Captura {cap.isoformat()} caducada ({round(age_sec / 60, 1)}m > {max_odds_age_minutes}m)"
    return 'REUSABLE_EXISTING_SNAPSHOT', cap.isoformat()


def _sibling_preflights(folder: Path, current_base: Path) -> list[Path]:
    """Locate verified market_preflight.json files in sibling evaluations of the same day window."""
    parent = folder.parent
    if not parent.is_dir():
        return []
    found = []
    for p in sorted(parent.glob('*/prospective_trial/daily_runs/*/preflight/*/market_preflight.json'), reverse=True):
        if current_base not in p.parents:
            found.append(p)
    for p in sorted(parent.glob('*/prospective_trial/preflight/*/market_preflight.json'), reverse=True):
        if current_base not in p.parents:
            found.append(p)
    return found


def _rebind_sibling_preflight(root: Path, base: Path, dest: Path, sib_path: Path, fixture: dict, prepath: Path, args):
    """Safely re-audit a fresh, identity-matched TXT from a sibling evaluation without scraping."""
    doc = trial.read(sib_path)
    matches = [m for m in doc.get('matches', []) if str(m.get('fixture_id')) == str(fixture['fixture_id'])]
    if len(matches) != 1:
        raise trial.TrialError('SIBLING_PREFLIGHT_FIXTURE_NOT_UNIQUE')
    pm = matches[0]
    eid = str(pm.get('bwin_event_id') or '')
    bwin_url = str(pm.get('bwin_url') or '')
    if not eid.isdigit():
        raise trial.TrialError('SIBLING_PREFLIGHT_EVENT_MISSING')
    snap = pm.get('snapshot') or {}
    target = resolve_source(root, snap.get('snapshot_path'), snap.get('sha256'))
    cls, detail = classify_existing_snapshot(target, eid, bwin_url, trial.now_madrid(), args.max_odds_age)
    if cls != 'REUSABLE_EXISTING_SNAPSHOT':
        raise trial.TrialError(f'{cls}: {detail}')
    disc_path = resolve_source(root, doc.get('source_discovery'), doc.get('source_sha256'))
    disc_doc = trial.read(disc_path)
    events = [e for e in disc_doc.get('matches', []) if str(e.get('fixture_id')) == str(fixture['fixture_id'])]
    if len(events) != 1 or match_identity(fixture, events[0]):
        raise trial.TrialError('SIBLING_DISCOVERY_IDENTITY_MISMATCH')
    checked = validate_match(events[0], disc_doc.get('catalog_observed', []), trial.now_madrid(), args.min_lead, 180)
    if checked['status'] != 'READY_FOR_SCRAPER':
        raise trial.TrialError(','.join(checked['reasons']))
    cmd_sib = [
        '--discovery', str(disc_path), '--preselection', str(prepath),
        '--fixture-id', str(fixture['fixture_id']), '--reuse-existing',
        '--root', str(root), '--min-lead-minutes', str(args.min_lead),
        '--output-dir', str(dest)
    ]
    now_arg = getattr(args, 'now_utc', None) or getattr(args, 'asof', None)
    if now_arg:
        cmd_sib.extend(['--now-utc', str(now_arg)])
    trial.run(root, 'bwin_market_preflight.py', cmd_sib, base / 'logs')
    return verified_preflight(root, dest / 'market_preflight.json', fixture, prepath, args)


def resolve_source(root, raw, digest=None):
    from prematch_paths import local_reference
    try:
        return local_reference(root, raw, digest)
    except ValueError as exc:
        raise trial.TrialError(str(exc)) from exc


def stamp():
    return trial.now_madrid().strftime('%Y%m%dT%H%M%S%f')


def reusable_preparation(root, folder, day, args):
    """Revalidate the source chain; legacy preparations need no invented hashes."""
    base = folder / 'prospective_trial'
    if not (base / 'prospective_test_summary.json').is_file():
        raise trial.TrialError('PREPARATION_SCOPE_INCOMPLETE:MISSING_PREPARATION_SUMMARY')
    summary = trial.read(base / 'prospective_test_summary.json')
    methodology = summary.get('preselection', {}).get('methodology', {})
    if not methodology:
        methodology = trial.read(base / 'enriched/preselection.json').get('methodology', {})
    requested_history = getattr(args, 'history', None)
    requested_details = getattr(args, 'details', None)
    if ((requested_history is not None and methodology.get('sample_history_cap') != requested_history) or
            (requested_details is not None and methodology.get('sample_details_cap') != requested_details)):
        raise trial.TrialError('PREPARATION_DEPTH_MISMATCH')
    begin, end = trial.local_window(day, args.start, args.end)
    if (summary.get('day'), summary.get('window_start_madrid'), summary.get('window_end_madrid')) != (
            day.isoformat(), begin.isoformat(), end.isoformat()):
        raise trial.TrialError('PREPARATION_WINDOW_MISMATCH')
    prepath = base / 'enriched/preselection.json'
    pre = trial.read(prepath)
    generated = trial.dt(pre.get('generated_at_utc'))
    if generated is None or not timedelta(0) <= trial.now_madrid() - generated <= timedelta(hours=24):
        raise trial.TrialError('PREPARATION_STALE_OR_UNDATED')
    subset = base / 'fixtures_major_test.json'
    if pre.get('source_filter_sha256') != trial.sha(subset):
        raise trial.TrialError('PRESELECTION_FILTER_SHA_MISMATCH')
    subset_doc = trial.read(subset)
    major = folder / 'fixtures_major.json'
    if trial.read(major).get('source_sha256') != trial.sha(folder/'fixtures.json'):
        raise trial.TrialError('LEAGUE_FILTER_SOURCE_SHA_MISMATCH')
    if subset_doc.get('trial_subset', {}).get('source_sha256') != trial.sha(major):
        raise trial.TrialError('MAJOR_FILTER_SHA_MISMATCH')
    allowed = {str(x['fixture_id']): x for x in subset_doc['fixtures']}
    for f in pre['matches']:
        if str(f['fixture_id']) not in allowed or match_identity(f, allowed[str(f['fixture_id'])]):
            raise trial.TrialError('PREPARATION_FIXTURE_IDENTITY_MISMATCH')
    reference=trial.execution_reference(args)
    expected=trial.select_fixtures(trial.read(major),getattr(args,'team',None) or [],0,reference,
                                   args.min_lead,getattr(args,'horizon_hours',3) or 0)
    expected_ids={str(f['fixture_id']) for f in expected}
    covered_ids={str(f['fixture_id']) for f in pre['matches']}
    deferred_ids={str(i) for i in summary.get('evaluation_deferred_ids',[])}
    budget=getattr(args,'evaluation_budget',0)
    budget_satisfied=bool(budget and len(expected_ids & covered_ids)>=min(budget,len(expected_ids)))
    if expected_ids-covered_ids and (not (expected_ids-covered_ids).issubset(deferred_ids) or not budget_satisfied):
        raise trial.TrialError('PREPARATION_SCOPE_INCOMPLETE: hay candidatos elegibles sin evaluar para este horizonte/presupuesto')
    disc_deferred={str(i) for i in summary.get('discovery_deferred_ids',[])}
    disc_budget=getattr(args,'discovery_budget',0)
    eligible_pre=expected_ids & covered_ids
    if eligible_pre & disc_deferred and (not disc_budget or len(eligible_pre-disc_deferred)<min(disc_budget,len(eligible_pre))):
        raise trial.TrialError('DISCOVERY_SCOPE_INCOMPLETE: el presupuesto actual permite evaluar pendientes')
    for batch in pre.get('source_batches',[]):
        if trial.sha(resolve_source(root,batch['path'],batch['sha256']))!=batch['sha256'] or trial.sha(resolve_source(root,batch['input'],batch['input_sha256']))!=batch['input_sha256']:
            raise trial.TrialError('ENRICHMENT_BATCH_SHA_MISMATCH')
    pattern = trial.read(base / 'patterns/pattern_engine.json')
    if pattern.get('data_sources', {}).get('preselection_sha256') != trial.sha(prepath):
        raise trial.TrialError('PATTERN_PRESELECTION_SHA_MISMATCH')
    return summary


def verified_preflight(root, path, fixture, prepath, args):
    """Recheck originals, actual TXT and time; a previous success flag is insufficient."""
    now_arg = trial.dt(getattr(args, 'now_utc', None))
    now_ref = now_arg.astimezone(trial.TZ) if now_arg is not None else trial.now_madrid()
    cutoff = trial.dt(fixture.get('analysis_cutoff_utc'))
    if cutoff is None or cutoff > now_ref:
        raise trial.TrialError('FIXTURE_ANALYSIS_CUTOFF_AFTER_NOW_OR_UNDATED')
    doc = trial.read(path)
    matches = [m for m in doc.get('matches', []) if str(m.get('fixture_id')) == str(fixture['fixture_id'])]
    if len(matches) != 1:
        raise trial.TrialError('PREFLIGHT_FIXTURE_NOT_UNIQUE')
    match = matches[0]
    if match.get('snapshot', {}).get('snapshot_path'):
        snapshot = match['snapshot']
        snapshot['snapshot_path'] = str(resolve_source(root, snapshot['snapshot_path'], snapshot.get('sha256')))
    discovery_path = resolve_source(root, doc.get('source_discovery'), doc.get('source_sha256'))
    if trial.sha(discovery_path) != doc.get('source_sha256'):
        raise trial.TrialError('DISCOVERY_SHA_MISMATCH')
    discovery = trial.read(discovery_path)
    events = [m for m in discovery.get('matches', []) if str(m.get('fixture_id')) == str(fixture['fixture_id'])]
    if len(events) != 1 or match_identity(fixture, events[0]):
        raise trial.TrialError('DISCOVERY_FIXTURE_IDENTITY_MISMATCH')
    event = events[0]
    if any(str(match.get(k)) != str(event.get(k)) for k in ('bwin_event_id', 'bwin_url')):
        raise trial.TrialError('PREFLIGHT_DISCOVERY_EVENT_CONFLICT')
    checked = validate_match(event, discovery.get('catalog_observed', []), now_ref, args.min_lead, 180)
    if checked['status'] != 'READY_FOR_SCRAPER':
        raise trial.TrialError(','.join(checked['reasons']))
    if not match.get('snapshot'):
        scraper_err = (match.get('scraper') or {}).get('error')
        status = match.get('status') or 'SCRAPER_FAILED_OR_UNVERIFIED'
        raise trial.TrialError(f'SCRAPER_CAPTURE_FAILED: {scraper_err or status}')
    reasons, _, _ = validate_snapshot(fixture, match, doc, path, prepath, None,
                                     now_ref, args.max_odds_age, args.min_lead, False, project_root=root)
    if reasons:
        raise trial.TrialError(','.join(reasons))
    snapshot = match.get('snapshot', {})
    expected = root / 'outputs/matches' / str(match['bwin_event_id']) / 'inputs' / f"cuotas_bwin_{match['bwin_event_id']}.txt"
    actual_snapshot = Path(snapshot['snapshot_path']).resolve()
    manifest_exists = (expected.parent.parent / 'manifest.json').exists()
    immutable_root = (Path(path).resolve().parent / 'captures' / str(match['bwin_event_id'])).resolve()
    is_immutable_capture = (
        manifest_exists
        and actual_snapshot.name == expected.name
        and immutable_root in actual_snapshot.parents
        and (match.get('scraper') or {}).get('canonical_unchanged') is True
    )
    if actual_snapshot != expected.resolve() and not is_immutable_capture:
        raise trial.TrialError('SNAPSHOT_EVENT_DIRECTORY_MISMATCH')
    gate = match.get('quality_gate', {})
    if gate.get('join_status') != 'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF':
        raise trial.TrialError('STATISTICAL_JOIN_INVALID')
    # A commercial shortlist is narrower than a verified research market.
    # Keep the identity, quote, SHA and scanner gates; the tactical selector
    # decides later whether a quote can support conditional research.
    # Compare exported market tables with their claimed counts as well.
    usable=0
    for name, count in [('market_selection_review.csv', snapshot['selection_triplets_verified']),
                        ('market_research_shortlist.csv', gate['researchable_after_data_gate'])]:
        with (path.parent / name).open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != count or any(str(r['fixture_id']) != str(fixture['fixture_id']) or
                str(r['bwin_event_id']) != str(match['bwin_event_id']) for r in rows):
            raise trial.TrialError('MARKET_TABLE_COUNT_OR_ID_MISMATCH')
        if name=='market_selection_review.csv':
            usable=sum(r.get('market_kind') not in ('other','combined_or_builder','goalscorers','assists','half_full_time')
                and not is_period(r.get('market',''))
                and not is_player_market(r.get('market',''),r.get('market_kind',''))
                and not any(token in r.get('market','').casefold() for token in ('live','en vivo','en directo','resto del partido','marcador actual','prorroga'))
                and r.get('data_status') not in ('IDENTITY_MISMATCH','NO_STATISTICAL_SOURCE','DETAIL_NOT_EVALUATED')
                for r in rows)
    if not usable:raise trial.TrialError('NO_USABLE_VERIFIED_MARKET')
    match['research_usable_market_selections']=usable
    return match


def discovery_for(root, base, fixture, args, failures):
    """Reuse local evidence, reconcile auto vs manual without silent override, then direct/bounded discovery."""
    fid = fixture['fixture_id']
    paths = sorted((base / 'bwin').rglob('bwin_discovery.json'),
                   key=lambda p: (p.parent.name != 'bwin', str(p)), reverse=True)
    auto_paths = [p for p in paths if not p.parent.name.startswith('manual_')]

    def validate_paths(candidates):
        for path in candidates:
            try:
                doc = trial.read(path)
                matches = [m for m in doc.get('matches', []) if str(m.get('fixture_id')) == str(fid)]
                if len(matches) != 1:
                    continue
                if not matches[0].get('bwin_event_id'):
                    failures.append({'fixture_id':fid,'phase':'discovery_validation','reason':'NOT_FOUND_IN_SCANNED_PAGES',
                                     'offer_proven_absent':False,'note':'No observado en las páginas escaneadas; no demuestra ausencia en Bwin.','path':str(path)})
                    continue
                if match_identity(fixture, matches[0]):
                    raise trial.TrialError('DISCOVERY_FIXTURE_IDENTITY_MISMATCH')
                checked = validate_match(matches[0], doc.get('catalog_observed', []), trial.now_madrid(), args.min_lead, 180)
                if checked['status'] == 'READY_FOR_SCRAPER':
                    return path, matches[0]
                failures.append({'fixture_id': fid, 'phase': 'discovery_validation', 'path': str(path),
                                 'reason': ','.join(checked['reasons'])})
            except (trial.TrialError, OSError, ValueError, KeyError) as exc:
                failures.append({'fixture_id': fid, 'phase': 'discovery_validation', 'reason': str(exc)})
        return None

    cached_auto = validate_paths(auto_paths)

    def _first_auto_with_event():
        for ap in auto_paths:
            try:
                doc = trial.read(ap)
                ms = [m for m in doc.get('matches', []) if str(m.get('fixture_id')) == str(fid) and m.get('bwin_event_id')]
                if len(ms) == 1:
                    return ap, ms[0]
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None, None

    manual_path = base / 'bwin/manual_bwin_links.json'
    if manual_path.is_file():
        from bwin_manual_links import (
            _atomic_json,
            evaluate_observed_manual_identity,
            load_manual_links,
            manual_links_path,
            verify_manual_bwin_links_online,
        )
        store = load_manual_links(base.parent)
        m_links = dict(store.get('links') or {})
        m_entry = m_links.get(str(fid))
        if m_entry:
            manual_eid = str(m_entry.get('extracted_event_id') or '')
            auto_path, auto_row = (cached_auto[0], cached_auto[1]) if cached_auto else _first_auto_with_event()
            if auto_row and auto_row.get('bwin_event_id'):
                auto_eid = str(auto_row.get('bwin_event_id'))
                now_iso = trial.now_madrid().astimezone(timezone.utc).isoformat()
                if auto_eid == manual_eid:
                    if m_entry.get('auto_reconciliation', {}).get('status') != 'AUTO_AND_MANUAL_AGREE':
                        m_entry['auto_reconciliation'] = {
                            'status': 'AUTO_AND_MANUAL_AGREE',
                            'auto_event_id': auto_eid,
                            'manual_event_id': manual_eid,
                            'auto_discovery_path': str(auto_path),
                            'resolution': 'SAME_EVENT_ID_CONFIRMED',
                            'resolved_at_utc': now_iso,
                        }
                        m_links[str(fid)] = m_entry
                        store['links'] = m_links
                        _atomic_json(manual_links_path(base.parent), store)
                else:
                    if cached_auto:
                        failures.append({
                            'fixture_id': fid,
                            'phase': 'discovery_manual_reconciliation',
                            'failure_class': 'DOMAIN',
                            'reason': 'AUTO_VS_MANUAL_EVENT_ID_CONFLICT',
                            'auto_event_id': auto_eid,
                            'manual_event_id': manual_eid,
                            'auto_path': str(auto_path),
                            'resolution': 'ACCREDITED_AUTO_DISCOVERY_REVALIDATED_AND_PRESERVED',
                        })
                        m_entry['identity_status'] = 'BWIN_IDENTITY_CONFLICT'
                        m_entry['conflict_reasons'] = sorted(set((m_entry.get('conflict_reasons') or []) + [
                            f'AUTO_DISCOVERY_ALREADY_VERIFIED_DIFFERENT_EVENT:auto={auto_eid},manual={manual_eid}'
                        ]))
                        m_entry['auto_reconciliation'] = {
                            'status': 'CONFLICT_WITH_ACCREDITED_AUTO',
                            'auto_event_id': auto_eid,
                            'manual_event_id': manual_eid,
                            'auto_discovery_path': str(auto_path),
                            'resolution': 'ACCREDITED_AUTO_DISCOVERY_REVALIDATED_AND_PRESERVED',
                            'resolved_at_utc': now_iso,
                        }
                        m_links[str(fid)] = m_entry
                        store['links'] = m_links
                        _atomic_json(manual_links_path(base.parent), store)
                        return cached_auto
                    failures.append({
                        'fixture_id': fid,
                        'phase': 'discovery_manual_reconciliation',
                        'failure_class': 'DOMAIN',
                        'reason': 'DISCREPANCY_MANUAL_VS_UNVERIFIED_AUTO',
                        'auto_event_id': auto_eid,
                        'manual_event_id': manual_eid,
                        'auto_status': auto_row.get('status'),
                        'auto_path': str(auto_path),
                        'resolution': 'UNVERIFIED_AUTO_NOT_ACCREDITED_REQUIRE_MANUAL_VALIDATION',
                    })
                    m_entry['auto_reconciliation'] = {
                        'status': 'DISCREPANCY_WITH_UNVERIFIED_AUTO',
                        'auto_event_id': auto_eid,
                        'manual_event_id': manual_eid,
                        'auto_status': auto_row.get('status'),
                        'auto_discovery_path': str(auto_path),
                        'resolution': 'UNVERIFIED_AUTO_NOT_ACCREDITED_REQUIRE_MANUAL_VALIDATION',
                        'resolved_at_utc': now_iso,
                    }
                    m_links[str(fid)] = m_entry
                    store['links'] = m_links
                    _atomic_json(manual_links_path(base.parent), store)

            if cached_auto:
                return cached_auto

            m_status = m_entry.get('identity_status')
            if m_status == 'BWIN_IDENTITY_CONFLICT':
                raise trial.TrialError('BWIN_MANUAL_IDENTITY_CONFLICT:' + ','.join(m_entry.get('conflict_reasons') or ['CONFLICT']))
            if m_status == 'BWIN_MANUAL_LINK_PENDING_VALIDATION':
                if getattr(args, 'confirm_external', False):
                    from bwin_manual_links import MANUAL_BWIN_MIN_LEAD_MINUTES
                    now_arg = trial.dt(getattr(args, 'now_utc', None))
                    verify_manual_bwin_links_online(
                        base.parent,
                        [int(fid)],
                        min_lead_minutes=MANUAL_BWIN_MIN_LEAD_MINUTES,
                        now_utc=now_arg.astimezone(timezone.utc) if now_arg else None,
                    )
                    store = load_manual_links(base.parent)
                    m_entry = (store.get('links') or {}).get(str(fid)) or m_entry
                    m_status = m_entry.get('identity_status')
                    if m_status == 'BWIN_IDENTITY_CONFLICT':
                        raise trial.TrialError('BWIN_MANUAL_IDENTITY_CONFLICT:' + ','.join(m_entry.get('conflict_reasons') or ['CONFLICT']))
                    op_block = m_entry.get('operational_block') or {}
                    if op_block.get('blocked'):
                        raise trial.TrialError(str(op_block.get('reason') or 'PREMATCH_MANUAL_LEAD_WINDOW_EXPIRED'))
                    if m_status == 'BWIN_MANUAL_LINK_PENDING_VALIDATION':
                        raise trial.TrialError('BWIN_MANUAL_LINK_PENDING_VALIDATION:' + ','.join(m_entry.get('pending_reasons') or ['INSUFFICIENT_EVIDENCE']))
                else:
                    raise trial.TrialError('BWIN_MANUAL_LINK_PENDING_VALIDATION')
            if m_status == 'BWIN_MANUAL_LINK_VERIFIED':
                v_disc = ((m_entry.get('verification') or {}).get('discovery_json'))
                if v_disc and Path(v_disc).is_file():
                    v_doc = trial.read(Path(v_disc))
                    v_matches = [m for m in v_doc.get('matches', []) if str(m.get('fixture_id')) == str(fid)]
                    if len(v_matches) == 1:
                        obs = v_matches[0].get('inspection') or {}
                        re_st, re_conf, re_pend, _ = evaluate_observed_manual_identity(fixture, manual_eid, v_matches[0], obs)
                        if re_st != 'BWIN_MANUAL_LINK_VERIFIED':
                            raise trial.TrialError('BWIN_MANUAL_LINK_REVALIDATION_FAILED:' + ','.join(re_conf or re_pend or [re_st]))
                    res = validate_paths([Path(v_disc)])
                    if res:
                        return res
                    raise trial.TrialError('BWIN_MANUAL_LINK_REVALIDATION_FAILED')
    elif cached_auto:
        return cached_auto

    identity_catalog = getattr(args, 'bwin_identity_catalog', None)
    if identity_catalog is None:
        candidate = root / 'outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json'
        identity_catalog = candidate if candidate.is_file() else None
    if identity_catalog:
        from bwin_discovery import verified_identity_candidates
        try:
            bindings, _ = verified_identity_candidates(Path(identity_catalog), [fixture])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append({'fixture_id':fid,'phase':'persisted_identity','failure_class':'TECHNICAL',
                             'reason':str(exc),'path':str(identity_catalog)})
            bindings = {}
        if fid in bindings:
            dest = base / 'bwin' / f'direct_{fid}_{stamp()}'
            try:
                cmd_direct = [
                    '--input', str(base / 'enriched/preselection.json'),
                    '--fixture-id', str(fid), '--headless', '--output-dir', str(dest),
                    '--verified-identity-catalog', str(identity_catalog), '--direct-verified-only',
                    '--no-country-discovery', '--inspection-retries', '0',
                ]
                if getattr(args, 'confirm_external', False):
                    cmd_direct.append('--confirm-external')
                trial.run(root, 'bwin_discovery.py', cmd_direct, base / 'logs')
                result = validate_paths([dest / 'bwin_discovery.json'])
                if result:
                    return result
                raise trial.TrialError('PERSISTED_DIRECT_ROUTE_REVALIDATION_FAILED')
            except trial.TrialError as exc:
                failures.append({'fixture_id':fid,'phase':'direct_identity_revalidation',
                                 'failure_class':'TECHNICAL' if 'terminó con código' in str(exc) else 'DOMAIN',
                                 'attempt':1,'reason':str(exc),'path':str(identity_catalog)})
                raise

    # A shared acquisition has already compared every fixture locally and
    # performed its bounded country extension.  Reopening the complete calendar
    # for this single fixture cannot prove absence and caused the 13-Nov retry
    # storm. Preserve its explicit audit state and leave any further directed
    # search as a separate, budgeted manual operation.
    audit_path=base/'bwin/identity_audit/identity_audit.json'
    if audit_path.is_file():
        audit=trial.read(audit_path)
        rows=[row for row in audit.get('fixtures',[]) if str(row.get('fixture_id'))==str(fid)]
        if len(rows)==1 and rows[0].get('link_status')!='VERIFIED_LINK':
            state=rows[0].get('link_status','SEARCH_INCOMPLETE')
            reasons=','.join(rows[0].get('reason_codes',[]))
            failures.append({'fixture_id':fid,'phase':'shared_catalog_matching','failure_class':'DOMAIN',
                             'reason':state+(':'+reasons if reasons else ''),'path':str(audit_path)})
            raise trial.TrialError(f'SHARED_CATALOG_MATCH_UNRESOLVED:{state}:{reasons}')

    for attempt in range(1, getattr(args, 'discovery_retries', 1) + 1):
        if not trial.kickoff_valid(fixture, trial.now_madrid(), args.min_lead):
            break
        dest = base / 'bwin' / f'retry_{fid}_{stamp()}'
        try:
            cmd_retry = [
                '--input', str(base / 'enriched/preselection.json'),
                '--fixture-id', str(fid), '--headless', '--output-dir', str(dest),
                '--inspection-retries', '0',
            ]
            if getattr(args, 'confirm_external', False):
                cmd_retry.append('--confirm-external')
            trial.run(root, 'bwin_discovery.py', cmd_retry, base / 'logs')
            result = validate_paths([dest / 'bwin_discovery.json'])
            if result:
                return result
        except trial.TrialError as exc:
            failures.append({'fixture_id': fid, 'phase': 'discovery', 'failure_class':'TECHNICAL',
                             'attempt': attempt, 'reason': str(exc)})
    reasons=[f['reason'] for f in failures if str(f.get('fixture_id'))==str(fid) and f.get('phase')=='discovery_validation']
    raise trial.TrialError('DISCOVERY_RETRIES_EXHAUSTED: '+(reasons[-1] if reasons else 'NO_BWIN_EVIDENCE'))


def write_handoff(root, run_dir, ready, failures, scanner=None, min_lead=45, max_odds_age=120):
    now = trial.now_madrid()
    # Final clock check, including time spent preparing files/scanning other fixtures.
    valid = [(f, m, p) for f, m, p in ready if trial.kickoff_valid(f, now, min_lead) and
             trial.dt(m['snapshot']['capture_declared_madrid']) <= now < trial.dt(f['kickoff_madrid'])]
    doc = {'schema_version': '1.0-prematch-handoff', 'generated_madrid': now.isoformat(),
           'status': 'NO_VALID_CANDIDATE', 'actionable_bet': False,
           'requires_pre_kickoff_odds_refresh': True,
           'final_odds_policy': {'capture_within_kickoff_minutes':60,'max_age_minutes':15},
           'ready_to_bet': False,
           # Keep the original preparation gate for auditability, while
           # documenting the narrower gates for later operations.  A handoff
           # can therefore be revalidated/refreshed at T-5 without claiming
           # that its original deep preparation happened that late.
           'minimum_lead_minutes': min_lead,
           'manual_bwin_minimum_lead_minutes': 5,
           'review_minimum_lead_minutes': 5,
           'refresh_minimum_lead_minutes': 5,
           'finish_minimum_lead_minutes': 5,
           'operation_windows': {
               'preparation': {'minimum_lead_minutes': min_lead, 'purpose': 'full discovery/context/preflight'},
               'manual_bwin_bind_verify_scrape': {'minimum_lead_minutes': 5, 'purpose': 'bind, verify online and capture manual Bwin URL before kickoff'},
               'review': {'minimum_lead_minutes': 5, 'purpose': 'revalidate prepared handoff'},
               'refresh_odds': {'minimum_lead_minutes': 5, 'purpose': 'new Bwin capture'},
               'retry_context': {'minimum_lead_minutes': 5, 'purpose': 'new API context without scraper'},
               'finish': {'minimum_lead_minutes': 5, 'purpose': 'validation and engine before kickoff'}
           },
           'max_odds_age_minutes': max_odds_age, 'discard_reasons': failures,
           'scanner': str(scanner) if scanner else None,
           'warning': 'Investigación manual únicamente; sin ventaja, probabilidad calibrada ni apuesta acreditadas.'}
    if valid:
        fixture, match, preflight = valid[0]
        snapshot = match['snapshot']
        gate = match['quality_gate']
        event_dir = root / 'outputs/matches' / str(match['bwin_event_id'])
        context_dir = event_dir / 'research' / run_dir.name
        context_dir.mkdir(parents=True, exist_ok=True)
        identity_path = run_dir.parents[1] / 'fixtures_major_test.json'
        identity = {}
        if identity_path.is_file():
            originals = [f for f in trial.read(identity_path).get('fixtures', []) if str(f.get('fixture_id')) == str(fixture['fixture_id'])]
            if len(originals) == 1:
                identity = originals[0]
        context = context_dir / 'preselection_context.json'
        trial.dump(context, {'schema_version': '1.0-manual-research-context',
                            'coverage': 'PRESELECTION_HISTORY_NOT_FULL_MATCH_CONTEXT',
                            'fixture': fixture, 'api_fixture_identity': identity, 'preflight': str(preflight),
                            'source_preselection': str(run_dir.parents[1] / 'enriched/preselection.json')})
        doc.update(status='READY_FOR_MANUAL_RESEARCH', fixture_id=fixture['fixture_id'],
                   bwin_event_id=match['bwin_event_id'], bwin_url=match['bwin_url'],
                   home=fixture['home'], away=fixture['away'], home_api_id=identity.get('home_id'),
                   away_api_id=identity.get('away_id'), kickoff_madrid=fixture['kickoff_madrid'],
                   minutes_remaining=round((trial.dt(fixture['kickoff_madrid']) - now).total_seconds()/60, 2),
                   capture_madrid=snapshot['capture_declared_madrid'], odds_txt=snapshot['snapshot_path'],
                   odds_sha256=snapshot['sha256'], preflight=str(preflight),
                   market_review=str(preflight.parent / 'market_selection_review.csv'),
                   research_shortlist=str(preflight.parent / 'market_research_shortlist.csv'),
                   market_classification=gate['data_status_counts'], market_kinds=gate['market_kinds'],
                   researchable_selections=gate['researchable_after_data_gate'],
                   coverage_warnings=snapshot.get('warnings', []) + [
                       'PRESELECTION_CONTEXT_ONLY: verificar XI, entrenadores, bajas y árbitro manualmente',
                       'Jugadores, tarjetas, offsides, faltas y combinadas sin modelo validado',
                       'PRECIO_PENDIENTE_DE_VERIFICACION: snapshot, no precio garantizado actual'],
                   context_paths=[str(context)], context_sha256=trial.sha(context),
                   routing_reason='Único evento verificado; no hay ganador comparativo' if len(valid)==1 else
                                  'Prioridad de investigación por región, cobertura y mercados revisables tras el scanner; no implica ventaja financiera',
                   eligible_verified_events=len(valid),
                   next_command=f'"{__import__("sys").executable}" "{root / "prematch_daily.py"}" --review "{run_dir / "handoff_prematch.json"}"')
        brief = context_dir / 'research_brief.md'
        brief.write_text('\n'.join([
            f"# Investigación manual: {fixture['home']}–{fixture['away']}", '',
            f"- Fixture API: {fixture['fixture_id']}; equipos API: {identity.get('home_id')} / {identity.get('away_id')}.",
            f"- Bwin: {match['bwin_event_id']} — {match['bwin_url']}",
            f"- Inicio Madrid: {fixture['kickoff_madrid']}.",
            f"- Captura Madrid: {snapshot['capture_declared_madrid']}; SHA-256: {snapshot['sha256']}.",
            f"- Contexto estadístico: `{context}`.",
            f"- Mercados clasificados: `{preflight.parent / 'market_selection_review.csv'}`.",
            f"- Filas investigables: `{preflight.parent / 'market_research_shortlist.csv'}`.",
            f"- Scanner: `{scanner}`.", '',
            'El contexto contiene históricos agregados de preselección. No equivale al contexto completo de generate_context.py.',
            'Continuar manualmente con Grok/Gemini para verificar identidad, XI, entrenadores, bajas, sanciones, árbitro y mecanismos tácticos.',
            'Separar hechos confirmados, publicados, probables e inferidos; fechar fuentes y respetar el corte PREMATCH.',
            'No atribuir probabilidades calibradas, EV, ganador comparativo ni apuestas a estos resultados.',
            'Revalidar el handoff con el comando indicado antes de continuar. Los precios son snapshots.',
            '', f"Comando de revisión: `{doc['next_command']}`", ''
        ]), encoding='utf-8')
        doc['research_brief'] = str(brief)
        doc['run_outcome']='RESEARCH_HANDOFF_READY_NOT_A_BET'
    elif any(x.get('failure_class')=='TECHNICAL' for x in failures):
        doc['status'] = 'TECHNICAL_FAILURE'
        doc['run_outcome'] = 'TECHNICAL_FAILURE_REQUIRES_RETRY_OR_FIX'
    elif failures and any(x.get('phase') not in ('temporal',) for x in failures):
        doc['status'] = 'INSUFFICIENT_EVIDENCE'
        doc['run_outcome'] = 'NO_BET_LEGITIMATE'
    else:
        doc['run_outcome'] = 'NO_ELIGIBLE_FIXTURE_IN_WINDOW'
    run_dir.mkdir(parents=True, exist_ok=True)
    trial.dump(run_dir / 'handoff_prematch.json', doc)
    return doc


def _copy_for_audit(source, destination):
    source=Path(source)
    if not source.is_file():
        return None
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,destination)
    return destination


def _audit_hashes(folder):
    records=[]
    for path in sorted(Path(folder).rglob('*')):
        if path.is_file() and path.name not in ('MANIFEST.json','prematch_comparison_v2.zip'):
            records.append({'path':str(path.relative_to(folder)),'sha256':trial.sha(path),'bytes':path.stat().st_size})
    return records


def _comparison_report(tactical, comparison_ready, doc, scanner_doc):
    ranks={str(row['fixture_id']):row for row in tactical.get('ranking',[])}
    audits={str(row.get('fixture_id')):row for row in (scanner_doc or {}).get('audits',[])}
    status='EVIDENCE_COMPARISON_COMPLETE' if len(comparison_ready)>=3 and sum(ranks.get(str(f['fixture_id']),{}).get('eligible',False) for f,_,_ in comparison_ready)>=3 else 'COMPARISON_INCOMPLETE'
    lines=['# Comparativa PREMATCH: hasta tres partidos','',
           f'- Estado: **{status}**. Eventos con captura, identidad, SHA y scanner verificados: {len(comparison_ready)}.',
           '- Los scores son prioridades descriptivas de investigación. No son probabilidad, EV, cuota justa ni instrucción de apuesta.','',
           '| Partido | Fixture API | Event ID Bwin | Kickoff Madrid | Muestra efectiva | Tactical Research Score | Market Readiness | Señales sustentadas | Señales contradictorias | Familias investigables | Cuotas capturadas | Hipótesis directas | Hipótesis indirectas | Mercados sin modelo | Scanner | Resultado | Razón |',
           '| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |']
    selected=str(doc.get('fixture_id') or '')
    ordered=sorted(comparison_ready,key=lambda item:-ranks.get(str(item[0]['fixture_id']),{}).get('research_priority',-1))
    for fixture,match,_ in ordered:
        fid=str(fixture['fixture_id']);x=tactical['fixtures'][fid];rank=ranks.get(fid,{})
        cover=x.get('coverage_summary',{})
        sample=f"{cover.get('metric_observations_valid','?')}/{cover.get('historical_matches_detailed','?')} detallados de {cover.get('historical_matches_available','?')} históricos"
        result='SELECCIONADO_PARA_INVESTIGACION_MANUAL' if fid==selected else 'EVALUADO_NO_SELECCIONADO'
        reason=doc.get('routing_reason','') if fid==selected else ('Alternativa comparada: menor prioridad conjunta o limitación operativa frente al seleccionado.' if selected else 'No se alcanzó una selección con evidencia y disponibilidad suficientes.')
        lines.append('| '+' | '.join(str(v).replace('|','/') for v in [
            f"{fixture['home']}–{fixture['away']}",fid,match.get('bwin_event_id',''),fixture.get('kickoff_madrid',''),sample,
            rank.get('tactical_research_score',''),rank.get('market_execution_readiness',''),x.get('signal_count',''),
            ', '.join(x.get('contradictions',[])) or 'ninguna detectada',', '.join(x.get('signal_families',[])) or 'ninguna',
            match.get('snapshot',{}).get('selection_triplets_verified',''),rank.get('direct_market_hypotheses',''),rank.get('indirect_market_hypotheses',''),
            rank.get('unmodelled_market_count',''),audits.get(fid,{}).get('status','NO_EVALUADO'),result,reason])+' |')
    lines.append('')
    if status=='COMPARISON_INCOMPLETE':
        lines += ['## Comparación incompleta','',f'Solo se validaron {len(comparison_ready)} capturas distintas dentro de la ventana vigente. No se añadieron cuotas históricas para completar artificialmente tres casos.','']
    alternative=next((row for row in ordered if str(row[0]['fixture_id'])!=selected),None)
    if selected:
        winner=ranks.get(selected,{})
        lines += ['## Selección comparativa','',
                  f"Ganador: fixture {selected}. Tactical Research Score {winner.get('tactical_research_score')}; Market Execution Readiness {winner.get('market_execution_readiness')}; orden conjunto {winner.get('research_priority')}.",
                  f"Alternativa principal: {alternative[0]['fixture_id']} ({alternative[0]['home']}–{alternative[0]['away']})." if alternative else 'No hubo alternativa con captura validada.',
                  f"La selección {'no ' if len(comparison_ready)>=2 else ''}depende exclusivamente de ser el único evento capturado.",
                  'Los mercados directos son coincidencias de equipo, métrica y tiempo reglamentario. Los indirectos de equipo a total de partido siguen requiriendo un modelo conjunto. Ninguno acredita una ventaja económica.', '']
    for fixture,match,_ in ordered:
        fid=str(fixture['fixture_id']);x=tactical['fixtures'][fid];rank=ranks.get(fid,{})
        lines += [f"## {fixture['home']}–{fixture['away']} ({fid})",'']
        supported=sorted((signal for signal in x['signals'] if signal['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL'),key=lambda signal:-signal['priority_score'])[:3]
        if not supported:
            lines.append('- No hay tres señales sustentadas; la debilidad es estadística, no un descarte de valor.')
        for signal in supported:
            own=signal['own'];opp=signal['opponent_conceded']
            lines.append(f"- **{signal['metric']} / {signal['team_side']}**: producción {own['mean']} (n={own['metric_observations_valid']}/{own['historical_matches_available']}) y concesión rival {opp['mean']} (n={opp['metric_observations_valid']}/{opp['historical_matches_available']}); CV {own['cv']}/{opp['cv']}; score descriptivo {signal['priority_score']}.")
            links=signal.get('markets',[])[:3]
            if links:
                for link in links:
                    lines.append(f"  - Bwin observado: `{link['market']}` — `{link['selection']}` @ {link['odd']} ({link['association_category']}; {link['execution_status']}).")
            else:
                lines.append('  - Sin mercado exacto verificado para esta señal en el snapshot observado.')
        lines += [f"- Contradicciones: {', '.join(x.get('contradictions',[])) or 'ninguna detectada por las reglas actuales'}.",
                  f"- Cobertura mínima conservadora: {x.get('coverage_summary',{})}. Los datos no detallados no se tratan como cero.",
                  f"- Scanner: {audits.get(fid,{}).get('status','NO_EVALUADO')}; directo={rank.get('direct_market_hypotheses',0)}, indirecto={rank.get('indirect_market_hypotheses',0)}, sin modelo={rank.get('unmodelled_market_count',0)}.",
                  '- Para estimar una probabilidad falta un modelo calibrado y, según el mercado, información de XI, exposición de jugador, rival, entrenador y contexto.','']
    return '\n'.join(lines)+'\n',status


def write_comparison_audit(root, base, run_dir, tactical, comparison_ready, doc, scanner_path, args):
    """Create a self-contained, copy-only audit package for the actual run."""
    root=Path(root);base=Path(base);out=root/'outputs/engineering/prematch_comparison_v2'
    if (out/'MANIFEST.json').exists():out=run_dir/'comparison_audit'
    out.mkdir(parents=True,exist_ok=True)
    scanner_doc=trial.read(scanner_path) if scanner_path and Path(scanner_path).is_file() else {}
    report,status=_comparison_report(tactical,comparison_ready,doc,scanner_doc)
    (out/'COMPARATIVA_PREMATCH_3_PARTIDOS.md').write_text(report,encoding='utf-8')
    lines=['# Resumen de ejecución PREMATCH Comparison V2','',
           f'- Run: `{run_dir.name}`.',f'- Estado comparativo: `{status}`.',
           f'- Capturas verificadas distintas: {len(comparison_ready)} de objetivo 3.',
           '- No se ejecutaron Grok, Gemini ni análisis final. Las cuotas siguen sujetas a la revisión final de 60/15 minutos.','',
           '## Artefactos y procedencia','']
    _copy_for_audit(base/'partidos_diarios.csv',out/'partidos_diarios.csv')
    _copy_for_audit(run_dir/'handoff_prematch.json',out/'handoff_prematch.json')
    _copy_for_audit(run_dir/'candidate_ledger.json',out/'candidate_ledger.json')
    _copy_for_audit(scanner_path,out/'opportunity_scan.json') if scanner_path else None
    _copy_for_audit(base/'enriched/preselection.json',out/'sources/preselection.json')
    _copy_for_audit(base/'patterns/pattern_engine.json',out/'sources/pattern_engine.json')
    _copy_for_audit(base/'logs/pipeline_stage_timings.jsonl',out/'logs/pipeline_stage_timings.jsonl')
    budget={'strategy':'RUNTIME_CAPTURE_CAP_COUNTS_ONLY_VERIFIED_READY_FIXTURES',
            'max_scrapes':getattr(args,'max_scrapes',0),'evaluation_budget':getattr(args,'evaluation_budget',0),
            'discovery_budget':getattr(args,'discovery_budget',0),
            'bwin_top_batch_size':getattr(args,'bwin_top',None),
            'captured_verified_fixture_ids':[f['fixture_id'] for f,_,_ in comparison_ready],
            'comparison_status':status}
    trial.dump(out/'budget_assignment.json',budget)
    for fixture,match,preflight in comparison_ready:
        fid=str(fixture['fixture_id']);detail=Path(tactical['fixtures'][fid]['detail_json'])
        _copy_for_audit(detail,out/'tactical'/f'{fid}.json')
        _copy_for_audit(preflight,out/'preflight'/f'{fid}_market_preflight.json')
        snapshot=Path(match.get('snapshot',{}).get('snapshot_path',''))
        _copy_for_audit(snapshot,out/'snapshots'/snapshot.name)
        source=Path(trial.read(preflight).get('source_discovery',''))
        _copy_for_audit(source,out/'discovery'/f'{fid}_bwin_discovery.json')
        lines.append(f'- Fixture {fid}: Bwin {match.get("bwin_event_id")}; captura {match.get("snapshot",{}).get("capture_declared_madrid")}; preflight `{preflight}`.')
    lines += ['', 'La comparación usa solamente snapshots conservados y hashes en `MANIFEST.json`.']
    (out/'RESUMEN_EJECUCION.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    verification={'comparison_status':status,'captured_verified_count':len(comparison_ready),
                  'capture_target_reached':len(comparison_ready)>=3,
                  'eligible_candidate_target_reached':sum(r.get('eligible',False) for r in tactical.get('ranking',[]))>=3,
                  'snapshot_comparison_status':'SNAPSHOT_COMPARISON_COMPLETE' if len(comparison_ready)>=3 else 'COMPARISON_INCOMPLETE',
                  'evidence_comparison_status':status,
                  'fixture_ids':[f['fixture_id'] for f,_,_ in comparison_ready],
                  'api_calls_new':0,'bwin_captures_verified':len(comparison_ready),
                  'no_calibrated_probability_or_ev':True,
                  'preflight_and_scanner_required':True}
    trial.dump(out/'verification_results.json',verification)
    manifest={'schema_version':'1.0-prematch-comparison-audit','generated_madrid':trial.now_madrid().isoformat(),
              'capture_target_reached':verification['capture_target_reached'],
              'eligible_candidate_target_reached':verification['eligible_candidate_target_reached'],
              'snapshot_comparison_status':verification['snapshot_comparison_status'],
              'evidence_comparison_status':status,
              'run_directory':str(run_dir),'comparison_status':status,'fixture_ids':[f['fixture_id'] for f,_,_ in comparison_ready],
              'artifacts':_audit_hashes(out)}
    trial.dump(out/'MANIFEST.json',manifest)
    zip_path=out.with_suffix('.zip')
    with zipfile.ZipFile(zip_path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out.rglob('*')):
            if path.is_file():archive.write(path,path.relative_to(out.parent))
    return out,status,zip_path


def scan(root, folder, day, args):
    scan_started=time.perf_counter();phase_timings={}
    reference=trial.execution_reference(args)
    args.max_odds_age = getattr(args, 'max_odds_age', 120)
    args.discovery_retries = getattr(args, 'discovery_retries', 1)
    from prematch_audit import CandidateLedger, failure_state
    base = folder / 'prospective_trial'
    ledger=CandidateLedger.load(base/'candidate_ledger.json')
    phase_started=time.perf_counter();prepared=reusable_preparation(root, folder, day, args)
    phase_timings['reusable_preparation']=round(time.perf_counter()-phase_started,4)
    discovery_deferred={str(i) for i in prepared.get('discovery_deferred_ids',[])}
    run_dir = base / 'daily_runs' / stamp()
    prepath = base / 'enriched/preselection.json'
    fixtures = trial.read(prepath)['matches']
    raw_path=folder/'fixtures.json';major_path=folder/'fixtures_major.json'
    if raw_path.is_file():
        raw=trial.read(raw_path)
        for f in raw.get('fixture_audit',raw.get('fixtures',[])):
            if str(f['fixture_id']) not in ledger.rows:
                ledger.record(f,'DESCUBIERTO','discovery','LOCAL_API_SNAPSHOT',str(raw_path))
            if f.get('discovery_exclusion'):
                ledger.record(f,'EXCLUIDO_POR_HORARIO','discovery',f['discovery_exclusion'])
    if major_path.is_file():
        major=trial.read(major_path)
        for f in major.get('excluded',[])+major.get('review',[]):
            ledger.record(f,'FILTRADO_POR_LIGA','league_filter',f.get('filter_reason'))
        for f in major.get('fixtures',[]):
            reason=trial.scope_reason(f,reference,args.min_lead,getattr(args,'horizon_hours',3))
            if reason:ledger.record(f,'EXCLUIDO_POR_HORARIO','scope',reason)
    from prematch_tactical_signal_engine import run_engine,diversified_order
    phase_started=time.perf_counter();tactical_pre=run_engine(prepath,base/'patterns/pattern_engine.json' if (base/'patterns/pattern_engine.json').is_file() else None,
        base/'tactical',root,profiles=root/'config/tactical_profiles/profiles.json')
    phase_timings['statistical_ranking_precommercial']=round(time.perf_counter()-phase_started,4)
    phase_started=time.perf_counter()
    fixtures=diversified_order(fixtures,{fid:x['priority_score'] for fid,x in tactical_pre['fixtures'].items()})
    failures, ready = [], []
    from bwin_manual_links import QUEUE_SCHEMA, load_manual_links, manual_links_path, resolve_bwin_search_status
    manual_links_map = dict(load_manual_links(folder).get('links') or {})
    preflights = list((base / 'preflight').rglob('market_preflight.json'))
    preflights += list((base / 'daily_runs').rglob('market_preflight.json'))
    for fixture in fixtures:
        fid = fixture['fixture_id']
        if str(fid) not in ledger.rows:
            ledger.record(fixture,'DESCUBIERTO','preselection','EXISTING_PRESELECTION')
        if str(fid) in discovery_deferred:
            ledger.record(fixture,'DIFERIDO_POR_PRESUPUESTO','bwin_discovery','DISCOVERY_BUDGET_NOT_ANALYZED')
            continue
        reason=trial.scope_reason(fixture,reference,args.min_lead,getattr(args,'horizon_hours',3))
        if reason:
            ledger.record(fixture,'EXCLUIDO_POR_HORARIO','scope',reason)
            failures.append({'fixture_id': fid, 'phase': 'temporal', 'reason': reason})
            continue
        if ('data_readiness_score' in fixture and fixture['data_readiness_score'] is None) or not fixture.get('details_checked',True) or fixture.get('data_readiness_status') in ('EVALUACION_INCOMPLETA','PENDIENTE_DETALLE') or fixture.get('evidence_tier') in ('NO_STATISTICAL_DATA','BELOW_MINIMUM_EVIDENCE'):
            ledger.record(fixture,'SIN_COBERTURA','preselection',fixture.get('evidence_tier') or fixture.get('data_readiness_status'))
            failures.append({'fixture_id':fid,'phase':'coverage','reason':'INSUFFICIENT_STATISTICAL_COVERAGE'})
            continue
        if args.max_scrapes and len(ready) >= args.max_scrapes:
            ledger.record(fixture,'DIFERIDO_POR_PRESUPUESTO','capture','MAX_SCRAPES_BUDGET_NOT_ANALYZED')
            continue
        reused = False
        for path in preflights:
            # Avoid adding spurious failures for other fixture preflights.
            try:
                if not any(str(m.get('fixture_id')) == str(fid) for m in trial.read(path).get('matches', [])):
                    continue
                match = verified_preflight(root, path, fixture, prepath, args)
                ready.append((fixture, match, path)); reused = True
                print(f'[{fid}] Snapshot y preflight revalidados; sin API ni captura nueva.', flush=True)
                break
            except (trial.TrialError, OSError, ValueError, KeyError, TypeError) as exc:
                ledger.record(fixture,failure_state(exc),'reuse',str(exc),str(path))
                failures.append({'fixture_id': fid, 'phase': 'reuse', 'path': str(path), 'reason': str(exc)})
        if reused:
            continue
        sibling_used = False
        for sib_idx, sib_path in enumerate(_sibling_preflights(folder, base)):
            try:
                if not any(str(m.get('fixture_id')) == str(fid) for m in trial.read(sib_path).get('matches', [])):
                    continue
                dest = run_dir / 'preflight' / f'{fid}_reuse_{sib_idx}'
                match = _rebind_sibling_preflight(root, base, dest, sib_path, fixture, prepath, args)
                ready.append((fixture, match, dest / 'market_preflight.json'))
                sibling_used = True
                print(f'[{fid}] Snapshot fresco de evaluación previa reauditado y vinculado; sin scraping nuevo.', flush=True)
                break
            except (trial.TrialError, OSError, ValueError, KeyError, TypeError) as exc:
                ledger.record(fixture, failure_state(exc), 'sibling_reuse', str(exc), str(sib_path))
                failures.append({'fixture_id': fid, 'phase': 'sibling_reuse', 'path': str(sib_path), 'reason': str(exc)})
        if sibling_used:
            continue
        try:
            path, event = discovery_for(root, base, fixture, args, failures)
            eid = str(event.get('bwin_event_id') or '')
            target = root / 'outputs/matches' / eid / 'inputs' / f'cuotas_bwin_{eid}.txt'
            if target.exists():
                cls, detail = classify_existing_snapshot(
                    target, eid, str(event.get('bwin_url') or ''), trial.now_madrid(), args.max_odds_age
                )
                if not getattr(args, 'refresh_existing_odds', False):
                    if cls == 'REUSABLE_EXISTING_SNAPSHOT':
                        # Only reuse via --reuse-existing if discovery catalog observation isn't newer than TXT capture.
                        obs_dt = trial.dt((event.get('time_evidence') or {}).get('observed_at_utc') or (event.get('inspection') or {}).get('observed_at_utc'))
                        cap_dt = trial.dt(detail)
                        if obs_dt and cap_dt and obs_dt <= cap_dt + timedelta(minutes=2):
                            dest = run_dir / 'preflight' / f'{fid}_reuse_local'
                            cmd_reuse = [
                                '--discovery', str(path), '--preselection', str(prepath),
                                '--fixture-id', str(fid), '--reuse-existing', '--root', str(root),
                                '--min-lead-minutes', str(args.min_lead), '--output-dir', str(dest)
                            ]
                            now_arg = getattr(args, 'now_utc', None) or getattr(args, 'asof', None)
                            if now_arg:
                                cmd_reuse.extend(['--now-utc', str(now_arg)])
                            trial.run(root, 'bwin_market_preflight.py', cmd_reuse, base / 'logs')
                            match = verified_preflight(root, dest / 'market_preflight.json', fixture, prepath, args)
                            ready.append((fixture, match, dest / 'market_preflight.json'))
                            continue
                        raise trial.TrialError(f'CROSS_RUN_SNAPSHOT_WITHOUT_PREFLIGHT ({detail}): preservado; usar --refresh-existing-odds para recapturar')
                    raise trial.TrialError(f'{cls}: {detail}; preservado, usar --refresh-existing-odds para recapturar')
            capture_attempts=max(1,args.discovery_retries)
            for attempt in range(capture_attempts):
                dest = run_dir / 'preflight' / f'{fid}_{attempt}'
                try:
                    has_manifest = bool(eid) and (root / 'outputs/matches' / eid / 'manifest.json').exists()
                    if getattr(args, 'refresh_existing_odds', False) and target.exists():
                        refresh_args = ['--refresh-existing', '--immutable-capture'] if has_manifest else ['--refresh-existing']
                    else:
                        refresh_args = []
                    cmd_preflight = [
                        '--discovery', str(path), '--preselection', str(prepath),
                        '--fixture-id', str(fid), '--run-scraper', '--root', str(root),
                        '--min-lead-minutes', str(args.min_lead), '--output-dir', str(dest), *refresh_args,
                    ]
                    now_arg = getattr(args, 'now_utc', None) or getattr(args, 'asof', None)
                    if now_arg:
                        cmd_preflight.extend(['--now-utc', str(now_arg)])
                    if getattr(args, 'confirm_external', False):
                        cmd_preflight.append('--confirm-external')
                    trial.run(root, 'bwin_market_preflight.py', cmd_preflight, base / 'logs')
                    match = verified_preflight(root, dest / 'market_preflight.json', fixture, prepath, args)
                    ready.append((fixture, match, dest / 'market_preflight.json'))
                    if str(fid) in manual_links_map:
                        store = load_manual_links(base.parent)
                        mentry = (store.get('links') or {}).get(str(fid))
                        if isinstance(mentry, dict):
                            mentry['quote_capture_status'] = 'CAPTURED_VERIFIED'
                            mentry['operational_status'] = 'CLEAR'
                            mentry['operational_block'] = None
                            mentry['last_preflight'] = str((dest / 'market_preflight.json').resolve())
                            trial.dump(manual_links_path(base.parent), store)
                            manual_links_map[str(fid)] = mentry
                    break
                except (trial.TrialError, OSError, ValueError, KeyError) as exc:
                    ledger.record(fixture,failure_state(exc),'capture',str(exc))
                    failures.append({'fixture_id': fid, 'phase': 'capture', 'attempt': attempt,
                                     'failure_class':'TECHNICAL' if 'terminó con código' in str(exc) else 'DOMAIN',
                                     'reason': str(exc)})
                    if target.exists() or attempt == capture_attempts-1:
                        break
                    # Reinspect only this event before retrying, with all identity/time gates.
                    path, event = discovery_for(root, base, fixture, args, failures)
        except (trial.TrialError, OSError, ValueError, KeyError, TypeError) as exc:
            ledger.record(fixture,failure_state(exc),'capture',str(exc))
            failures.append({'fixture_id': fid, 'phase': 'capture',
                             'failure_class':'TECHNICAL' if 'terminó con código' in str(exc) else 'DOMAIN',
                             'reason': str(exc)})
    phase_timings['identity_discovery_and_capture']=round(time.perf_counter()-phase_started,4)
    scanner_path = None
    phase_started=time.perf_counter()
    if ready:
        out = run_dir / 'opportunity'
        cmd = ['--preselection', str(prepath), '--pattern', str(base / 'patterns/pattern_engine.json'),
               '--min-lead-minutes', str(args.min_lead), '--max-age-minutes', str(args.max_odds_age), '--output-dir', str(out)]
        now_arg = getattr(args, 'now_utc', None) or getattr(args, 'asof', None)
        if now_arg:
            cmd.extend(['--now-utc', str(now_arg)])
        for _, _, path in ready:
            cmd += ['--preflight', str(path)]
        try:
            trial.run(root, 'prematch_opportunity_scanner.py', cmd, base / 'logs')
            scanner_path = out / 'opportunity_scan.json'
            report = trial.read(scanner_path)
            ids = {str(a['fixture_id']) for a in report['audits'] if a['status']=='VERIFIED'}
            for fixture,_,_ in ready:
                if str(fixture['fixture_id']) in ids:
                    ledger.record(fixture,'ANALIZADO','opportunity_scanner','VERIFIED',str(scanner_path))
                else:
                    ledger.record(fixture,'SIN_CUOTAS','opportunity_scanner','SCANNER_REJECTED',str(scanner_path))
            ready = [r for r in ready if str(r[0]['fixture_id']) in ids]
            failures += [{'fixture_id': a['fixture_id'], 'phase': 'scanner', 'reason': ','.join(a['reasons'])}
                         for a in report['audits'] if a['status']!='VERIFIED']
        except (trial.TrialError, OSError, ValueError, KeyError) as exc:
            for fixture,_,_ in ready:
                ledger.record(fixture,'SIN_CUOTAS','opportunity_scanner',str(exc))
            ready = [];scanner_path=None
            failures.append({'phase': 'scanner', 'failure_class':'TECHNICAL', 'reason': str(exc)})
    phase_timings['commercial_scanner']=round(time.perf_counter()-phase_started,4)
    # Enlace comercial sidecar: nunca participa en tiers ni en write_handoff.
    sport_queue=base/'patterns_v2/research_queue_sport.json'
    if sport_queue.is_file():
        from prematch_pattern_discovery import commercial_link
        commercial_link(sport_queue,scanner_path,base/'bwin/bwin_discovery.json',run_dir/'patterns_v2')
    # Revalidate after subprocesses and before publishing the final handoff.
    final_ready = []
    for fixture, _, path in ready:
        try:
            final_ready.append((fixture, verified_preflight(root, path, fixture, prepath, args), path))
        except (trial.TrialError, OSError, ValueError, KeyError) as exc:
            ledger.record(fixture,failure_state(exc),'final_validation',str(exc))
            failures.append({'fixture_id': fixture['fixture_id'], 'phase': 'final_validation', 'reason': str(exc)})
    # Keep every independently captured and scanner-verified fixture for the
    # comparative audit, even if its tactical evidence later prevents selection.
    comparison_ready=list(final_ready)
    phase_started=time.perf_counter();tactical=run_engine(prepath,base/'patterns/pattern_engine.json' if (base/'patterns/pattern_engine.json').is_file() else None,
        run_dir/'tactical',root,preflights=[path for _,_,path in final_ready],scanner=scanner_path,
        profiles=root/'config/tactical_profiles/profiles.json')
    phase_timings['final_statistical_and_commercial_views']=round(time.perf_counter()-phase_started,4)
    (base/'tactical').mkdir(parents=True,exist_ok=True)
    for name in ('tactical_signal_engine.json','TACTICAL_SELECTION_REPORT.md'):
        shutil.copy2(run_dir/'tactical'/name,base/'tactical'/name)
    rank={str(r['fixture_id']):r for r in tactical['ranking']}
    qualified=[]
    for fixture,match,path in final_ready:
        row=rank.get(str(fixture['fixture_id']),{})
        if row.get('research_eligible'):qualified.append((fixture,match,path))
        else:
            failures.append({'fixture_id':fixture['fixture_id'],'phase':'tactical_selection','reason':','.join(row.get('hard_block_reasons',['TACTICAL_ENGINE_UNAVAILABLE']))})
    qualified.sort(key=lambda item:({'A':0,'B':1}[rank[str(item[0]['fixture_id'])]['selection_tier']],
                                    -rank[str(item[0]['fixture_id'])]['research_priority'],str(item[0]['fixture_id'])))
    final_ready=qualified
    doc = write_handoff(root, run_dir, final_ready, failures, scanner_path, args.min_lead, args.max_odds_age)
    if doc.get('fixture_id') is not None:
        selected=next(f for f,_,_ in final_ready if str(f['fixture_id'])==str(doc['fixture_id']))
        ledger.record(selected,'SELECCIONADO','handoff','MANUAL_RESEARCH_ONLY',str(run_dir/'handoff_prematch.json'))
        chosen=rank[str(doc['fixture_id'])]
        doc.update(selection_tier=chosen['selection_tier'],research_eligible=True,
                   eligible=chosen['eligible'],research_missing_evidence=chosen['research_missing_evidence'],
                   hard_block_reasons=chosen['hard_block_reasons'],research_evidence=chosen['research_evidence'])
        if chosen['selection_tier']=='B':
            doc['coverage_warnings']+=['NIVEL_B: investigación condicionada; carencias: '+', '.join(chosen['research_missing_evidence'])]
    doc['tactical_report']=str(run_dir/'tactical/TACTICAL_SELECTION_REPORT.md')
    if doc.get('fixture_id') is not None:
        detail=tactical['fixtures'][str(doc['fixture_id'])]
        doc['tactical_signal_json']=detail['detail_json']
        doc['tactical_signal_sha256']=detail['detail_sha256']
        brief=Path(doc['research_brief'])
        brief.write_text(brief.read_text(encoding='utf-8')+f"\nNivel {doc['selection_tier']}; elegible comercial actual: {doc['eligible']}; carencias: {doc['research_missing_evidence']}.\nSeñales descriptivas (no EV): `{doc['tactical_signal_json']}`.\nReporte de prioridad: `{doc['tactical_report']}`.\n",encoding='utf-8')
    doc['tactical_selection_ranking']=tactical['ranking']
    doc['statistical_research_ranking']=tactical.get('statistical_ranking',[])
    doc['ranking_separation']=tactical.get('ranking_separation',{})
    verified_fids = {str(f['fixture_id']) for f, _, _ in final_ready}
    manual_links_map = dict(load_manual_links(folder).get('links') or {})
    disc_by_fid = {}
    if (base / 'bwin/bwin_discovery.json').is_file():
        for dm in trial.read(base / 'bwin/bwin_discovery.json').get('matches', []):
            if dm.get('fixture_id') is not None:
                disc_by_fid[str(dm['fixture_id'])] = dm
    reconciliation_queue = []
    for s_row in tactical.get('statistical_ranking', []):
        sfid = str(s_row['fixture_id'])
        lrow = ledger.rows.get(sfid)
        drow = disc_by_fid.get(sfid)
        mrow = manual_links_map.get(sfid)
        b_status = resolve_bwin_search_status(ledger_row=lrow, discovery_row=drow, manual_link=mrow)
        reconciliation_queue.append({
            'fixture_id': s_row['fixture_id'],
            'statistical_position': s_row.get('position'),
            'statistical_research_score': s_row.get('statistical_research_score'),
            'home': s_row['home'],
            'away': s_row['away'],
            'bwin_search_status': b_status,
            'commercial_verified': sfid in verified_fids,
            'offer_proven_absent': False,
            'manual_url_provided': (mrow or {}).get('bwin_url'),
            'extracted_event_id': (mrow or {}).get('extracted_event_id') or (drow or {}).get('bwin_event_id'),
            'quote_capture_status': 'CAPTURED_VERIFIED' if sfid in verified_fids else ((mrow or {}).get('quote_capture_status') or 'NOT_CAPTURED'),
            'bwin_ledger_state': (lrow or {}).get('state'),
            'bwin_reason': (lrow or {}).get('reason'),
        })
    trial.dump(base / 'bwin/bwin_reconciliation_queue.json', {
        'schema_version': QUEUE_SCHEMA,
        'generated_madrid': trial.now_madrid().isoformat(),
        'offer_proven_absent_policy': 'NEVER_INFERS_ABSENCE_FROM_INCOMPLETE_OR_BUDGETED_SEARCH',
        'queue': reconciliation_queue,
    })
    doc['bwin_reconciliation_queue'] = str(base / 'bwin/bwin_reconciliation_queue.json')
    doc['research_pending_candidates'] = [
        {
            'fixture_id': s_row['fixture_id'],
            'home': s_row['home'],
            'away': s_row['away'],
            'statistical_research_score': s_row.get('statistical_research_score'),
            'signal_count': s_row.get('signal_count', 0),
            'signal_families': s_row.get('signal_families', []),
            'research_status': 'RESEARCH_PENDING',
            'commercial_verified': False,
            'bwin_search_status': resolve_bwin_search_status(
                ledger_row=ledger.rows.get(str(s_row['fixture_id'])),
                discovery_row=disc_by_fid.get(str(s_row['fixture_id'])),
                manual_link=manual_links_map.get(str(s_row['fixture_id'])),
            ),
            'bwin_ledger_state': (ledger.rows.get(str(s_row['fixture_id'])) or {}).get('state'),
            'bwin_reason': (ledger.rows.get(str(s_row['fixture_id'])) or {}).get('reason'),
            'offer_proven_absent': False,
        }
        for s_row in tactical.get('statistical_ranking', [])
        if str(s_row['fixture_id']) not in verified_fids
        and (s_row.get('signal_count', 0) > 0 or (s_row.get('statistical_research_score') or 0) >= 50)
        and (ledger.rows.get(str(s_row['fixture_id'])) or {}).get('state') not in {'IDENTIDAD_INCORRECTA', 'EXCLUIDO_POR_HORARIO'}
    ]
    identity_audit_path=base/'bwin/identity_audit/identity_audit.json'
    if identity_audit_path.is_file():
        identity_audit=trial.read(identity_audit_path)
        doc['identity_coverage_audit']={'path':str(identity_audit_path.resolve()),
            'sha256':trial.sha(identity_audit_path),'schema_version':identity_audit.get('schema_version'),
            'metrics':identity_audit.get('metrics',{}),'network_used':identity_audit.get('network_used')}
    if doc.get('fixture_id') is not None:
        doc['routing_reason']='Prioridad conjunta de señal, cobertura por familia, diversidad, identidad, margen y scanner; ver ranking y reporte táctico. No representa valor financiero.'
    doc['horizon_hours']=getattr(args,'horizon_hours',3)
    doc['candidate_ledger']=str(run_dir/'candidate_ledger.json')
    doc['comparison_incomplete_due_to_budget']=any(r['state']=='DIFERIDO_POR_PRESUPUESTO' for r in ledger.rows.values())
    doc['capture_target_reached']=len(comparison_ready)>=3
    doc['eligible_candidate_target_reached']=sum(r.get('eligible',False) for r in tactical['ranking'])>=3
    doc['research_candidate_target_reached']=len(qualified)>=3
    doc['snapshot_comparison_status']='SNAPSHOT_COMPARISON_COMPLETE' if doc['capture_target_reached'] else 'COMPARISON_INCOMPLETE'
    doc['evidence_comparison_status']='EVIDENCE_COMPARISON_COMPLETE' if doc['eligible_candidate_target_reached'] else 'COMPARISON_INCOMPLETE'
    doc['comparison_status']=doc['evidence_comparison_status']
    doc['comparison_verified_events']=len(comparison_ready)
    phase_timings['total']=round(time.perf_counter()-scan_started,4)
    phase_timings['discovery_retry_directories_in_window']=len(list((base/'bwin').glob('retry_*')))
    phase_timings['direct_identity_directories_in_window']=len(list((base/'bwin').glob('direct_*')))
    trial.dump(run_dir/'phase_metrics.json',phase_timings)
    doc['phase_metrics']=phase_timings
    doc['phase_metrics_path']=str(run_dir/'phase_metrics.json')
    evaluation_path=folder/'evaluation_manifest.json'
    if evaluation_path.is_file():
        evaluation=trial.evaluation_manifest(folder)
        doc['evaluation_id']=evaluation['evaluation_id']
        doc['evaluation_manifest']=str(evaluation_path)
        doc['evaluation_manifest_sha256']=trial.sha(evaluation_path)
        doc['execution_reference_madrid']=evaluation['reference_madrid']
    doc['comparison_audit']=str(root/'outputs/engineering/prematch_comparison_v2')
    doc['comparison_zip']=str(root/'outputs/engineering/prematch_comparison_v2.zip')
    ledger.write(run_dir/'candidate_ledger.json');ledger.write()
    trial.dump(run_dir/'handoff_prematch.json',doc)
    # Latest pointer contains the complete result, with immutable run outputs retained.
    doc['run_handoff'] = str(run_dir / 'handoff_prematch.json')
    if doc.get('status') == 'READY_FOR_MANUAL_RESEARCH':
        doc['canonical_prepare_command'] = (
            f'"{__import__("sys").executable}" "{root / "prematch_case.py"}" '
            f'prepare --handoff "{run_dir / "handoff_prematch.json"}"')
    trial.dump(base / 'handoff_prematch.json', doc)
    from prematch_daily_csv import export_daily
    export_daily(base)
    audit_folder, comparison_status, audit_zip = write_comparison_audit(root,base,run_dir,tactical,comparison_ready,doc,scanner_path,args)
    doc['comparison_audit']=str(audit_folder);doc['comparison_zip']=str(audit_zip)
    trial.dump(run_dir/'handoff_prematch.json',doc);trial.dump(base/'handoff_prematch.json',doc)
    if comparison_status!=doc['comparison_status']:
        raise trial.TrialError('COMPARISON_STATUS_INCONSISTENT')
    print(f"\n{doc['status']}: {base / 'handoff_prematch.json'}", flush=True)
    return doc
