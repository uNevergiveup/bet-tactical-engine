#!/usr/bin/env python3
"""Primera evaluación cuantitativa de primeras divisiones (SIN PICKS).

Entrada: fixtures_major.json producido por filter_major_leagues.py.
Usa API-Football /fixtures (historial), /fixtures?ids (estadísticas por lotes)
y /standings (tabla vigente). No accede a Bwin ni genera probabilidades.

Ejemplos:
 python preselect_matches.py --input outputs/discovery/2026-11-01_1600_0200/fixtures_major.json --max-matches 5
 python preselect_matches.py --input outputs/discovery/2026-11-01_1600_0200/fixtures_major.json
 python preselect_matches.py --input .../fixtures_major.json --fixture-id 1570440
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from urllib.parse import urlencode
import requests
from prematch_cache_record import historical_available, acquisition_before

VERSION = '1.3-preselection'
UTC = timezone.utc
FT = {'FT','AET','PEN'}
METRICS = {'corners':'Corner Kicks','shots':'Total Shots','on_target':'Shots on Goal',
           'fouls':'Fouls','offsides':'Offsides','yellow':'Yellow Cards','xg':'expected_goals'}

# Additional observed fields; deliberately excluded from readiness weights.
EXTRA_METRICS = {'shots_off_target':'Shots off Goal','blocked_shots':'Blocked Shots',
    'possession_pct':'Ball Possession','red':'Red Cards','saves':'Goalkeeper Saves',
    'passes':'Total passes','accurate_passes':'Passes accurate','pass_accuracy_pct':'Passes %',
    'shots_inside_box':'Shots insidebox','shots_outside_box':'Shots outsidebox'}

class PreselectionError(RuntimeError): pass
class GlobalApiError(RuntimeError): pass


def utc_datetime(value:Any) -> datetime:
    if not isinstance(value,str): raise ValueError('Fecha inexistente')
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None or d.utcoffset() is None:raise ValueError('Fecha sin zona explícita')
    return d.astimezone(UTC)


def positive_int(v:Any) -> int | None:
    return v if isinstance(v,int) and not isinstance(v,bool) and v>0 else None


def numeric(v:Any) -> float | None:
    if isinstance(v,bool) or v is None: return None
    if isinstance(v,(int,float)) and math.isfinite(v):return float(v)
    if isinstance(v,str):
        v=v.strip().replace('%','').replace(',','.')
        try:
            x=float(v)
            return x if math.isfinite(x) else None
        except ValueError:pass
    return None


def summarize(values:list[float]) -> dict[str,Any]:
    if not values:return {'n':0,'mean':None,'stdev':None,'cv':None,'min':None,'max':None}
    avg=mean(values);std=pstdev(values)
    return {'n':len(values),'mean':round(avg,3),'stdev':round(std,3),
            'cv':round(std/abs(avg),3) if len(values)>1 and abs(avg)>0.0001 else None,
            'min':min(values),'max':max(values)}


class ApiFootball:
    """Cache por consulta, límite simple 3 rps, errores explícitos, nunca almacena claves."""
    def __init__(self,api_key:str,base_url:str,root:Path,timeout:int=25,refresh:bool=False,
                 request_budget:int=0,request_reserve:int=0,asof:datetime|None=None):
        self.asof=asof
        self.root=root;self.base_url=base_url.rstrip('/');self.timeout=timeout
        self.refresh=refresh;self.last_request=0.0;self.requests_sent=0;self.cache_hits=0
        if request_budget<0 or request_reserve<0 or request_budget and request_reserve>=request_budget:
            raise PreselectionError('Presupuesto API inválido: reserve debe ser menor que budget')
        self.request_budget=request_budget;self.request_reserve=request_reserve;self.request_ledger=[]
        self.consecutive_failures=0
        self._ft_detail_cache_index:dict[int,tuple[datetime,dict[str,Any],Path,str]]|None=None
        self.session=requests.Session()
        self.session.headers.update({'x-apisports-key':api_key,'Accept':'application/json',
                                     'User-Agent':f'prematch-preselection/{VERSION}'})
    def close(self):self.session.close()
    def _load_ft_detail_cache_index(self) -> dict[int,tuple[datetime,dict[str,Any],Path,str]]:
        if self._ft_detail_cache_index is not None:
            return self._ft_detail_cache_index
        index:dict[int,tuple[datetime,dict[str,Any],Path,str]]={}
        cache_dir=self.root/'outputs'/'data'/'cache'/'preselect_api'
        if not cache_dir.is_dir():
            self._ft_detail_cache_index=index
            return index
        for p in sorted(cache_dir.glob('*.json')):
            try:
                raw_bytes=p.read_bytes()
                obj=json.loads(raw_bytes)
                stamp=utc_datetime(obj.get('retrieved_at_utc'))
                if self.asof is not None and stamp>self.asof:
                    continue
                rows=obj.get('response')
                if not isinstance(rows,list):
                    continue
                file_sha=hashlib.sha256(raw_bytes).hexdigest()
                for row in rows:
                    if not isinstance(row,dict):continue
                    fix=row.get('fixture') or {}
                    fid=positive_int(fix.get('id'))
                    if fid is None or (fix.get('status') or {}).get('short')!='FT':continue
                    stats=row.get('statistics')
                    if not isinstance(stats,list) or not stats:continue
                    prev=index.get(fid)
                    if prev is None or stamp>prev[0]:
                        index[fid]=(stamp,row,p,file_sha)
            except (OSError,ValueError,TypeError,KeyError):
                continue
        self._ft_detail_cache_index=index
        return index
    def get_fixture_details(self,batch:list[int],cutoff:datetime|None=None) -> list[dict[str,Any]]:
        if self.refresh or not batch:
            return self.get('fixtures',{'ids':'-'.join(str(x) for x in batch)}) if batch else []
        limit=self.asof or cutoff or datetime.now(UTC)
        index=self._load_ft_detail_cache_index()
        cached_rows=[];reused_fids=[];used_sources={};remaining=[]
        max_stamp=None
        for fid in batch:
            hit=index.get(fid)
            if hit is not None:
                stamp,row,path,file_sha=hit
                if stamp<=limit and historical_available(row,limit):
                    cached_rows.append(dict(row,_acquired_at_utc=stamp.isoformat()))
                    reused_fids.append(fid)
                    used_sources[str(path.resolve())]={'path':str(path.resolve()),'sha256':file_sha,'retrieved_at_utc':stamp.isoformat()}
                    if max_stamp is None or stamp>max_stamp:max_stamp=stamp
                    continue
            remaining.append(fid)
        if cached_rows:
            self.cache_hits+=1
            self.request_ledger.append({
                'request_id':'hist_ft_detail_'+'_'.join(str(x) for x in reused_fids),
                'endpoint':'fixtures',
                'parameters':{'ids':'-'.join(str(x) for x in reused_fids)},
                'requested_at_utc':datetime.now(UTC).isoformat(),
                'status':'HISTORICAL_DETAIL_CACHE_HIT',
                'attempts':0,
                'reused_fixture_ids':reused_fids,
                'cache_sources':list(used_sources.values()),
                'first_observed_at_utc':max_stamp.isoformat() if max_stamp else None,
            })
        if remaining:
            cached_rows.extend(self.get('fixtures',{'ids':'-'.join(str(x) for x in remaining)}))
        return cached_rows
    def get(self,route:str,params:dict[str,Any]) -> list[dict[str,Any]]:
        query=urlencode(sorted((str(k),str(v)) for k,v in params.items()))
        key=hashlib.sha256((route+'?'+query).encode()).hexdigest()
        cache=self.root/'outputs'/'data'/'cache'/'preselect_api'/f'{key}.json'
        if not self.refresh and cache.is_file():
            try:
                obj=json.loads(cache.read_text(encoding='utf-8'))
                stamp=utc_datetime(obj['retrieved_at_utc'])
                age=(self.asof or datetime.now(UTC))-stamp
                if timedelta(0)<=age<timedelta(hours=12) and isinstance(obj.get('response'),list) and obj.get('endpoint',route)==route and obj.get('parameters',dict(sorted(params.items())))==dict(sorted(params.items())):
                    self.cache_hits+=1
                    self.request_ledger.append({'request_id':key,'endpoint':route,'parameters':dict(sorted(params.items())),
                        'requested_at_utc':datetime.now(UTC).isoformat(),'status':'CACHE_HIT','attempts':0,
                        'cache_path':str(cache.resolve()),'cache_sha256':hashlib.sha256(cache.read_bytes()).hexdigest(),
                        'response_sha256':hashlib.sha256(json.dumps(obj['response'],sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                        'first_observed_at_utc':obj['retrieved_at_utc']})
                    return [dict(row, _acquired_at_utc=stamp.isoformat()) if isinstance(row,dict) else row for row in obj['response']]
            except (OSError,ValueError,TypeError,KeyError):pass
        if self.asof is not None:
            raise PreselectionError('FROZEN_ASOF_REQUIRES_VERIFIED_PRIOR_CACHE')
        if self.request_budget and self.requests_sent>=self.request_budget-self.request_reserve:
            self.request_ledger.append({'request_id':key,'endpoint':route,'parameters':dict(sorted(params.items())),
                'requested_at_utc':datetime.now(UTC).isoformat(),'status':'BLOCKED_BY_REQUEST_RESERVE',
                'attempts':0,'budget':self.request_budget,'reserve':self.request_reserve})
            raise PreselectionError('API_REQUEST_BUDGET_RESERVE_REACHED')
        last_error=''
        for attempt in range(3):
            try:
                if self.request_budget and self.requests_sent>=self.request_budget-self.request_reserve:
                    raise PreselectionError('API_REQUEST_BUDGET_RESERVE_REACHED')
                elapsed=time.monotonic()-self.last_request
                if elapsed < 0.36:time.sleep(0.36-elapsed)
                self.last_request=time.monotonic()
                resp=self.session.get(self.base_url+'/'+route.lstrip('/'),params=params,timeout=self.timeout)
                self.requests_sent+=1
                if resp.status_code in (401,403):raise GlobalApiError(f'API error HTTP {resp.status_code}: credenciales/permisos')
                if resp.status_code==429:
                    last_error='429 rate limit';time.sleep(min(60,float(resp.headers.get('Retry-After','8'))));continue
                if 400<=resp.status_code<500:raise PreselectionError(f'API HTTP {resp.status_code}: {resp.text[:180]}')
                resp.raise_for_status()
                data=resp.json()
                if not isinstance(data,dict) or data.get('errors'):
                    raise PreselectionError(f'API /{route} devolvió errors: {data.get("errors") if isinstance(data,dict) else "estructura"}')
                rows=data.get('response')
                if not isinstance(rows,list):raise PreselectionError('Respuesta sin lista response')
                page=data.get('paging') or {}
                if int(page.get('total') or 1)>1:raise PreselectionError(f'/{route} paginado; no se admite muestra parcial')
                cache.parent.mkdir(parents=True,exist_ok=True)
                tmp=cache.with_name(f'.{cache.name}.{os.getpid()}.tmp')
                tmp.write_text(json.dumps({'retrieved_at_utc':datetime.now(UTC).isoformat(),
                                           'endpoint':route,'parameters':dict(sorted(params.items())),
                                           'response':rows},ensure_ascii=False),encoding='utf-8')
                from prematch_cache_record import preserve_cache_version
                preserve_cache_version(cache)
                tmp.replace(cache)
                self.request_ledger.append({'request_id':key,'endpoint':route,'parameters':dict(sorted(params.items())),
                    'requested_at_utc':datetime.now(UTC).isoformat(),'status':'NETWORK_OK','attempts':attempt+1,
                    'http_status':resp.status_code,'cache_path':str(cache.resolve()),
                    'cache_sha256':hashlib.sha256(cache.read_bytes()).hexdigest(),
                    'response_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                    'first_observed_at_utc':json.loads(cache.read_text(encoding='utf-8'))['retrieved_at_utc']})
                self.consecutive_failures=0
                acquired=self.request_ledger[-1]['first_observed_at_utc']
                return [dict(row, _acquired_at_utc=acquired) if isinstance(row,dict) else row for row in rows]
            except (requests.RequestException,ValueError) as exc:
                last_error=str(exc)
                if attempt<2:time.sleep(attempt+1)
        self.request_ledger.append({'request_id':key,'endpoint':route,'parameters':dict(sorted(params.items())),
            'requested_at_utc':datetime.now(UTC).isoformat(),'status':'NETWORK_ERROR','attempts':3,'error':last_error})
        self.consecutive_failures+=1
        if self.consecutive_failures>=3:raise GlobalApiError('API_GENERAL_UNAVAILABILITY: '+last_error)
        raise PreselectionError(f'Falló /{route}?{query}: {last_error}')


def prepare_history(raw:list[dict[str,Any]],team_id:int,as_of:datetime,
                    lookback:int=20) -> list[dict[str,Any]]:
    games=[];ids=set()
    for r in raw:
        if not isinstance(r,dict):continue
        fix=r.get('fixture') or {};teams=r.get('teams') or {};goals=r.get('goals') or {};lg=r.get('league') or {}
        fid=positive_int(fix.get('id'))
        if fid is None or fid in ids:continue
        try:dt=utc_datetime(fix.get('date'))
        except ValueError:continue
        if dt>=as_of or dt<as_of-timedelta(days=600) or (fix.get('status') or {}).get('short') not in FT:continue
        if not historical_available(r, as_of):continue
        home=(teams.get('home') or {}).get('id');away=(teams.get('away') or {}).get('id')
        if team_id not in (home,away) or home==away:continue
        gh=goals.get('home');ga=goals.get('away')
        if not isinstance(gh,int) or isinstance(gh,bool) or not isinstance(ga,int) or isinstance(ga,bool):continue
        ids.add(fid)
        games.append({**{key:r[key] for key in ('result_available_at_utc','completed_at_utc','fixture_completed_at_utc','_acquired_at_utc') if key in r},'id':fid,'date_utc':dt.isoformat(),'status':(fix.get('status') or {}).get('short'),'league_id':lg.get('id'),
                      'season':lg.get('season'),'home_id':home,'away_id':away,
                      'goals_home':gh,'goals_away':ga,
                      'team_goals':gh if team_id==home else ga,
                      'opponent_goals':ga if team_id==home else gh,
                      'venue':'HOME' if home==team_id else 'AWAY'})
    games.sort(key=lambda x:x['date_utc'],reverse=True)
    return games[:lookback]


def team_basic(games:list[dict[str,Any]],league_id:int,now:datetime) -> dict[str,Any]:
    same=[g for g in games if g['league_id']==league_id]
    return {'completed_total':len(games),'same_competition_total':len(same),
       'home_games':sum(g['venue']=='HOME' for g in games),
       'away_games':sum(g['venue']=='AWAY' for g in games),
       'days_since_last_match':round((now-utc_datetime(games[0]['date_utc'])).total_seconds()/86400,1) if games else None,
       'goals_for':summarize([g['team_goals'] for g in games]),
       'goals_against':summarize([g['opponent_goals'] for g in games]),
       'same_competition_goals_for':summarize([g['team_goals'] for g in same]),
       'same_competition_goals_against':summarize([g['opponent_goals'] for g in same])}


def read_stat_block(detail:dict[str,Any],team_id:int) -> dict[str,float]:
    entries=detail.get('statistics')
    if not isinstance(entries,list):return {}
    matching=[x for x in entries if isinstance(x,dict) and (x.get('team') or {}).get('id')==team_id]
    if len(matching)!=1:return {}
    stats=matching[0].get('statistics') or []
    mapped={s.get('type'):numeric(s.get('value')) for s in stats if isinstance(s,dict)}
    # xG alternativo en algunas versiones de API
    if mapped.get('expected_goals') is None:mapped['expected_goals']=mapped.get('Expected Goals')
    return {alias:mapped[k] for alias,k in {**METRICS, **EXTRA_METRICS}.items() if mapped.get(k) is not None}


def team_detail(games:list[dict[str,Any]],team_id:int,details:dict[int,dict[str,Any]]) -> dict[str,Any]:
    metrics={key:[] for key in METRICS}
    conceded={key:[] for key in METRICS}
    checked=0
    for g in games:
        r=details.get(g['id'])
        if not r:continue
        checked+=1
        other=g['away_id'] if g['home_id']==team_id else g['home_id']
        own=read_stat_block(r,team_id);opp=read_stat_block(r,other)
        for key in METRICS:
            if key in own:metrics[key].append(own[key])
            if key in opp:conceded[key].append(opp[key])
    return {'fixture_details_available':checked,
      'own':{k:summarize(v) for k,v in metrics.items()},
      'conceded':{k:summarize(v) for k,v in conceded.items()},
      'missing_not_zero':True}


def select_detail_games(games:list[dict[str,Any]],league_id:int,season:int,
                        cutoff:datetime,limit:int) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    """Elige la muestra que el motor táctico realmente admite sin alterar el histórico general."""
    eligible=[];rejected=Counter()
    for game in games:
        try:at=utc_datetime(game.get('date_utc'))
        except ValueError:
            rejected['INVALID_DATE']+=1;continue
        if at>=cutoff:rejected['AT_OR_AFTER_CUTOFF']+=1
        elif game.get('status')!='FT':rejected['NON_REGULAR_TIME_FT']+=1
        elif game.get('league_id')!=league_id:rejected['DIFFERENT_COMPETITION']+=1
        elif game.get('season')!=season:rejected['DIFFERENT_SEASON']+=1
        else:eligible.append(game)
    eligible.sort(key=lambda x:x['date_utc'],reverse=True)
    selected=eligible[:limit]
    return selected,{'policy':'SAME_LEAGUE_SEASON_FT_BEFORE_CUTOFF','requested_cap':limit,
        'eligible_n':len(eligible),'selected_n':len(selected),
        'selected_fixture_ids':[g['id'] for g in selected],
        'alternative_sources_used':[],'rejected_reasons':dict(sorted(rejected.items())),
        'shortfall_n':max(0,limit-len(selected))}


def standing_for(raw:list[dict[str,Any]],team_id:int) -> dict[str,Any]:
    matches=[]
    for obj in raw:
        lg=(obj.get('league') or {}) if isinstance(obj,dict) else {}
        for group in lg.get('standings') or []:
            if not isinstance(group,list):continue
            for r in group:
                if (r.get('team') or {}).get('id')==team_id:matches.append(r)
    if len(matches)!=1:return {'status':'NOT_AVAILABLE' if not matches else 'AMBIGUOUS'}
    r=matches[0]
    return {'status':'AVAILABLE','rank':r.get('rank'),'points':r.get('points'),
       'goals_diff':r.get('goalsDiff'),'form':r.get('form'),'group':r.get('group'),
       'description':r.get('description'),'played':(r.get('all') or {}).get('played')}


def last_h2h(home:list[dict[str,Any]],away:list[dict[str,Any]],home_id:int,away_id:int) -> dict[str,Any]:
    matched=[g for g in home if set((g['home_id'],g['away_id']))=={home_id,away_id}]
    if not matched:return {'status':'NOT_FOUND_IN_RECENT_HISTORY','count':0,'last':None}
    return {'status':'FOUND_IN_TEAM_HISTORY','count':len(matched),'last':matched[0]}


# Los pesos describen utilidad para investigar familias estadísticas, NO ventaja de cuotas.
METRIC_WEIGHTS = {'corners':14, 'shots':12, 'on_target':8, 'fouls':7,
                  'offsides':4, 'yellow':7, 'xg':8}  # 60 puntos


def preliminary_score(h:dict[str,Any],a:dict[str,Any],
                      hs:dict[str,Any],ass:dict[str,Any]) -> int:
    """Cobertura exploratoria 0..100: históricos, liga, sede, tabla, recencia.

    No compara métricas de juego; eso exige consultar detalles después.
    El umbral no constituye una exclusión, solo prioridad de adquisición.
    """
    score=0.0
    for team,venue in ((h,'home_games'),(a,'away_games')):
        score+=15*min(team['completed_total'],20)/20
        score+=18*min(team['same_competition_total'],20)/20
        score+=4*min(team[venue],8)/8
        rec=team.get('days_since_last_match')
        score+=3 if rec is not None and rec<=35 else (1 if rec is not None and rec<=75 else 0)
    for table in (hs,ass):
        if table.get('status')=='AVAILABLE':score+=10
    return round(min(100,max(0,score)))


def initial_data_gate(h:dict[str,Any],a:dict[str,Any],prelim:int) -> str:
    if h['completed_total']==0 or a['completed_total']==0:
        return 'SIN_HISTORIAL_VALIDO'
    if min(h['completed_total'],a['completed_total'])<5:
        return 'MUESTRA_BASICA_ESCASA'
    return 'APTO_SEGUN_DATOS_BASICOS' if prelim>=60 else 'REVISION_COBERTURA_BASICA'


def metric_profile(key:str,hd:dict[str,Any],ad:dict[str,Any],
                   detail_limit:int) -> dict[str,Any]:
    """Compleción de 4 series (producido/concedido L/V) + variabilidad.

    Ausencia = n cero, sin transformar una métrica ausente en valor deportivo cero.
    """
    summaries=[hd['own'][key],hd['conceded'][key],ad['own'][key],ad['conceded'][key]]
    counts=[int(x.get('n',0)) for x in summaries]
    denominator=max(1,detail_limit)
    coverage=round(100*sum(min(n,denominator)/denominator for n in counts)/4,1)
    if min(counts)>=4 and all(isinstance(x.get('cv'),(int,float)) for x in summaries):
        # CV 0,20 no equivale a 80% prob. Estabilidad descriptiva no validada.
        cvs=[x['cv'] for x in summaries]
        stability=round(max(0,min(100,100*(1-(sum(cvs)/4)/1.4))),1)
    else:
        stability=None
    return {'coverage_pct':coverage,'stability_score':stability,
        'n_series':{'home_for':counts[0],'home_against':counts[1],
                    'away_for':counts[2],'away_against':counts[3]},
        'home_for_mean':summaries[0].get('mean'),
        'home_against_mean':summaries[1].get('mean'),
        'away_for_mean':summaries[2].get('mean'),
        'away_against_mean':summaries[3].get('mean'),
        'coverage_class':'SUFICIENTE' if min(counts)>=6 else
                         'PARCIAL' if min(counts)>=3 else 'ESCASA'}


FAMILY_METRIC_MAP = {
    'goals': 'goals',
    'corners': 'corners',
    'shots': 'shots',
    'cards': 'yellow',
    'fouls': 'fouls',
    'offsides': 'offsides',
}
MINIMUM_FAMILY_SAMPLE = 4
RICH_FAMILY_SAMPLE = 10


def assess_family_evidence(
    h: dict[str, Any],
    a: dict[str, Any],
    d1: dict[str, Any] | None,
    d2: dict[str, Any] | None,
    observed: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Evalúa cobertura mínima por familia sin excluir partidos con una familia apta."""
    family_tiers: dict[str, dict[str, Any]] = {}
    h_completed = int(h.get('completed_total') or 0)
    a_completed = int(a.get('completed_total') or 0)
    basic_min_n = min(h_completed, a_completed)

    for family, metric in FAMILY_METRIC_MAP.items():
        if metric == 'goals':
            if observed and (observed.get('home') or observed.get('away')):
                h_own_n = sum(numeric((r.get('own') or {}).get('goals')) is not None for r in (observed.get('home') or []))
                h_con_n = sum(numeric((r.get('conceded') or {}).get('goals')) is not None for r in (observed.get('home') or []))
                a_own_n = sum(numeric((r.get('own') or {}).get('goals')) is not None for r in (observed.get('away') or []))
                a_con_n = sum(numeric((r.get('conceded') or {}).get('goals')) is not None for r in (observed.get('away') or []))
                counts = [h_own_n, h_con_n, a_own_n, a_con_n]
            else:
                counts = [h_completed, h_completed, a_completed, a_completed]
        elif d1 and d2:
            summaries = [d1['own'][metric], d1['conceded'][metric], d2['own'][metric], d2['conceded'][metric]]
            counts = [int(x.get('n', 0)) for x in summaries]
        elif observed and (observed.get('home') or observed.get('away')):
            h_own_n = sum(numeric((r.get('own') or {}).get(metric)) is not None for r in (observed.get('home') or []))
            h_con_n = sum(numeric((r.get('conceded') or {}).get(metric)) is not None for r in (observed.get('home') or []))
            a_own_n = sum(numeric((r.get('own') or {}).get(metric)) is not None for r in (observed.get('away') or []))
            a_con_n = sum(numeric((r.get('conceded') or {}).get(metric)) is not None for r in (observed.get('away') or []))
            counts = [h_own_n, h_con_n, a_own_n, a_con_n]
        else:
            counts = [0, 0, 0, 0]

        min_n = min(counts) if counts else 0
        max_n = max(counts) if counts else 0
        if max_n == 0:
            tier = 'NO_STATISTICAL_DATA'
        elif min_n < MINIMUM_FAMILY_SAMPLE:
            tier = 'BELOW_MINIMUM_EVIDENCE'
        elif min_n >= RICH_FAMILY_SAMPLE:
            tier = 'RICH_EVIDENCE_AVAILABLE'
        else:
            tier = 'MINIMUM_EVIDENCE_MET'
        family_tiers[family] = {
            'metric': metric,
            'min_series_n': min_n,
            'max_series_n': max_n,
            'tier': tier,
            'eligible': tier in ('MINIMUM_EVIDENCE_MET', 'RICH_EVIDENCE_AVAILABLE'),
        }

    eligible_families = [fam for fam, info in family_tiers.items() if info['eligible']]
    any_data = any(info['max_series_n'] > 0 for info in family_tiers.values()) or max(h_completed, a_completed) > 0
    if not any_data:
        overall_tier = 'NO_STATISTICAL_DATA'
    elif not eligible_families or basic_min_n < 4:
        overall_tier = 'BELOW_MINIMUM_EVIDENCE'
    else:
        detailed_eligible = [fam for fam in eligible_families if fam != 'goals' and family_tiers[fam]['min_series_n'] >= 6]
        if (len(detailed_eligible) >= 2) or (
            family_tiers['goals']['min_series_n'] >= RICH_FAMILY_SAMPLE and len(detailed_eligible) >= 1
        ):
            overall_tier = 'RICH_EVIDENCE_AVAILABLE'
        else:
            overall_tier = 'MINIMUM_EVIDENCE_MET'

    return {
        'evidence_tier': overall_tier,
        'family_evidence_tiers': family_tiers,
        'eligible_market_families': eligible_families,
        'rank_eligible': overall_tier in ('MINIMUM_EVIDENCE_MET', 'RICH_EVIDENCE_AVAILABLE'),
    }


