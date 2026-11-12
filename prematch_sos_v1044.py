#!/usr/bin/env python3
"""PREMATCH v1.0.4.4 — strength-of-schedule, strictly as-of offline evidence.

Offline, read-only except own two report files. Not a betting model or calibrated
league rating. Defaults to a biased local pool reconstructed from the two teams'
recorded fixtures. A larger historical reference pool can be added explicitly.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = 'prematch-sos-1.0.4.4'
FINISHED = {'FT', 'AET', 'PEN'}
METRICS = {
    'goals_for': ('own', 'goals', 'conceded', 'goals'),
    'goals_against': ('conceded', 'goals', 'own', 'goals'),
    'shots_for': ('own', 'shots', 'conceded', 'shots'),
    'on_target_for': ('own', 'on_target', 'conceded', 'on_target'),
    'corners_for': ('own', 'corners', 'conceded', 'corners'),
    'xg_for': ('own', 'xg', 'conceded', 'xg'),
    'shots_against': ('conceded', 'shots', 'own', 'shots'),
    'on_target_against': ('conceded', 'on_target', 'own', 'on_target'),
    'corners_against': ('conceded', 'corners', 'own', 'corners'),
    'xg_against': ('conceded', 'xg', 'own', 'xg'),
}
WINDOWS = (5, 10, 20)
MIN_PRIOR_OPPONENT = 3
MIN_PRIOR_OPPONENT_SAME_VENUE = 2
MIN_BASELINE_FIXTURES = 12
MIN_BASELINE_TEAMS = 8
MIN_WINDOW_ADJUSTED_N = 3
SHRINK_K = 5.0
FACTOR_RANGE = (0.75, 1.25)


def parse_time(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else None


def number(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
        return None
    return float(x)


def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def fmt(value: Any) -> str:
    return 'N/D' if value is None else f'{value:.3f}' if isinstance(value, (int, float)) else str(value)


def _id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def read_json(path: Path) -> dict:
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict):
        raise ValueError(f'JSON raíz no es objeto: {path}')
    return obj


def file_hash(path: Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def normalize_record(raw: dict) -> dict:
    """Canonical single fixture from performance; rejects ambiguous venue/IDs."""
    tid = _id(raw.get('_tracked_team_id'))
    opponent = raw.get('opponent') or {}
    oid = _id(opponent.get('id'))
    fid = _id(raw.get('fixture_id'))
    lid = _id(raw.get('league_id'))
    season = raw.get('season')
    dt = parse_time(raw.get('kickoff_utc'))
    venue = raw.get('venue')
    if not (tid and oid and fid and lid and dt and isinstance(season, int)
            and venue in ('HOME', 'AWAY') and tid != oid):
        raise ValueError(f'Identidad/fecha incompleta en fixture {fid}')
    if raw.get('status') not in FINISHED:
        raise ValueError(f'Fixture histórico no finalizado: {fid}')
    own, conceded = dict(raw.get('own') or {}), dict(raw.get('conceded') or {})
    own['goals'] = raw.get('goals_for')
    conceded['goals'] = raw.get('goals_against')
    if venue == 'HOME':
        home, away = {'id': tid, 'stats': own}, {'id': oid, 'stats': conceded}
    else:
        home, away = {'id': oid, 'stats': conceded}, {'id': tid, 'stats': own}
    return {'fixture_id': fid, 'kickoff_utc': dt.isoformat(), 'league_id': lid,
            'season': season, 'home': home, 'away': away, 'status': raw['status']}


def build_pool(performance: dict, *, cutoff: datetime, target_fixture: int) -> list[dict]:
    pool = {}
    for side in ('home', 'away'):
        data = (performance.get('teams') or {}).get(side) or {}
        tid = _id(data.get('team_id'))
        if tid is None:
            raise ValueError(f'Sin ID de equipo: {side}')
        for row in data.get('historical_fixtures') or []:
            rec = normalize_record({**row, '_tracked_team_id': tid})
            d = parse_time(rec['kickoff_utc'])
            if rec['fixture_id'] == target_fixture or d + timedelta(hours=3) >= cutoff:
                raise ValueError(f'Violación del corte temporal o fixture objetivo: {rec["fixture_id"]}')
            fid = rec['fixture_id']
            if fid in pool:
                prev = pool[fid]
                if (prev['home']['id'], prev['away']['id'], prev['kickoff_utc'], prev['league_id']) != (
                        rec['home']['id'], rec['away']['id'], rec['kickoff_utc'], rec['league_id']):
                    raise ValueError(f'Conflicto al deduplicar fixture {fid}')
                # Preserve the first record; the shared IDs never count as two fixtures.
                continue
            pool[fid] = rec
    return sorted(pool.values(), key=lambda r: (r['kickoff_utc'], r['fixture_id']))


def validate_extra_pool(obj: dict, *, cutoff: datetime, target_fixture: int) -> list[dict]:
    if obj.get('schema') != 'prematch-sos-reference-pool-1.0':
        raise ValueError('Schema de reference-pool no soportado')
    result = []
    seen = set()
    for g in obj.get('games') or []:
        fid, dt = _id(g.get('fixture_id')), parse_time(g.get('kickoff_utc'))
        home, away = g.get('home') or {}, g.get('away') or {}
        if not (fid and dt and _id(home.get('id')) and _id(away.get('id'))
                and _id(home.get('id')) != _id(away.get('id'))
                and _id(g.get('league_id')) and isinstance(g.get('season'), int)
                and isinstance(home.get('stats'), dict) and isinstance(away.get('stats'), dict)
                and g.get('status') in FINISHED):
            raise ValueError(f'Fixture externo incompleto: {fid}')
        if fid == target_fixture or dt + timedelta(hours=3) >= cutoff:
            raise ValueError(f'Fixture externo objetivo/post-cutoff: {fid}')
        if fid in seen:
            raise ValueError(f'Fixture externo duplicado: {fid}')
        seen.add(fid)
        result.append({**g, 'kickoff_utc': dt.isoformat()})
    return result


def team_side(game: dict, team_id: int) -> tuple[dict, dict, str] | None:
    if game['home']['id'] == team_id:
        return game['home'], game['away'], 'HOME'
    if game['away']['id'] == team_id:
        return game['away'], game['home'], 'AWAY'
    return None


def prior_reference(pool: list[dict], row: dict, opponent_id: int,
                    reference_section: str, metric: str, opponent_venue: str) -> dict:
    """Opponent profile and pool baseline: every reference must finish BEFORE match."""
    kickoff = parse_time(row['kickoff_utc'])
    candidate_games = [g for g in pool if
                       g['league_id'] == row['league_id'] and g['season'] == row['season'] and
                       parse_time(g['kickoff_utc']) + timedelta(hours=3) < kickoff]
    baseline_values = []
    baseline_fixtures = set()
    baseline_teams = set()
    opponent_values = []
    opponent_venue_values = []
    opponent_fixtures = []
    for g in candidate_games:
        # Compare opponent strength against teams in the SAME venue role:
        # an away opponent's concessions vs other away defenses, not a mixed league mean.
        role = opponent_venue.lower()
        s = g[role]
        matched = (g['home'] if role == 'away' else g['away']) if reference_section == 'conceded' else s
        baseline_val = number((matched.get('stats') or {}).get(metric))
        if baseline_val is not None:
            baseline_values.append(baseline_val)
            baseline_fixtures.add(g['fixture_id'])
            baseline_teams.add(s['id'])
        op = team_side(g, opponent_id)
        if op is None:
            continue
        opp, other, venue = op
        stat = opp if reference_section == 'own' else other
        val = number((stat.get('stats') or {}).get(metric))
        if val is None:
            continue
        opponent_values.append(val)
        opponent_fixtures.append(g['fixture_id'])
        if venue == opponent_venue:
            opponent_venue_values.append(val)
    # Reject if too few confirmed previous fixtures; never use the target game itself.
    reasons = []
    if len(opponent_values) < MIN_PRIOR_OPPONENT:
        reasons.append('OPPONENT_PRIOR_N_LT_3')
    if len(opponent_venue_values) < MIN_PRIOR_OPPONENT_SAME_VENUE:
        reasons.append('OPPONENT_SAME_VENUE_N_LT_2')
    if len(baseline_fixtures) < MIN_BASELINE_FIXTURES or len(baseline_teams) < MIN_BASELINE_TEAMS:
        reasons.append('REFERENCE_POOL_THIN')
    baseline = avg(baseline_values)
    opponent_avg = avg(opponent_venue_values)
    if baseline is None or baseline <= 0 or opponent_avg is None:
        reasons.append('BASELINE_OR_OPPONENT_MEAN_UNAVAILABLE')
    factor = None
    if not reasons:
        # Shrink small-sample strength toward no adjustment; cap descriptive adjustment.
        raw_ratio = opponent_avg / baseline
        weight = len(opponent_venue_values) / (len(opponent_venue_values) + SHRINK_K)
        factor = max(FACTOR_RANGE[0], min(FACTOR_RANGE[1], 1 + weight * (raw_ratio - 1)))
    return {
        'opponent_prior_n': len(opponent_values),
        'opponent_same_venue_n': len(opponent_venue_values),
        'baseline_metric_observed_n': len(baseline_values),
        'baseline_fixture_n': len(baseline_fixtures),
        'baseline_distinct_teams_n': len(baseline_teams),
        'opponent_prior_fixture_ids': opponent_fixtures,
        'baseline_mean': baseline,
        'opponent_same_venue_mean': opponent_avg,
        'factor': factor,
        'reasons': reasons,
    }


def analyze(performance: dict, *, identity: dict | None = None, quality: dict | None = None,
            extra_pool: dict | None = None, source_hashes: dict | None = None) -> dict:
    event = str(performance.get('event_id_bwin', ''))
    target = _id(performance.get('target_fixture_id'))
    kickoff = parse_time(performance.get('target_kickoff_utc'))
    cutoff = parse_time(performance.get('analysis_cutoff_utc'))
    if not (event.isdecimal() and target and cutoff and kickoff and cutoff < kickoff):
        raise ValueError('Identidad o corte PREMATCH inválidos')
    if identity:
        if (str(identity.get('bwin_event_id')) != event or
                identity.get('api_fixture_id') != target or
                parse_time(identity.get('kickoff_utc')) != kickoff):
            raise ValueError('La identidad no coincide con el rendimiento')
    if quality:
        if (str(quality.get('event_id_bwin')) != event or
                quality.get('target_fixture_id') != target):
            raise ValueError('Auditoría de calidad de otro evento/fixture')
        if quality.get('quality_status') == 'BLOCKED' or any(
                i.get('severity') == 'CRITICAL' for i in (quality.get('issues') or [])):
            raise ValueError('QUALITY_AUDIT_BLOCKED: no ajustar estadísticas')
    pool = build_pool(performance, cutoff=cutoff, target_fixture=target)
    local_count = len(pool)
    if extra_pool:
        if ('target_event_id_bwin' in extra_pool and
                str(extra_pool['target_event_id_bwin']) != event):
            raise ValueError('Pool de referencias pertenece a otro evento')
        if ('target_cutoff_utc' in extra_pool and
                parse_time(extra_pool['target_cutoff_utc']) != cutoff):
            raise ValueError('Pool de referencias pertenece a otro cutoff')
        extra = validate_extra_pool(extra_pool, cutoff=cutoff, target_fixture=target)
        by_id = {r['fixture_id']: r for r in pool}
        for rec in extra:
            if rec['fixture_id'] in by_id:
                original = by_id[rec['fixture_id']]
                if (original['home']['id'], original['away']['id'], original['kickoff_utc']) != (
                        rec['home']['id'], rec['away']['id'], rec['kickoff_utc']):
                    raise ValueError(f'Conflicto de fixture entre pools: {rec["fixture_id"]}')
                continue
            by_id[rec['fixture_id']] = rec
        pool = sorted(by_id.values(), key=lambda r: (r['kickoff_utc'], r['fixture_id']))
    result = {'schema': SCHEMA, 'event_id_bwin': event, 'target_fixture_id': target,
              'analysis_cutoff_utc': cutoff.isoformat(), 'target_kickoff_utc': kickoff.isoformat(),
              'identity_status': (identity or {}).get('identity_status', 'NOT_SUPPLIED'),
              'quality_status': (quality or {}).get('quality_status', 'NOT_SUPPLIED'),
              'reference': {'mode': 'PARTIAL_OBSERVED_MATCH_POOL',
                            'unique_fixtures': len(pool), 'original_team_fixture_pool': local_count,
                            'extra_reference_fixtures': len(pool)-local_count,
                            'bias': 'MUESTRA_DE_PARTIDOS_DE_DOS_EQUIPOS; NO_CENSO_DE_LIGA',
                            'as_of_rule': 'fixture_referencia_kickoff_utc + 3h < fixture_historico_kickoff_utc',
                            'venue_specific_prior_required': True,
                            'venue_matched_baseline_required': True},
              'parameters': {'min_prior_opponent': MIN_PRIOR_OPPONENT,
                             'min_prior_same_venue': MIN_PRIOR_OPPONENT_SAME_VENUE,
                             'min_baseline_fixtures': MIN_BASELINE_FIXTURES,
                             'min_baseline_distinct_teams': MIN_BASELINE_TEAMS,
                             'min_window_adjusted_n': MIN_WINDOW_ADJUSTED_N,
                             'shrinkage_k': SHRINK_K, 'factor_clip': list(FACTOR_RANGE)},
              'teams': {}, 'warnings': [
                  'Referencia parcial: muestra condicionada a los históricos de los dos equipos, no una escala completa de liga',
                  'Ajustes solo descriptivos; no hay calibración, EV, probabilidades ni autorización de apuestas',
                  'La fecha de cada estadística histórica depende de los metadatos guardados; no se atesta independientemente la captura original',
              ], 'odds_enabled': False, 'source_sha256': source_hashes or {}}
    for side in ('home', 'away'):
        team = (performance.get('teams') or {}).get(side) or {}
        team_id = _id(team.get('team_id'))
        if not team_id:
            raise ValueError(f'Equipo sin ID en {side}')
        rows = sorted(team.get('historical_fixtures') or [], key=lambda r: r['kickoff_utc'], reverse=True)
        metric_details = {}
        windows = {}
        for metric, (own_side, own_key, reference_side, ref_key) in METRICS.items():
            evidence = []
            for row in rows:
                d = parse_time(row.get('kickoff_utc'))
                if not d or d + timedelta(hours=3) >= cutoff or row.get('fixture_id') == target:
                    raise ValueError(f'Fixture filtrado posterior al corte: {row.get("fixture_id")}')
                val = (number(row.get('goals_for' if metric == 'goals_for' else 'goals_against'))
                       if metric in ('goals_for', 'goals_against')
                       else number((row.get(own_side) or {}).get(own_key)))
                if val is None:
                    evidence.append({'fixture_id': row['fixture_id'], 'value': None,
                                     'adjusted': None, 'reason': 'METRIC_MISSING'})
                    continue
                # Opponent ID and match venue, from the perspective of the opponent.
                oid = _id((row.get('opponent') or {}).get('id'))
                opp_venue = 'AWAY' if row.get('venue') == 'HOME' else 'HOME'
                if not oid or row.get('venue') not in ('HOME', 'AWAY'):
                    raise ValueError(f'Rival o localía inválida en fixture {row.get("fixture_id")}')
                evidence_ref = prior_reference(pool, row, oid, reference_side, ref_key, opp_venue)
                factor = evidence_ref['factor']
                eligible = factor is not None
                evidence.append({'fixture_id': row['fixture_id'], 'kickoff_utc': row['kickoff_utc'],
                                 'opponent_id': oid, 'venue': row['venue'],
                                 'league_id': row['league_id'], 'season': row['season'],
                                 'value': val, 'adjusted': round(val / factor, 5) if eligible else None,
                                 'reference_factor': round(factor, 5) if eligible else None,
                                 'reference_evidence': evidence_ref,
                                 'reason': 'DESCRIPTIVE_ADJUSTMENT' if eligible else '|'.join(evidence_ref['reasons'])})
            metric_details[metric] = evidence
            windows[metric] = {}
            for window in WINDOWS:
                selected = evidence[:window]
                raw = [v['value'] for v in selected if v['value'] is not None]
                adjusted_pairs = [v for v in selected if v['adjusted'] is not None]
                status = ('INCOMPLETE_WINDOW' if len(selected) < window
                          else 'DESCRIPTIVE_PARTIAL_REFERENCE' if len(adjusted_pairs) >= MIN_WINDOW_ADJUSTED_N
                          else 'NOT_ESTIMABLE_NO_AS_OF_REFERENCE')
                windows[metric][f'L{window}'] = {
                    'matches_n': len(selected), 'window_required_n': window,
                    'window_complete': len(selected) == window,
                    'raw_metric_n': len(raw),
                    'raw_mean': round(avg(raw), 5) if raw else None,
                    'adjusted_metric_n': len(adjusted_pairs),
                    'raw_matched_mean': round(avg([x['value'] for x in adjusted_pairs]),5) if adjusted_pairs else None,
                    'adjusted_descriptive_mean': (
                        round(avg([x['adjusted'] for x in adjusted_pairs]),5)
                        if status == 'DESCRIPTIVE_PARTIAL_REFERENCE' else None),
                    'status': status,
                }
        gaps: dict[tuple[int, int, int, str], dict] = {}
        for observation in metric_details['shots_for']:
            if observation.get('adjusted') is not None or observation.get('opponent_id') is None:
                continue
            key = (observation['opponent_id'], observation['league_id'],
                   observation['season'], observation['venue'])
            if key not in gaps:
                gaps[key] = {'opponent_id': observation['opponent_id'],
                             'league_id': observation['league_id'],
                             'season': observation['season'],
                             'team_venue': observation['venue'],
                             'affected_fixture_ids': [], 'reasons': []}
            gaps[key]['affected_fixture_ids'].append(observation['fixture_id'])
            gaps[key]['reasons'].append(observation['reason'])
        missing = sorted(gaps.values(), key=lambda x: (-len(x['affected_fixture_ids']), x['opponent_id']))
        result['teams'][side] = {'team_id': team_id, 'team_name': team.get('team_name'),
                                 'observed_n': len(rows), 'windows': windows,
                                 'reference_gaps_shots_for': missing,
                                 'evidence': metric_details}
    return result


def markdown(report: dict) -> str:
    lines = [f'# PREMATCH Fase 2.2 — Strength of Schedule — Bwin {report["event_id_bwin"]}', '',
             f'- Fixture API: `{report["target_fixture_id"]}`',
             f'- Corte: `{report["analysis_cutoff_utc"]}`',
             f'- Identidad: `{report["identity_status"]}`',
             f'- Calidad: `{report["quality_status"]}`',
             f'- Referencia: {report["reference"]["unique_fixtures"]} fixtures observados, '
             'muestra **no representativa de toda la liga**.', '',
             '> **No es rating de liga calibrado ni pronóstico.** Solo utiliza referencias históricas '
             'conocidas antes de cada encuentro. `N/D` significa que no hay referencia as-of suficiente.', '']
    for side in ('home', 'away'):
        team = report['teams'][side]
        lines += [f'## {team["team_name"]} ({side})', '',
                  '| Métrica | Ventana | Media bruta (n) | Ajuste descriptivo (n) | Estado |',
                  '|---|---|---:|---:|---|']
        for metric, by_window in team['windows'].items():
            for window, item in by_window.items():
                raw = f'{fmt(item["raw_mean"])} ({item["raw_metric_n"]}/{item["matches_n"]})'
                adj = f'{fmt(item["adjusted_descriptive_mean"])} ({item["adjusted_metric_n"]}/{item["matches_n"]})'
                lines.append(f'| {metric} | {window} | {raw} | {adj} | {item["status"]} |')
        lines += ['', 'Los ajustes, cuando existen, usan solo la **submuestra emparejada**; '
                  'no comparar directamente con la media bruta de toda la ventana.',
                  f'**Rivales con referencias insuficientes (remates):** {len(team["reference_gaps_shots_for"])} perfiles competición/temporada/localía.', '']
        notable = []
        for metric in ('shots_for', 'xg_for', 'corners_for'):
            for row in team['evidence'][metric]:
                if row.get('reason') == 'DESCRIPTIVE_ADJUSTMENT':
                    notable.append(f'- {metric} fixture `{row["fixture_id"]}`: '
                                   f'factor `{row["reference_factor"]}`, '
                                   f'n previo del rival `{row["reference_evidence"]["opponent_prior_n"]}`')
        if notable:
            lines += ['**Evidencias elegibles (solo descriptivas):**', ''] + notable[:20] + ['']
    lines += ['## Restricciones', ''] + ['- '+w for w in report['warnings']]
    lines += ['', 'El informe no utiliza cuotas ni habilita `finish`.', '']
    return '\n'.join(lines)


def run(event_id: str, root: Path, extra_path: Path | None = None) -> tuple[Path, Path, dict]:
    folder = root / 'outputs' / 'matches' / event_id / 'analysis' / 'deep_v1042'
    perf_path = folder / f'rendimiento_profundo_{event_id}.json'
    identity_path = root / 'outputs' / 'matches' / event_id / 'inputs' / 'match_identity.json'
    quality_path = folder / f'auditoria_calidad_{event_id}.json'
    if not perf_path.is_file():
        raise FileNotFoundError(f'No existe rendimiento profundo: {perf_path}')
    if not quality_path.is_file():
        raise FileNotFoundError(f'Primero ejecutar auditoría V1.0.4.3: {quality_path}')
    if not identity_path.is_file():
        raise FileNotFoundError(f'Identidad no encontrada: {identity_path}')
    inputs = {'performance': file_hash(perf_path), 'identity': file_hash(identity_path),
              'quality': file_hash(quality_path)}
    external = read_json(extra_path) if extra_path else None
    if extra_path:
        inputs['reference_pool'] = file_hash(extra_path)
    result = analyze(read_json(perf_path), identity=read_json(identity_path),
                     quality=read_json(quality_path), extra_pool=external, source_hashes=inputs)
    jpath = folder / f'ajuste_rivales_{event_id}.json'
    mpath = folder / f'ajuste_rivales_{event_id}.md'
    # Write only the new outputs, never original snapshots.
    jpath.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    mpath.write_text(markdown(result), encoding='utf-8')
    return jpath, mpath, result


def main():
    ap = argparse.ArgumentParser(description='Fase 2.2 — Ajuste descriptivo as-of. Sin llamadas API.')
    ap.add_argument('--event-id', required=True)
    ap.add_argument('--root', type=Path, default=Path('.'))
    ap.add_argument('--reference-pool', type=Path, help='Pool adicional versionado; no datos futuros')
    args = ap.parse_args()
    jpath, mpath, res = run(args.event_id, args.root, args.reference_pool)
    print(f'Fase 2.2 · Bwin {args.event_id} · referencia {res["reference"]["unique_fixtures"]} partidos')
    for side in ('home', 'away'):
        t = res['teams'][side]
        s = t['windows']['shots_for']['L20']
        print(f'{t["team_name"]}: L20 tiros {s["raw_metric_n"]}/{s["matches_n"]} '
              f'| ajustables {s["adjusted_metric_n"]}/{s["matches_n"]} '
              f'| {s["status"]}')
    print(jpath)
    print(mpath)

if __name__ == '__main__':
    main()
