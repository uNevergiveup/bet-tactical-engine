#!/usr/bin/env python3
"""Offline diagnostic for PREMATCH Bwin Discovery -> Preflight identity/time mismatches.

Reads existing JSON only. Never opens Bwin, calls API-Football, or modifies inputs.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
MADRID=ZoneInfo('Europe/Madrid')

def load(path):
    if not path.is_file(): return None
    return json.loads(path.read_text(encoding='utf-8-sig'))

def diagnose(discovery, preflight_root):
    data=load(discovery)
    if not data or not isinstance(data.get('matches'),list):
        raise ValueError(f'Discovery inválido o no encontrado: {discovery}')
    out=[]
    for m in data['matches']:
        if not m.get('bwin_event_id'):
            continue
        eid=str(m['bwin_event_id']); fid=str(m.get('fixture_id'))
        path=preflight_root/fid/'market_preflight.json'
        pre=load(path)
        result=None
        if pre:
            matches=[p for p in pre.get('matches',[]) if str(p.get('fixture_id'))==fid]
            result=matches[0] if matches else None
        inspected=m.get('inspection') or {}
        related=[c for c in data.get('catalog_observed',[]) if str(c.get('bwin_event_id'))==eid]
        row={
            'fixture_id':m.get('fixture_id'), 'match': f"{m.get('home')}–{m.get('away')}",
            'kickoff_madrid':m.get('kickoff_madrid'), 'bwin_event_id':eid,
            'bwin_url':m.get('bwin_url'), 'discovery_status':m.get('status'),
            'identity_status':m.get('identity_status'),
            'page_title':inspected.get('page_title'),
            'title_teams_match':inspected.get('title_teams_match'),
            'event_id_from_url':inspected.get('event_id_from_url'),
            'time_evidence_type':inspected.get('time_evidence_type'),
            'countdown_difference_minutes':inspected.get('countdown_difference_minutes'),
            'countdown_observed_at_utc':inspected.get('countdown_observed_at_utc'),
            'event_start_from_countdown':inspected.get('event_start_from_countdown'),
            'event_start_machine_readable':inspected.get('event_start_machine_readable'),
            'preflight_status':result.get('status') if result else 'NO_PREFLIGHT',
            'preflight_reasons':result.get('reasons',[]) if result else [],
            'catalog_observations':[{'text':c.get('text','')[:300],
                                     'captured_at_utc':c.get('text_observed_at_utc'),
                                     'bwin_url':c.get('bwin_url')} for c in related[:5]],
        }
        rel=(m.get('identity_status')=='NAME_AND_RELATIVE_TIME_CORROBORATED'
             and inspected.get('title_teams_match') is True
             and str(inspected.get('event_id_from_url'))==eid
             and inspected.get('time_evidence_type')=='RELATIVE_CATALOG_COUNTDOWN'
             and related)
        row['offline_diagnostic']=('CANDIDATE_COUNTDOWN_FIX_RECHECK_WITH_PATCH' if rel
                                   else 'MUST_REVIEW_EVENT_IDENTITY' if inspected.get('title_teams_match') is False
                                   else 'NEED_KICKOFF_CORROBORATION_OR_OTHER_EVIDENCE')
        out.append(row)
    return {'source':str(discovery),'events':out}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--discovery',type=Path,required=True)
    p.add_argument('--preflight-root',type=Path,required=True)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    try:
        doc=diagnose(args.discovery,args.preflight_root)
        for e in doc['events']:
            print(f"[{e['fixture_id']}] {e['match']} | Bwin={e['bwin_event_id']} | {e['kickoff_madrid']}")
            print(f"  Discovery: {e['discovery_status']} / {e['identity_status']}")
            print(f"  Page title: {e['page_title']!r} | teams_ok={e['title_teams_match']}")
            print(f"  Event ID in inspected URL: {e['event_id_from_url']}")
            print(f"  Time: {e['time_evidence_type']} | countdown_delta={e['countdown_difference_minutes']}m")
            print(f"  Preflight: {e['preflight_status']} | {', '.join(e['preflight_reasons']) or 'sin motivos registrados'}")
            print(f"  Próximo paso: {e['offline_diagnostic']}")
            for c in e['catalog_observations'][:2]:
                print(f"  Bwin catalog anchor: {c['text']!r} | at={c['captured_at_utc']}")
            print()
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(f"Diagnóstico guardado: {args.output}")
        return 0
    except (OSError,ValueError,json.JSONDecodeError) as e:
        p.exit(2,f'ERROR: {e}\n')
if __name__=='__main__':raise SystemExit(main())