def score_readiness(h:dict[str,Any],a:dict[str,Any],s1:dict[str,Any],
                    s2:dict[str,Any],d1:dict[str,Any],d2:dict[str,Any],
                    detail_limit:int=8,observed:dict[str,list[dict[str,Any]]]|None=None) -> tuple[int,str,list[str],dict[str,Any],int|None,int|None]:
    """Separación: cobertura factual, estabilidad descriptiva e investigación.

    Puntuar varios mercados evita que corners+tiros saturen 100 puntos.
    No genera señales de apuesta, P(E), EV ni pronóstico.
    """
    notes=[]
    for team,label in ((h,'LOCAL'),(a,'VISITANTE')):
        if team['completed_total']<10:
            notes.append(f'{label}: muestra histórica corta ({team["completed_total"]})')
        if team['same_competition_total']<5:
            notes.append(f'{label}: pocos juegos en misma liga ({team["same_competition_total"]})')
        if team.get('days_since_last_match') is None:
            notes.append(f'{label}: sin historial válido')
    for standings,label in ((s1,'LOCAL'),(s2,'VISITANTE')):
        if standings.get('status')!='AVAILABLE':notes.append(f'{label}: tabla no disponible')

    # 30 puntos históricos: 20 por volumen, 10 por comparabilidad de liga.
    score=0.0
    for team in (h,a):
        score+=10*min(team['completed_total'],20)/20
        score+=5*min(team['same_competition_total'],12)/12
    # 10 por tabla. No se interpreta como incentivo táctico.
    score+=5*(s1.get('status')=='AVAILABLE')+5*(s2.get('status')=='AVAILABLE')

    market_profiles={}
    for key,weight in METRIC_WEIGHTS.items():
        profile=metric_profile(key,d1,d2,detail_limit)
        market_profiles[key]=profile
        score+=weight*profile['coverage_pct']/100
        if profile['coverage_class']=='ESCASA':
            notes.append(f'{key}: cobertura escasa (series {list(profile["n_series"].values())})')
    coverage=round(min(100,max(0,score)))
    stability_pairs=[(METRIC_WEIGHTS[k],v['stability_score']) for k,v in market_profiles.items()
                     if v['stability_score'] is not None]
    st=(round(sum(w*x for w,x in stability_pairs)/sum(w for w,_ in stability_pairs))
        if stability_pairs else None)
    # Prioridad de investigación: no confundir con score de datos ni rentabilidad.
    priority=round(.7*coverage+.3*st) if st is not None else None
    ev_info = assess_family_evidence(h, a, d1, d2, observed)
    if coverage >= 72:
        status = 'DATOS_APROVECHABLES'
    elif coverage >= 45 or ev_info['rank_eligible']:
        status = 'REVISION'
    else:
        status = 'COBERTURA_LIMITADA'
    return coverage,status,notes,market_profiles,st,priority


