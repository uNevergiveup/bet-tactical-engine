#!/usr/bin/env python3
"""Release integration gates. No external AI execution and no bets."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import shlex
import shutil
import sys
import zipfile
from datetime import timezone
from pathlib import Path
from unittest.mock import patch
import prematch_daily as daily
import prematch_prospective_trial as t
import prematch_single as single

VERSION='v0.1.0-rc1'


def reference(path):
    path=Path(path).resolve()
    return {'path':str(path),'sha256':t.sha(path)}


def verify_refs(refs):
    for name,ref in refs.items():
        if t.sha(Path(ref['path']))!=ref['sha256']:raise ValueError('SOURCE_CHANGED: '+name)


def bridge(root,handoff_path,output=None,*,historical=False,asof=None):
    """Translate the chosen fixture only; never invoke a second selector."""
    handoff_path=Path(handoff_path).resolve();doc=t.read(handoff_path)
    immutable=Path(doc.get('run_handoff') or handoff_path).resolve()
    if immutable!=handoff_path:
        if not immutable.is_file() or t.sha(immutable)!=t.sha(handoff_path):
            raise ValueError('IMMUTABLE_HANDOFF_MISMATCH')
        handoff_path=immutable;doc=t.read(handoff_path)
    if historical and not asof:raise ValueError('Historical bridge requires an explicit source cutoff')
    if asof and not historical:raise ValueError('Simulated time cannot create a live selection')
    now=asof if historical else t.now_madrid()
    with contextlib.redirect_stdout(io.StringIO()),patch.object(t,'now_madrid',return_value=now):
        daily.review(handoff_path,root)
    ctx=t.read(Path(doc['context_paths'][0]))
    fixture=ctx['fixture'];identity=ctx.get('api_fixture_identity') or {}
    if not all(identity.get(k) is not None for k in ('home_id','away_id','league_id','season')):
        raise ValueError('CANONICAL_TEAM_OR_COMPETITION_ID_MISSING')
    if identity['home_id']==identity['away_id']:raise ValueError('DUPLICATE_TEAM_ID')
    for key in ('fixture_id','home','away','kickoff_madrid'):
        if str(fixture.get(key))!=str(identity.get(key)) or str(doc.get(key))!=str(identity.get(key)):
            raise ValueError('BRIDGE_IDENTITY_MISMATCH: '+key)
    rank=next((r for r in doc.get('tactical_selection_ranking',[]) if str(r['fixture_id'])==str(doc['fixture_id'])),None)
    if not rank or not rank.get('research_eligible',rank.get('eligible',False)) or rank.get('selection_tier','A') not in ('A','B'):
        raise ValueError('CHOSEN_FIXTURE_NOT_RESEARCH_ELIGIBLE')
    tier=rank.get('selection_tier','A')
    if (doc.get('selection_tier',tier)!=tier or bool(doc.get('eligible',rank.get('eligible')))!=bool(rank.get('eligible'))
            or doc.get('research_missing_evidence',rank.get('research_missing_evidence',[]))!=rank.get('research_missing_evidence',[])
            or rank.get('hard_block_reasons')):
        raise ValueError('HANDOFF_SELECTION_TIER_CONFLICT')
    eid=str(doc['bwin_event_id'])
    output=Path(output) if output else root/'outputs/matches'/eid/'selection'/now.strftime('%Y%m%dT%H%M%S%f')
    output.mkdir(parents=True,exist_ok=False)
    refs={'handoff':reference(handoff_path),'context':reference(doc['context_paths'][0]),
          'preflight':reference(doc['preflight']),'odds':reference(doc['odds_txt'])}
    if doc.get('tactical_signal_json'):
        refs['tactical']=reference(doc['tactical_signal_json'])
        if refs['tactical']['sha256']!=doc.get('tactical_signal_sha256'):raise ValueError('TACTICAL_SHA_MISMATCH')
    selection={'schema_version':'1.0-selected-single-fixture','status':'SELECTED_FOR_DEEP_RESEARCH',
        'fixture_id':doc['fixture_id'],'bwin_event_id':eid,'bwin_url':doc['bwin_url'],
        'home':identity['home'],'away':identity['away'],'home_api_id':identity['home_id'],'away_api_id':identity['away_id'],
        'league_id':identity['league_id'],'league':identity.get('league'),'season':identity['season'],
        'kickoff_madrid':doc['kickoff_madrid'],'selection_asof_madrid':now.isoformat(),
        'market_families_ready_for_research':rank.get('matched_families',[]),
        'selection_tier':tier,'research_eligible':True,'eligible':bool(rank.get('eligible')),
        'research_missing_evidence':rank.get('research_missing_evidence',[]),
        'hard_block_reasons':[],
        'odds_txt':doc['odds_txt'],'odds_sha256':doc['odds_sha256'],'capture_madrid':doc['capture_madrid'],
        'source_refs':refs,'source_decision':str(output/'selection_decision.json'),
        'actionable_bet':False,'ready_to_bet':False,'version':VERSION}
    winner={**selection,'market_families_ready':selection['market_families_ready_for_research']}
    decision={'schema_version':'1.0-handoff-transition','status':'SELECTED','method':'VALIDATED_DAILY_HANDOFF_NO_RERANKING',
              'provisional_or_selected':winner,'source_refs':refs,'actionable_bet':False}
    single.validate_selected(selection,decision)
    if historical:
        selection.update(status='HISTORICAL_AUDIT_ONLY',actionable_bet=False)
        decision['status']='HISTORICAL_AUDIT_ONLY'
    t.dump(output/'selection_decision.json',decision);t.dump(output/'selected_match.json',selection)
    return output/'selected_match.json'


def selected_context(root,path):
    ctx=single.load_context(argparse.Namespace(root=root,selected=Path(path),decision=None))
    verify_refs(ctx['selection'].get('source_refs',{}))
    return ctx


def _price_blind(value):
    if isinstance(value,dict):
        blocked=('odds','prediction','price','ev_valid','stake')
        return {k:_price_blind(v) for k,v in value.items()
                if not any(token in k.casefold() for token in blocked)}
    if isinstance(value,list):return [_price_blind(v) for v in value]
    return value


def _create_identical_or_fail(path,content):
    """Creation-only output: idempotent for identical bytes, never overwrite."""
    encoded=content.encode('utf-8') if isinstance(content,str) else content
    if path.exists():
        if not path.is_file() or path.read_bytes()!=encoded:
            raise ValueError('RESEARCH_KIT_V2_WOULD_OVERWRITE: '+str(path))
        return False
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as handle:handle.write(encoded)
    return True


def _research_identity(ctx,manifest):
    entry=(manifest.get('inputs') or {}).get('identity') or {}
    path=Path(entry.get('path') or '')
    if not path.is_file() or not entry.get('sha256') or t.sha(path)!=entry['sha256']:
        raise ValueError('RESEARCH_IDENTITY_SHA_INVALID')
    identity=t.read(path)
    if (str(identity.get('bwin_event_id'))!=ctx['event_id'] or
            int(identity.get('api_fixture_id') or 0)!=ctx['fixture_id'] or
            identity.get('bwin_url')!=ctx['url']):
        raise ValueError('RESEARCH_IDENTITY_EVENT_MISMATCH')
    if identity.get('identity_status') not in {'FIXTURE_AND_BWIN_TIME_VERIFIED','FIXTURE_VERIFIED_BY_GENERATOR'}:
        raise ValueError('RESEARCH_IDENTITY_NOT_FULLY_VERIFIED')
    kickoff=t.dt(identity.get('kickoff_utc'))
    if not kickoff or kickoff.astimezone(timezone.utc)!=ctx['kickoff'].astimezone(timezone.utc):
        raise ValueError('RESEARCH_IDENTITY_KICKOFF_MISMATCH')
    if not identity.get('league_name') or identity.get('league_id') is None or identity.get('season') is None:
        raise ValueError('RESEARCH_COMPETITION_METADATA_MISSING')
    return identity,path


def research_kit(root,path):
    """Create three specialized V2 prompts without touching active research."""
    ctx=selected_context(root,path);single.ensure_prematch(ctx)
    manifest=single.match_manifest(ctx)
    if manifest.get('status')!='PREPARADO':raise ValueError('Full event context must be PREPARADO')
    identity,identity_path=_research_identity(ctx,manifest)
    eid=ctx['event_id'];folder=ctx['directory']/'research'/'prompts_v2'
    external=ctx['directory']/'research'/'external'
    raw_context=Path(manifest['inputs']['context_json']['path'])
    if not raw_context.is_file() or t.sha(raw_context)!=manifest['inputs']['context_json']['sha256']:
        raise ValueError('RESEARCH_CONTEXT_SHA_INVALID')
    cutoff=ctx['selection_time'].astimezone(timezone.utc)
    if cutoff>=ctx['kickoff'].astimezone(timezone.utc):raise ValueError('RESEARCH_CUTOFF_NOT_PREMATCH')
    research_context=folder/f'contexto_cualitativo_{eid}.json'
    projected={'schema_version':'2.0-price-blind-research-context','cutoff_utc':cutoff.isoformat(),
        'source_context':reference(raw_context),'source_identity':reference(identity_path),
        'projection':'QUALITATIVE_RESEARCH_WITHOUT_MARKET_PRICES_OR_PREDICTIONS',
        'data':_price_blind(t.read(raw_context))}
    context_bytes=(json.dumps(projected,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    context_sha=hashlib.sha256(context_bytes).hexdigest()
    common=f'''Contrato del evento (no reinterpretar):
- Event ID Bwin: {eid}
- Fixture ID API-Football: {ctx['fixture_id']}
- Partido: {identity['home']['api_name']}–{identity['away']['api_name']}
- Competición: {identity['league_name']} (League ID {identity['league_id']})
- Temporada API-Football: {identity['season']}
- Kickoff: {ctx['kickoff'].isoformat()} Europe/Madrid; {identity['kickoff_utc']} UTC
- cutoff_utc estricto: {cutoff.isoformat()}

Procedencia validada:
- Identidad: {identity_path.resolve()} · SHA-256 {t.sha(identity_path)}
- Contexto cualitativo: {research_context.resolve()} · SHA-256 {context_sha}

Aplica tus instrucciones permanentes ya configuradas; este texto sólo especializa el encargo.
Usa exclusivamente información publicada antes de cutoff_utc. Separa hechos, inferencias y datos no verificados.
No produzcas picks, probabilidades, pronósticos ni recomendaciones financieras.
'''
    gemini=common+f'''
Tarea Gemini — scouting táctico profundo:
1. Analiza entrenadores y patrones realmente documentados, sin convertir una formación nominal en conducta demostrada.
2. Reconstruye mecanismos colectivos con partidos históricos identificados por Fixture ID: salida, presión, bloque, transiciones, áreas de progresión y defensa del área.
3. Examina roles individuales, continuidad, minutos, reemplazos y duelos plausibles sólo cuando exista evidencia suficiente.
4. Contrasta localía, muestras recientes y anteriores, balón parado, faltas, córners y diferencias H1/H2 cuando las fuentes lo permitan.
5. Formula escenarios contrapuestos. Para cada hipótesis incluye evidencia favorable, contraria, limitaciones y qué la confirmaría o refutaría.
6. Cita fuente primaria, fecha de publicación y fecha del partido para cada afirmación material.

Salida independiente prevista: {external/f'gemini_{eid}.md'}
No leas ni completes el informe de Grok; conserva independencia investigadora.
'''
    grok=common+f'''
Tarea Grok — actualidad verificable:
1. Revisa cuentas oficiales en X, webs de clubes y competición, ruedas de prensa y prensa local fechada.
2. Verifica entrenadores actuales, declaraciones, lesiones, sanciones, dudas, convocatoria, viajes y disponibilidad.
3. Distingue XI oficial, XI probable y mera especulación; identifica fuente y hora exacta de cada uno.
4. Investiga cambios tácticos recientes sólo con testimonios o observaciones fechadas, indicando evidencia contraria.
5. Registra posts individuales y URLs originales; separa fuentes independientes de republicaciones o sindicación.
6. Señala expresamente qué actualidad no pudo corroborarse antes del cutoff.

Salida independiente prevista: {external/f'grok_{eid}.md'}
No leas ni completes el informe de Gemini; conserva independencia investigadora.
'''
    argon=common+f'''
Tarea Argon — auditoría cruzada y síntesis canónica:
- Entradas independientes: {external/f'gemini_{eid}.md'} y {external/f'grok_{eid}.md'}.
- Verifica primero Event ID, Fixture ID, kickoff y cutoff de ambos archivos. Conserva sus bytes originales.
- Audita afirmación por afirmación: fuente primaria, fecha, independencia, identidad de equipo/jugador y nivel CONFIRMADO, PROBABLE, INFERIDO o NO VERIFICADO.
- Detecta acuerdos que proceden de una misma fuente, contradicciones reales, omisiones y afirmaciones posteriores al cutoff.
- No rellenes hechos ausentes ni resuelvas contradicciones mediante suposiciones. Mantén explícito NO VERIFICADO cuando corresponda.
- Produce una matriz de evidencia favorable/contraria y una síntesis cualitativa trazable, sin convertirla en pronóstico.

Salidas previstas, sin alterar las entradas:
- Auditoría de fusión: {external/f'fusion_{eid}.md'}
- Informe cualitativo canónico compatible con finish: {external/f'reporte_cualitativo_{eid}.md'}
El informe canónico debe conservar Event ID, Fixture ID, kickoff, cutoff_utc y registro de fuentes fechadas.
'''
    outputs={folder/f'PROMPT_GEMINI_{eid}.txt':gemini,
             folder/f'PROMPT_GROK_{eid}.txt':grok,
             folder/f'PROMPT_ARGON_{eid}.txt':argon}
    # Check the complete transaction before writing a single byte.
    candidates={research_context:context_bytes,**{p:body.encode('utf-8') for p,body in outputs.items()}}
    for target,body in candidates.items():
        if target.exists() and (not target.is_file() or target.read_bytes()!=body):
            raise ValueError('RESEARCH_KIT_V2_WOULD_OVERWRITE: '+str(target))
    for target,body in candidates.items():_create_identical_or_fail(target,body)
    return folder

def analysis_input(root,path):
    ctx=single.load_context(argparse.Namespace(root=root,selected=Path(path),decision=None))
    single.ensure_prematch(ctx)
    manifest=single.match_manifest(ctx)
    if manifest.get('status')!='FINALIZADO':raise ValueError('Validation and motor must complete before final analyst')
    odds=manifest['inputs']['odds'];snap=single.snapshot_info(Path(odds['path']),ctx,max_age=15)
    daily.final_odds_check({'kickoff_madrid':ctx['kickoff'].isoformat(),'capture_madrid':snap['capture']},t.now_madrid())
    binding=ctx['directory']/'analysis_input_binding.json'
    if binding.exists():raise ValueError('Final analyst input already issued: preserve one execution; audit before any new attempt')
    refs={k:reference(v['path']) for k,v in manifest['inputs'].items()}
    refs['manifest']=reference(ctx['directory']/'manifest.json')
    refs['prompt']=reference(manifest['codex_prompt'])
    for p in (ctx['directory']/'validation').glob('*.json'):refs['validation/'+p.name]=reference(p)
    for p in (ctx['directory']/'probabilistic').glob('*'):
        if p.is_file():refs['motor/'+p.name]=reference(p)
    t.dump(binding,{'status':'INPUT_ISSUED_NOT_EXECUTED','source_refs':refs,'odds_sha256':snap['sha256'],
                    'fixture_id':ctx['fixture_id'],'bwin_event_id':ctx['event_id'],'issued_madrid':t.now_madrid().isoformat()})
    return Path(manifest['codex_prompt'])


def validate_external_inputs(ctx,canonical_report):
    """Record supplied independent reports, without claiming provider execution."""
    folder=ctx['directory']/'research/external';eid=ctx['event_id'];refs={}
    for provider in ('grok','gemini','fusion'):
        path=folder/f'{provider}_{eid}.md'
        body=path.read_text(encoding='utf-8-sig')
        if 'STATUS: PENDIENTE_MANUAL' in body or len(body.strip())<200:raise ValueError(provider+': investigación/fusión pendiente')
        import re
        event=re.search(r'(?im)^Event ID:\s*(\d+)\s*$',body)
        fixture=re.search(r'(?im)^Fixture ID:\s*(\d+)\s*$',body)
        if not event or event.group(1)!=eid or not fixture or fixture.group(1)!=str(ctx['fixture_id']):
            raise ValueError(provider+': identidad explícita ausente o incorrecta')
        cutoff=re.search(r'(?im)^cutoff_utc:\s*(\S+)\s*$',body)
        when=t.dt(cutoff.group(1)) if cutoff else None
        if not when or when>t.now_madrid() or when>=ctx['kickoff']:raise ValueError(provider+': cutoff_utc ausente/futuro/postkickoff')
        refs[provider]=reference(path)
    refs['canonical']=reference(canonical_report)
    t.dump(ctx['directory']/'research_provenance.json',{'status':'MANUAL_FILES_RECEIVED_NOT_PROVIDER_EXECUTION_CERTIFIED',
        'source_refs':refs,'imported_madrid':t.now_madrid().isoformat(),'fixture_id':ctx['fixture_id'],'bwin_event_id':eid})


def final_review(root,path,analysis,market=None,selection=None,odd=None):
    ctx=single.load_context(argparse.Namespace(root=root,selected=Path(path),decision=None))
    single.ensure_prematch(ctx)
    manifest=single.match_manifest(ctx)
    if manifest.get('status')!='FINALIZADO':raise ValueError('Downstream stages invalid or incomplete')
    binding=t.read(ctx['directory']/'analysis_input_binding.json');verify_refs(binding['source_refs'])
    expected=ctx['directory']/'analysis'/f'analisis_{ctx["event_id"]}.md'
    if Path(analysis).resolve()!=expected.resolve():raise ValueError('Analysis must belong to the selected event directory')
    text=expected.read_text(encoding='utf-8');first=text.splitlines()[0].upper()
    snap=single.snapshot_info(Path(manifest['inputs']['odds']['path']),ctx,max_age=15)
    daily.final_odds_check({'kickoff_madrid':ctx['kickoff'].isoformat(),'capture_madrid':snap['capture']},t.now_madrid())
    if snap['sha256']!=binding['odds_sha256']:raise ValueError('PRICE_OR_LINE_CHANGED: analysis invalid; rerun affected stages or block')
    status='NO_BET' if 'NO BET' in first or 'NO_BET' in first else None
    if status is None:
        if not all((market,selection,odd)) or not ('CANDIDATO' in first):raise ValueError('Explicit analyst decision and exact quote required')
        if any(token in market.casefold() for token in ('jugador','goleador')):
            raise ValueError('PLAYER_MARKET_BLOCKED: exposición/XI oficial requieren revisión individual acreditada antes de decisión candidata')
        from bwin_market_preflight import snapshot_odds
        quotes=snapshot_odds(Path(manifest['inputs']['odds']['path']),ctx['event_id'])['_parsed_selection_rows']
        if not any(q['market']==market and q['selection']==selection and float(q['odd'])==float(odd) for q in quotes):raise ValueError('Final literal market/selection/odd not present')
        if market not in text or selection not in text:raise ValueError('Final quote not discussed in analysis')
        status='CANDIDATO_PARA_DECISION_MANUAL'
    result={'status':status,'fixture_id':ctx['fixture_id'],'bwin_event_id':ctx['event_id'],
            'analysis':reference(expected),'snapshot':snap,'checked_madrid':t.now_madrid().isoformat(),
            'value_opportunity_validated':False,'actionable_bet':False,'ready_to_bet':False}
    t.dump(ctx['directory']/'final_review.json',result)
    t.dump(ctx['directory']/'release_state.json',dict(result,version=VERSION,stage='FINAL_REVIEW_COMPLETED'))
    return result


def freeze(root):
    out=root/'outputs/releases'/VERSION;out.mkdir(parents=True,exist_ok=True)
    target=out/'source_snapshot.zip'
    if target.exists():raise ValueError('Release snapshot already exists; never overwrite rc1')
    paths=sorted([*root.glob('*.py'),*root.glob('*.md'),*root.glob('config/*.py'),*root.glob('config/*.json'),
                  *root.glob('config/tactical_profiles/*.json'),*root.glob('instructions/*.txt'),*root.glob('instructions/*.md'),
                  *root.glob('docs/*.md'),*root.glob('tests/*.py'),*root.glob('tests/fixtures/*')])
    records={str(p.relative_to(root)):reference(p) for p in paths if p.is_file()}
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in records:archive.write(root/name,name)
    preserved={str(p.relative_to(root)):reference(p) for p in (root/'outputs/engineering').glob('prematch_comparison_v2_1*') if p.is_file()}
    t.dump(out/'SOURCE_MANIFEST.json',{'version':VERSION,'python':sys.version,'executable':sys.executable,'platform':platform.platform(),
           'git':'NOT_A_REPOSITORY','frozen_madrid':t.now_madrid().isoformat(),'files':records,'archive':reference(target),'preserved_evidence':preserved})
    return target


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    sub=parser.add_subparsers(dest='action',required=True)
    b=sub.add_parser('bridge');b.add_argument('--handoff',type=Path,required=True);b.add_argument('--output-dir',type=Path)
    b.add_argument('--historical',action='store_true');b.add_argument('--asof')
    for name in ('research-kit','analysis-input','final-review'):
        p=sub.add_parser(name);p.add_argument('--selected',type=Path,default=Path('outputs/latest/selected_match.json'))
        if name=='final-review':
            p.add_argument('--analysis',type=Path,required=True);p.add_argument('--market');p.add_argument('--selection');p.add_argument('--odd',type=float)
    sub.add_parser('freeze')
    args=parser.parse_args(argv);root=args.root.resolve()
    try:
        if args.action=='bridge':result=bridge(root,args.handoff,args.output_dir,historical=args.historical,asof=t.dt(args.asof) if args.asof else None)
        elif args.action=='research-kit':result=research_kit(root,args.selected)
        elif args.action=='analysis-input':result=analysis_input(root,args.selected)
        elif args.action=='final-review':result=final_review(root,args.selected,args.analysis,args.market,args.selection,args.odd)
        else:result=freeze(root)
        print(json.dumps(result,ensure_ascii=False,indent=2) if isinstance(result,dict) else result)
        return 0
    except (ValueError,t.TrialError,OSError,KeyError,TypeError) as exc:
        if args.action=='final-review':
            try:
                ctx=single.load_context(argparse.Namespace(root=root,selected=args.selected,decision=None))
                t.dump(ctx['directory']/'release_state.json',{'status':'BLOQUEADO','reason':str(exc),'fixture_id':ctx['fixture_id'],
                    'bwin_event_id':ctx['event_id'],'actionable_bet':False,'updated_madrid':t.now_madrid().isoformat()})
            except (ValueError,OSError,KeyError,TypeError):pass
        print('BLOQUEADO:',exc,file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
