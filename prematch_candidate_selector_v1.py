#!/usr/bin/env python3
"""Offline candidate selector for Phase 2C.

Consumes only the evidence-ranking CSVs. It never calls APIs, opens Bwin, or
authorizes bets. The statistical universe remains available for later expansion.
"""
from __future__ import annotations
import argparse,csv,json,hashlib
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from prematch_predictive_evidence_ranking_v1 import write_once, write_csv

MADRID=ZoneInfo('Europe/Madrid')
FAMILIES=('goals','corners','shots','on_target','fouls','yellow','offsides')
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dt(v):
    x=datetime.fromisoformat(str(v).replace('Z','+00:00'))
    if x.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return x
def read(p):
    with Path(p).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def num(v):
    try:return float(v)
    except (TypeError,ValueError):return 0.0

def select(source:Path,out:Path,*,asof=None,max_bwin=5,top_general=20,top_family=10):
    manifests=source/'manifest.json'
    manifest=json.loads(manifests.read_text()) if manifests.exists() else {}
    files=sorted(source.glob('ranking_predictibilidad_*.csv'))
    if len(files)!=1: raise ValueError('GENERAL_RANKING_NOT_UNIQUE')
    general_file=files[0]; tag=general_file.stem.rsplit('_',1)[1]
    general=read(general_file)
    byid={r['fixture_id']:dict(r) for r in general}
    if len(byid)!=len(general): raise ValueError('DUPLICATE_FIXTURE_IN_GENERAL_RANKING')
    if max_bwin<0 or top_general<0 or top_family<0: raise ValueError('NEGATIVE_BUDGET')
    positions={fid:{} for fid in byid}
    family_files=[]
    for fam in FAMILIES:
        p=source/f'ranking_{fam}_{tag}.csv'; family_files.append(p)
        seen=set()
        for r in read(p):
            if r['fixture_id'] not in byid or r['fixture_id'] in seen: raise ValueError('FAMILY_FIXTURE_CONFLICT')
            seen.add(r['fixture_id'])
            positions[r['fixture_id']][fam]=int(r['rank'])
    chosen=set(r['fixture_id'] for r in general if int(r['rank'])<=top_general)
    chosen.update(fid for fid,ps in positions.items() if any(rank<=top_family for rank in ps.values()))
    asof=dt(asof) if asof else datetime.now(timezone.utc)
    if manifest.get('source_cutoff') and asof<dt(manifest['source_cutoff']):
        raise ValueError('SELECTION_BEFORE_SOURCE_CUTOFF')
    rows=[]
    for fid in sorted(chosen,key=lambda x:(int(byid[x]['rank']),int(x))):
        r=byid[fid]; kickoff=dt(r['kickoff_madrid']); prematch=kickoff.astimezone(timezone.utc)>asof.astimezone(timezone.utc)
        temporal='PREMATCH' if prematch else 'STARTED_OR_PAST'
        ps=positions.get(fid,{})
        fams=r['familias_destacadas']
        limitations=r['limitaciones']
        if 'offsides' in fams: limitations += '; fuera de juego solo experimental/táctico'
        reasons=[f"TOP general {r['rank']}" if int(r['rank'])<=top_general else 'TOP por familia']
        reasons += [f'{k} #{v}' for k,v in sorted(ps.items())]
        if not prematch: reasons.append('kickoff ya iniciado o pasado')
        else: reasons.append('sin Event ID Bwin verificado: pendiente de barrido')
        rows.append({
          'fixture_id':fid,'partido':r['partido'],'kickoff_madrid':r['kickoff_madrid'],'rank_general':r['rank'],
          'posiciones_por_familia':json.dumps(ps,ensure_ascii=False,sort_keys=True,separators=(',',':')),
          'familias_destacadas':fams,'patrones_respaldados':r['patrones_respaldados'],
          'patrones_sin_contradiccion':r['patrones_respaldados_sin_contradiccion'],'cobertura':r['cobertura_efectiva'],
          'muestra':r['tamaño_muestra'],'score_prioridad':r['puntuacion_priorizacion'],'motivos_seleccion':'; '.join(reasons),
          'limitaciones':limitations,'estado_temporal':temporal,'bwin_event_id':'','estado_identidad':'PENDING_BWIN_IDENTITY',
          # A future kickoff alone cannot authorize opening an unbound event.
          'elegible_para_scraping':'false'})
    rows.sort(key=lambda r:(0 if r['estado_temporal']=='PREMATCH' else 1,int(r['rank_general']),int(r['fixture_id'])))
    for i,r in enumerate(rows,1):r['rank_candidato']=i
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    fields=['rank_candidato','fixture_id','partido','kickoff_madrid','rank_general','posiciones_por_familia','familias_destacadas','patrones_respaldados','patrones_sin_contradiccion','cobertura','muestra','score_prioridad','motivos_seleccion','limitaciones','estado_temporal','bwin_event_id','estado_identidad','elegible_para_scraping']
    write_csv(out/f'candidatos_prematch_{tag}.csv',rows,fields)
    doc={'schema_version':'1.1-prematch-candidate-selection','status':'EVIDENCE_PRIORITY_UNCALIBRATED','evaluation_asof_utc':asof.astimezone(timezone.utc).isoformat(),'source_dir':str(source.resolve()),'window':manifest.get('window'),'source_hashes':{p.name:sha(p) for p in [general_file,*family_files]},'selection_policy':{'top_general':top_general,'top_family':top_family,'max_bwin_captures':max_bwin,'statistical_universe':len(byid),'no_bwin_scraping':True},'counts':{'candidate_unique':len(rows),'prematch_available':sum(r['estado_temporal']=='PREMATCH' for r in rows),'started_or_past':sum(r['estado_temporal']!='PREMATCH' for r in rows),'bwin_event_verified':0,'pending_identity':len(rows),'eligible_for_scraping':sum(r['elegible_para_scraping']=='true' for r in rows)},'candidates':rows}
    raw=json.dumps(doc,ensure_ascii=False,indent=2,sort_keys=True)+'\n'; write_once(out/f'candidatos_prematch_{tag}.json',raw)
    md=[f'# Selección PREMATCH 2C — {tag}','',f"Candidatos únicos: **{len(rows)}**; PREMATCH: **{doc['counts']['prematch_available']}**; Event ID Bwin verificado: **0**.",'',f'Universo disponible: {len(byid)} fixtures. Siguiente paso obligatorio: vincular identidad con evidencia Bwin local; cuotas y XI requieren comprobar frescura antes del uso comercial. Investigación externa opcional.','', '## TOP 20 candidato']
    for r in rows[:20]:md.append(f"{r['rank_candidato']}. `{r['fixture_id']}` {r['partido']} — general #{r['rank_general']}; familias {r['posiciones_por_familia']}; {r['motivos_seleccion']}")
    write_once(out/f'candidatos_prematch_{tag}.md','\n'.join(md)+'\n')
    return doc

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--ranking-dir',required=True,type=Path);p.add_argument('--output-dir',required=True,type=Path);p.add_argument('--asof');p.add_argument('--max-bwin-captures',type=int,default=5);a=p.parse_args(argv)
    doc=select(a.ranking_dir,a.output_dir,asof=a.asof,max_bwin=a.max_bwin_captures)
    print(json.dumps(doc['counts'],ensure_ascii=False))
    print(f'Archivos: {a.output_dir}. Siguiente paso obligatorio: vincular candidatos con catálogo Bwin verificable antes de captura. Investigación externa opcional; XI oficial requerido si condiciona la tesis. Sin scraping automático.')
    manifest=json.loads((a.ranking_dir/'manifest.json').read_text()) if (a.ranking_dir/'manifest.json').exists() else {}
    if manifest.get('window') and manifest.get('source'):
        print(f"Si existen preflights locales: python prematch_daily.py --date {manifest['window']['start'][:10]} --market-mapping-only --market-mapping-patterns {manifest['source']}")
if __name__=='__main__':main()
