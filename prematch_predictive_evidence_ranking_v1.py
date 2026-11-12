#!/usr/bin/env python3
"""Offline evidence-priority ranking for the frozen Predictive Engine V1 snapshot.

This is a review-priority heuristic, not a probability or betting ranking.
It reads only ``predictive_patterns.json`` and never consults odds or APIs.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math
import io
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from pathlib import Path

FAMILIES = ('goals', 'corners', 'shots', 'on_target', 'fouls', 'yellow', 'offsides')
SUPPORTED = {'DESCRIPTIVE_SUPPORTED'}
def family_name(value):
    value = str(value or '')
    return 'yellow' if value == 'cards' else value
def category_name(pattern):
    """Map the canonical metric to the requested view without duplicating rows."""
    metric = str(pattern.get('metric') or '')
    if metric == 'on_target': return 'on_target'
    if metric == 'yellow': return 'yellow'
    if metric == 'xg': return 'goals'
    return family_name(pattern.get('family') or metric)

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def finite(x): return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def avg(xs):
    xs=[float(x) for x in xs if finite(x)]
    return sum(xs)/len(xs) if xs else 0.0
def pattern_status(p): return str(p.get('validation_status') or p.get('status') or 'INSUFFICIENT_EVIDENCE')
def coverage(p):
    c=p.get('coverage_effective_pct') or {}
    vals=[v for v in c.values() if finite(v)] if isinstance(c,dict) else []
    return avg(vals)/100.0
def sample(p):
    n=p.get('history_n') or {}
    vals=[v for v in n.values() if isinstance(v,(int,float)) and v>0]
    return min(vals) if vals else 0
def contrad(p): return str(p.get('contradiction_status') or '').upper() in {'PRESENT','CONTRADICTORY'}
def stability(p):
    s=str(p.get('stability_status') or '').upper()
    return 1.0 if s in {'AVAILABLE','STABLE','ROBUST'} else 0.5 if s in {'PRELIMINARY','LIMITED'} else 0.0

def row_quality(p):
    status=pattern_status(p)
    support=1.0 if status in SUPPORTED else 0.35 if status.startswith('EXPLOR') else 0.0
    no_contra=0.0 if contrad(p) else 1.0
    # Components are dimensionless and bounded; no goals/corners/etc. magnitudes are added.
    return 100*(0.35*support + 0.20*no_contra + 0.20*coverage(p) +
                0.15*min(sample(p),10)/10 + 0.10*stability(p))

def aggregate(fid, rows):
    first=rows[0]
    statuses=[pattern_status(p) for p in rows]
    supported=sum(s in SUPPORTED for s in statuses)
    exploratory=sum(s.startswith('EXPLOR') for s in statuses)
    insufficient=sum(s in {'INSUFFICIENT_EVIDENCE','INSUFFICIENT_DATA'} for s in statuses)
    no_contra=sum(not contrad(p) for p in rows)
    score=avg([row_quality(p) for p in rows])
    # Breadth is bounded and only rewards independent families, never raw scale.
    breadth=min(len(set(category_name(p) for p in rows)), len(FAMILIES))/len(FAMILIES)
    score=round(0.80*score + 20*breadth, 4)
    cov=round(avg([coverage(p) for p in rows])*100, 2)
    n=round(avg([sample(p) for p in rows]), 2)
    fam=defaultdict(list)
    for p in rows: fam[category_name(p) or 'unknown'].append(p)
    highlights=sorted(fam, key=lambda f: (-max(row_quality(x) for x in fam[f]), f))[:3]
    limited=[]
    if supported==0: limited.append('sin patrones DESCRIPTIVE_SUPPORTED')
    if no_contra < len(rows): limited.append('contradicciones registradas')
    if cov < 80: limited.append('cobertura efectiva parcial')
    if n < 5: limited.append('muestra reducida')
    if not limited: limited.append('prioridad basada en evidencia descriptiva; sin calibración fuera de muestra')
    return {
      'fixture_id':fid,'partido':f"{first.get('home','')}–{first.get('away','')}",
      'competición':first.get('league') or first.get('competition') or (f"league_id:{first.get('league_id')}" if first.get('league_id') is not None else ''),
      'kickoff_madrid':first.get('kickoff_madrid') or '',
      'patrones_totales':len(rows),'patrones_respaldados':supported,
      'patrones_respaldados_sin_contradiccion':sum(pattern_status(p) in SUPPORTED and not contrad(p) for p in rows),
      'patrones_exploratorios':exploratory,'patrones_sin_datos':insufficient,
      'cobertura_efectiva':cov,'tamaño_muestra':n,
      'familias_destacadas':','.join(highlights),'estabilidad':round(avg([stability(p) for p in rows]),3),
      'validacion_temporal':sum(str(p.get('temporal_validation_status','')).upper() not in {'','NOT_AVAILABLE','NOT_AVAILABLE_NO_HOLDOUT'} for p in rows),
      'puntuacion_priorizacion':score,'nivel_confianza':'EVIDENCE_PRIORITY_UNCALIBRATED',
      'motivos_ranking':'; '.join([f'{supported}/{len(rows)} respaldados',f'cobertura {cov:.1f}%',f'muestra media {n:.1f}']),'limitaciones':'; '.join(limited),
      '_rows':rows}

def family_row(fid, family, rows):
    base=aggregate(fid, rows); score=round(avg([row_quality(p) for p in rows]),4)
    values = {
      'fixture_id':fid,'partido':base['partido'],'competición':base['competición'],'kickoff_madrid':base['kickoff_madrid'],
      'familia':family,'equipo_local':rows[0].get('home',''),'equipo_visitante':rows[0].get('away',''),
      'metricas':','.join(sorted({str(p.get('metric')) for p in rows})),
      'goles_observados_n':sum(p.get('metric')=='goals' for p in rows),
      'xg_n':sum(p.get('metric')=='xg' for p in rows),
      'patrones_totales':len(rows),'patrones_respaldados':sum(pattern_status(p) in SUPPORTED for p in rows),
      'señal':'; '.join(sorted({str((p.get('effect_descriptive') or p.get('effect_quantified') or {}).get('direction','')) for p in rows if isinstance(p.get('effect_descriptive') or p.get('effect_quantified'),dict)})),
      'tamaño_muestra':round(avg([sample(p) for p in rows]),2),'cobertura_efectiva':round(avg([coverage(p) for p in rows])*100,2),
      'contradicciones':sum(contrad(p) for p in rows),'puntuacion_priorizacion':score,
      'motivo_prioridad':f"{sum(pattern_status(p) in SUPPORTED for p in rows)}/{len(rows)} respaldados; cobertura {round(avg([coverage(p) for p in rows])*100,2)}%; escala no combinada",
      'limitaciones':'fueras de juego: experimental/táctico' if family=='offsides' else 'sin calibración fuera de muestra'}
    return values

def write_csv(path, rows, fields):
    buf=io.StringIO(newline='')
    w=csv.DictWriter(buf,fieldnames=fields); w.writeheader(); w.writerows({k:r.get(k,'') for k in fields} for r in rows)
    write_once(path,buf.getvalue().encode('utf-8-sig'))

def write_once(path, raw):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    raw=raw.encode('utf-8') if isinstance(raw,str) else raw
    if path.exists():
        if path.read_bytes()!=raw: raise ValueError(f'OUTPUT_EXISTS_CONFLICT:{path}')
    else:
        with path.open('xb') as f: f.write(raw)

def unique_patterns(rows):
    seen={}
    for p in rows:
        key=p.get('pattern_id') or hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()
        if key in seen and seen[key]!=p: raise ValueError(f'PATTERN_ID_CONFLICT:{key}')
        seen[key]=p
    return list(seen.values())

def build(source, out, *, day=None, start='15:00', end='03:00', preselection=None):
    source=Path(source); doc=json.loads(source.read_text(encoding='utf-8-sig'))
    source_digest=sha(source)
    if not isinstance(doc.get('descriptive_discovery',{}).get('patterns'),list): raise ValueError('CANONICAL_PREDICTIVE_PATTERNS_REQUIRED')
    if preselection and sha(preselection)!=doc.get('preselection_sha256'):
        raise ValueError('PRESELECTION_SHA_MISMATCH')
    original=doc['descriptive_discovery']['patterns']
    pats=unique_patterns(original); by=defaultdict(list)
    cutoff=datetime.fromisoformat(doc['asof_madrid'].replace('Z','+00:00'))
    if cutoff.tzinfo is None: raise ValueError('CUTOFF_TIMEZONE_REQUIRED')
    inferred=min((datetime.fromisoformat(p['kickoff_madrid']).astimezone(ZoneInfo('Europe/Madrid')).date() for p in pats),default=cutoff.astimezone(ZoneInfo('Europe/Madrid')).date())
    date=datetime.fromisoformat(day).date() if day else inferred
    begin=datetime.combine(date,time.fromisoformat(start),ZoneInfo('Europe/Madrid'))
    finish=datetime.combine(date,time.fromisoformat(end),ZoneInfo('Europe/Madrid'))
    if finish<=begin: finish+=timedelta(days=1)
    if any(datetime.fromisoformat(p['kickoff_madrid'])<=cutoff for p in pats):
        raise ValueError('TARGET_NOT_FUTURE_AT_FROZEN_CUTOFF')
    pats=[p for p in pats if begin<=datetime.fromisoformat(p['kickoff_madrid'])<finish]
    tag=date.strftime('%Y%m%d')
    for p in pats: by[str(p.get('fixture_id'))].append(p)
    general=[aggregate(fid,rows) for fid,rows in by.items()]
    general.sort(key=lambda r:(-r['puntuacion_priorizacion'],-r['patrones_respaldados_sin_contradiccion'],-r['cobertura_efectiva'],r['kickoff_madrid'],int(r['fixture_id'])))
    for i,r in enumerate(general,1): r['rank']=i
    fields=['rank','fixture_id','partido','competición','kickoff_madrid','patrones_totales','patrones_respaldados','patrones_respaldados_sin_contradiccion','patrones_exploratorios','patrones_sin_datos','cobertura_efectiva','tamaño_muestra','familias_destacadas','estabilidad','validacion_temporal','puntuacion_priorizacion','nivel_confianza','motivos_ranking','limitaciones']
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    write_csv(out/f'ranking_predictibilidad_{tag}.csv',general,fields)
    family_fields=['rank','fixture_id','partido','competición','kickoff_madrid','familia','equipo_local','equipo_visitante','metricas','goles_observados_n','xg_n','patrones_totales','patrones_respaldados','señal','tamaño_muestra','cobertura_efectiva','contradicciones','puntuacion_priorizacion','motivo_prioridad','limitaciones']
    family_counts={}
    for fam in FAMILIES:
      rows=[]
      for fid,allrows in by.items():
        fr=[p for p in allrows if category_name(p)==fam]
        if fr: rows.append(family_row(fid,fam,fr))
      rows.sort(key=lambda r:(-r['puntuacion_priorizacion'],-r['patrones_respaldados'],-r['cobertura_efectiva'],int(r['fixture_id'])))
      for i,r in enumerate(rows,1): r['rank']=i
      write_csv(out/f'ranking_{fam}_{tag}.csv',rows,family_fields); family_counts[fam]=len(rows)
    individuals=[{'pattern_id':p.get('pattern_id'),'fixture_id':p['fixture_id'],'team':p.get('team'),'team_side':p.get('team_side'),'metric':p.get('metric'),'family':category_name(p),'score':round(row_quality(p),4),'validation_status':pattern_status(p),'contradictions':contrad(p),'sample':sample(p),'coverage':coverage(p)*100,'evidence_fixture_ids':json.dumps(p.get('evidence_fixture_ids',{}),sort_keys=True),'commercial_model_supported':p.get('metric') not in ('xg','offsides'),'source_sha256':source_digest} for p in pats]
    individuals.sort(key=lambda p:(-p['score'],str(p['fixture_id']),str(p['pattern_id'])))
    for i,p in enumerate(individuals,1):p['rank']=i
    write_csv(out/f'ranking_individual_{tag}.csv',individuals,['rank','pattern_id','fixture_id','team','team_side','metric','family','score','validation_status','contradictions','sample','coverage','evidence_fixture_ids','commercial_model_supported','source_sha256'])
    manifest={'schema_version':'1.1-evidence-priority-ranking','date_tag':tag,'window':{'start':begin.isoformat(),'end':finish.isoformat(),'timezone':'Europe/Madrid'},'source_cutoff':doc.get('source_cutoff_utc',doc['asof_madrid']),'status':'EVIDENCE_PRIORITY_UNCALIBRATED','source':str(source.resolve()),'source_sha256':sha(source),'source_patterns':len(original),'duplicate_patterns_removed':len(original)-len(unique_patterns(original)),'patterns':len(pats),'fixtures':len(by),'family_counts':family_counts,'actionable_bet':False,
              'scoring_formula':'0.80 * row_quality + 20 * bounded_family_breadth; row_quality=35% status support + 20% no contradiction + 20% coverage + 15% capped sample + 10% stability','sensitivity':'dimensionless bounded components; no metric magnitudes are added; ties use supported_without_contradiction, coverage, kickoff, fixture_id'}
    write_once(out/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
    return general, manifest

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--patterns',required=True,type=Path); ap.add_argument('--preselection',type=Path); ap.add_argument('--date'); ap.add_argument('--start',default='15:00'); ap.add_argument('--end',default='03:00'); ap.add_argument('--output-dir',required=True,type=Path); a=ap.parse_args(argv)
    rows,m=build(a.patterns,a.output_dir,day=a.date,start=a.start,end=a.end,preselection=a.preselection); print(json.dumps({'fixtures':len(rows),'patterns':m['patterns'],'output_dir':str(a.output_dir.resolve()),'next_action':'Seleccionar candidatos offline','next_command':f'python prematch_candidate_selector_v1.py --ranking-dir {a.output_dir} --output-dir {a.output_dir}/candidates','external_research':'OPTIONAL_AFTER_MARKET_VALIDATION'},ensure_ascii=False))
if __name__=='__main__': main()
