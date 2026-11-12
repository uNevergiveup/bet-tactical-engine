#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Selector de UN partido PREMATCH V0.1.

Compara prioridad API, integridad de identidad Bwin, cuotas auditadas >=1.20,
y familias con datos apropiados. NO estima EV, probabilidades ni picks.
NO inicia ninguna captura a menos que el usuario active --auto-preflight-missing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse, unquote
from zoneinfo import ZoneInfo

VERSION = '0.1-single-match-selector'
MADRID = ZoneInfo('Europe/Madrid')
VALID_PREFLIGHT = {'MARKET_TRIPLES_EXTRACTED', 'HISTORICAL_MARKET_TRIPLES_EXTRACTED'}
JOIN_OK = 'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF'
TIME_OK = {'DISPLAY_TIME_CORROBORATED', 'ABSOLUTE_TIME_VERIFIED', 'COUNTDOWN_TIME_CORROBORATED'}


def read_json(path):
    from prematch_paths import control_file
    path = control_file(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError(f'JSON debe ser objeto: {path}')
    return data


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_time(value):
    if not isinstance(value, str): return None
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        return dt if dt.utcoffset() is not None else None
    except ValueError:
        return None


def score_100(value):
    try: return min(100.0,max(0.0,float(value)))
    except (ValueError,TypeError): return 0.0


def bwin_id_from_url(url):
    try:
        p=urlparse(str(url or ''))
        if p.scheme!='https' or p.hostname not in {'www.bwin.es','bwin.es'}: return None
        path=unquote(p.path)
        match=re.search(r'/sports/eventos/[^/]+-2:(\d+)$',path)
        return match.group(1) if match else None
    except (ValueError,TypeError): return None


def valid_discovery_match(m):
    if not str(m.get('status','')).startswith(('EVENT_FOUND_','IDENTITY_VERIFIED_')):
        return False
    eid=str(m.get('bwin_event_id') or '')
    return bool(eid and bwin_id_from_url(m.get('bwin_url'))==eid)


def event_time_ok(preflight):
    d=preflight.get('time_evidence') or {}
    if d.get('status') in TIME_OK and d.get('difference_minutes') is not None:
        try: return abs(float(d['difference_minutes'])) <= (4.0 if d['status']=='COUNTDOWN_TIME_CORROBORATED' else 3.0)
        except (TypeError,ValueError): return False
    return False


def temporal_identity_contract(preflight_match, discovery, discovery_sha, preflight_path):
    """Bind the accepted Bwin clock evidence to the exact selected identity.

    This is a transport contract, not a new source of truth.  Consumers must
    still re-open and hash the preflight and discovery files before using it.
    """
    evidence=preflight_match.get('time_evidence') or {}
    return {
        'schema_version':'1.0-phase3-temporal-identity',
        'status':'BWIN_API_KICKOFF_VERIFIED',
        'fixture_id':int(preflight_match['fixture_id']),
        'bwin_event_id':str(preflight_match['bwin_event_id']),
        'bwin_url':preflight_match['bwin_url'],
        'home':preflight_match['home'],'away':preflight_match['away'],
        'timezone':discovery.get('user_time_reference'),
        'evidence_status':evidence.get('status'),'source':evidence.get('source'),
        'observed_at_utc':evidence.get('observed_at_utc'),
        'bwin_kickoff_madrid':(evidence.get('bwin_visible_kickoff_madrid') or
                                evidence.get('bwin_inferred_kickoff_madrid') or
                                preflight_match.get('kickoff_madrid')),
        'api_kickoff_madrid':evidence.get('api_kickoff_madrid'),
        'difference_minutes':evidence.get('difference_minutes'),
        'catalog_text':evidence.get('catalog_text'),
        'discovery_sha256':discovery_sha,
        'preflight_sha256':sha256(Path(preflight_path)),
    }


def scan_preflights(paths, folder):
    candidates=list(paths)
    if folder:
        if not folder.is_dir(): raise ValueError(f'No existe --preflight-dir: {folder}')
        candidates.extend(folder.rglob('market_preflight.json'))
    unique=sorted({p.resolve() for p in candidates})
    return [(p,read_json(p)) for p in unique]


def audit_preflight_match(m,preflight,disc,preselect,discovery_sha,preselect_sha,now,age_minutes,min_candidate_odd=Decimal("1.20")):
    reasons=[]
    if m.get('_snapshot_reference_error'):
        reasons.extend(['SNAPSHOT_SHA_MISMATCH', m['_snapshot_reference_error']])
    fid=str(m.get('fixture_id'))
    if preflight.get('source_sha256')!=discovery_sha:reasons.append('DISCOVERY_SHA_MISMATCH')
    if preflight.get('source_preselection_sha256')!=preselect_sha:reasons.append('PRESELECTION_SHA_MISMATCH')
    if str(m.get('bwin_event_id') or '')!=str(disc.get('bwin_event_id') or ''): reasons.append('BWIN_ID_MISMATCH')
    if m.get('bwin_url')!=disc.get('bwin_url'):reasons.append('BWIN_URL_MISMATCH')
    if m.get('home')!=preselect.get('home') or m.get('away')!=preselect.get('away'):reasons.append('TEAM_MISMATCH')
    if m.get('kickoff_madrid')!=preselect.get('kickoff_madrid'):reasons.append('KICKOFF_TEXT_MISMATCH')
    if m.get('status') not in VALID_PREFLIGHT:reasons.append('PREFLIGHT_STATUS_NOT_VALID')
    if not event_time_ok(m):reasons.append('TIME_NOT_CORROBORATED')
    if (m.get('time_evidence') or {}).get('status') in ('COUNTDOWN_TIME_CORROBORATED', 'ABSOLUTE_TIME_VERIFIED'):
        from bwin_market_preflight import validate_match
        try:
            source_path = Path(preflight.get('source_discovery') or '')
            if sha256(source_path) != preflight.get('source_sha256'):
                raise ValueError('COUNTDOWN_DISCOVERY_SHA_MISMATCH')
            source = read_json(source_path)
            events = [e for e in source.get('matches', []) if str(e.get('fixture_id')) == fid]
            if len(events)!=1 or str(events[0].get('bwin_event_id'))!=str(m.get('bwin_event_id')):
                raise ValueError('COUNTDOWN_SOURCE_IDENTITY_MISMATCH')
            checked = validate_match(events[0], source.get('catalog_observed', []), now, 0, 180)
            if checked['status'] != 'READY_FOR_SCRAPER':
                raise ValueError('COUNTDOWN_SOURCE_NOT_REVALIDATED')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reasons.append(str(exc))
    snapshot=m.get('snapshot') or {}
    gate=m.get('quality_gate') or {}
    if not snapshot.get('event_id_verified_in_txt'):reasons.append('TXT_EVENT_ID_NOT_VERIFIED')
    if snapshot.get('status')!='MARKET_TRIPLES_EXTRACTED':reasons.append('TXT_MARKETS_UNVERIFIED')
    if gate.get('join_status')!=JOIN_OK:reasons.append('STATISTICAL_JOIN_INVALID')
    if str(gate.get('minimum_candidate_odd'))!=str(preflight.get('min_odd_candidate')):reasons.append('ODD_THRESHOLD_CONFLICT')
    try:
        if Decimal(str(gate.get('minimum_candidate_odd')))<min_candidate_odd:
            reasons.append('PREFLIGHT_MIN_ODD_BELOW_SELECTOR_THRESHOLD')
    except InvalidOperation:
        reasons.append('PREFLIGHT_MIN_ODD_INVALID')
    if float(gate.get('price_eligible') or 0)<float(gate.get('researchable_after_data_gate') or 0):reasons.append('INCONSISTENT_COUNTS')
    kinds=gate.get('market_kinds') or {}
    valid_kinds=sorted(k for k,v in kinds.items() if isinstance(v,dict) and (v.get('researchable') or 0)>0)
    total_ready=sum(int(kinds[k].get('researchable') or 0) for k in valid_kinds)
    if total_ready!=int(gate.get('researchable_after_data_gate') or 0):reasons.append('READY_COUNT_MISMATCH')
    if not valid_kinds:reasons.append('NO_RESEARCHABLE_MARKET_FAMILY')
    dt=parse_time(snapshot.get('capture_declared_madrid'))
    kickoff=parse_time(preselect.get('kickoff_madrid'))
    if not dt:reasons.append('NO_SNAPSHOT_CAPTURE_TIMESTAMP')
    if dt and kickoff and dt>=kickoff:reasons.append('CAPTURE_AFTER_KICKOFF')
    if dt:
        age=(now-dt).total_seconds()/60
        if age < -2:reasons.append('SNAPSHOT_FROM_FUTURE')
        if age>age_minutes:reasons.append('SNAPSHOT_TOO_OLD')
    else: age=None
    snap_path=Path(snapshot.get('snapshot_path') or '_missing_')
    if not snap_path.is_file(): reasons.append('SNAPSHOT_FILE_MISSING')
    else:
        actual=sha256(snap_path)
        if actual!=snapshot.get('sha256'): reasons.append('SNAPSHOT_SHA_MISMATCH')
        if snap_path.stat().st_size!=snapshot.get('snapshot_bytes'):reasons.append('SNAPSHOT_BYTES_MISMATCH')
    if preflight.get('prematch_only') is not True:reasons.append('NOT_PREMATCH_ONLY')
    return sorted(set(reasons)),valid_kinds,age


def prepare_rows(preselection,discovery,preflights,now,min_lead,max_snapshot_age,max_discovery_age,min_competitors,min_candidate_odd=Decimal('1.20')):
    pref={str(m.get('fixture_id')):m for m in preselection['matches']}
    disc={str(m.get('fixture_id')):m for m in discovery['matches']}
    if len(pref)!=len(preselection['matches']) or len(disc)!=len(discovery['matches']):
        raise ValueError('fixture_id duplicado en preselection o discovery')
    # Evita mezclar eventos incompatibles de diferentes archivos.
    reports={}
    for path,data in preflights:
        for m in data.get('matches') or []:
            from prematch_paths import local_reference, reference_root
            project = reference_root(preselection['_path'], path)
            snap = m.get('snapshot') or {}
            try:
                snap['snapshot_path'] = str(local_reference(project, snap.get('snapshot_path'), snap.get('sha256')))
                if data.get('source_discovery'):
                    data = dict(data, source_discovery=str(local_reference(project, data['source_discovery'], data.get('source_sha256'))))
            except (ValueError, OSError) as exc:
                snap['snapshot_path'] = ''
                m['_snapshot_reference_error'] = str(exc)
            fid=str(m.get('fixture_id'))
            reports.setdefault(fid,[]).append((path,data,m))
    pre_hash=sha256(preselection['_path'])
    disc_hash=sha256(discovery['_path'])
    generated=parse_time(discovery.get('generated_at_utc'))
    age_discovery=(now-generated).total_seconds()/60 if generated else None
    rows=[]
    # Un mismo Event ID no puede representar dos fixtures distintos en esta decisión.
    event_owners={}
    for fid,m in disc.items():
        if valid_discovery_match(m):
            event_owners.setdefault(str(m.get('bwin_event_id')),set()).add(fid)
    for fid,s in pref.items():
        k=parse_time(s.get('kickoff_madrid'))
        if k is None: continue
        lead=(k-now).total_seconds()/60
        if lead<=0: continue
        d=disc.get(fid)
        if not d: continue
        reasons=[]
        if lead<min_lead: reasons.append('INSUFFICIENT_ANALYSIS_TIME')
        if not s.get('details_checked'): reasons.append('STATISTICAL_DETAIL_NOT_CHECKED')
        if not valid_discovery_match(d):reasons.append('BWIN_IDENTITY_NOT_ESTABLISHED')
        if len(event_owners.get(str(d.get('bwin_event_id')),set()))>1:reasons.append('BWIN_ID_REUSED_ACROSS_FIXTURES')
        if s.get('home')!=d.get('home') or s.get('away')!=d.get('away'):
            reasons.append('DISCOVERY_TEAM_MISMATCH')
        if s.get('gender')!=d.get('gender'):reasons.append('DISCOVERY_GENDER_MISMATCH')
        if parse_time(d.get('kickoff_madrid'))!=k:reasons.append('DISCOVERY_KICKOFF_MISMATCH')
        if age_discovery is None or age_discovery<-2 or age_discovery>max_discovery_age:
            reasons.append('DISCOVERY_STALE_OR_MISSING_TIMESTAMP')
        found=reports.get(fid,[])
        unique_event_ids={str(m.get('bwin_event_id')) for _,_,m in found}
        if len(unique_event_ids)>1: reasons.append('CONFLICTING_PREFLIGHT_EVENT_IDS')
        candidates=[]
        for path,data,m in found:
            audit,market_kinds,price_age=audit_preflight_match(m,data,d,s,disc_hash,pre_hash,now,max_snapshot_age,min_candidate_odd)
            cap=parse_time((m.get('snapshot') or {}).get('capture_declared_madrid'))
            candidates.append((len(audit), -(cap.timestamp() if cap else -1e20),str(path),audit,market_kinds,price_age,m))
        candidates.sort(key=lambda x:(x[0],x[1],x[2]))
        chosen=candidates[0] if candidates else None
        status='NEEDS_MARKET_PREFLIGHT' if not found else 'MARKET_PREFLIGHT_REJECTED'
        markets=[];market_age=None;report_path=None;gate={};price_status=None
        if chosen:
            _,_,report_path,audit,markets,market_age,pm=chosen
            reasons.extend(audit)
            gate=pm.get('quality_gate') or {}
            price_status=pm.get('status')
        else:
            reasons.append('NO_MARKET_PREFLIGHT')
        if not reasons: status='QUALIFIED_FOR_COMPARISON'
        elif any(r not in {'NO_MARKET_PREFLIGHT'} for r in reasons):status='BLOCKED_OR_NEEDS_REFRESH'
        # Sin contar cuotas ni selección individuales como oportunidad independiente.
        kind_score=round(100*min(len(markets),3)/3,3)
        priority=score_100(s.get('research_priority_score'))
        readiness=score_100(s.get('data_readiness_score'))
        stability=score_100(s.get('stability_score'))
        final_score=round(.35*priority+.25*readiness+.15*stability+.25*kind_score,3)
        rows.append({'fixture_id':int(fid),'home':s.get('home'),'away':s.get('away'),
            'league':s.get('league'),'kickoff_madrid':s.get('kickoff_madrid'),
            'bwin_event_id':d.get('bwin_event_id'),'bwin_url':d.get('bwin_url'),
            'lead_minutes':round(lead,1),'research_priority_score':priority,
            'data_readiness_score':readiness,'stability_score':stability,
            'market_families_ready':markets,'market_family_diversity_score':kind_score,
            'researchable_selections':int(gate.get('researchable_after_data_gate') or 0),
            'captured_selections':int((chosen[6].get('snapshot') or {}).get('selection_triplets_verified') or 0) if chosen else 0,
            'price_eligible_selections':int(gate.get('price_eligible') or 0),
            'price_snapshot_age_minutes':round(market_age,2) if market_age is not None else None,
            'score':final_score,'status':status,'block_reasons':sorted(set(reasons)),
            'preflight_path':report_path,'preflight_status':price_status,
            'odds_sha256':(chosen[6].get('snapshot') or {}).get('sha256') if chosen and not chosen[3] else None,
            'odds_txt':(chosen[6].get('snapshot') or {}).get('snapshot_path') if chosen and not chosen[3] else None,
            'preflight_sha256':sha256(Path(report_path)) if report_path else None,
            'temporal_identity_evidence':(temporal_identity_contract(
                chosen[6],discovery,disc_hash,report_path) if chosen and not chosen[3] else None),
            'details_checked':bool(s.get('details_checked'))})
    rows.sort(key=lambda x:(x['status']!='QUALIFIED_FOR_COMPARISON',-x['score'],-x['research_priority_score'],x['kickoff_madrid'],x['fixture_id']))
    qualified=[r for r in rows if r['status']=='QUALIFIED_FOR_COMPARISON']
    if len(qualified)>=min_competitors:
        status='SELECTED'; winner=qualified[0]
    elif qualified:
        status='PROVISIONAL_INSUFFICIENT_COMPARISON';winner=qualified[0]
    else:
        status='NO_SELECTION';winner=None
    return status,winner,rows,{'preflight_reports_received':len(preflights),
        'future_discovered_matches':len(rows),'qualified_matches':len(qualified),
        'minimum_comparable_matches':min_competitors,
        'discovery_age_minutes':round(age_discovery,2) if age_discovery is not None else None}


def export(output_dir,status,winner,rows,meta,preselection,discovery,now,args):
    output_dir.mkdir(parents=True,exist_ok=True)
    selected_path=output_dir/'selected_match.json'
    if selected_path.exists(): selected_path.unlink()  # No dejar una selección anterior engañosa
    decision={'schema_version':VERSION,'asof_madrid':now.astimezone(MADRID).isoformat(),
        'status':status,'method':'DATA_READINESS_AND_MARKET_DIVERSITY_NO_EV_NO_PREDICTION',
        'weights':{'research_priority_score':0.35,'data_readiness_score':0.25,
                   'stability_score':0.15,'max_3_distinct_data_ready_market_families':0.25},
        'constraints':{'min_candidate_odd':str(args.min_candidate_odd),'min_lead_minutes':args.min_lead_minutes,
                       'max_snapshot_age_minutes':args.max_snapshot_age_minutes,
                       'max_discovery_age_minutes':args.max_discovery_age_minutes,
                       'min_comparable_matches':args.min_comparable_matches},
        'sources':{'preselection':str(args.preselection),'preselection_sha256':sha256(args.preselection),
                   'discovery':str(args.discovery),'discovery_sha256':sha256(args.discovery),
                   'preflights':[str(p) for p in args.preflight_files_resolved]},
        'summary':meta,'provisional_or_selected':winner,'ranked_matches':rows,
        'caveats':['Puntuaciones heurísticas: NO son probabilidades, EV ni recomendaciones.',
                   'Diversidad de familias no significa independencia estadística.',
                   'Cuotas históricas pueden haber cambiado; se deben recapturar cerca del kickoff.',
                   'Las familias de mercado no sustituyen análisis de XI, árbitro o táctica.']}
    (output_dir/'selection_decision.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if status=='SELECTED' and args.asof is None:
        selection={'schema_version':'1.0-selected-single-fixture','status':'SELECTED_FOR_DEEP_RESEARCH',
            'fixture_id':winner['fixture_id'],'bwin_event_id':winner['bwin_event_id'],
            'bwin_url':winner['bwin_url'],'home':winner['home'],'away':winner['away'],
            'kickoff_madrid':winner['kickoff_madrid'],'selection_asof_madrid':now.astimezone(MADRID).isoformat(),
            'market_families_ready_for_research':winner['market_families_ready'],
            'market_snapshot_age_minutes':winner['price_snapshot_age_minutes'],
            'selection_score_not_probability':winner['score'],
            'odds_sha256':winner.get('odds_sha256'),'odds_txt':winner.get('odds_txt'),
            'preflight_path':winner.get('preflight_path'),'preflight_sha256':winner.get('preflight_sha256'),
            'temporal_identity_evidence':winner.get('temporal_identity_evidence'),
            'source_decision':str((output_dir/'selection_decision.json').resolve()),
            'warning':'Un solo partido para investigación; actualizar cuotas y confirmar XI. NO es un pick ni EV.'}
        selected_path.write_text(json.dumps(selection,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    fields=['fixture_id','home','away','league','kickoff_madrid','bwin_event_id','lead_minutes',
       'research_priority_score','data_readiness_score','stability_score','score','status',
       'market_families_ready','researchable_selections','captured_selections','price_eligible_selections',
       'price_snapshot_age_minutes','block_reasons','preflight_path']
    with (output_dir/'selection_ranking.csv').open('w',encoding='utf-8-sig',newline='') as fh:
        writer=csv.DictWriter(fh,fieldnames=fields);writer.writeheader()
        for r in rows:
            obj={k:r.get(k) for k in fields};obj['market_families_ready']=';'.join(r['market_families_ready']);obj['block_reasons']=';'.join(r['block_reasons'])
            writer.writerow(obj)
    md=['# Selección automática de un único encuentro','',f'- Estado: **{status}**',
       f'- Hora de decisión: **{now.astimezone(MADRID).isoformat()}**',
       f"- Comparables validados: **{meta['qualified_matches']} / {meta['minimum_comparable_matches']}** requeridos",'']
    if winner:
        md.extend([f"- Mejor candidato: **{winner['home']} – {winner['away']}** (fixture {winner['fixture_id']}, Bwin {winner['bwin_event_id']})",
                   f"- Puntaje heurístico: **{winner['score']}/100**; familias de mercado aptas: **{', '.join(winner['market_families_ready'])}**",''])
    md.extend(['| Fixture | Partido | Kickoff Madrid | Estado | Puntuación | Familias útiles | Motivos |',
               '|---|---|---|---|---:|---|---|'])
    for r in rows:
        md.append(f"| {r['fixture_id']} | {r['home']} – {r['away']} | {r['kickoff_madrid']} | {r['status']} | {r['score']} | {', '.join(r['market_families_ready']) or '—'} | {', '.join(r['block_reasons']) or '—'} |")
    md.extend(['','**Importante:** la puntuación solo prioriza qué partido investigar. No mide posibilidad de acierto, calidad de una apuesta ni valor esperado.'])
    (output_dir/'selection_ranking.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
    return decision


def invoke_preflight_missing(args,preselection,discovery,now,existing_files):
    if args.auto_preflight_missing<1:return []
    if not args.preflight_script.is_file():raise ValueError('No existe bwin_market_preflight.py para --auto-preflight-missing')
    d_time=parse_time(discovery.get('generated_at_utc'))
    if not d_time or not (-2 <= (now-d_time).total_seconds()/60 <= args.max_discovery_age_minutes):
        raise ValueError('Discovery demasiado antiguo: vuelve a ejecutar bwin_discovery.py antes de --auto-preflight-missing')
    done=set()
    stat={str(x['fixture_id']):x for x in preselection['matches']}
    discovered={str(x['fixture_id']):x for x in discovery['matches']}
    pre_hash=sha256(args.preselection)
    disc_hash=sha256(args.discovery)
    for path,report in existing_files:
        for m in report.get('matches') or []:
            fid=str(m.get('fixture_id'))
            if fid in stat and fid in discovered:
                problems,_,_=audit_preflight_match(m,report,discovered[fid],stat[fid],
                    disc_hash,pre_hash,now,args.max_snapshot_age_minutes,args.min_candidate_odd)
                if not problems:done.add(fid)
    candidates=[]
    for m in discovery.get('matches') or []:
        fid=str(m.get('fixture_id'));s=stat.get(fid)
        if not s or fid in done or not s.get('details_checked') or not valid_discovery_match(m):continue
        if m.get('home')!=s.get('home') or m.get('away')!=s.get('away') or m.get('gender')!=s.get('gender'):continue
        kickoff=parse_time(s.get('kickoff_madrid'))
        if not kickoff or (kickoff-now).total_seconds()/60<args.min_lead_minutes:continue
        candidates.append((-(score_100(s.get('research_priority_score'))),fid,m))
    candidates.sort(key=lambda x:(x[0],x[1]))
    results=[]
    for _,fid,m in candidates[:args.auto_preflight_missing]:
        out=(args.output_dir/'auto_preflight'/fid).resolve()
        command=[sys.executable,str(args.preflight_script.resolve()),'--discovery',str(args.discovery.resolve()),
            '--preselection',str(args.preselection.resolve()),'--fixture-id',fid,'--run-scraper',
            '--root',str(args.root.resolve()),'--min-odd-candidate',str(args.min_candidate_odd),
            '--min-lead-minutes',str(args.min_lead_minutes),
            '--max-discovery-age-minutes',str(args.max_discovery_age_minutes),'--output-dir',str(out)]
        print(f'[{fid}] Ejecutando Bwin preflight del evento {m["bwin_event_id"]} (captura explícitamente autorizada) ...',flush=True)
        result=subprocess.run(command,cwd=args.root.resolve(),text=True,capture_output=True,timeout=args.scraper_timeout_seconds)
        out.mkdir(parents=True,exist_ok=True)
        (out/'auto_preflight_invocation.log').write_text(result.stdout+'\n--- stderr ---\n'+result.stderr,encoding='utf-8')
        report_path=out/'market_preflight.json'
        if result.returncode==0 and report_path.is_file():
            results.append(report_path)
            print(f'[{fid}] Preflight guardado: {report_path}',flush=True)
        else:print(f'[{fid}] Preflight no válido (rc={result.returncode}); ver auto_preflight_invocation.log',flush=True)
    return results


def main(argv=None):
    parser=argparse.ArgumentParser(description='Selecciona un único encuentro para investigación PREMATCH, sin picks ni EV')
    parser.add_argument('--preselection',type=Path,required=True)
    parser.add_argument('--discovery',type=Path,required=True)
    parser.add_argument('--preflight',type=Path,action='append',default=[],help='market_preflight.json, repetible')
    parser.add_argument('--preflight-dir',type=Path,help='Busca market_preflight.json en subcarpetas')
    parser.add_argument('--output-dir',type=Path,default=Path('outputs/discovery/single_match_selection'))
    parser.add_argument('--asof',type=str,default=None,help='Fecha ISO con offset, para reproducir una auditoría')
    parser.add_argument('--min-lead-minutes',type=int,default=45)
    parser.add_argument('--max-snapshot-age-minutes',type=int,default=120)
    parser.add_argument('--max-discovery-age-minutes',type=int,default=180)
    parser.add_argument('--min-comparable-matches',type=int,default=2)
    parser.add_argument('--min-candidate-odd',type=str,default='1.20',help='Cuota mínima de candidatos que debe haber aplicado el preflight')
    parser.add_argument('--auto-preflight-missing',type=int,default=0,help='Capturas adicionales permitidas (0 por defecto: no navega)')
    parser.add_argument('--preflight-script',type=Path,default=Path('bwin_market_preflight.py'))
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--scraper-timeout-seconds',type=int,default=240)
    args=parser.parse_args(argv)
    now=parse_time(args.asof) if args.asof else datetime.now(timezone.utc)
    if not now:parser.error('--asof exige ISO 8601 con offset, por ejemplo 2026-11-01T19:00:00+01:00')
    try:
        args.min_candidate_odd=Decimal(args.min_candidate_odd)
        if not args.min_candidate_odd.is_finite() or args.min_candidate_odd<Decimal('1.20'):
            parser.error('La cuota mínima del selector debe ser al menos 1.20')
    except InvalidOperation:parser.error('--min-candidate-odd debe ser decimal')
    if min(args.min_lead_minutes,args.max_snapshot_age_minutes,args.max_discovery_age_minutes,args.min_comparable_matches,args.scraper_timeout_seconds)<1 or args.auto_preflight_missing<0:
        parser.error('Parámetros de límites inválidos')
    if args.auto_preflight_missing and args.asof:
        parser.error('--auto-preflight-missing no admite --asof: nunca usar una hora simulada para activar scraping real')
    args.preselection=args.preselection.resolve();args.discovery=args.discovery.resolve();args.output_dir=args.output_dir.resolve()
    for p in (args.preselection,args.discovery):
        if not p.is_file():parser.error(f'Archivo no encontrado: {p}')
    preselection=read_json(args.preselection);discovery=read_json(args.discovery)
    if not isinstance(preselection.get('matches'),list) or not isinstance(discovery.get('matches'),list):
        parser.error('Preselection y discovery deben contener matches[]')
    preselection['_path']=args.preselection; discovery['_path']=args.discovery
    try:
        existing=scan_preflights(args.preflight,args.preflight_dir)
        new=invoke_preflight_missing(args,preselection,discovery,now,existing)
        all_reports=scan_preflights([*args.preflight,*new],args.preflight_dir)
        args.preflight_files_resolved=[p for p,_ in all_reports]
        status,winner,rows,meta=prepare_rows(preselection,discovery,all_reports,now,
            args.min_lead_minutes,args.max_snapshot_age_minutes,args.max_discovery_age_minutes,args.min_comparable_matches,args.min_candidate_odd)
        if args.asof and status=='SELECTED':
            status='HISTORICAL_REPLAY_SELECTED_NOT_ACTIONABLE'
        export(args.output_dir,status,winner,rows,meta,preselection,discovery,now,args)
    except (ValueError,KeyError,OSError,subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print(f'Estado: {status} | comparables={meta["qualified_matches"]}/{meta["minimum_comparable_matches"]}',flush=True)
    for r in rows[:10]:
        print(f'[{r["fixture_id"]}] {r["home"]}–{r["away"]} | puntaje={r["score"]} | {r["status"]} | {", ".join(r["block_reasons"])}',flush=True)
    if status=='SELECTED':print(f'SELECCIONADO: {winner["fixture_id"]} / Bwin {winner["bwin_event_id"]}',flush=True)
    elif status.startswith('PROVISIONAL'):print('PROVISIONAL: no se emite selected_match.json hasta comparar suficientes partidos',flush=True)
    else:print('SIN SELECCIÓN: no se emite selected_match.json',flush=True)
    print(f'Salida: {args.output_dir / "selection_decision.json"} | {args.output_dir / "selection_ranking.md"}',flush=True)
    return 0 if status=='SELECTED' else 3


if __name__=='__main__':
    raise SystemExit(main())
