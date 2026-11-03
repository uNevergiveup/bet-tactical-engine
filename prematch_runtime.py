"""Safe orchestration shared by the trial launcher and the daily entry point.

No final analysis, report fusion, validator finish or bets are executed here.
"""
from __future__ import annotations

import csv
import json
import shutil
import zipfile
from datetime import timedelta, timezone
from pathlib import Path

import prematch_prospective_trial as trial
from bwin_market_preflight import validate_match
from market_quality_gate import is_period, is_player_market
from prematch_opportunity_scanner import match_identity, validate_snapshot


def resolve_source(root, raw):
    path = Path(raw or '__missing__')
    return path if path.is_absolute() else root / path


def stamp():
    return trial.now_madrid().strftime('%Y%m%dT%H%M%S%f')


def reusable_preparation(root, folder, day, args):
    """Revalidate the source chain; legacy preparations need no invented hashes."""
    base = folder / 'prospective_trial'
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
    expected=trial.select_fixtures(trial.read(major),getattr(args,'team',[]),0,trial.now_madrid(),
                                   args.min_lead,getattr(args,'horizon_hours',3))
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
        if trial.sha(resolve_source(root,batch['path']))!=batch['sha256'] or trial.sha(resolve_source(root,batch['input']))!=batch['input_sha256']:
            raise trial.TrialError('ENRICHMENT_BATCH_SHA_MISMATCH')
    pattern = trial.read(base / 'patterns/pattern_engine.json')
    if pattern.get('data_sources', {}).get('preselection_sha256') != trial.sha(prepath):
        raise trial.TrialError('PATTERN_PRESELECTION_SHA_MISMATCH')
    return summary