def evaluate_fixture(f:dict[str,Any],histories:dict[int,list[dict[str,Any]]],
  details:dict[int,dict[str,Any]],standings:dict[tuple[int,int],list[dict[str,Any]]],
  errors:dict[str,str],now:datetime,detail_limit:int,
  details_selected:bool=True,detail_requested_ids:set[int]|None=None) -> dict[str,Any]:
    lid=f['league_id'];season=f['season'];hid=f['home_id'];aid=f['away_id']
    cutoff=min(now,utc_datetime(f['kickoff_utc']))
    h=[g for g in histories.get(hid,[]) if utc_datetime(g['date_utc'])<cutoff]
    a=[g for g in histories.get(aid,[]) if utc_datetime(g['date_utc'])<cutoff]
    hb=team_basic(h,lid,cutoff);ab=team_basic(a,lid,cutoff)
    hdetail,hselection=select_detail_games(h,lid,season,cutoff,detail_limit)
    adetail,aselection=select_detail_games(a,lid,season,cutoff,detail_limit)
    hd=team_detail(hdetail,hid,details) if details_selected else None
    ad=team_detail(adetail,aid,details) if details_selected else None
    standings_rows=standings.get((lid,season),[])
    hs=standing_for(standings_rows,hid);ass=standing_for(standings_rows,aid)
    prelim=preliminary_score(hb,ab,hs,ass)
    from prematch_tactical_history import observations_from_games
    requested=detail_requested_ids or set()
    # Always preserve FT goal histories for every eligible fixture, even when
    # adaptive acquisition did not request detailed match statistics.  The
    # observation adapter labels all unrequested metrics NOT_REQUESTED, never 0.
    observed={'home':observations_from_games(h,hid,details,requested),
              'away':observations_from_games(a,aid,details,requested)}
    ev_info = assess_family_evidence(hb, ab, hd, ad, observed)
    if details_selected:
        grade,status,notes,profiles,stability,priority=score_readiness(
            hb,ab,hs,ass,hd,ad,detail_limit,observed=observed)
    else:
        grade=None;status='PENDIENTE_DETALLE';notes=['Estadísticas detalladas no consultadas: barrido preliminar']
        profiles={};stability=None;priority=None
    for teamid,label in ((hid,'LOCAL'),(aid,'VISITANTE')):
        if str(teamid) in errors:
            status='EVALUACION_INCOMPLETA';grade=None;priority=None
            notes.append(f'{label}: error consultando histórico')
    if f'{lid}:{season}' in errors:
        notes.append('Tabla no consultada por error de API')
    return {'fixture_id':f['fixture_id'],'kickoff_madrid':f['kickoff_madrid'],
      'home_id':hid,'away_id':aid,'statistical_observations':observed,
      'detail_selection':{'home':hselection,'away':aselection},
      'home':f['home'],'away':f['away'],'country':f['country'],'league':f['league'],
      'league_id':lid,'season':season,'gender':f['gender'],'region':f['region'],
      'competition_category':f.get('competition_category'),
      'evidence_tier':ev_info['evidence_tier'],
      'family_evidence_tiers':ev_info['family_evidence_tiers'],
      'eligible_market_families':ev_info['eligible_market_families'],
      'rank_eligible':ev_info['rank_eligible'],
      'category_status':f.get('category_status'),
      'category_evidence_status':f.get('category_evidence_status'),
      'category_level':f.get('category_level'),
      'category_provenance':f.get('category_provenance'),
      'competition_identity_status':f.get('competition_identity_status'),
      'competition_eligibility':f.get('competition_eligibility'),
      'competition_eligibility_reason':f.get('competition_eligibility_reason'),
      'participant_eligibility':f.get('participant_eligibility'),
      'participant_eligibility_reason':f.get('participant_eligibility_reason'),
      'national_team_evidence':f.get('national_team_evidence'),
      'cup_eligibility_evidence':f.get('cup_eligibility_evidence'),
      'competition_uncertainties':(['ROTATION','COMPETITIVE_INTENSITY','SQUAD_CALLUPS',
          'COACH_REGIME','SUBSTITUTIONS'] if lid==10 else []),
      'coverage_exception_reason':f.get('coverage_exception_reason'),
      'region_priority':f['region_priority'],'analysis_cutoff_utc':cutoff.isoformat(),
      'preliminary_coverage_score':prelim,'initial_data_gate':initial_data_gate(hb,ab,prelim),
      'data_readiness_score':grade,
      'data_readiness_status':status,'stability_score':stability,
      'research_priority_score':priority,'market_profiles':profiles,
      'details_checked':details_selected,'warnings':notes,
      'home_evidence':{'history':hb,'details':hd,'standings':hs},
      'away_evidence':{'history':ab,'details':ad,'standings':ass},
      'h2h':last_h2h(h,a,hid,aid),
      'rivalry':{'status':'NOT_VERIFIED','note':'Requiere fuentes externas; H2H no prueba rivalidad'},
      'competitive_motivation':{'status':'NOT_INFERRED','note':'Posición no equivale a obligación táctica de ganar'},
      'bwin_market_availability':'NOT_CHECKED',
      'note':'Puntajes heurísticos de cobertura/variabilidad, NO probabilidad de apuesta ni EV'}


