#!/usr/bin/env python3
"""Reproduce verified repository identity cases without network access."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from bwin_identity_audit import audit, read, sha, write_reports


def build(dataset: dict, root: Path):
    fixtures=[];catalog=[]
    for case in dataset.get('positive_cases',[]):
        evidence=root/case['evidence']
        if not evidence.is_file() or sha(evidence)!=case['evidence_sha256']:
            raise ValueError('REGRESSION_EVIDENCE_SHA_MISMATCH:'+str(evidence))
        kickoff=datetime.fromisoformat(case['kickoff_utc'].replace('Z','+00:00'))
        local=kickoff.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Madrid'))
        observed=local-timedelta(hours=2)
        day_label='Hoy' if observed.date()==local.date() else 'Mañana'
        gender='F' if case['category'].startswith('F ') else 'M'
        team_kind='NATIONAL' if case['category'].endswith('NATIONAL') else 'CLUB'
        fixtures.append({'fixture_id':case['fixture_id'],'home':case['api_home'],'away':case['api_away'],
            'kickoff_madrid':local.isoformat(),'kickoff_utc':kickoff.isoformat(),'league':case['competition'],
            'league_id':case['league_id'],'country':case['country'],
            'gender':gender,'team_kind':team_kind,'category_status':'ELIGIBLE','season':2026})
        from competition_registry import category
        fixtures[-1].update(category(fixtures[-1]))
        slug=quote((case['bwin_home']+'-'+case['bwin_away']).lower().replace(' ','-'))
        catalog.append({'bwin_event_id':case['bwin_event_id'],
            'bwin_url':f'https://www.bwin.es/es/sports/eventos/{slug}-2:{case["bwin_event_id"]}',
            'text':f'{case["competition"]} {day_label} / {local:%H:%M} {case["bwin_home"]} - {case["bwin_away"]}',
            'text_source':'VERIFIED_REGRESSION_EVIDENCE','text_observed_at_utc':observed.isoformat(),
            'source_pages':[str(evidence.resolve())]})
    # Real-evidence positives define the recall denominator. Negative controls
    # are exercised by the feature test suite and never fabricated as Bwin IDs.
    return fixtures,catalog


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--dataset',type=Path,default=Path('config/bwin_identity_regression.json'))
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv);root=args.root.resolve();dataset=args.dataset if args.dataset.is_absolute() else root/args.dataset
    try:
        truth=read(dataset);fixtures,catalog=build(truth,root)
        document=audit(fixtures,catalog,[],[{'path':str(dataset.resolve()),'sha256':sha(dataset),'rows':len(catalog)}],
                       catalog_complete=True,truth=truth)
        write_reports(args.output_dir,document)
        print(json.dumps(document['metrics'],ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError,json.JSONDecodeError) as exc:
        print('BLOQUEADO:',exc,file=__import__('sys').stderr);return 2


if __name__=='__main__':raise SystemExit(main())
