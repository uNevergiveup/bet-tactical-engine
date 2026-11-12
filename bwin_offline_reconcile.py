#!/usr/bin/env python3
"""Reconcile every API fixture against a frozen Bwin catalog, without network."""
from __future__ import annotations
import argparse,csv,hashlib,json
from collections import Counter
from pathlib import Path
from bwin_discovery import (TZ,category_compatibility,catalog_relative_kickoff,
    competition_similarity,displayed_header_kickoff,local_kickoff,match_catalog,norm,
    senior_category_corroborated)

VERSION='1.0-frozen-catalog-reconciliation'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def load_fixtures(path):
    doc=json.loads(Path(path).read_text())
    rows=doc.get('fixtures') if isinstance(doc,dict) else None
    if not isinstance(rows,list):raise ValueError('FULL_FIXTURE_INVENTORY_REQUIRED')
    ids=[r.get('fixture_id') for r in rows]
    if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_FIXTURE_ID')
    return rows

def prior_by_fixture(discovery):return {str(r['fixture_id']):r for r in discovery.get('matches',[]) if r.get('fixture_id') is not None}

def inspection_evidence(prior,event_id):
    row=next((r for r in prior.values() if str(r.get('bwin_event_id'))==str(event_id)),None)
    return (row or {}).get('inspection') or {}

