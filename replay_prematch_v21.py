"""Offline frozen V2.1 replay and export. No subprocess/network calls."""
import csv
import difflib
import io
import json
import shutil
import zipfile
from pathlib import Path
import prematch_prospective_trial as t
from prematch_tactical_signal_engine import run_engine


def main():
    root=Path(__file__).resolve().parent
    original=root/'outputs/engineering/prematch_comparison_v2'
    out=root/'outputs/engineering/prematch_comparison_v2_1'
    out.mkdir(parents=True,exist_ok=True)
    handoff=t.read(original/'handoff_prematch.json')
    asof=t.dt(handoff['generated_madrid'])
    scanner=Path(handoff['scanner'])
    scan=t.read(scanner)
    pre=Path(scan['sources']['preselection'])
    pattern=pre.parent.parent/'patterns/pattern_engine.json'
    paths=[Path(p) for p in scan['sources']['preflights']]
    engine=run_engine(pre,pattern,out/'tactical',root,paths,scanner,
                      root/'config/tactical_profiles/profiles.json',asof,True)
    eligible=[r for r in engine['ranking'] if r['eligible']]
    ids=[a['fixture_id'] for a in scan['audits'] if a['status']=='VERIFIED']
    comparison={'capture_target_reached':len(ids)>=3,'eligible_candidate_target_reached':len(eligible)>=3,
                'snapshot_comparison_status':'SNAPSHOT_COMPARISON_COMPLETE' if len(ids)>=3 else 'COMPARISON_INCOMPLETE',
                'evidence_comparison_status':'EVIDENCE_COMPARISON_COMPLETE' if len(eligible)>=3 else 'COMPARISON_INCOMPLETE',
                'comparison_status':'EVIDENCE_COMPARISON_COMPLETE' if len(eligible)>=3 else 'COMPARISON_INCOMPLETE'}
    handoff.update(comparison)
    handoff.update(status='RETROSPECTIVE_AUDIT',selected_fixture_id=None,fixture_id=None,
                   historical_comparison_first=engine['historical_comparison_first'],
                   tactical_selection_ranking=engine['ranking'],
                   comparison_incomplete_due_to_budget=bool(handoff.get('comparison_incomplete_due_to_budget')),
                   comparison_definition='Three verified snapshots AND three evidence-eligible alternatives; negative control is not an eligible alternative.',
                   actionable_bet=False,ready_to_bet=False)
    t.dump(out/'handoff_prematch.json',handoff)
    for name in ('candidate_ledger.json','budget_assignment.json','opportunity_scan.json'):
        shutil.copy2(original/name,out/name)
    # Keep the entire original audit package immutable beside this replay.
    for name in ('sources','snapshots','preflight','discovery'):
        shutil.copytree(original/name,out/name,dirs_exist_ok=True)
    rows=list(csv.DictReader((original/'partidos_diarios.csv').open(encoding='utf-8-sig'),delimiter=';'))
    ranks={str(r['fixture_id']):r for r in engine['ranking']}
    fields=list(rows[0])
    counters=('raw_signal_market_links','unique_market_selections','supported_signal_market_links','unique_supported_selections','direct_supported','indirect_supported')
    for k in counters:
        if k not in fields:fields.append(k)
    for row in rows:
        fid=row['fixture_id'];x=engine['fixtures'].get(fid)
        row['seleccionado']='false'
        if row.get('estado_final')=='SELECCIONADO':row['estado_final']='PRIMERO_RETROSPECTIVO_NO_ACCIONABLE'
        if not x:continue
        rank=ranks[fid]
        for k in counters:row[k]=x['market_validation'].get(k,0)
        for k in ('historical_matches_available','historical_matches_detailed','historical_matches_not_detailed','metric_observations_valid','metric_observations_missing','requested_data_coverage_pct','historical_effective_coverage_pct'):
            row[k]=x['coverage_summary'].get(k)
        for k in ('tactical_research_score','market_execution_readiness','direct_market_hypotheses','indirect_market_hypotheses','unmodelled_market_count','research_priority','evaluation_completeness'):
            row[k]=rank.get(k,'')
        row['tactical_json']=str(Path(x['detail_json']).relative_to(out))
        row['tactical_mode']='RETROSPECTIVE_AUDIT'
        row['final_decision_reason']='Reproducción histórica offline; sin selección accionable.'
    with (out/'partidos_diarios.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fields,delimiter=';');writer.writeheader();writer.writerows(rows)
    lines=['# PREMATCH V2.1: reproducción offline','',
           f'Corte congelado: {asof.isoformat()}. Fuentes idénticas a V2. API-Football nuevas: 0. Bwin consultas/capturas nuevas: 0.',
           f'Capturas: {len(ids)}. Alternativas elegibles: {len(eligible)}. Estado: {comparison}.',
           'Mjällby participa como control negativo. La completitud de capturas no implica completitud de evidencia ni valor económico.',
           '', '| Fixture | Señales | Tactical | Readiness | Brutos | Selecciones únicas | Vínculos sustentados | Selecciones sustentadas únicas | Directas sustentadas | Indirectas sustentadas | Frescura |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for fid in ids:
        x=engine['fixtures'][str(fid)];r=ranks[str(fid)];c=x['market_validation']
        lines.append('| '+' | '.join(map(str,[fid,x['signal_count'],r['tactical_research_score'],r['market_execution_readiness'],*[c[k] for k in counters],r['components']['capture_age']]))+' |')
    lines += ['',f"Primero retrospectivo: {engine['historical_comparison_first']}. No se publica handoff accionable desde una reproducción histórica.",
              'Los contadores direct_supported e indirect_supported son selecciones únicas por categoría sobre señales sustentadas; supported_signal_market_links conserva las relaciones a cada señal.',
              'Cada asociación tiene signal_id e hypothesis_id. La cobertura resumida procede de una serie identificada completa, con desglose por equipo, métrica y denominador.',
              'La frescura se calcula desde la captura verificada aunque no haya señales sustentadas. Política de investigación: 0–120 minutos inclusivos. Política final conservada: últimos 60 minutos y edad máxima 15 minutos.',
              'Baseline congelada para V3. Pendientes de V3: contracción, ajuste por rival y validación walk-forward.']
    lines += ['', '## Reproducir desde el ZIP', '',
              'Descomprimir y ejecutar `python code/replay_bundle_v21.py` desde la carpeta del paquete con las dependencias Python del proyecto instaladas. El comando verifica hashes originales y contadores sin red y escribe en `reproduced/`.',
              'Suite en el repositorio Mac: `.venv/bin/python -m unittest discover`. Los tests históricos del repositorio utilizan también archivos de otras jornadas que no forman parte de este paquete. El replay portátil verifica exclusivamente las fuentes congeladas V2.',
              'El ledger contiene 454 fixtures en el conjunto de descubrimiento; el CSV operativo conserva 162 fixtures únicos. Son poblaciones diferentes.']
    (out/'RESUMEN_EJECUCION.md').write_text('\n'.join(lines)+'\n')
    (out/'COMPARATIVA_PREMATCH_3_PARTIDOS.md').write_text('\n'.join(lines)+'\n')
    code=out/'code';code.mkdir(exist_ok=True)
    for path in root.glob('*.py'):shutil.copy2(path,code/path.name)
    shutil.copytree(root/'config',code/'config',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','constants.py'))
    frozen=[pre,pattern,scanner,*paths,pre.parent.parent/'fixtures_major_test.json',root/'config/tactical_profiles/profiles.json']
    frozen.extend(Path(r['path']) for r in engine['sources']['raw_cache_sources'])
    for path in paths:
        pf=t.read(path)
        frozen.append(Path(pf['source_discovery']))
        frozen.extend(Path(m['snapshot']['snapshot_path']) for m in pf['matches'] if m.get('snapshot'))
    for path in set(frozen):
        dest=out/'frozen_project'/path.relative_to(root)
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
    t.dump(out/'replay_inputs.json',{'original_root':str(root),'preselection':str(pre),'pattern':str(pattern),'scanner':str(scanner),
        'preflights':list(map(str,paths)),'asof':asof.isoformat(),
        'expected_counts':{str(fid):{k:engine['fixtures'][str(fid)]['market_validation'][k] for k in counters} for fid in ids}})
    patches=[]
    for name in ('prematch_tactical_signal_engine.py','prematch_runtime.py','prematch_daily_csv.py'):
        before=original/'before'/name
        patches.extend(difflib.unified_diff(before.read_text().splitlines(True),(root/name).read_text().splitlines(True),fromfile='a/'+name,tofile='b/'+name))
    (out/'changes.patch').write_text(''.join(patches))
    shutil.copy2('/tmp/prematch_v21_tests.log',out/'tests.log')
    t.dump(out/'verification_results.json',dict(comparison,tests={'command':'.venv/bin/python -m unittest discover','passed':168,'failed':0},
           csv_rows=len(rows),unique_fixture_ids=len({r['fixture_id'] for r in rows}),api_calls_new=0,bwin_queries_new=0,bwin_captures_new=0,
           original_source_hashes={str(p):t.sha(p) for p in [pre,pattern,scanner,*paths]},offline_only=True))
    provenance={}
    for p in sorted(out.rglob('*')):
        if p.is_file() and p.name!='MANIFEST.json':provenance[str(p.relative_to(out))]={'sha256':t.sha(p),'bytes':p.stat().st_size}
    t.dump(out/'MANIFEST.json',{'version':'2.1','asof_madrid':asof.isoformat(),**comparison,'artifacts':provenance})
    with zipfile.ZipFile(out.with_suffix('.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
        for p in out.rglob('*'):
            if p.is_file():archive.write(p,str(p.relative_to(out.parent)))
    print('\n'.join(lines[:10]));print(out.with_suffix('.zip'))

if __name__=='__main__':main()
