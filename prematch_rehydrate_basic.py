#!/usr/bin/env python3
"""Reconstruye observaciones FT desde respuestas de API-Football ya incluidas
   en el ledger de un preselection congelado. NO consulta la red ni genera picks.

Este archivo produce un *replay offline derivado*, no una predicción que se haya
registrado en directo en el instante histórico del snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlencode

from preselect_matches import prepare_history, utc_datetime
from prematch_tactical_history import observations_from_games


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rebuild(source: Path, root: Path) -> tuple[dict, dict]:
    source = Path(source)
    pre = json.loads(source.read_text(encoding='utf-8-sig'))
    if pre.get('schema_version') != '1.3-preselection':
        raise ValueError('Solo se admite preselection 1.3')
    asof = utc_datetime(pre['generated_at_utc'])
    ledger = pre.get('api_request_ledger')
    if not isinstance(ledger, list) or not ledger:
        raise ValueError('Snapshot sin ledger de peticiones: no se pueden verificar respuestas históricas')

    source_hash = digest(source.read_bytes())
    history = defaultdict(list)
    details = {}
    reasons = Counter()
    good_hashes = set()
    cache_dir = root / 'outputs/data/cache/preselect_api'
    for entry in ledger:
        if entry.get('status') not in ('NETWORK_OK', 'CACHE_HIT') or entry.get('endpoint') != 'fixtures':
            continue
        request_id = entry.get('request_id')
        if not isinstance(request_id, str) or len(request_id) != 64 or any(ch not in '0123456789abcdef' for ch in request_id):
            reasons['BAD_REQUEST_ID'] += 1
            continue
        params = entry.get('parameters') or {}
        expected_id = digest(('fixtures?' + urlencode(sorted((str(k), str(v)) for k, v in params.items()))).encode())
        if request_id != expected_id:
            reasons['CACHE_QUERY_HASH_MISMATCH'] += 1
            continue
        cache = cache_dir / (request_id + '.json')
        if not cache.is_file():
            reasons['CACHE_FILE_MISSING'] += 1
            continue
        raw_bytes = cache.read_bytes()
        if digest(raw_bytes) != entry.get('cache_sha256'):
            reasons['CACHE_SHA_MISMATCH'] += 1
            continue
        try:
            record = json.loads(raw_bytes)
            seen = utc_datetime(record['retrieved_at_utc'])
            logged = utc_datetime(entry['first_observed_at_utc'])
            if max(seen, logged) > asof:
                reasons['CACHE_AFTER_SNAPSHOT'] += 1
                continue
            response = record.get('response')
            if not isinstance(response, list):
                raise ValueError('response no es lista')
            # A legacy cache can lack stored endpoint/parameters.  Its request
            # ID must still prove the exact normalized route and arguments.
            if (record.get('endpoint') not in (None, 'fixtures') or
                    record.get('parameters') not in (None, params)):
                reasons['CACHE_REQUEST_IDENTITY_MISMATCH'] += 1
                continue
            expected_response_sha = entry.get('response_sha256')
            if expected_response_sha and digest(json.dumps(response, sort_keys=True,
                    separators=(',', ':')).encode()) != expected_response_sha:
                reasons['CACHE_RESPONSE_SHA_MISMATCH'] += 1
                continue
        except (ValueError, KeyError, TypeError):
            reasons['CACHE_INVALID'] += 1
            continue
        good_hashes.add(digest(raw_bytes))
        if 'team' in params:
            try:
                team_id = int(params['team'])
                if team_id <= 0:
                    raise ValueError('team id')
            except (ValueError, TypeError):
                reasons['BAD_TEAM_ID'] += 1
                continue
            history[team_id].extend(response)
        elif 'ids' in params:
            for record in response:
                if not isinstance(record, dict):
                    continue
                fid = (record.get('fixture') or {}).get('id')
                if isinstance(fid, int) and fid > 0:
                    details[fid] = record

    requested_ids = set(details)
    limit = int((pre.get('methodology') or {}).get('sample_history_cap') or 20)
    restored_fixtures = []
    restored_sides = 0
    rows_restored = 0
    for match in pre.get('matches', []):
        observed = match.setdefault('statistical_observations', {'home': [], 'away': []})
        cutoff = utc_datetime(match['analysis_cutoff_utc'])
        fixture_changed = False
        for side in ('home', 'away'):
            if observed.get(side):
                continue
            team_id = int(match[side + '_id'])
            games = prepare_history(history.get(team_id, []), team_id, cutoff, lookback=limit)
            rows = observations_from_games(games, team_id, details, requested_ids=requested_ids)
            if rows:
                observed[side] = rows
                restored_sides += 1
                rows_restored += len(rows)
                fixture_changed = True
        if fixture_changed:
            restored_fixtures.append(match['fixture_id'])

    audit = {
        'status': 'OFFLINE_DERIVED_REPLAY_NOT_PROSPECTIVE_PREDICTION',
        'original_preselection_sha256': source_hash,
        'original_source_asof_utc': asof.isoformat(),
        'cache_files_verified': len(good_hashes),
        'restored_fixture_count': len(restored_fixtures),
        'restored_fixture_ids': restored_fixtures,
        'restored_team_sides': restored_sides,
        'restored_history_rows': rows_restored,
        'cache_skips_by_reason': dict(sorted(reasons.items())),
        'no_network_access': True,
        'original_snapshot_was_not_modified': True,
    }
    pre['offline_rehydration'] = audit
    return pre, audit


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--preselection', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--root', type=Path, default=Path('.'))
    args = p.parse_args()
    source, output = args.preselection.resolve(), args.output.resolve()
    if source == output:
        p.error('La salida debe ser distinta del snapshot original')
    try:
        pre, audit = rebuild(source, args.root.resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(pre, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 2
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    print('Salida:', output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