def reconcile_fixture(fixture,catalog,prior,pages):
    candidates=match_catalog(fixture,catalog);old=prior.get(str(fixture['fixture_id']))
    base={'fixture_id':fixture['fixture_id'],'home':fixture['home'],'away':fixture['away'],
        'home_id':fixture['home_id'],'away_id':fixture['away_id'],'kickoff_madrid':fixture['kickoff_madrid'],
        'league':fixture.get('league'),'league_id':fixture.get('league_id'),'season':fixture.get('season'),
        'country':fixture.get('country'),'gender':fixture.get('gender','UNKNOWN'),
        'category_status_input':fixture.get('category_status'),'previous_status':(old or {}).get('status','NOT_IN_LEGACY_COHORT'),
        'previous_event_id':(old or {}).get('bwin_event_id'),'pages_scanned':pages,
        'candidate_count':len(candidates),'candidates':[],'event_id':None,'bwin_url':None,
        'post_status':'NOT_OBSERVED_IN_LOCAL_CATALOG','identity_verified':False,'quote_capture_verified':False,
        'root_cause':'SEARCH_OR_CATALOG_COVERAGE','recovery':'DIRECTED_ONLINE_PILOT_OR_FUTURE_DISCOVERY'}
    if not candidates:
        if base['previous_status']=='NOT_IN_LEGACY_COHORT':
            base.update(root_cause='LEGACY_FILTER_EXCLUDED_AND_NO_COMPATIBLE_EVENT_IN_168_RETAINED',recovery='FUTURE_FULL_INVENTORY_DISCOVERY')
        else:
            base.update(root_cause='NO_COMPATIBLE_TWO_TEAM_EVENT_IN_168_RETAINED',recovery='DIRECTED_ONLINE_PILOT_OR_FUTURE_DISCOVERY')
        return base
    for item in candidates[:10]:
        clock=catalog_relative_kickoff(item);api=local_kickoff(fixture['kickoff_madrid'])
        base['candidates'].append({'event_id':item['bwin_event_id'],'url':item['bwin_url'],
            'text':item.get('text'),'source_pages':item.get('source_pages',[]),'name_score':item['name_score'],
            'home_similarity':item['home_similarity'],'away_similarity':item['away_similarity'],
            'order':item['team_order_status'],'category':item['category_compatibility'],
            'competition_catalog_score':item.get('competition_similarity'),
            'catalog_kickoff_madrid':clock.isoformat() if clock else None,
            'kickoff_difference_minutes':abs((clock-api).total_seconds())/60 if clock and api else None})
    if len(candidates)>1 and candidates[0]['name_score']-candidates[1]['name_score']<.05:
        base.update(post_status='AMBIGUOUS_LOCAL_CANDIDATES',root_cause='MULTIPLE_NAME_CANDIDATES',recovery='MANUAL_IDENTITY_REVIEW');return base
    win=candidates[0];event_id=win['bwin_event_id'];clock=catalog_relative_kickoff(win);api=local_kickoff(fixture['kickoff_madrid'])
    inspect=inspection_evidence(prior,event_id);absolute=local_kickoff(inspect.get('event_start_machine_readable'))
    scoped_header=displayed_header_kickoff(inspect.get('displayed_time_evidence') or {})
    used_clock=absolute or scoped_header or clock;difference=abs((used_clock-api).total_seconds())/60 if used_clock and api else None
    competition_score=inspect.get('competition_match_score')
    competition_ok=isinstance(competition_score,(int,float)) and not isinstance(competition_score,bool) and competition_score>=.82
    category=win['category_compatibility'];category_unknown=category=='CATEGORY_UNCONFIRMED'
    senior_inferred=senior_category_corroborated(fixture,category,competition_score)
    if senior_inferred:category_unknown=False
    base.update(event_id=event_id,bwin_url=win['bwin_url'],source_pages=win.get('source_pages',[]),
        name={'status':'CANDIDATE_COMPATIBLE','score':win['name_score'],'order':win['team_order_status']},
        time={'status':'MATCH' if difference is not None and difference<=15 else 'CONFLICT' if difference is not None else 'MISSING',
              'api':api.isoformat() if api else None,'observed':used_clock.isoformat() if used_clock else None,
              'source':'INSPECTION_ABSOLUTE' if absolute else 'SCOPED_PREMATCH_HEADER' if scoped_header else 'CATALOG_RELATIVE_CAPTURE_ANCHORED' if clock else None,
              'difference_minutes':difference},
        category={'status':'SENIOR_COMPETITION_CORROBORATED' if senior_inferred else 'UNKNOWN_PENDING' if category_unknown else category},
        competition={'status':'CORROBORATED_INSPECTION' if competition_ok else 'UNVERIFIED','score':competition_score},
        recovery='OFFLINE_LOCAL_CATALOG')
    if difference is not None and difference>15:
        base.update(post_status='CANDIDATE_TIME_CONFLICT',root_cause='OBSERVED_KICKOFF_CONTRADICTS_API',recovery='MANUAL_SCHEDULE_REVIEW');return base
    if difference is None:
        base.update(post_status='CANDIDATE_TIME_UNVERIFIED',root_cause='NO_CAPTURE_ANCHORED_CLOCK');return base
    if category_unknown:
        base.update(post_status='CANDIDATE_CATEGORY_UNKNOWN',root_cause='CATEGORY_EVIDENCE_ABSENT');return base
    if not competition_ok:
        base.update(post_status='CANDIDATE_COMPETITION_UNVERIFIED',root_cause='COMPETITION_EVIDENCE_ABSENT');return base
    base.update(post_status='IDENTITY_VERIFIED_OFFLINE',identity_verified=True,root_cause='RECOVERED_FROM_FROZEN_EVIDENCE')
    return base

