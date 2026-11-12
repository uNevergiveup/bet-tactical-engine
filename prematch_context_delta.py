"""Offline differential target-lineup refresh. Historical inputs remain immutable."""
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from prematch_cache_record import unpack_record, payload_sha
from prematch_paths import local_reference


def when(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('DELTA_TIMEZONE_REQUIRED')
    return result


def lineup_state(response, team_ids):
    """Formation/grid are provider observations, never inferred tactical roles."""
    states = {}
    for block in response:
        tid = (block.get('team') or {}).get('id')
        if type(tid) is not int or tid not in team_ids or tid in states:
            raise ValueError('DELTA_LINEUP_TEAM_IDENTITY')
        players = {}
        for item in block.get('startXI', []):
            p = item.get('player') or {}
            pid = p.get('id')
            if type(pid) is not int or pid <= 0 or pid in players:
                raise ValueError('DELTA_PLAYER_IDENTITY')
            players[pid] = {key: p.get(key) for key in ('pos', 'grid', 'number')}
        if len(players) > 11:
            raise ValueError('DELTA_TOO_MANY_STARTERS')
        states[tid] = {'players': players, 'formation': block.get('formation'),
                       'coach_id': (block.get('coach') or {}).get('id')}
    return states


def compare_lineups(before, after, team_ids):
    old, new = lineup_state(before, team_ids), lineup_state(after, team_ids)
    changes = []
    for tid in team_ids:
        a, b = old.get(tid, {}), new.get(tid, {})
        if a != b:
            changes.append({'team_id': tid, 'removed': sorted(set(a.get('players', {})) - set(b.get('players', {}))),
                            'added': sorted(set(b.get('players', {})) - set(a.get('players', {}))),
                            'roles_changed': sorted(pid for pid in set(a.get('players', {})) & set(b.get('players', {}))
                                                    if a['players'][pid] != b['players'][pid]),
                            'formation_changed': a.get('formation') != b.get('formation'),
                            'coach_changed': a.get('coach_id') != b.get('coach_id')})
    return {'changes': changes, 'changed': bool(changes),
            'status': 'API_STRUCTURED_NOT_OFFICIAL_CERTIFICATION',
            'affected_models': ['player_identity_and_exposure', 'target_tactical_interpretation'] if changes else [],
            'unchanged_models': ['frozen_team_historical_statistics', 'frozen_predictive_baselines'],
            'financial_advantage': 'NOT_DEMONSTRATED'}


def replace_lineup_block(text, old, new):
    h = re.search(r'^LINEUPS DEL FIXTURE OBJETIVO\s*\n=+\s*$', text, re.M)
    if not h:
        raise ValueError('DELTA_TEXT_LINEUP_SECTION_MISSING')
    start = text.find('{', h.end())
    if start < 0:
        raise ValueError('DELTA_TEXT_LINEUP_OBJECT_MISSING')
    obj, length = json.JSONDecoder().raw_decode(text[start:])
    if obj != old:
        raise ValueError('DELTA_JSON_TEXT_CONFLICT')
    return text[:start] + json.dumps(new, ensure_ascii=False, indent=2) + text[start + length:]


def refresh_lineups(ctx, manifest, snapshot, digest, now):
    """Consume a dated local API record; never fetch data or overwrite inputs."""
    if not now < ctx['kickoff']:
        raise ValueError('DELTA_AFTER_KICKOFF')
    source = local_reference(ctx['root'], snapshot, digest)
    record = json.loads(source.read_text())
    payload, acquired = unpack_record(record)
    if not acquired <= now < ctx['kickoff'] or acquired >= ctx['kickoff']:
        raise ValueError('DELTA_ACQUISITION_NOT_PREMATCH')
    if payload.get('get') != 'fixtures/lineups' or str((payload.get('parameters') or {}).get('fixture')) != str(ctx['fixture_id']):
        raise ValueError('DELTA_FIXTURE_IDENTITY')
    if payload.get('errors'):
        raise ValueError('DELTA_API_ERRORS')
    refs = manifest['inputs']
    js = local_reference(ctx['root'], refs['context_json']['path'], refs['context_json']['sha256'])
    txt = local_reference(ctx['root'], refs['context']['path'], refs['context']['sha256'])
    original = json.loads(js.read_text())
    from match_identity import identity_from_payload, validate_identity
    identity = identity_from_payload(original)
    validate_identity(identity, original, ctx['event_id'], ctx['url'])
    if identity['api_fixture_id'] != ctx['fixture_id'] or when(identity['kickoff_utc']) != ctx['kickoff']:
        raise ValueError('DELTA_CONTEXT_IDENTITY')
    team_ids = [identity[s]['api_team_id'] for s in ('home', 'away')]
    if any(type(tid) is not int or tid <= 0 for tid in team_ids):
        raise ValueError('DELTA_CONTEXT_TEAM_IDENTITY')
    old = original['context'].get('lineups') or {'response': []}
    after = payload.get('response')
    if not isinstance(after, list):
        raise ValueError('DELTA_LINEUP_RESPONSE_INVALID')
    state = lineup_state(after, team_ids)
    if set(state) != set(team_ids) or any(len(s['players']) != 11 for s in state.values()):
        raise ValueError('DELTA_REQUIRES_BOTH_COMPLETE_XI')
    previous = original.get('differential_refresh', {}).get('acquired_at_utc') or original.get('generated_at')
    if not previous or acquired <= when(previous):
        raise ValueError('DELTA_NOT_NEWER_THAN_CONTEXT')
    delta = compare_lineups(old.get('response', []), after, team_ids)
    updated = copy.deepcopy(original)
    updated['context']['lineups'] = payload
    # Keep generated_at: the historical context was generated then, not now.
    updated['differential_refresh'] = {'endpoint': 'fixtures/lineups', 'acquired_at_utc': acquired.isoformat(),
                                      'source_sha256': digest, 'parent_context_sha256': refs['context_json']['sha256'],
                                      'delta': delta}
    text = replace_lineup_block(txt.read_text(), old, payload)
    run_id = payload_sha({'parent': refs['context_json']['sha256'], 'snapshot': digest})[:24]
    out = ctx['directory'] / 'context_updates' / run_id
    if out.exists():
        raise ValueError('DELTA_ALREADY_EXISTS_USE_PINNED_RESULT')
    out.mkdir(parents=True)
    eid = ctx['event_id']
    files = {'context_json': (f'match_engine_{eid}.json', updated),
             'context': (f'match_engine_{eid}.txt', text),
             'identity': ('match_identity.json', identity),
             'lineup_delta': ('lineup_delta.json', delta)}
    result = copy.deepcopy(manifest)
    for key, (name, value) in files.items():
        p = out / name
        p.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
        result['inputs'][key] = {'path': str(p.resolve()), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    (out / 'parent_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (out / 'acquisition_record.json').write_bytes(source.read_bytes())
    result['status'] = 'PREPARADO'
    for step in ('validacion', 'motor'):
        result.get('steps', {}).pop(step, None)
    result.pop('codex_prompt', None)
    result.pop('validation_state', None)
    result['invalidated_by_context_refresh'] = {'run': str(out.resolve()), 'delta': delta,
        'stages': ['validation', 'probabilistic', 'codex_analysis', 'final_review'], 'at_utc': now.isoformat()}
    return result, out


def evidence_key(spec, context):
    """Only actual evidence_for dependencies; no invented lineup probability factor."""
    dependencies = {'spec': spec, 'histories': context['histories'],
                    'teams': {s: context['target'][s] for s in ('home', 'away')}}
    if spec.get('type') == 'player':
        from motor_probabilistico import resolve_player
        dependencies['resolved_player'] = resolve_player(spec['player_label'], context)
    return payload_sha(dependencies)


class EvidenceCache:
    """Reuse descriptive calculations only; prices, gates and final ranking are rebuilt."""
    def __init__(self, event_id, source=None, digest=None):
        from motor_probabilistico import __file__ as engine_file
        self.code = payload_sha({str(p.name): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (Path(engine_file), Path(__file__))})
        self.event_id = str(event_id)
        self.items = {}
        self.reused = self.recalculated = 0
        if source is not None:
            p = Path(source)
            if not digest or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                raise ValueError('EVIDENCE_CACHE_SHA_MISMATCH')
            doc = json.loads(p.read_text())
            if doc.get('event_id') != self.event_id:
                raise ValueError('EVIDENCE_CACHE_EVENT_MISMATCH')
            if doc.get('code_sha256') == self.code:
                self.items = doc['items']

    def evaluate(self, spec, context, evaluator):
        key = evidence_key(spec, context)
        if key in self.items:
            self.reused += 1
            return copy.deepcopy(self.items[key])
        value = evaluator(spec, context)
        self.items[key] = copy.deepcopy(value)
        self.recalculated += 1
        return value

    def document(self):
        return {'event_id': self.event_id, 'code_sha256': self.code, 'items': self.items,
                'reused': self.reused, 'recalculated': self.recalculated,
                'price_evaluation': 'ALWAYS_REBUILT_FROM_CURRENT_SNAPSHOT'}