COVERAGE_RULES = {'enabled': True, 'advanced_bonus': 5.0,
                  'additional_capability_weight': 0.5, 'minimum_sample': 4,
                  'skip_declared_false_details': True}


def validate_coverage_rules(rules=None):
    supplied = rules or {}
    if not isinstance(supplied,dict) or set(supplied)-set(COVERAGE_RULES):
        raise PreselectionError('INVALID_COVERAGE_RULES')
    config={**COVERAGE_RULES,**supplied}
    for key in ('enabled','skip_declared_false_details'):
        if not isinstance(config[key],bool):raise PreselectionError('INVALID_COVERAGE_RULES_'+key)
    for key in ('advanced_bonus','additional_capability_weight','minimum_sample'):
        value=numeric(config[key])
        if value is None or value<0 or key=='minimum_sample' and (value<1 or value!=int(value)):
            raise PreselectionError('INVALID_COVERAGE_RULES_'+key)
        config[key]=int(value) if key=='minimum_sample' else value
    return config


def competition_coverage(rows, league_id, season):
    """Identity-bound tri-state contract. No remote booleans inferred from names."""
    matches = [r for r in rows if isinstance(r, dict) and
               (r.get('league') or {}).get('id') == league_id and
               any(x.get('year') == season for x in r.get('seasons', []))]
    contract = {'league_id': league_id, 'season': season, 'status': 'UNKNOWN',
                'competition_type': None, 'capabilities': {k:'UNKNOWN' for k in
                ('statistics_fixtures','standings','events','lineups','statistics_players','players')}}
    if len(matches) != 1:return contract
    seasons = [x for x in matches[0].get('seasons', []) if x.get('year') == season]
    if len(seasons) != 1:return contract
    cov = seasons[0].get('coverage') or {}; fixture = cov.get('fixtures') or {}
    contract['competition_type'] = (matches[0].get('league') or {}).get('type')
    for key in contract['capabilities']:
        value = fixture.get(key) if key in ('statistics_fixtures','events','lineups','statistics_players') else cov.get(key)
        contract['capabilities'][key] = 'TRUE' if value is True else 'FALSE' if value is False else 'UNKNOWN'
    contract['status'] = 'DECLARED'
    return contract