def verified_preflight(root, path, fixture, prepath, args):
    """Recheck originals, actual TXT and time; a previous success flag is insufficient."""
    cutoff = trial.dt(fixture.get('analysis_cutoff_utc'))
    if cutoff is None or cutoff > trial.now_madrid():
        raise trial.TrialError('FIXTURE_ANALYSIS_CUTOFF_AFTER_NOW_OR_UNDATED')
    doc = trial.read(path)
    matches = [m for m in doc.get('matches', []) if str(m.get('fixture_id')) == str(fixture['fixture_id'])]
    if len(matches) != 1:
        raise trial.TrialError('PREFLIGHT_FIXTURE_NOT_UNIQUE')
    match = matches[0]
    discovery_path = resolve_source(root, doc.get('source_discovery'))
    if trial.sha(discovery_path) != doc.get('source_sha256'):
        raise trial.TrialError('DISCOVERY_SHA_MISMATCH')
    discovery = trial.read(discovery_path)
    events = [m for m in discovery.get('matches', []) if str(m.get('fixture_id')) == str(fixture['fixture_id'])]
    if len(events) != 1 or match_identity(fixture, events[0]):
        raise trial.TrialError('DISCOVERY_FIXTURE_IDENTITY_MISMATCH')
    event = events[0]
    if any(str(match.get(k)) != str(event.get(k)) for k in ('bwin_event_id', 'bwin_url')):
        raise trial.TrialError('PREFLIGHT_DISCOVERY_EVENT_CONFLICT')
    checked = validate_match(event, discovery.get('catalog_observed', []), trial.now_madrid(), args.min_lead, 180)
    if checked['status'] != 'READY_FOR_SCRAPER':
        raise trial.TrialError(','.join(checked['reasons']))
    reasons, _, _ = validate_snapshot(fixture, match, doc, path, prepath, None,
                                     trial.now_madrid(), args.max_odds_age, args.min_lead, False)
    if reasons:
        raise trial.TrialError(','.join(reasons))
    snapshot = match.get('snapshot', {})
    expected = root / 'outputs/matches' / str(match['bwin_event_id']) / 'inputs' / f"cuotas_bwin_{match['bwin_event_id']}.txt"
    if Path(snapshot['snapshot_path']).resolve() != expected.resolve():
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
    """Use a revalidated retry first. Refresh only this fixture, with bounded attempts."""
    fid = fixture['fixture_id']
    paths = sorted((base / 'bwin').rglob('bwin_discovery.json'),
                   key=lambda p: (p.parent.name != 'bwin', str(p)), reverse=True)
    for attempt in range(args.discovery_retries + 1):
        if attempt:
            dest = base / 'bwin' / f'retry_{fid}_{stamp()}'
            try:
                trial.run(root, 'bwin_discovery.py', ['--input', str(base / 'enriched/preselection.json'),
                          '--fixture-id', str(fid), '--headless', '--output-dir', str(dest)], base / 'logs')
                paths = [dest / 'bwin_discovery.json']
            except trial.TrialError as exc:
                failures.append({'fixture_id': fid, 'phase': 'discovery', 'attempt': attempt, 'reason': str(exc)})
                paths = []
        for path in paths:
            try:
                doc = trial.read(path)
                matches = [m for m in doc.get('matches', []) if str(m.get('fixture_id')) == str(fid)]
                if len(matches) != 1:
                    continue
                if not matches[0].get('bwin_event_id'):
                    failures.append({'fixture_id':fid,'phase':'discovery_validation','reason':'NOT_FOUND_IN_SCANNED_PAGES','path':str(path)})
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
        if not trial.kickoff_valid(fixture, trial.now_madrid(), args.min_lead):
            break
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
           'minimum_lead_minutes': min_lead, 'max_odds_age_minutes': max_odds_age, 'discard_reasons': failures,
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
    elif failures and any(x.get('phase') not in ('temporal',) for x in failures):
        doc['status'] = 'INSUFFICIENT_EVIDENCE'
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
    args.max_odds_age = getattr(args, 'max_odds_age', 120)
    args.discovery_retries = getattr(args, 'discovery_retries', 2)
    from prematch_audit import CandidateLedger, failure_state
    base = folder / 'prospective_trial'
    ledger=CandidateLedger.load(base/'candidate_ledger.json')
    prepared=reusable_preparation(root, folder, day, args)
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
            reason=trial.scope_reason(f,trial.now_madrid(),args.min_lead,getattr(args,'horizon_hours',3))
            if reason:ledger.record(f,'EXCLUIDO_POR_HORARIO','scope',reason)
    from prematch_tactical_signal_engine import run_engine,diversified_order
    tactical_pre=run_engine(prepath,base/'patterns/pattern_engine.json' if (base/'patterns/pattern_engine.json').is_file() else None,
        base/'tactical',root,profiles=root/'config/tactical_profiles/profiles.json')
    fixtures=diversified_order(fixtures,{fid:x['priority_score'] for fid,x in tactical_pre['fixtures'].items()})
    failures, ready = [], []
    preflights = list((base / 'preflight').rglob('market_preflight.json'))
    preflights += list((base / 'daily_runs').rglob('market_preflight.json'))
    for fixture in fixtures:
        fid = fixture['fixture_id']
        if str(fid) not in ledger.rows:
            ledger.record(fixture,'DESCUBIERTO','preselection','EXISTING_PRESELECTION')
        if str(fid) in discovery_deferred:
            ledger.record(fixture,'DIFERIDO_POR_PRESUPUESTO','bwin_discovery','DISCOVERY_BUDGET_NOT_ANALYZED')
            continue
        reason=trial.scope_reason(fixture,trial.now_madrid(),args.min_lead,getattr(args,'horizon_hours',3))
        if reason:
            ledger.record(fixture,'EXCLUIDO_POR_HORARIO','scope',reason)
            failures.append({'fixture_id': fid, 'phase': 'temporal', 'reason': reason})
            continue
        if ('data_readiness_score' in fixture and fixture['data_readiness_score'] is None) or not fixture.get('details_checked',True) or fixture.get('data_readiness_status') in ('EVALUACION_INCOMPLETA','PENDIENTE_DETALLE'):
            ledger.record(fixture,'SIN_COBERTURA','preselection',fixture.get('data_readiness_status'))
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
        try:
            for known in (base / 'bwin').rglob('bwin_discovery.json'):
                for event in trial.read(known).get('matches', []):
                    eid = str(event.get('bwin_event_id') or '')
                    if str(event.get('fixture_id')) == str(fid) and eid.isdigit():
                        existing = root / 'outputs/matches' / eid / 'inputs' / f'cuotas_bwin_{eid}.txt'
                        if existing.is_file() and not getattr(args,'refresh_existing_odds',False):
                            raise trial.TrialError('EXISTING_ODDS_NOT_REUSABLE: preservado; pasar al siguiente candidato')
            path, event = discovery_for(root, base, fixture, args, failures)
            target = root / 'outputs/matches' / str(event['bwin_event_id']) / 'inputs' / f"cuotas_bwin_{event['bwin_event_id']}.txt"
            if target.exists() and not getattr(args,'refresh_existing_odds',False):
                # A stale or unauthenticated snapshot is preserved. Move on, never overwrite it.
                raise trial.TrialError('EXISTING_ODDS_NOT_REUSABLE: se conserva; pasar al siguiente candidato')
            for attempt in range(args.discovery_retries + 1):
                dest = run_dir / 'preflight' / f'{fid}_{attempt}'
                try:
                    refresh_args=['--refresh-existing'] if getattr(args,'refresh_existing_odds',False) and target.exists() else []
                    trial.run(root, 'bwin_market_preflight.py', ['--discovery', str(path), '--preselection', str(prepath),
                              '--fixture-id', str(fid), '--run-scraper', '--root', str(root),
                              '--min-lead-minutes', str(args.min_lead), '--output-dir', str(dest),*refresh_args], base / 'logs')
                    match = verified_preflight(root, dest / 'market_preflight.json', fixture, prepath, args)
                    ready.append((fixture, match, dest / 'market_preflight.json'))
                    break
                except (trial.TrialError, OSError, ValueError, KeyError) as exc:
                    ledger.record(fixture,failure_state(exc),'capture',str(exc))
                    failures.append({'fixture_id': fid, 'phase': 'capture', 'attempt': attempt, 'reason': str(exc)})
                    if target.exists() or attempt == args.discovery_retries:
                        break
                    # Reinspect only this event before retrying, with all identity/time gates.
                    path, event = discovery_for(root, base, fixture, args, failures)
        except (trial.TrialError, OSError, ValueError, KeyError, TypeError) as exc:
            ledger.record(fixture,failure_state(exc),'capture',str(exc))
            failures.append({'fixture_id': fid, 'phase': 'capture', 'reason': str(exc)})
    scanner_path = None
    if ready:
        out = run_dir / 'opportunity'
        cmd = ['--preselection', str(prepath), '--pattern', str(base / 'patterns/pattern_engine.json'),
               '--min-lead-minutes', str(args.min_lead), '--max-age-minutes', str(args.max_odds_age), '--output-dir', str(out)]
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
            failures.append({'phase': 'scanner', 'reason': str(exc)})
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
    tactical=run_engine(prepath,base/'patterns/pattern_engine.json' if (base/'patterns/pattern_engine.json').is_file() else None,
        run_dir/'tactical',root,preflights=[path for _,_,path in final_ready],scanner=scanner_path,
        profiles=root/'config/tactical_profiles/profiles.json')
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
    doc['comparison_audit']=str(root/'outputs/engineering/prematch_comparison_v2')
    doc['comparison_zip']=str(root/'outputs/engineering/prematch_comparison_v2.zip')
    ledger.write(run_dir/'candidate_ledger.json');ledger.write()
    trial.dump(run_dir/'handoff_prematch.json',doc)
    # Latest pointer contains the complete result, with immutable run outputs retained.
    doc['run_handoff'] = str(run_dir / 'handoff_prematch.json')
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
