"""Human CSV projection of canonical snapshots and the candidate ledger.

UTF-8-SIG, semicolon separator, empty = unknown/not evaluated, never zero evidence.
"""
import csv
import io
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
import prematch_prospective_trial as t

TACTICAL_FIELDS='tactical_engine_evaluado tactical_signal_count tactical_signal_families tactical_confidence_level tactical_data_coverage tactical_profile_status tactical_contradictions tactical_priority_score tactical_reason historical_matches_available historical_matches_detailed historical_matches_not_detailed metric_observations_valid metric_observations_missing requested_data_coverage_pct historical_effective_coverage_pct tactical_research_score market_execution_readiness direct_market_hypotheses indirect_market_hypotheses unmodelled_market_count scanner_status evaluation_completeness comparison_rank final_decision_reason market_hypotheses_count market_hypotheses_verified research_priority tactical_json tactical_mode'.split()
FIELDS='fecha_ventana inicio_ventana_madrid fin_ventana_madrid fixture_id kickoff_madrid pais liga league_id temporada local visitante liga_elegible motivo_filtro_liga prematch_elegible minutos_hasta_kickoff_en_captura fase_maxima estado_final motivo_exclusion_o_pendiente detalle_evidencia preselection_evaluada preselection_score cobertura_api patrones_detectados patrones_resumen bwin_busqueda_estado bwin_event_id bwin_url identidad_bwin_estado evidencia_horaria_estado cuotas_capturadas captura_madrid sha256_snapshot cuotas_elegibles mercados_investigables opportunity_evaluado opportunity_estado mejor_hipotesis_mercado mejor_hipotesis_cuota score_oportunidad seleccionado razon_seleccion_o_no_seleccion actualizado_madrid'.split()


FIELDS += TACTICAL_FIELDS
FIELDS += 'raw_signal_market_links unique_market_selections supported_signal_market_links unique_supported_selections direct_supported indirect_supported'.split()

def read(p):
    return t.read(p) if p.is_file() else {}


def atomic(path, value, encoding='utf-8'):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    tmp.write_text(value,encoding=encoding);tmp.replace(path)