def apply_coverage_priority(match, rules=None):
    rules = validate_coverage_rules(rules)
    contract = match.get('competition_coverage') or competition_coverage([],match.get('league_id'),match.get('season'))
    match['competition_coverage'] = contract
    match['coverage_rules'] = rules
    caps = contract['capabilities']; observations = match.get('statistical_observations') or {}
    families = {}
    for family,metric in [('goals','goals'),('corners','corners'),('cards','yellow'),('shots','shots')]:
        series = {side+'_'+block:sum(numeric((r.get(block) or {}).get(metric)) is not None for r in {r.get('fixture_id'):r for r in rows if r.get('fixture_id') is not None}.values()
                  if (not r.get('acquired_at_utc') or acquisition_before(r['acquired_at_utc'],utc_datetime(match['analysis_cutoff_utc']))) and r.get('status')=='FT' and r.get('league_id') == match.get('league_id') and r.get('season') == match.get('season') and
                  historical_available(r,utc_datetime(match['analysis_cutoff_utc'])))
                  for side,rows in observations.items() for block in ('own','conceded')}
        n = min(series.values()) if len(series) == 4 else 0
        families[family] = {'metric':metric, 'series_n':series, 'minimum_series_n':n,
                            'status':'OBSERVED_SUFFICIENT_SAMPLE' if n>=rules['minimum_sample'] else 'LIMITED_OR_ABSENT',
                            'missing_is_zero':False}
    families['players'] = {'declared':caps['statistics_players'], 'status':'EXPOSURE_NOT_VERIFIED',
                           'minimum_series_n':None}
    is_national_friendly = (
        match.get('competition_eligibility_reason') == 'NATIONAL_FRIENDLY'
        or match.get('league_id') == 10
    )
    exception = (
        contract.get('competition_type') == 'Cup'
        or bool(match.get('coverage_exception_reason'))
        or (is_national_friendly and caps['standings'] == 'FALSE')
    )
    # Exemptions are explicit sporting metadata, never inferred from a missing table.
    declared = caps['statistics_fixtures']=='TRUE' and (caps['standings']=='TRUE' or exception)
    observed = any(families[k]['minimum_series_n']>=rules['minimum_sample'] for k in ('corners','cards','shots'))
    advanced = declared and observed
    reasons = ['DECLARED_COLLECTIVE_AND_STANDINGS' if declared and caps['standings']=='TRUE' else
               'DOCUMENTED_NO_TABLE_EXCEPTION' if declared else 'DECLARED_COVERAGE_LIMITED_OR_UNKNOWN',
               'OBSERVED_SAMPLE_SUFFICIENT' if observed else 'OBSERVED_SAMPLE_LIMITED']
    bonus = (rules['advanced_bonus'] + rules['additional_capability_weight'] *
             sum(caps[k]=='TRUE' for k in ('events','lineups','statistics_players','players'))) if advanced else 0
    legacy = match.get('legacy_research_priority_score', match.get('research_priority_score'))
    match['legacy_research_priority_score'] = legacy
    exception_reason = match.get('coverage_exception_reason') or (
        'NATIONAL_FRIENDLY_NO_STANDINGS_FORMAT'
        if is_national_friendly and caps['standings'] != 'TRUE'
        else ('PROVIDER_CUP_FORMAT' if exception else None)
    )
    match['statistical_priority'] = {'category':'ADVANCED' if advanced else 'SECONDARY',
        'reasons':reasons, 'exception_reason':exception_reason,
        'families':families, 'bonus':bonus if rules['enabled'] else 0, 'rules':rules,
        'interpretation':'DATA_PRIORITY_ONLY_NOT_PROBABILITY'}
    match['research_priority_score'] = round(legacy + (bonus if rules['enabled'] else 0),3) if legacy is not None else None


def assign_ranks(matches:list[dict[str,Any]]) -> None:
    matches.sort(key=lambda r:(r['data_readiness_score'] is None,
        -(r['research_priority_score'] if r['research_priority_score'] is not None else -1),
        -(r['data_readiness_score'] if r['data_readiness_score'] is not None else -1),
        -(r['preliminary_coverage_score'] or 0),r['region_priority'],r['kickoff_madrid'],r['fixture_id']))
    rank=0
    for r in matches:
        if r['data_readiness_score'] is None:
            r['data_readiness_rank']=None
        else:
            rank+=1;r['data_readiness_rank']=rank


def choose_detail_fixtures(fixtures:list[dict[str,Any]],histories:dict[int,list],
   standings:dict[tuple[int,int],list],now:datetime,top:int,coverage_contracts=None,coverage_rules=None) -> set[int]:
    """Planificación equilibrada (no una muestra arbitraria de una liga por vez).

    La señal es una HEURÍSTICA para decidir QUÉ consultar, no para sugerir picks.
    Separa las cuotas por región, evita acaparamiento de una liga y añade las
    preferencias operativas indicadas por el usuario sin afirmar mercados Bwin.
    """
    # Dependencia ligera, mismo directorio; no consulta API.
    try:
        from rank_matches import basic_signal, choose_expansion
    except ImportError as exc:
        raise PreselectionError(
            'Modo adaptativo V1.3 requiere rank_matches.py junto a preselect_matches.py'
        ) from exc
    items=[]
    for f in fixtures:
        cutoff=min(now,utc_datetime(f['kickoff_utc']))
        h=team_basic(histories.get(f['home_id'],[]),f['league_id'],cutoff)
        a=team_basic(histories.get(f['away_id'],[]),f['league_id'],cutoff)
        table=standings.get((f['league_id'],f['season']),[])
        hs=standing_for(table,f['home_id']);ass=standing_for(table,f['away_id'])
        prelim=preliminary_score(h,a,hs,ass)
        item={'fixture_id':f['fixture_id'], 'league_id':f['league_id'],
              'season':f['season'],'region':f['region'],
              'kickoff_madrid':f['kickoff_madrid'],
              'preliminary_coverage_score':prelim,
              'initial_data_gate':initial_data_gate(h,a,prelim),
              'home_evidence':{'history':h,'standings':hs},
              'away_evidence':{'history':a,'standings':ass},
              'h2h':{'count':0}}
        item['competition_coverage']=(coverage_contracts or {}).get((f['league_id'],f['season']))
        item['coverage_rules']=coverage_rules
        item['coverage_exception_reason']=f.get('coverage_exception_reason')
        item['_planning_score'],item['_planning_reasons']=basic_signal(item)
        items.append(item)
    chosen=choose_expansion(items,top,max_per_league=2)
    return {x['fixture_id'] for x in chosen}


def validate_temporal_sources(report):
    cutoff=utc_datetime(report['generated_at_utc'])
    for record in report.get('api_request_ledger',[]):
        value=record.get('first_observed_at_utc') or record.get('fetched_at_utc')
        if value and not acquisition_before(value,cutoff):
            raise PreselectionError('SOURCE_ACQUISITION_UNVERIFIED_OR_AFTER_CUTOFF')


def rescore_existing(report:dict[str,Any],detail_limit:int|None=None) -> dict[str,Any]:
    """Recalcula un preselection V1.1/1.2 sin llamar a la API. No amplía muestra."""
    if report.get('schema_version') not in ('1.1-preselection','1.2-preselection','1.3-preselection'):
        raise PreselectionError('--rescore-json requiere un preselection.json V1.1 o V1.2')
    validate_temporal_sources(report)
    limit=detail_limit or (report.get('methodology') or {}).get('sample_details_cap') or 8
    for m in report.get('matches',[]):
        h=m['home_evidence'];a=m['away_evidence']
        m['preliminary_coverage_score']=preliminary_score(h['history'],a['history'],h['standings'],a['standings'])
        m['initial_data_gate']=initial_data_gate(h['history'],a['history'],m['preliminary_coverage_score'])
        if h.get('details') is None or a.get('details') is None:
            old_status=m.get('data_readiness_status')
            m.update({'data_readiness_score':None,'research_priority_score':None,
                      'stability_score':None,'market_profiles':{},'details_checked':False,
                      'data_readiness_status':old_status if old_status=='EVALUACION_INCOMPLETA' else 'PENDIENTE_DETALLE'})
            continue
        old_status=m.get('data_readiness_status')
        obs = m.get('statistical_observations')
        ev_info = assess_family_evidence(h['history'], a['history'], h['details'], a['details'], obs)
        grade,status,warnings,profiles,stability,priority=score_readiness(
            h['history'],a['history'],h['standings'],a['standings'],h['details'],a['details'],limit,observed=obs)
        if old_status in ('EVALUACION_INCOMPLETA','REVISION_PARCIAL_API'):
            warnings=list(dict.fromkeys((m.get('warnings') or [])+warnings))
            status=old_status
            if old_status=='EVALUACION_INCOMPLETA':grade=None;priority=None
        m.update({'data_readiness_score':grade,'data_readiness_status':status,
                  'research_priority_score':priority,'stability_score':stability,
                  'market_profiles':profiles,'details_checked':True,'warnings':warnings,
                  'evidence_tier':ev_info['evidence_tier'],
                  'family_evidence_tiers':ev_info['family_evidence_tiers'],
                  'eligible_market_families':ev_info['eligible_market_families'],
                  'rank_eligible':ev_info['rank_eligible']})
    for match in report['matches']:
        match.pop('legacy_research_priority_score',None)
        apply_coverage_priority(match,(report.get('methodology') or {}).get('coverage_rules'))
    assign_ranks(report['matches'])
    report['schema_version']=VERSION
    report.setdefault('methodology',{}).update({
        'ranking':'COVERAGE_AND_STABILITY_HEURISTICS_NOT_BET_VALUE',
        'stage':'RESCORE_OFFLINE','no_bwin_odds':True,'no_news':True,
        'not_a_predicted_match_outcome':True})
    report['rescore_only']=True
    report['api_usage']={'requests_sent':0,'cache_hits':0}
    report['counts']['evaluated']=len(report['matches'])
    report['counts']['fully_evaluated']=sum(x['data_readiness_score'] is not None for x in report['matches'])
    report['counts']['details_requested']=sum(x.get('details_checked',False) for x in report['matches'])
    return report


