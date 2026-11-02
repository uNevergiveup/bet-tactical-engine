"""Run from the extracted V2.1/code directory. Original bytes remain unchanged.

Only JSON path values are translated in memory to the relocated evidence tree.
SHA checks continue to hash the original archived bytes.
"""
import json
from pathlib import Path
import prematch_prospective_trial as t
import prematch_opportunity_scanner as scanner
from prematch_tactical_signal_engine import run_engine

def main():
    bundle=Path(__file__).resolve().parent.parent
    meta=json.loads((bundle/'replay_inputs.json').read_text())
    original_root=meta['original_root']
    tree=bundle/'frozen_project'
    def relocate(value):
        if isinstance(value,str) and value.startswith(original_root+'/'):
            return str(tree/value[len(original_root)+1:])
        if isinstance(value,list):return [relocate(x) for x in value]
        if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
        return value
    def read(path):
        return relocate(json.loads(Path(path).read_text(encoding='utf-8-sig')))
    t.read=read
    scanner.load_json=read
    pre=Path(relocate(meta['preselection']))
    result=run_engine(pre,Path(relocate(meta['pattern'])),bundle/'reproduced',tree,
        [Path(relocate(p)) for p in meta['preflights']],Path(relocate(meta['scanner'])),
        tree/'config/tactical_profiles/profiles.json',t.dt(meta['asof']),True)
    observed={fid:x['market_validation'] for fid,x in result['fixtures'].items()}
    for fid,expected in meta['expected_counts'].items():
        for key,value in expected.items():
            assert observed[fid][key]==value,(fid,key,observed[fid][key],value)
    print('OFFLINE_REPLAY_VERIFIED: original SHA checks and market counts; API=0, Bwin=0')

if __name__=='__main__':main()
