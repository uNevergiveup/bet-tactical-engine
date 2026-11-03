"""Offline API-Football observations, with availability and fixture cutoffs.

No network, inference of team IDs, or use of a cache downloaded after asof.
"""
from pathlib import Path
from datetime import timedelta
import json
from preselect_matches import read_stat_block, numeric
import prematch_prospective_trial as t


def observations_from_games(games, team_id, details, requested_ids=None):
    rows=[]
    requested=set(details if requested_ids is None else requested_ids)
    for g in games:
        other=g['away_id'] if g['home_id']==team_id else g['home_id']
        raw=details.get(g['id'],{})
        own=read_stat_block(raw,team_id);opp=read_stat_block(raw,other)
        # Preserve observed blocked shots without altering old readiness weights.
        for target,tid in ((own,team_id),(opp,other)):
            blocks=[v for v in raw.get('statistics',[]) if v.get('team',{}).get('id')==tid]
            if len(blocks)==1:
                for item in blocks[0].get('statistics',[]):
                    if item.get('type')=='Blocked Shots' and numeric(item.get('value')) is not None:
                        target['blocked_shots']=numeric(item['value'])
        # Only regular-time FT games enter the tactical sample.
        if g.get('status')!='FT':continue
        own['goals']=g['team_goals'];opp['goals']=g['opponent_goals']
        request_status=('NOT_REQUESTED' if g['id'] not in requested else
                        'VALID' if bool(raw.get('statistics')) and (own or opp) else 'REQUESTED_MISSING')
        rows.append({'fixture_id':g['id'],'team_id':team_id,'opponent_id':other,
                     'kickoff_utc':g['date_utc'],'league_id':g['league_id'],'season':g['season'],
                     'venue':g['venue'],'status':'FT','statistics_requested':g['id'] in requested,
                     'statistics_request_status':request_status,'own':own,'conceded':opp})
    return rows


class LocalHistoryCache:
    def __init__(self,root,asof):
        self.asof=asof;self.records={};self.sources={};self.loaded_sha={}
        for p in sorted((Path(root)/'outputs/data/cache/preselect_api').glob('*.json')):
            try:
                data=p.read_bytes();doc=json.loads(data);self.loaded_sha[str(p)]=__import__('hashlib').sha256(data).hexdigest();seen=t.dt(doc.get('retrieved_at_utc'))
                if seen is None or seen.timestamp()>asof.timestamp():continue
                for raw in doc.get('response',[]):
                    f=raw.get('fixture',{});fid=f.get('id');at=t.dt(f.get('date'))
                    if not isinstance(fid,int) or not at or at>=asof:continue
                    if f.get('status',{}).get('short')!='FT':continue
                    previous=self.records.get(fid)
                    # Prefer a complete statistics block over a later basic history row.
                    key=(bool(raw.get('statistics')),seen.timestamp())
                    if previous is None or key>previous[0]:
                        self.records[fid]=(key,raw,p)
            except (ValueError,OSError,TypeError):continue

    def for_team(self,team_id,league_id,cutoff,limit=20,season=None):
        from preselect_matches import prepare_history
        chosen=[];details={};refs={}
        for _,raw,path in self.records.values():
            teams=raw.get('teams',{});ids=[teams.get(s,{}).get('id') for s in ('home','away')]
            at=t.dt(raw['fixture']['date'])
            if team_id not in ids or raw.get('league',{}).get('id')!=league_id or not at or at>=cutoff:continue
            if season is not None and raw.get('league',{}).get('season')!=season:continue
            chosen.append(raw);details[raw['fixture']['id']]=raw
            refs[raw['fixture']['id']]=path
        games=prepare_history(chosen,team_id,cutoff,limit)
        # prepare_history now preserves FT status; backward compatibility with old snapshots is explicit.
        for g in games:g['status']='FT'
        requested={fid for fid,raw in details.items() if isinstance(raw.get('statistics'),list)}
        observations=observations_from_games(games,team_id,details,requested)
        used=sorted({refs[g['fixture_id']] for g in observations})
        for path in used:self.sources[str(path)]={'path':str(path),'sha256':self.loaded_sha[str(path)]}
        return observations
