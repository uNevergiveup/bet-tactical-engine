"""Reproduce commercial research from a frozen local run, without acquisition."""
import argparse,json,sys,hashlib
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from prematch_commercial_models import contract,eligible,estimate,validate,prematch_features
from prematch_market_mapping_v1 import _compatible,_preflight_identity,_load_market_rows,map_markets
from bwin_market_preflight import snapshot_odds

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--run',type=Path,required=True);ap.add_argument('--output-dir',type=Path,required=True)
    args=ap.parse_args();root=args.run;out=args.output_dir
    def load(p):return json.loads(p.read_text())
    mapping=load(root/'real_market_mapping/market_mapping.json');obs=load(root/'advanced_score_states/historical_observations.json')
    targets={str(r['fixture_id']):r for r in obs['targets']};base=load(root/'base_ranking.json')
    raw={p['pattern_id']:p for row in base for p in row['_rows']}
    catalogs={};models=[];snapshots=[]
    for path in sorted((root/'local_preflight').glob('*/market_preflight.json')):
        match=load(path)['matches'][0];fid=str(match['fixture_id']);target=targets[fid]
        pattern=next(r for r in raw.values() if str(r['fixture_id'])==fid)
        valid,reason=_preflight_identity(pattern,match)
        if not valid:raise ValueError(reason)
        rows,reason=_load_market_rows(path,match)
        if reason:raise ValueError(reason)
        snap=Path(match['snapshot']['snapshot_path']);snapshot=snapshot_odds(snap,match['bwin_event_id'])
        contracts=[contract(r,target) for r in snapshot['_parsed_selection_rows']]
        catalogs[fid]={'match':match,'contracts':contracts,'adapter_rows':rows}
        snapshots.append({'path':str(snap),'sha256':hashlib.sha256(snap.read_bytes()).hexdigest(),'metadata':match['snapshot']})
        for c in contracts:
            if c['status']!='EXACT_COUNT_CONTRACT':continue
            history=eligible(obs['observations'],target,c['metric'],c['period'],c['team_id'])
            fit=estimate([r['value'] for r in history],c['line']);validation=validate(history,c['line'])
            models.append({'fixture_id':int(fid),'event_id':match['bwin_event_id'],'contract':c,'model':fit,
                'validation':validation,'historical_records':history,'snapshot':match['snapshot'],
                'model_status':fit['status'],'quote_status':'FROZEN_QUOTE_NOT_CURRENT','commercially_evaluable':False,
                'lineup_dependency':'TEAM_COMPOSITION_UNVERIFIED','actionable_bet':False,
                'explanation':'Contrato literal observado; estimación histórica exploratoria. Sin calibración comercial independiente ni precio vigente acreditado.'})
    cases=[];unsupported=[];small=[]
    for row in mapping['mappings']:
        if row['reason']=='NO_COMPATIBLE_TEAM_MODEL_IN_V1':
            unsupported.append({**row,'classification':'EXCLUDED_COMMERCIAL_POLICY' if row['metric']=='offsides' else 'LATENT_EXPECTATION_NOT_A_SETTLED_COUNT',
                'resolution':'No modelo comercial legítimo directo: xG no son goles y offsides están excluidos.'});continue
        if row['reason']=='DESCRIPTIVE_PATTERN_LACKS_MINIMUM_SAMPLE':
            target=targets[str(row['fixture_id'])];h=eligible(obs['observations'],target,row['metric'],row['period'],row['team_id'])
            small.append({**row,'additional_local_compatible_history':h,'original_sample_gate_retained':True,
                'required':'Más antecedentes comparables de ambos equipos en la localía del patrón; sin rebajar mínimo.'})
        if row['reason']!='NO_EXACT_TEAM_MARKET_LINE_IN_VERIFIED_CAPTURE':continue
        fid=str(row['fixture_id']);c=catalogs[fid];pattern=raw[row['pattern_id']]
        relevant=[r for r in c['contracts'] if r['metric']==row['metric']]
        exact=[r for r in c['adapter_rows'] if _compatible(r,pattern)]
        classification=('EXACT_OWNER_ALIAS_FIXED' if exact else 'MARKET_REALLY_ABSENT_IN_CAPTURE' if not relevant else
            'MATCH_TOTAL_REQUIRES_JOINT_MODEL' if not any(r.get('scope')=='TEAM' for r in relevant) else
            'OWNER_OR_PERIOD_CONTRACT_MISMATCH')
        cases.append({**row,'pattern':pattern,'available_contracts':relevant,'all_observed_headings':sorted(set(r['market'] for r in c['contracts'])),
            'adapter_required':{'scope':'TEAM','team':pattern.get('team'),'period':pattern.get('period'),'metric':row['metric']},
            'classification':classification,'mismatch_field':'market.scope' if classification=='MATCH_TOTAL_REQUIRES_JOINT_MODEL' else 'market.owner_or_period' if relevant else 'market.metric',
            'direct_semantic_equivalence':bool(exact),'correctable_by_alias':bool(exact),
            'resolution':'Se evalúa total conjunto por separado; no equivale a total del equipo.' if classification=='MATCH_TOTAL_REQUIRES_JOINT_MODEL' else 'Conservar bloqueo de contrato exacto.'})
    integrated=load(root/'final_integration/integrated_research.json')
    for fixture in integrated['ranking']:
        fixture['commercial_research']=[r for r in models if r['fixture_id']==fixture['fixture_id']]
    for fixture in integrated['explanations']['fixtures']:
        fixture['commercial_research']=[r for r in models if r['fixture_id']==fixture['fixture_id']]
    advanced=load(root/'advanced_score_states/advanced_patterns.json')
    out.mkdir(parents=True,exist_ok=False)
    after=map_markets(Path(load(root/'predictive_pointer.json')['path']),sorted((root/'local_preflight').glob('*/market_preflight.json')),output_dir=out/'mapping_after')
    summary={'before':dict(Counter(r['reason'] for r in mapping['mappings'])),
        'after_original_patterns':dict(Counter(r['reason'] for r in after['mappings'])),
        'cases59':len(cases),'cases16':len(unsupported),'case_classifications':dict(Counter(r['classification'] for r in cases)),
        'unsupported_groups':dict(Counter(r['classification'] for r in unsupported)),
        'exact_count_selection_contracts':len(models),'models':dict(Counter(r['model_status'] for r in models)),
        'validated_commercial_selections':0,'value_demonstrated':0,'ready':False}
    docs={'cases59.json':cases,'cases16.json':unsupported,'sample_review.json':small,'models_validation.json':models,
        'summary.json':summary,'snapshots.json':snapshots,'integrated_research.json':integrated,
        'conditional_features.json':[{'pattern_id':r['pattern_id'],**prematch_features(r)} for r in advanced['patterns']]}
    for name,doc in docs.items():
        with (out/name).open('x') as handle:json.dump(doc,handle,ensure_ascii=False,indent=2)
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