def run(fixtures_path,discovery_path,fixture_ids=(),preflights=(),market_mapping=None):
    fixtures=load_fixtures(fixtures_path);discovery=json.loads(Path(discovery_path).read_text())
    if fixture_ids:
        wanted=set(fixture_ids);fixtures=[r for r in fixtures if r['fixture_id'] in wanted]
        missing=wanted-{r['fixture_id'] for r in fixtures}
        if missing:raise ValueError('FIXTURE_ID_NOT_IN_INVENTORY:'+','.join(map(str,sorted(missing))))
    catalog=discovery.get('catalog_observed',[]);prior=prior_by_fixture(discovery)
    pages=[{'url':r.get('catalog_page'),'status':r.get('status'),'events_found':r.get('events_found')} for r in discovery.get('pages',[])]
    matrix=[reconcile_fixture(r,catalog,prior,pages) for r in fixtures]
    captured=set()
    for path in preflights:
        doc=json.loads(Path(path).read_text())
        for match in doc.get('matches',[]):
            snap=match.get('snapshot') or {}
            candidate=Path(str(snap.get('snapshot_path') or ''))
            if candidate.is_file() and snap.get('sha256')==sha(candidate):captured.add(int(match['fixture_id']))
    compatible=set()
    if market_mapping:
        document=json.loads(Path(market_mapping).read_text())
        compatible={int(r['fixture_id']) for r in document.get('mappings',[]) if r.get('mapping_status')=='MARKET_MATCHED'}
    for row in matrix:
        row['quote_capture_verified']=row['fixture_id'] in captured
        row['compatible_market_verified']=row['fixture_id'] in compatible
    counts=dict(Counter(r['post_status'] for r in matrix))
    counts.update(fixtures_with_candidate=sum(r['event_id'] is not None for r in matrix),
        fixtures_identity_verified=sum(r['identity_verified'] for r in matrix),
        fixtures_quote_capture_verified=len(captured & {r['fixture_id'] for r in matrix}),
        fixtures_compatible_market_verified=len(compatible & {r['fixture_id'] for r in matrix}),
        fixtures_without_sufficient_identity=sum(not r['identity_verified'] for r in matrix),
        fixtures_not_observed_in_consulted_sources=sum(r['post_status']=='NOT_OBSERVED_IN_LOCAL_CATALOG' for r in matrix))
    return {'schema_version':VERSION,'inputs':{'fixtures':str(Path(fixtures_path).resolve()),'fixtures_sha256':sha(fixtures_path),
        'discovery':str(Path(discovery_path).resolve()),'discovery_sha256':sha(discovery_path),
        'preflights':[{'path':str(Path(p).resolve()),'sha256':sha(p)} for p in preflights],
        'market_mapping':{'path':str(Path(market_mapping).resolve()),'sha256':sha(market_mapping)} if market_mapping else None},
        'catalog_events':len(catalog),'fixtures':matrix,'counts':counts,
        'definitions':{'NOT_OBSERVED_IN_LOCAL_CATALOG':'Not evidence that Bwin did not offer the fixture',
            'identity_verified':'Does not imply odds were captured'}}

def write(result,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    (out/'reconciliation_73.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    fields=['fixture_id','match','kickoff_madrid','league','season','event_id','previous_status','post_status','origin',
        'name_status','time_status','time_source','time_difference_minutes','category_status','root_cause','recovery',
        'quote_capture_verified','compatible_market_verified']
    with (out/'reconciliation_73.csv').open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fields);w.writeheader()
        for row in result['fixtures']:
            w.writerow({'fixture_id':row['fixture_id'],'match':row['home']+' vs '+row['away'],'kickoff_madrid':row['kickoff_madrid'],
                'league':row['league'],'season':row['season'],'event_id':row.get('event_id'),'previous_status':row['previous_status'],
                'post_status':row['post_status'],'origin':' | '.join(row.get('source_pages',[])),
                'name_status':(row.get('name') or {}).get('status'),'time_status':(row.get('time') or {}).get('status'),
                'time_source':(row.get('time') or {}).get('source'),'time_difference_minutes':(row.get('time') or {}).get('difference_minutes'),
                'category_status':(row.get('category') or {}).get('status'),'root_cause':row['root_cause'],'recovery':row['recovery'],
                'quote_capture_verified':row['quote_capture_verified'],'compatible_market_verified':row['compatible_market_verified']})
    (out/'summary.json').write_text(json.dumps({'catalog_events':result['catalog_events'],'fixtures':len(result['fixtures']),'counts':result['counts']},indent=2)+'\n')

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--discovery',type=Path,required=True)
    p.add_argument('--fixture-id',type=int,action='append',default=[]);p.add_argument('--preflight',type=Path,action='append',default=[])
    p.add_argument('--market-mapping',type=Path);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args(argv)
    result=run(a.fixtures,a.discovery,a.fixture_id,a.preflight,a.market_mapping);write(result,a.output_dir);print(json.dumps(result['counts'],ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())
