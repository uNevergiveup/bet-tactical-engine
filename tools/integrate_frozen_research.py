"""Integrate already calculated local artifacts without refitting or acquisition."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from prematch_market_mapping_v1 import integrate_advanced

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run',type=Path,required=True)
    ap.add_argument('--advanced',type=Path,required=True)
    ap.add_argument('--mapping',type=Path,required=True)
    ap.add_argument('--discovery',type=Path,required=True)
    ap.add_argument('--output-dir',type=Path,required=True)
    args=ap.parse_args()
    paths=[args.run/'base_ranking.json',args.advanced,args.mapping,args.run/'explanations.json',args.discovery]
    docs=[json.loads(p.read_text()) for p in paths]
    result=integrate_advanced(*docs)
    args.output_dir.mkdir(parents=True,exist_ok=False)
    result['sources']=[{'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    with (args.output_dir/'integrated_research.json').open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    lines=['# Investigación estadística integrada','', 'Prioridad descriptiva; valor financiero no demostrado.','']
    for row in result['ranking']:
        lines.extend([f"## {row['rank']}. {row['partido']} ({row['fixture_id']})",'',row['motivos_ranking'],row['limitaciones'],''])
        # One representative per family: full evidence remains in JSON.
        families={}
        for e in row['advanced_evidence']:families.setdefault(e['family'],e)
        for family,e in sorted(families.items()):
            lines.extend([f'### {family}',e['explanation'],
                f"Aplicabilidad: {e['applicability']['condition_status']}. Estado: {e['validation_status']}.",
                'Falta confirmar XI, comparabilidad del rival y contrato Bwin. Modelo predictivo no validado.',''])
    with (args.output_dir/'explanations.md').open('x') as f:f.write('\n'.join(lines))
    print(json.dumps({'fixtures':len(result['ranking']),'status':result['status'],'ready':False}))

if __name__=='__main__':main()