def export_daily(base):
    base=Path(base);folder=base.parent;source=read(folder/'fixtures.json')
    if not source:return []
    window=source.get('window',{})
    start=t.dt(str(window.get('start_madrid','')).split(' ')[0]);end=t.dt(str(window.get('end_madrid','')).split(' ')[0])
    if not start or not end:
        try:
            d,a,b=folder.name.split('_');start,end=t.local_window(t.parse_date(d),t.parse_clock(a[:2]+':'+a[2:]),t.parse_clock(b[:2]+':'+b[2:]))
        except ValueError:return []
    fixtures={}
    for f in source.get('fixture_audit',[])+source.get('fixtures',[]):
        ko=t.dt(f.get('kickoff_madrid'))
        if ko and start.timestamp()<=ko.timestamp()<=end.timestamp(): fixtures[str(f['fixture_id'])]=f
    major=read(folder/'fixtures_major.json')
    league={str(f['fixture_id']):f for key in ('fixtures','review','excluded') for f in major.get(key,[])}
    pre={str(f['fixture_id']):f for f in read(base/'enriched/preselection.json').get('matches',[])}
    for bp in sorted((base/'enrichment_batches').glob('*/preselection.json')):
        bd=read(bp);inp=bp.parent/'fixtures.json'
        if inp.is_file() and bd.get('source_filter_sha256')==t.sha(inp):
            for f in bd.get('matches',[]):pre.setdefault(str(f['fixture_id']),f)
    patterns=read(base/'patterns/pattern_engine.json').get('patterns',[])
    disc={str(f['fixture_id']):f for f in read(base/'bwin/bwin_discovery.json').get('matches',[])}
    for bp in sorted((base/'bwin/batches').glob('*/bwin_discovery.json')):
        for f in read(bp).get('matches',[]):disc.setdefault(str(f['fixture_id']),f)
    ledger={str(f['fixture_id']):f for f in read(base/'candidate_ledger.json').get('fixtures',[])}
    handoff=read(base/'handoff_prematch.json'); scans={};pf={};hypotheses={}
    for p in sorted(list((base/'preflight').rglob('market_preflight.json'))+list((base/'daily_runs').rglob('market_preflight.json'))):
        for f in read(p).get('matches',[]):
            fid=str(f['fixture_id']);previous=pf.get(fid,({},None))[0]
            if fid not in pf or f.get('snapshot') or not previous.get('snapshot'):pf[fid]=(f,p)
    for p in sorted((base/'daily_runs').rglob('opportunity_scan.json')):
        doc=read(p)
        for f in doc.get('audits',[]):scans[str(f['fixture_id'])]=(f,p)
        for q in doc.get('selection_reviews',[]):
            if q.get('status')=='DISCREPANCIA_EXPLORATORIA_REVISAR':
                fid=str(q['fixture_id'])
                if fid not in hypotheses or (q.get('gap_worst_pp') or 0)>(hypotheses[fid].get('gap_worst_pp') or 0):hypotheses[fid]=q
    mapping={'FILTRADO_POR_LIGA':'DESCARTADO_POR_REGLA','EXCLUIDO_POR_FILTRO_MANUAL':'DESCARTADO_POR_REGLA',
      'EXCLUIDO_POR_HORARIO':'BLOQUEADO_TIEMPO','IDENTIDAD_INCORRECTA':'BLOQUEADO_IDENTIDAD',
      'DIFERIDO_POR_PRESUPUESTO':'NO_EVALUADO_POR_LIMITE','EXCLUIDO_POR_LIMITE_ANTERIOR':'NO_EVALUADO_POR_LIMITE',
      'ANALIZADO':'ANALIZADO_SIN_SENAL_VALIDADA','SELECCIONADO':'SELECCIONADO'}
    tactical=read(base/'tactical/tactical_signal_engine.json')
    # A stale engine for another preselection is not attached to this CSV.
    if tactical and tactical.get('sources',{}).get('preselection_sha256')!=t.sha(base/'enriched/preselection.json'):tactical={}
    tactical_rank={str(r['fixture_id']):r for r in tactical.get('ranking',[])}
    tactical_position={str(r['fixture_id']):index+1 for index,r in enumerate(tactical.get('ranking',[]))}
    rows=[];now=t.now_madrid()
    for fid,f in fixtures.items():
        l=league.get(fid,{});s=pre.get(fid,{});d=disc.get(fid,{});a=ledger.get(fid,{})
        m,path=pf.get(fid,({},None));snap=m.get('snapshot',{});gate=m.get('quality_gate',{})
        scan,scanpath=scans.get(fid,({},None));capture=t.dt(snap.get('capture_declared_madrid'))
        state=mapping.get(a.get('state'),a.get('state','PENDIENTE'));reason=a.get('reason','ETAPA_NO_EJECUTADA')
        if state=='SIN_CUOTAS' and any(x in str(reason).upper() for x in ('STALE','CADUC','EXISTING_ODDS_NOT_REUSABLE')):state='PENDIENTE_ACTUALIZAR_CUOTAS'
        if m.get('status')=='BLOCKED':
            reason=','.join(m.get('reasons',[]));state='BLOQUEADO_IDENTIDAD' if any(x in reason for x in ('IDENTITY','TITLE','NAME','EVENT_ID','MISMATCH')) else 'BLOQUEADO_TIEMPO'
        if scan.get('status')=='VERIFIED':state='ANALIZADO_SIN_SENAL_VALIDADA'
        selected=(str(handoff.get('fixture_id'))==fid and handoff.get('status')=='READY_FOR_MANUAL_RESEARCH' and scan.get('status')=='VERIFIED')
        if selected:state='SELECCIONADO'
        if selected and not t.kickoff_valid(f,now,45):
            state='BLOQUEADO_TIEMPO';reason='MATCH_STARTED_OR_SHORT_LEAD';selected=False
        if capture and now.timestamp()-capture.timestamp()>handoff.get('max_odds_age_minutes',120)*60:
            state='PENDIENTE_ACTUALIZAR_CUOTAS';reason='SNAPSHOT_CADUCADO_NO_EJECUTABLE';selected=False
        if snap and (not Path(snap.get('snapshot_path','')).is_file() or t.sha(Path(snap['snapshot_path']))!=snap.get('sha256')):
            state='SIN_CUOTAS';reason='SNAPSHOT_SHA_NO_VERIFICABLE';selected=False
        ps=[p for p in patterns if str(p.get('fixture_id'))==fid and p.get('pattern_flag')=='ASIMETRIA_RELATIVA_ALTA']
        row=dict.fromkeys(FIELDS,'');row.update(fecha_ventana=start.date().isoformat(),inicio_ventana_madrid=start.isoformat(),fin_ventana_madrid=end.isoformat(),
            fixture_id=f['fixture_id'],kickoff_madrid=f['kickoff_madrid'],pais=f.get('country',''),liga=f.get('league',''),league_id=f.get('league_id',''),temporada=f.get('season',''),local=f.get('home',''),visitante=f.get('away',''),
            liga_elegible=('true' if l.get('filter_status')=='ELIGIBLE' else 'false' if l.get('filter_status')=='EXCLUDED' else 'unknown'),motivo_filtro_liga=l.get('filter_reason',''),
            prematch_elegible=str(t.scope_reason(f,now,45,0) is None).lower(),fase_maxima=a.get('stage','discovery'),estado_final=state,motivo_exclusion_o_pendiente=reason,
            detalle_evidencia=' | '.join(str(x) for x in [folder/'fixtures.json',base/'candidate_ledger.json',path,scanpath] if x),
            preselection_evaluada=str(bool(s)).lower(),preselection_score=s.get('research_priority_score',''),cobertura_api=s.get('data_readiness_status',''),patrones_detectados=len(ps) if s else '',patrones_resumen=' | '.join(sorted({str(p.get('metric'))+':'+str(p.get('team_side')) for p in ps})),
            bwin_busqueda_estado=d.get('status','NO_EVALUADO'),bwin_event_id=m.get('bwin_event_id',d.get('bwin_event_id','')) or '',bwin_url=m.get('bwin_url',d.get('bwin_url','')) or '',identidad_bwin_estado=m.get('discovery_identity_status',d.get('identity_status','')),evidencia_horaria_estado=m.get('time_evidence',{}).get('status',''),
            cuotas_capturadas=snap.get('selection_triplets_verified',''),captura_madrid=snap.get('capture_declared_madrid',''),sha256_snapshot=snap.get('sha256',''),cuotas_elegibles=gate.get('price_eligible',''),mercados_investigables=gate.get('researchable_after_data_gate',''),
            opportunity_evaluado=str(bool(scan)).lower(),opportunity_estado=scan.get('status','NO_EVALUADO'),seleccionado=str(selected).lower(),razon_seleccion_o_no_seleccion=handoff.get('routing_reason','') if selected else reason,actualizado_madrid=now.isoformat())
        if capture:row['minutos_hasta_kickoff_en_captura']=round((t.dt(f['kickoff_madrid']).timestamp()-capture.timestamp())/60,2)
        if fid in hypotheses:
            q=hypotheses[fid];row['mejor_hipotesis_mercado']=q.get('market','');row['mejor_hipotesis_cuota']=q.get('odd','')
            if state=='ANALIZADO_SIN_SENAL_VALIDADA':state='CANDIDATO_INVESTIGACION';row['estado_final']=state
        if selected:row['fase_maxima']='handoff'
        elif scan:row['fase_maxima']='opportunity_scanner'
        elif m:row['fase_maxima']='preflight'
        tx=tactical.get('fixtures',{}).get(fid)
        row['tactical_engine_evaluado']=str(bool(tx)).lower()
        if tx:
            for field in ('raw_signal_market_links','unique_market_selections','supported_signal_market_links','unique_supported_selections','direct_supported','indirect_supported'):
                row[field]=tx.get('market_validation',{}).get(field,'')
            summary=tx.get('coverage_summary',{})
            rank=tactical_rank.get(fid,{})
            row.update(tactical_signal_count=tx['signal_count'],tactical_signal_families='|'.join(tx['signal_families']),
                tactical_confidence_level=tx['confidence_level'],tactical_data_coverage=tx['data_coverage'],
                tactical_profile_status=tx['profile_status'],tactical_contradictions='|'.join(tx['contradictions']),
                tactical_priority_score=tx['priority_score'],tactical_reason=tx['reason'],
                historical_matches_available=summary.get('historical_matches_available',''),
                historical_matches_detailed=summary.get('historical_matches_detailed',''),
                historical_matches_not_detailed=summary.get('historical_matches_not_detailed',''),
                metric_observations_valid=summary.get('metric_observations_valid',''),
                metric_observations_missing=summary.get('metric_observations_missing',''),
                requested_data_coverage_pct=summary.get('requested_data_coverage_pct',''),
                historical_effective_coverage_pct=summary.get('historical_effective_coverage_pct',''),
                tactical_research_score=rank.get('tactical_research_score',tx.get('tactical_research_score','')),
                market_execution_readiness=rank.get('market_execution_readiness',''),
                direct_market_hypotheses=rank.get('direct_market_hypotheses',tx.get('direct_market_hypotheses','')),
                indirect_market_hypotheses=rank.get('indirect_market_hypotheses',tx.get('indirect_market_hypotheses','')),
                unmodelled_market_count=rank.get('unmodelled_market_count',tx.get('unmodelled_market_count','')),
                scanner_status=rank.get('scanner_status',scan.get('status','NO_EVALUADO')),
                evaluation_completeness=rank.get('evaluation_completeness','NO_EVALUADO'),
                comparison_rank=tactical_position.get(fid,''),
                final_decision_reason=handoff.get('routing_reason','') if selected else ' | '.join(rank.get('reasons',[])) or reason,
                market_hypotheses_count=tx['market_hypotheses_count'],market_hypotheses_verified=tx['market_hypotheses_verified'],
                research_priority=rank.get('research_priority',''),tactical_json=tx['detail_json'],tactical_mode=tactical['mode'])
        rows.append(row)
    rows.sort(key=lambda r:(r['kickoff_madrid'],str(r['fixture_id'])))
    stream=io.StringIO(newline='');w=csv.DictWriter(stream,fieldnames=FIELDS,delimiter=';');w.writeheader();w.writerows(rows)
    atomic(base/'partidos_diarios.csv',stream.getvalue(),'utf-8-sig')
    atomic(base/'partidos_diarios.json',json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    from collections import Counter
    counts=Counter(r['estado_final'] for r in rows)
    atomic(base/'partidos_diarios.md','# Partidos diarios\n\nCSV UTF-8-SIG, separador punto y coma. Vacío = no medido/no disponible.\n\n'+json.dumps(dict(counts),ensure_ascii=False,indent=2)+'\n')
    return rows