def history_seasons_for_team(fixture_seasons:set[int], cutoff:datetime,
                             max_seasons:int=3) -> list[int]:
    """Prioriza la temporada del fixture; completa con campañas anteriores.

    No se debe derivar la temporada solo del año civil: algunas ligas
    2026/27 se identifican como 2026 y otras competiciones usan 2027.
    """
    if not 1 <= max_seasons <= 3:
        raise PreselectionError('history-seasons fuera de rango 1..3')
    seeds=sorted({int(s) for s in fixture_seasons if positive_int(s)},reverse=True)
    if not seeds:raise PreselectionError('Falta la temporada del equipo en fixtures_major.json')
    # Dos equipos pueden aparecer en varias ligas durante la ventana.
    # Priorizamos temporadas explícitamente referenciadas antes que inferidas.
    choices=list(seeds)
    for y in seeds:
        for prev in (y-1,y-2):
            if prev not in choices: choices.append(prev)
    return choices[:max_seasons]


def run_evaluator(doc:dict[str,Any],client:Any,now:datetime,history_limit:int=20,
                  detail_limit:int=8,max_matches:int=0,fixture_id:int|None=None,
                  history_seasons:int=3,mode:str='full',detail_top:int=30,
                  fixture_ids:set[int]|None=None, coverage_rules:dict|None=None,
                  source_filter_sha256:str|None=None) -> dict[str,Any]:
    if doc.get('schema_version')!='1.0-major-leagues':
        raise PreselectionError('Entrada no es fixtures_major.json V1.0')
    if not 1 <= history_seasons <= 3:
        raise PreselectionError('history-seasons debe estar entre 1 y 3')
    if mode not in ('full','quick','adaptive'):
        raise PreselectionError('mode debe ser full, quick o adaptive')
    if detail_top<1:raise PreselectionError('detail-top debe ser >=1')
    input_fixtures=list(doc['fixtures']);fixtures=list(input_fixtures)
    if fixture_id is not None:
        fixtures=[f for f in fixtures if f['fixture_id']==fixture_id]
        if not fixtures:raise PreselectionError(f'Fixture {fixture_id} no está en el filtro elegible')
    if fixture_ids is not None:
        if fixture_id is not None:
            raise PreselectionError('No combinar --fixture-id con --fixture-ids-file')
        known={f['fixture_id'] for f in fixtures}
        missing=fixture_ids-known
        if missing:
            raise PreselectionError(f'IDs no encontrados en fixtures_major.json: {sorted(missing)}')
        fixtures=[f for f in fixtures if f['fixture_id'] in fixture_ids]
    stale=[f for f in fixtures if f['status']!='NS' or utc_datetime(f['kickoff_utc'])<=now]
    fixtures=[f for f in fixtures if f not in stale]
    fixtures=sorted(fixtures,key=lambda f:(f['kickoff_utc'],f['fixture_id']))
    if max_matches>0:fixtures=fixtures[:max_matches]
    if not fixtures:
        raise PreselectionError('No quedan fixtures futuros elegibles para esta ejecución PREMATCH')
    coverage_rules = validate_coverage_rules(coverage_rules)
    coverage_contracts = {}
    for lid, season in sorted({(f['league_id'], f['season']) for f in fixtures}):
        try:
            coverage_contracts[(lid,season)] = competition_coverage(client.get('leagues', {'id':lid,'season':season}), lid, season)
        except PreselectionError as exc:
            coverage_contracts[(lid,season)] = competition_coverage([],lid,season)
            coverage_contracts[(lid,season)]['query_error'] = str(exc)
        records=[r for r in getattr(client,'request_ledger',[]) if r.get('endpoint')=='leagues' and r.get('parameters')=={'id':lid,'season':season}]
        coverage_contracts[(lid,season)]['source']=records[-1] if records else {'status':'PROVENANCE_NOT_RECORDED'}
    cutoff_by_team={}
    seasons_by_team=defaultdict(set)
    for f in fixtures:
        for id_key in ('home_id','away_id'):
            tid=f[id_key]
            dt=min(now,utc_datetime(f['kickoff_utc']))
            cutoff_by_team[tid]=min(dt,cutoff_by_team.get(tid,dt))
            seasons_by_team[tid].add(f['season'])

    histories={};errors={};history_queries=0
    history_seasons_used={};supplement_warnings={}
    for i,(tid,cutoff) in enumerate(sorted(cutoff_by_team.items()),1):
        upper=cutoff.date();lower=upper-timedelta(days=550)
        seasons=history_seasons_for_team(seasons_by_team[tid],cutoff,history_seasons)
        combined=[];raw_combined=[];used=[];failed=[]
        for season in seasons:
            if len(combined)>=history_limit:break
            # /fixtures con team+from+to requiere season en la respuesta real
            # de API-Football. No reintentar idéntica petición sin season.
            params={'team':tid,'season':season,'from':lower.isoformat(),
                    'to':upper.isoformat()}
            history_queries+=1
            try:
                rows=client.get('fixtures',params)
                if not isinstance(rows,list):
                    raise PreselectionError(f'/fixtures team={tid} season={season}: response no es lista')
                used.append(season)
                raw_combined.extend(rows)
                combined=prepare_history(raw_combined,tid,cutoff,history_limit)
            except PreselectionError as exc:
                failed.append(f'season={season}: {exc}')
        histories[tid]=combined
        history_seasons_used[str(tid)]=used
        if failed:
            # Si una temporada dio datos válidos, conservamos lo recopilado;
            # no interpretamos una temporada fallida como falta de partidos.
            if combined:
                errors[f'history_partial_{tid}']='; '.join(failed)
                supplement_warnings[str(tid)]='Cobertura parcial: '+'; '.join(failed)
            else:
                errors[str(tid)]='; '.join(failed)
        if i==len(cutoff_by_team) or i%25==0:
            print(f'  Históricos: {i}/{len(cutoff_by_team)} equipos',flush=True)

    # En la primera pasada se consultan historiales y tablas, nunca detalles.
    table_keys=sorted({(f['league_id'],f['season']) for f in fixtures})
    standings={};standings_queries=0
    for i,(lid,season) in enumerate(table_keys,1):
        if coverage_contracts[(lid,season)]['capabilities']['standings']=='FALSE' and not any(f.get('coverage_probe_reason') for f in fixtures if (f['league_id'],f['season'])==(lid,season)):
            standings[(lid,season)]=[]
            continue
        standings_queries+=1
        try:standings[(lid,season)]=client.get('standings',{'league':lid,'season':season})
        except PreselectionError as exc:errors[f'{lid}:{season}']=str(exc);standings[(lid,season)]=[]
        if i==len(table_keys) or i%20==0:
            print(f'  Clasificaciones: {i}/{len(table_keys)} competiciones',flush=True)

    if mode=='full':detail_selected={f['fixture_id'] for f in fixtures}
    elif mode=='quick':detail_selected=set()
    else:detail_selected=choose_detail_fixtures(fixtures,histories,standings,now,min(detail_top,len(fixtures)),coverage_contracts,coverage_rules)
    chosen=[f for f in fixtures if f['fixture_id'] in detail_selected]
    detail_ids=set()
    for f in chosen:
        for tid in (f['home_id'],f['away_id']):
            cutoff=min(now,utc_datetime(f['kickoff_utc']))
            subset=[g for g in histories.get(tid,[]) if utc_datetime(g['date_utc'])<cutoff]
            contract=coverage_contracts[(f['league_id'],f['season'])]
            if (coverage_rules['skip_declared_false_details'] and contract['capabilities']['statistics_fixtures']=='FALSE'
                    and not f.get('coverage_probe_reason')):
                continue
            selected,_=select_detail_games(subset,f['league_id'],f['season'],cutoff,detail_limit)
            detail_ids.update(g['id'] for g in selected)
    details={};detail_list=sorted(detail_ids)
    for i in range(0,len(detail_list),20):
        batch=detail_list[i:i+20]
        try:
            if hasattr(client,'get_fixture_details'):
                rows=client.get_fixture_details(batch,cutoff=now)
            else:
                rows=client.get('fixtures',{'ids':'-'.join(str(x) for x in batch)})
            for raw in rows:
                if isinstance(raw,dict):
                    fid=(raw.get('fixture') or {}).get('id')
                    if fid in batch:details[fid]=raw
        except PreselectionError as exc:errors[f'detail_batch_{i//20}']=str(exc)
        if i==0 or (i//20)%10==9 or i+20>=len(detail_list):
            print(f'  Detalle estadístico: {min(i+20,len(detail_list))}/{len(detail_list)} partidos',flush=True)
    print(f'  Modo: {mode} | Detalle completo solicitado: {len(detail_selected)}/{len(fixtures)}',flush=True)
    result=[evaluate_fixture(f,histories,details,standings,errors,now,detail_limit,
                              f['fixture_id'] in detail_selected,detail_ids) for f in fixtures]
    # Un error de detalle por lote puede crear una falsa puntuación baja:
    # suspender la puntuación de los partidos cuyos históricos afectados
    # pertenecen a lotes fallidos, en lugar de fingir 'datos escasos'.
    failed_detail_ids=set()
    for index in range(0,len(detail_list),20):
        if f'detail_batch_{index//20}' in errors:
            failed_detail_ids.update(detail_list[index:index+20])
    fixtures_by_id={f['fixture_id']:f for f in fixtures}
    for match in result:
        fx=fixtures_by_id[match['fixture_id']]
        for tid,label in ((fx['home_id'],'LOCAL'),(fx['away_id'],'VISITANTE')):
            if str(tid) in supplement_warnings:
                match['warnings'].append(f'{label}: {supplement_warnings[str(tid)]}')
                if match['data_readiness_score'] is not None:
                    match['data_readiness_status']='REVISION_PARCIAL_API'
            selection_key='home' if label=='LOCAL' else 'away'
            selected_ids=set(match.get('detail_selection',{}).get(selection_key,{}).get('selected_fixture_ids',[]))
            if selected_ids & failed_detail_ids:
                match['warnings'].append(f'{label}: lote de estadísticas detalladas fallido')
                match['data_readiness_score']=None
                match['research_priority_score']=None
                match['data_readiness_status']='EVALUACION_INCOMPLETA'
    assign_ranks(result)
    for match in result:
        match['legacy_data_readiness_rank'] = match['data_readiness_rank']
        original=next(f for f in fixtures if f['fixture_id']==match['fixture_id'])
        match['coverage_rules']=coverage_rules
        match['competition_coverage']=coverage_contracts[(match['league_id'],match['season'])]
        match['coverage_exception_reason']=original.get('coverage_exception_reason')
        match['detail_acquisition_reason']=('DECLARED_FALSE_SKIPPED' if coverage_rules['skip_declared_false_details'] and match['competition_coverage']['capabilities']['statistics_fixtures']=='FALSE' and not original.get('coverage_probe_reason') else original.get('coverage_probe_reason') or 'NORMAL_DETAIL_PLAN')
        apply_coverage_priority(match,coverage_rules)
    assign_ranks(result)
    if isinstance(client,ApiFootball) and client.asof is None:
        now=datetime.now(UTC)
        for match in result:
            if now>=utc_datetime(match['kickoff_madrid']):
                match['data_readiness_score']=None;match['research_priority_score']=None
                match['data_readiness_status']='EVALUACION_INCOMPLETA'
                errors[str(match['fixture_id'])]='ACQUISITION_CROSSED_KICKOFF'
            else:match['analysis_cutoff_utc']=now.isoformat()
        assign_ranks(result)
    evaluated_ids={m['fixture_id'] for m in result};stale_ids={f['fixture_id'] for f in stale}
    fixture_ledger=[]
    for f in input_fixtures:
        fid=f['fixture_id']
        if fid in evaluated_ids:status='EVALUATED'
        elif fid in stale_ids:status='EXCLUDED_TEMPORAL'
        elif fixture_id is not None or fixture_ids is not None:status='NOT_SELECTED_EXPLICIT_SCOPE'
        elif max_matches>0:status='NOT_EVALUATED_MAX_MATCHES_LIMIT'
        else:status='NOT_EVALUATED_UNKNOWN'
        fixture_ledger.append({'fixture_id':fid,'home':f.get('home'),'away':f.get('away'),
                               'kickoff_madrid':f.get('kickoff_madrid'),'status':status})
    skipped_targets=sum(m.get('detail_acquisition_reason')=='DECLARED_FALSE_SKIPPED' for m in result if m['fixture_id'] in detail_selected)
    return {'schema_version':VERSION,'generated_at_utc':now.isoformat(),
      'source_filter_sha256':source_filter_sha256,'source_filter_generated_at_utc':doc.get('generated_at_utc'),
      'counts':{'input_filter':len(doc['fixtures']),'evaluated':len(result),
                'stale_skipped':len(stale),'histories_requested':len(cutoff_by_team),
                'history_season_requests':history_queries,
                'detail_fixture_ids':len(detail_list),'standings_requested':standings_queries,'coverage_contracts_requested':len(coverage_contracts),
                'api_errors':len(errors),
                'details_requested':len(detail_selected)-skipped_targets,'detail_targets_skipped_declared_false':skipped_targets,
                'fully_evaluated':sum(m['data_readiness_score'] is not None for m in result),
                'pending_detail':len(fixtures)-len(detail_selected)},
      'api_errors':errors,'api_usage':{'requests_sent':getattr(client,'requests_sent',None),
                                    'cache_hits':getattr(client,'cache_hits',None),
                                    'request_budget':getattr(client,'request_budget',0),
                                    'request_reserve':getattr(client,'request_reserve',0)},
      'api_request_ledger':getattr(client,'request_ledger',[]),
      'fixture_evaluation_ledger':fixture_ledger,
      'history_seasons_used':history_seasons_used,
      'methodology':{'ranking':'COVERAGE_AND_STABILITY_HEURISTICS_NOT_BET_VALUE',
        'coverage_rules':coverage_rules, 'stage':mode,'detail_top':detail_top,
        'sample_history_cap':history_limit,'sample_details_cap':detail_limit,
        'sample_history_seasons_cap':history_seasons,
        'time_reference':'Min(fecha de evaluación, kickoff del partido); solo FT/AET/PEN anteriores',
        'no_bwin_odds':True,'no_news':True,'not_a_predicted_match_outcome':True},
      'matches':result}


def read_fixture_ids_file(path:Path) -> set[int]:
    """Una línea por fixture_id entero; permite comentarios y líneas vacías."""
    result=set()
    for i,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        line=line.split('#',1)[0].strip()
        if not line:continue
        if not line.isascii() or not line.isdecimal() or int(line)<=0:
            raise PreselectionError(f'{path}:{i}: solo se admiten IDs numéricos positivos')
        result.add(int(line))
    if not result:raise PreselectionError(f'No hay fixture IDs en {path}')
    return result


def merge_preselection_reports(base:dict[str,Any],updated:dict[str,Any],allow_new_fixtures:bool=False) -> dict[str,Any]:
    """Fusiona únicamente fixtures coincidentes; no finge haber actualizado los demás.

    Exige identidad del descubrimiento y de liga/temporada, registra diferencias
    temporales y recalcula rankings sin confundir N/D con estadística cero.
    Si allow_new_fixtures=True (mismo source_filter_sha256), incorpora fixtures nuevos
    de una tanda incremental sin sobrescribir ni alterar otros existentes.
    """
    if base.get('schema_version') not in ('1.2-preselection','1.3-preselection'):
        raise PreselectionError('--merge-report requiere un informe V1.2 o V1.3')
    if (not base.get('source_filter_sha256') or
        base['source_filter_sha256']!=updated.get('source_filter_sha256')):
        raise PreselectionError('El filtro base no coincide en SHA256: se prohíbe fusionar slates distintos')
    old={m['fixture_id']:m for m in base.get('matches',[])}
    if len(old)!=len(base.get('matches',[])):
        raise PreselectionError('IDs duplicados en informe base')
    replaced=[]
    added=[]
    for m in updated.get('matches',[]):
        fid=m['fixture_id']
        if fid not in old:
            if not allow_new_fixtures:
                raise PreselectionError(f'Fixture {fid} no figura en informe base')
            old[fid]=m
            added.append(fid)
            continue
        previous=old[fid]
        if (m.get('league_id'),m.get('season'),m.get('home'),m.get('away'),m.get('kickoff_madrid')) != (previous.get('league_id'),previous.get('season'),previous.get('home'),previous.get('away'),previous.get('kickoff_madrid')):
            raise PreselectionError(f'Identidad distinta para fixture {fid}: no se fusiona')
        # Conservamos el historial del primer informe para comparar la última
        # adquisición, pero el dato válido final es el más reciente.
        if m.get('details_checked'):
            old[fid]=m
            replaced.append(fid)
    combined=dict(base)
    combined['schema_version']=VERSION
    combined['generated_at_utc']=updated['generated_at_utc']
    combined['matches']=list(old.values())
    assign_ranks(combined['matches'])
    counts=dict(base.get('counts') or {})
    counts['evaluated']=len(old)
    counts['details_requested']=sum(bool(x.get('details_checked')) for x in combined['matches'])
    counts['fully_evaluated']=sum(x.get('data_readiness_score') is not None for x in combined['matches'])
    counts['pending_detail']=sum(not x.get('details_checked') for x in combined['matches'])
    counts['api_errors']=len(updated.get('api_errors',{}))
    combined['counts']=counts
    combined['api_errors']=updated.get('api_errors',{})
    combined['api_usage']=updated.get('api_usage')
    combined['merge_metadata']={
        'base_generated_at_utc':base.get('generated_at_utc'),
        'update_generated_at_utc':updated.get('generated_at_utc'),
        'base_api_usage':base.get('api_usage'),
        'update_api_usage':updated.get('api_usage'),
        'updated_fixture_ids':replaced,
        'added_fixture_ids':added,
        'old_detailed_count':sum(bool(x.get('details_checked')) for x in base.get('matches',[])),
        'output_detailed_count':counts['details_requested'],
        'temporal_note':'Las clasificaciones y el histórico pueden proceder de instantes distintos; NO usar como backtest as-of sin auditoría.'
    }
    combined['methodology']=dict(base.get('methodology') or {})
    combined['methodology']['stage']='merged_incremental_full'
    combined['methodology']['sample_details_cap']=updated.get('methodology',{}).get('sample_details_cap')
    return combined


def save_report(report:dict[str,Any],folder:Path):
    folder.mkdir(parents=True,exist_ok=True)
    json_target=folder/'preselection.json'
    json_tmp=json_target.with_name(f'.{json_target.name}.{os.getpid()}.tmp')
    json_tmp.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    json_tmp.replace(json_target)
    fields=['data_readiness_rank','fixture_id','kickoff_madrid','country','league','home','away',
            'competition_coverage','statistical_priority','legacy_data_readiness_rank','legacy_research_priority_score',
            'gender','category_status','region','initial_data_gate','preliminary_coverage_score','data_readiness_score',
            'stability_score','research_priority_score','data_readiness_status',
            'details_checked','home_history_n','away_history_n','home_corners_n',
            'away_corners_n','home_corners_avg','away_corners_avg',
            'corners_coverage_pct','corners_stability_score','shots_coverage_pct',
            'fouls_coverage_pct','xg_coverage_pct','home_rank','away_rank']
    with (folder/'preselection.csv').open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=fields);w.writeheader()
        for m in report['matches']:
            hb=m['home_evidence'];ab=m['away_evidence']
            hd=hb.get('details') or {};ad=ab.get('details') or {}
            profiles=m.get('market_profiles') or {}
            def metric(side,key,attr):return ((side.get('own') or {}).get(key) or {}).get(attr)
            def prof(key,attr):return (profiles.get(key) or {}).get(attr)
            w.writerow({**{k:m.get(k) for k in fields},
              'home_history_n':hb['history']['completed_total'],
              'away_history_n':ab['history']['completed_total'],
              'home_corners_n':metric(hd,'corners','n'),
              'away_corners_n':metric(ad,'corners','n'),
              'home_corners_avg':metric(hd,'corners','mean'),
              'away_corners_avg':metric(ad,'corners','mean'),
              'corners_coverage_pct':prof('corners','coverage_pct'),
              'corners_stability_score':prof('corners','stability_score'),
              'shots_coverage_pct':prof('shots','coverage_pct'),
              'fouls_coverage_pct':prof('fouls','coverage_pct'),
              'xg_coverage_pct':prof('xg','coverage_pct'),
              'home_rank':hb['standings'].get('rank'),
              'away_rank':ab['standings'].get('rank')})
    lines=[f'# Preselección estadística PREMATCH {VERSION} — sin picks','',
           f"- Partidos en salida: **{report['counts']['evaluated']}**",
           f"- Partidos con estadística detallada: **{report['counts'].get('fully_evaluated','N/D')}**",
           f"- Llamadas API nuevas: **{report['api_usage']['requests_sent']}** (caché: {report['api_usage']['cache_hits']})",
           f"- Errores API: **{report['counts']['api_errors']}**",
           '- El índice de datos mide cobertura, y estabilidad mide variabilidad descriptiva.',
           '- La prioridad NO mide valor esperado, probabilidad de acierto o liquidez Bwin.','',
           '| Puesto | Fixture | Encuentro | Preliminar | Cobertura | Estabilidad | Prioridad | Estado |',
           '|---:|---|---|---:|---:|---:|---:|---|']
    for m in report['matches']:
        def shown(x):return 'N/D' if x is None else str(x)
        lines.append(f"| {shown(m.get('data_readiness_rank'))} | `{m['fixture_id']}` | {m['home']}–{m['away']} | {shown(m.get('preliminary_coverage_score'))} | {shown(m.get('data_readiness_score'))} | {shown(m.get('stability_score'))} | {shown(m.get('research_priority_score'))} | {m['data_readiness_status']} |")
    for m in report['matches']:
        if m.get('statistical_priority'):
            lines.extend(['',f"### Cobertura declarada y prioridad — {m['fixture_id']}",json.dumps({'contract':m.get('competition_coverage'),'priority':m['statistical_priority'],'legacy_rank':m.get('legacy_data_readiness_rank')},ensure_ascii=False)])
    lines+=['','## Cobertura por familia estadística de los partidos detallados','']
    for m in report['matches']:
        if not m.get('market_profiles'):continue
        allp=m['market_profiles']
        vals=', '.join(f'{k}: {v["coverage_pct"]:.1f}%' for k,v in allp.items())
        lines.append(f"- `{m['fixture_id']}` **{m['home']}–{m['away']}**: {vals}")
    if report['api_errors']:
        lines+=['','## Errores de adquisición','']
        lines.extend(f'- {k}: {str(v)[:180]}' for k,v in report['api_errors'].items())
    (folder/'preselection.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--input',type=Path,help='fixtures_major.json; no se requiere con --rescore-json')
    p.add_argument('--output-dir',type=Path)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--max-matches',type=int,default=0,help='0=todos; prueba inicial 5')
    p.add_argument('--include-review',action='store_true',help='Investigar también clasificación pendiente, sin cambiar sus metadatos')
    p.add_argument('--fixture-id',type=int,help='Evalúa solo el partido elegido')
    p.add_argument('--fixture-ids-file',type=Path,
                   help='Archivo de IDs (uno por línea): evalúa exactamente esta tanda')
    p.add_argument('--merge-report',type=Path,
                   help='Fusiona los partidos con detalle de esta tanda en un preselection anterior del mismo filtro')
    p.add_argument('--history-limit',type=int,default=20)
    p.add_argument('--history-seasons',type=int,default=3,help='1..3 temporadas; completa si la actual tiene pocos históricos')
    p.add_argument('--detail-limit',type=int,default=8)
    p.add_argument('--refresh',action='store_true')
    p.add_argument('--request-budget',type=int,default=0,help='0=sin límite adicional; reserva excluida del uso normal')
    p.add_argument('--request-reserve',type=int,default=0,help='Capacidad que esta ejecución no consumirá')
    p.add_argument('--mode',choices=('quick','full','adaptive'),default='full',
                   help='quick=solo históricos/tabla; full=todo; adaptive=detalle TOP sin excluir resto')
    p.add_argument('--detail-top',type=int,default=30,
                   help='En --mode adaptive, número de encuentros con estadística detallada (plan equilibrado V1.3)')
    p.add_argument('--rescore-json',type=Path,
                   help='Recalcular un preselection V1.1/V1.2 sin consumir API')
    p.add_argument('--confirm-external',action='store_true')
    p.add_argument('--coverage-rules',type=Path,help='JSON de reglas de prioridad declarada; enabled=false conserva ranking anterior')
    p.add_argument('--asof', help='Instante único ISO8601 con offset para el corte PREMATCH de esta evaluación')
    args=p.parse_args(argv)
    if not args.rescore_json:
        from prematch_execution_control import require_external_confirmation
        try:require_external_confirmation(args.confirm_external)
        except ValueError as error:p.error(str(error))
    if args.fixture_id is not None and args.fixture_ids_file is not None:
        p.error('Usa --fixture-id o --fixture-ids-file, no ambos')
    if args.merge_report and not args.fixture_ids_file and not args.fixture_id:
        p.error('--merge-report requiere --fixture-ids-file o --fixture-id')
    if args.max_matches<0 or not 4<=args.history_limit<=50 or not 1<=args.detail_limit<=args.history_limit or not 1<=args.history_seasons<=3 or args.detail_top<1 or args.request_budget<0 or args.request_reserve<0 or args.request_budget and args.request_reserve>=args.request_budget:
        p.error('history-limit 4..50, history-seasons 1..3, detail-limit 1..history-limit, max-matches >=0')
    try:
        if args.rescore_json:
            old=json.loads(args.rescore_json.read_text(encoding='utf-8'))
            if args.coverage_rules:
                old.setdefault('methodology',{})['coverage_rules']=validate_coverage_rules(json.loads(args.coverage_rules.read_text()))
            report=rescore_existing(old)
            folder=args.output_dir or args.rescore_json.parent/'v12_rescore'
            save_report(report,folder)
            print(f"Recalificación sin API: {len(report['matches'])} | Resultados: {folder/'preselection.json'}")
            return 0
        if not args.input:
            raise PreselectionError('Indica --input fixtures_major.json o --rescore-json')
        from config.constants import API_KEY,FOOTBALL_BASE_URL,REQUEST_TIMEOUT
        if not API_KEY:raise PreselectionError('No se encontró API_KEY en config.constants')
        raw=args.input.read_bytes();doc=json.loads(raw)
        if args.include_review:
            doc=dict(doc,fixtures=doc.get('fixtures',[])+doc.get('review',[]))
            ids=[f['fixture_id'] for f in doc['fixtures']]
            if len(ids)!=len(set(ids)):raise PreselectionError('Fixture IDs duplicados entre elegibles y pendientes')
        chosen_ids=read_fixture_ids_file(args.fixture_ids_file) if args.fixture_ids_file else None
        client=ApiFootball(API_KEY,FOOTBALL_BASE_URL,args.root,REQUEST_TIMEOUT,args.refresh,
                           args.request_budget,args.request_reserve)
        evaluation_now = utc_datetime(args.asof) if args.asof else datetime.now(UTC)
        client.asof=utc_datetime(args.asof) if args.asof else None
        try:report=run_evaluator(doc,client,evaluation_now,args.history_limit,
                         args.detail_limit,args.max_matches,args.fixture_id,args.history_seasons,
                         args.mode,args.detail_top,chosen_ids,
                         json.loads(args.coverage_rules.read_text()) if args.coverage_rules else None)
        finally:client.close()
        report['source_filter_sha256']=hashlib.sha256(raw).hexdigest()
        report['run_status']='INCOMPLETE' if report['api_errors'] else 'COMPLETED'
        if args.merge_report:
            if args.mode!='full':
                raise PreselectionError('--merge-report requiere --mode full')
            baseline=json.loads(args.merge_report.read_text(encoding='utf-8'))
            report=merge_preselection_reports(baseline,report)
        folder=args.output_dir or args.input.parent
        save_report(report,folder)
        print(f"Evaluados: {report['counts']['evaluated']} | Detalle: {report['counts']['fully_evaluated']} | Consultas API: {report['api_usage']['requests_sent']} | Caché: {report['api_usage']['cache_hits']} | Errores API: {report['counts']['api_errors']}")
        print(f'Resultados: {folder/"preselection.json"} | {folder/"preselection.csv"} | {folder/"preselection.md"}')
        # Per-fixture acquisition failures remain visible in the report and
        # must not prevent the next independent, offline phase.
        return 0
    except (OSError,ValueError,KeyError,ImportError,PreselectionError,GlobalApiError) as e:
        print(f'ERROR: {e}',file=sys.stderr);return 2

from prematch_execution_control import confirmed_cli
main = confirmed_cli(main)

if __name__=='__main__':raise SystemExit(main())
