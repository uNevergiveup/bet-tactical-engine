#!/usr/bin/env python3
"""Build optional offline prior-reference pool from already stored PREMATCH snapshots.

Only snapshots declared at/before the target cutoff qualify. Does not validate
source timestamps against a trusted external clock; metadata must be audited.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from prematch_sos_v1044 import read_json, file_hash, parse_time, normalize_record, _id
from datetime import timedelta

SCHEMA = 'prematch-sos-reference-pool-1.0'


def collect(root: Path, event_id: str) -> dict:
    root = Path(root)
    target_dir = root / 'outputs' / 'matches' / event_id / 'analysis' / 'deep_v1042'
    target = read_json(target_dir / f'rendimiento_profundo_{event_id}.json')
    target_cutoff = parse_time(target.get('analysis_cutoff_utc'))
    target_kickoff = parse_time(target.get('target_kickoff_utc'))
    target_fid = _id(target.get('target_fixture_id'))
    if not target_cutoff or not target_kickoff or not target_fid or target_cutoff >= target_kickoff:
        raise ValueError('Corte del objetivo incorrecto')
    candidates = sorted((root / 'outputs' / 'matches').glob('*/analysis/deep_v1042/rendimiento_profundo_*.json'))
    retained = {}
    included_sources = []
    excluded = []
    for p in candidates:
        source_event = p.parent.parent.parent.name
        if source_event == event_id:
            continue
        try:
            d = read_json(p)
            source_cutoff = parse_time(d.get('analysis_cutoff_utc'))
            snapshot = parse_time(d.get('snapshot_generated_at_utc'))
            source_kickoff = parse_time(d.get('target_kickoff_utc'))
            if (not source_cutoff or not snapshot or not source_kickoff or
                    snapshot > source_cutoff or source_cutoff >= source_kickoff or
                    source_cutoff > target_cutoff):
                excluded.append({'event_id': source_event, 'reason': 'INVALID_OR_FUTURE_SNAPSHOT'})
                continue
            quality_path = p.parent / f'auditoria_calidad_{source_event}.json'
            if not quality_path.is_file():
                excluded.append({'event_id': source_event, 'reason': 'QUALITY_AUDIT_MISSING'})
                continue
            quality = read_json(quality_path)
            if (quality.get('quality_status') == 'BLOCKED' or
                    any(x.get('severity') == 'CRITICAL' for x in (quality.get('issues') or [])) or
                    quality.get('target_fixture_id') != d.get('target_fixture_id')):
                excluded.append({'event_id': source_event, 'reason': 'SOURCE_QUALITY_NOT_ELIGIBLE'})
                continue
            count = 0
            for side in ('home', 'away'):
                team = (d.get('teams') or {}).get(side) or {}
                tid = _id(team.get('team_id'))
                if tid is None:
                    raise ValueError('TEAM_ID_MISSING')
                for row in team.get('historical_fixtures') or []:
                    record = normalize_record({**row, '_tracked_team_id': tid})
                    kickoff = parse_time(record['kickoff_utc'])
                    if (record['fixture_id'] == target_fid or
                            kickoff + timedelta(hours=3) >= min(source_cutoff, target_cutoff)):
                        raise ValueError('HISTORICAL_FIXTURE_AFTER_SOURCE_CUTOFF')
                    fid = record['fixture_id']
                    if fid in retained:
                        other = retained[fid]
                        if (other['home']['id'], other['away']['id'], other['kickoff_utc']) != (
                                record['home']['id'], record['away']['id'], record['kickoff_utc']):
                            raise ValueError(f'INCONSISTENT_REFERENCE_FIXTURE {fid}')
                        continue
                    retained[fid] = record
                    count += 1
            included_sources.append({'event_id': source_event,
                                     'performance_sha256': file_hash(p),
                                     'quality_sha256': file_hash(quality_path),
                                     'new_unique_fixtures': count})
        except (ValueError, KeyError, TypeError) as exc:
            # Never include partial data from invalid sources.
            # Abort completely; a source inconsistency is not a harmless skip.
            raise ValueError(f'Reference pool source invalid {p}: {exc}') from exc
    return {'schema': SCHEMA, 'target_event_id_bwin': event_id,
            'target_cutoff_utc': target_cutoff.isoformat(),
            'source_scope': 'ONLY_LOCAL_SNAPSHOTS_BEFORE_TARGET_CUTOFF',
            'declaration_only': True,
            'included_sources': included_sources, 'excluded_sources': excluded,
            'games': sorted(retained.values(), key=lambda g: (g['kickoff_utc'],g['fixture_id']))}


def main():
    ap=argparse.ArgumentParser(description='Construir pool local histórico de otros snapshots; no usa API')
    ap.add_argument('--event-id',required=True)
    ap.add_argument('--root',type=Path, default=Path('.'))
    args=ap.parse_args()
    result=collect(args.root,args.event_id)
    folder=args.root/'outputs'/'matches'/args.event_id/'analysis'/'deep_v1042'
    path=folder/f'pool_referencias_{args.event_id}.json'
    path.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'Pool local: {len(result["games"])} fixtures adicionales; '
          f'{len(result["included_sources"])} snapshots elegibles; '
          f'{len(result["excluded_sources"])} omitidos')
    print(path)

if __name__=='__main__':
    main()
