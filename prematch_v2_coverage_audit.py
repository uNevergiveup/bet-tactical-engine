"""Read-only source audit; outputs a separate JSON, never acquires provider data."""
import argparse,json
from collections import defaultdict,Counter
from pathlib import Path
import prematch_advanced_discovery_v2 as e
from prematch_predictive_evidence_ranking_v1 import write_once

def run(base,cache_root,out):
    data=json.loads((base/'historical_observations.json').read_text());cache=e.EnrichedCache(cache_root)
    groups=defaultdict(list)
    for row in data['observations']:groups[(row['league_id'],row['season'],row['gender'],row['team_id'])].append(row)
    report=[];total=Counter()
    for key,rows in sorted(groups.items(),key=str):
        train,test=e.split_history(rows);metrics={}
        for metric in ('goals','xg','shots','on_target','corners','fouls','yellow','offsides'):
            n=sum(e.finite(r['metrics'].get('own.'+metric)) for r in rows)
            metrics[metric]={'valid':n,'total':len(rows),'train_valid':sum(e.finite(r['metrics'].get('own.'+metric)) for r in train),'bwin_status':'NOT_AUDITED_IN_THIS_STATISTICAL_SOURCE','provider_coverage':'UNKNOWN_NOT_PROVEN_BY_ABSENT_ACQUISITION'}
        missing=[]
        for row in rows:
            if not cache.candidates('fixtures',row['fixture_id']):
                missing.append({'fixture_id':row['fixture_id'],'reason':'FIXTURE_CACHE_ABSENT','players_cached':bool(cache.candidates('fixtures/players',row['fixture_id'])),'statistics_cached':bool(cache.candidates('fixtures/statistics',row['fixture_id']))});total['FIXTURE_CACHE_ABSENT']+=1
        report.append({'league_id':key[0],'season':key[1],'gender':key[2],'team_id':key[3],'families':metrics,'missing_identity':missing,'players':[],'player_status':'IDENTITY_NOT_ACQUIRED_NOT_PROVIDER_UNAVAILABLE'})
    result={'groups':report,'counts':dict(total),'cutoff_utc':data['audit']['cutoff_utc'],'acquisition':'NO_REQUESTS','commercial_availability':'INDEPENDENT_REQUIRES_BWIN_EVIDENCE'}
    write_once(out,json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+'\n');return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);p.add_argument('--cache-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(run(a.base,a.cache_root,a.output)['counts'])
