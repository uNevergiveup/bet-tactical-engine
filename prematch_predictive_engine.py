#!/usr/bin/env python3
"""PREMATCH Predictive Engine V1.0 MVP.

Offline-first sports modelling over a frozen preselection snapshot. It never
reads Bwin files, creates bets, estimates EV, or changes the stable selector.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pvariance
from zoneinfo import ZoneInfo

import prematch_pattern_discovery as discovery

VERSION = '1.0-mvp'
MADRID = ZoneInfo('Europe/Madrid')
FAMILIES = {
    'goals': 'goals', 'corners': 'corners', 'shots': 'shots',
    'on_target': 'shots_on_target', 'fouls': 'fouls',
    'yellow': 'yellow_cards', 'offsides': 'offsides',
}
PERIODS = ('FULL_TIME', 'FIRST_HALF', 'SECOND_HALF')
STATES = {'RULE_BASED', 'EXPLORATORY_UNVALIDATED', 'TEMPORALLY_VALIDATED',
          'INSUFFICIENT_EVIDENCE', 'CONTRADICTORY'}


class PredictiveError(RuntimeError):
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise PredictiveError(f'JSON raíz inválida: {path}')
    return value


def dump(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               sort_keys=True) + '\n', encoding='utf-8')


def dt(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def safe_mean(values):
    values = [float(x) for x in values if finite(x)]
    return mean(values) if values else None


def poisson_pmf(k: int, rate: float) -> float:
    if k < 0 or rate < 0:
        return 0.0
    if rate == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(rate) - rate - math.lgamma(k + 1))


def negative_binomial_pmf(k: int, rate: float, dispersion: float) -> float:
    if k < 0 or rate < 0 or dispersion <= 0:
        return 0.0
    if rate == 0:
        return 1.0 if k == 0 else 0.0
    p = dispersion / (dispersion + rate)
    return math.exp(math.lgamma(k + dispersion) - math.lgamma(dispersion)
                    - math.lgamma(k + 1) + dispersion * math.log(p)
                    + k * math.log1p(-p))


def distribution(rate: float, model: str, dispersion=None, support=80):
    fn = (lambda k: negative_binomial_pmf(k, rate, dispersion)) if model == 'NEGATIVE_BINOMIAL' else (lambda k: poisson_pmf(k, rate))
    values = [fn(k) for k in range(support + 1)]
    total = sum(values)
    if total <= 0:
        return []
    return [round(x / total, 10) for x in values]


def choose_count_model(values):
    """Use NB only with a stable, identified overdispersion estimate."""
    values = [float(v) for v in values if finite(v)]
    avg = safe_mean(values)
    var = pvariance(values) if len(values) > 1 else None
    if avg is None:
        return {'name': None, 'dispersion': None, 'reason': 'NO_VALID_COUNTS'}
    if len(values) < 12:
        return {'name': 'POISSON', 'dispersion': None, 'reason': 'NB_REQUIRES_AT_LEAST_12_VALUES'}
    if var is None or var <= avg * 1.20:
        return {'name': 'POISSON', 'dispersion': None, 'reason': 'NO_STABLE_OVERDISPERSION'}
    k = avg * avg / (var - avg) if var > avg else None
    if k is None or not 1.0 <= k <= 100.0:
        return {'name': 'POISSON', 'dispersion': None, 'reason': 'NB_DISPERSION_UNSTABLE'}
    return {'name': 'NEGATIVE_BINOMIAL', 'dispersion': round(k, 6),
            'reason': 'OVERDISPERSION_ESTIMATED_FROM_AT_LEAST_12_VALUES'}


def quantile(probs, q):
    total = 0.0
    for i, p in enumerate(probs):
        total += p
        if total >= q:
            return i
    return len(probs) - 1 if probs else None


def probability_over(probs, threshold):
    return sum(p for k, p in enumerate(probs) if k > threshold)


def observed_values(rows, block, metric, venue=None):
    output = []
    for row in rows:
        if venue and row.get('venue') != venue:
            continue
        value = (row.get(block) or {}).get(metric)
        if finite(value):
            output.append((dt(row.get('kickoff_utc')), float(value), row.get('fixture_id')))
    return [(at, value, fid) for at, value, fid in output if at]


def shrink(sample, reference, strength=6.0):
    if not sample:
        return reference
    avg = mean(sample)
    if reference is None:
        return avg
    return (len(sample) * avg + strength * reference) / (len(sample) + strength)


def logloss(y, p):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def walk_forward(points, min_train=6):
    """Expanding-window validation; every forecast precedes its target row."""
    points = sorted({(at, fid): value for at, value, fid in points}.items(), key=lambda x: (x[0][0], str(x[0][1])))
    values = [(key[0], value, key[1]) for key, value in points]
    predictions = []
    # Los fixtures con el mismo kickoff pertenecen al mismo bloque temporal.
    # Ningun resultado del bloque puede entrar como entrenamiento de otro.
    for target_at, actual, target_id in values:
        train = [value for at, value, _ in values if at < target_at]
        if len(train) < min_train:
            continue
        last_training_at = max(at for at, _, _ in values if at < target_at)
        baseline_rate = mean(train)
        recent = train[-5:]
        model_rate = shrink(recent, baseline_rate, 5.0)
        threshold = math.floor(baseline_rate)
        baseline_probs = distribution(baseline_rate, 'POISSON')
        choice = choose_count_model(train)
        model_probs = distribution(model_rate, choice['name'], choice['dispersion'])
        y = int(actual > threshold)
        predictions.append({
            'target_fixture_id': target_id, 'target_kickoff': target_at.isoformat(),
            'training_last_kickoff': last_training_at.isoformat(),
            'training_n': len(train), 'actual': actual, 'event_threshold': threshold,
            'baseline_rate': round(baseline_rate, 6), 'model_rate': round(model_rate, 6),
            'baseline_probability': round(probability_over(baseline_probs, threshold), 8),
            'model_probability': round(probability_over(model_probs, threshold), 8),
            'baseline_abs_error': abs(actual - baseline_rate),
            'model_abs_error': abs(actual - model_rate),
            # Evitar diferencias de ~1e-15 entre implementaciones de libm
            # al serializar resultados que deben ser reproducibles por SHA.
            'baseline_log_score': round(-math.log(max(poisson_pmf(int(actual), baseline_rate), 1e-12)), 12),
            'model_log_score': round(-math.log(max((negative_binomial_pmf(int(actual), model_rate, choice['dispersion'])
                                              if choice['name'] == 'NEGATIVE_BINOMIAL'
                                              else poisson_pmf(int(actual), model_rate)), 1e-12)), 12),
            'y': y,
        })
    if not predictions:
        return {'status': 'NOT_ENOUGH_TEMPORAL_EVIDENCE', 'valid_predictions': 0,
                'minimum_training_n': min_train, 'predictions': []}
    def avg(key):
        return mean(p[key] for p in predictions)
    brier_base = mean((p['baseline_probability'] - p['y']) ** 2 for p in predictions)
    brier_model = mean((p['model_probability'] - p['y']) ** 2 for p in predictions)
    ll_base = mean(logloss(p['y'], p['baseline_probability']) for p in predictions)
    ll_model = mean(logloss(p['y'], p['model_probability']) for p in predictions)
    enough = len(predictions) >= 6
    improved = enough and brier_model < brier_base and ll_model < ll_base and avg('model_abs_error') <= avg('baseline_abs_error')
    return {
        'status': 'PRELIMINARY_WALK_FORWARD_IMPROVEMENT' if improved else ('PRELIMINARY_NO_IMPROVEMENT' if enough else 'NOT_ENOUGH_TEMPORAL_EVIDENCE'),
        'valid_predictions': len(predictions), 'minimum_training_n': min_train,
        'brier_model': round(brier_model, 8), 'brier_baseline': round(brier_base, 8),
        'log_loss_model': round(ll_model, 8), 'log_loss_baseline': round(ll_base, 8),
        'mae_model': round(avg('model_abs_error'), 8), 'mae_baseline': round(avg('baseline_abs_error'), 8),
        'distribution_log_score_model': round(avg('model_log_score'), 8),
        'distribution_log_score_baseline': round(avg('baseline_log_score'), 8),
        'improvement_vs_baseline': improved,
        'validation_scope': 'UNIVARIATE_TEAM_HISTORY_NOT_MATCHUP_MODEL',
        'predictions': predictions,
    }


def validate_source(pre, asof):
    if pre.get('schema_version') not in ('1.2-preselection', '1.3-preselection'):
        raise PredictiveError('PRESELECTION_SCHEMA_UNSUPPORTED')
    generated = dt(pre.get('generated_at_utc'))
    if not generated or generated > asof:
        raise PredictiveError('PRESELECTION_UNDATED_OR_AFTER_ASOF')
    seen = set()
    for fixture in pre.get('matches', []):
        fid = fixture.get('fixture_id')
        if fid in seen:
            raise PredictiveError(f'DUPLICATE_FIXTURE_ID:{fid}')
        seen.add(fid)
        kickoff = dt(fixture.get('kickoff_madrid'))
        cutoff = dt(fixture.get('analysis_cutoff_utc'))
        if not kickoff or not cutoff or cutoff > asof or cutoff >= kickoff:
            raise PredictiveError(f'INVALID_TEMPORAL_IDENTITY:{fid}')
        if asof >= kickoff:
            raise PredictiveError(f'OPERATIONAL_ASOF_NOT_PREKICKOFF:{fid}')
        if fixture.get('home_id') == fixture.get('away_id'):
            raise PredictiveError(f'DUPLICATE_TEAM_ID:{fid}')


def league_reference(pre, asof):
    buckets = defaultdict(dict)
    for fixture in pre.get('matches', []):
        for side in ('home', 'away'):
            rows = discovery.clean_observations(fixture, side, asof)
            for metric in FAMILIES:
                for at, value, fid in observed_values(rows, 'own', metric):
                    key = (fixture.get('league_id'), fixture.get('season'), metric, 'ALL')
                    buckets[key][(str(fixture.get(side + '_id')), str(fid))] = value
                venue = 'HOME' if side == 'home' else 'AWAY'
                for at, value, fid in observed_values(rows, 'own', metric, venue):
                    key = (fixture.get('league_id'), fixture.get('season'), metric, venue)
                    buckets[key][(str(fixture.get(side + '_id')), str(fid))] = value
    return {key: {'n': len(vals), 'mean': safe_mean(vals.values())} for key, vals in buckets.items()}


def coverage_rows(pre, pre_sha, asof):
    base = discovery.build_coverage(pre, pre_sha, asof)
    rows = []
    for fixture in pre.get('matches', []):
        for side in ('home', 'away'):
            clean = discovery.clean_observations(fixture, side, asof)
            for metric, family in FAMILIES.items():
                vals = observed_values(clean, 'own', metric)
                requested = sum(metric == 'goals' or discovery.request_state(r) in ('VALID', 'REQUESTED_MISSING') for r in clean)
                rows.append({
                    'fixture_id': fixture['fixture_id'], 'league_id': fixture.get('league_id'),
                    'season': fixture.get('season'), 'team_id': fixture.get(side + '_id'),
                    'subject': side, 'family': family, 'metric': metric, 'period': 'FULL_TIME',
                    'history_available_n': len(clean), 'details_requested_n': requested,
                    'valid_observations_n': len(vals), 'missing_or_not_requested_n': len(clean) - len(vals),
                    'effective_coverage_pct': round(100 * len(vals) / len(clean), 2) if clean else None,
                    'availability_status': 'AVAILABLE' if vals else 'INSUFFICIENT_EVIDENCE',
                    'first_observed_at': min((x[0] for x in vals), default=None).isoformat() if vals else None,
                    'source_fixture_ids': sorted({str(x[2]) for x in vals}),
                })
                for period in ('FIRST_HALF', 'SECOND_HALF'):
                    rows.append({
                        'fixture_id': fixture['fixture_id'], 'league_id': fixture.get('league_id'),
                        'season': fixture.get('season'), 'team_id': fixture.get(side + '_id'),
                        'subject': side, 'family': family, 'metric': metric, 'period': period,
                        'history_available_n': len(clean), 'details_requested_n': 0,
                        'valid_observations_n': 0, 'missing_or_not_requested_n': len(clean),
                        'effective_coverage_pct': None, 'availability_status': 'INSUFFICIENT_EVIDENCE',
                        'reason': 'PERIOD_SPLIT_NOT_PRESENT_IN_FROZEN_SOURCE', 'first_observed_at': None,
                        'source_fixture_ids': [],
                    })
    return {'schema_version': '1.0-predictive-coverage', 'engine_version': VERSION,
            'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
            'observed_provider_matrix': base, 'rows': rows,
            'fixture_evaluation_ledger': pre.get('fixture_evaluation_ledger', []),
            'zero_missing_policy': 'zero is valid; missing and not requested remain distinct',
            'actionable_bet': False}


def fixture_models(pre, asof, references):
    models = []
    validations = []
    for fixture in pre.get('matches', []):
        observations = {side: discovery.clean_observations(fixture, side, asof) for side in ('home', 'away')}
        for side in ('home', 'away'):
            opposite = 'away' if side == 'home' else 'home'
            venue = 'HOME' if side == 'home' else 'AWAY'
            opponent_venue = 'AWAY' if side == 'home' else 'HOME'
            for metric, family in FAMILIES.items():
                own_points = observed_values(observations[side], 'own', metric)
                opp_points = observed_values(observations[opposite], 'conceded', metric)
                own = [x[1] for x in own_points]
                conceded = [x[1] for x in opp_points]
                reference = references.get((fixture.get('league_id'), fixture.get('season'), metric, 'ALL'), {})
                reference_rate = reference.get('mean')
                own_rate = shrink(own, reference_rate)
                conceded_rate = shrink(conceded, reference_rate)
                rate = safe_mean([x for x in (own_rate, conceded_rate) if x is not None])
                own_venue = [x[1] for x in observed_values(observations[side], 'own', metric, venue)]
                opp_venue = [x[1] for x in observed_values(observations[opposite], 'conceded', metric, opponent_venue)]
                venue_rate = safe_mean([safe_mean(own_venue), safe_mean(opp_venue)])
                if rate is not None and venue_rate is not None and min(len(own_venue), len(opp_venue)) >= 3:
                    rate = .75 * rate + .25 * venue_rate
                pool = own + conceded
                chosen = choose_count_model(pool)
                probs = distribution(rate, chosen['name'], chosen['dispersion']) if rate is not None else []
                coverage = min(len(own), len(conceded)) / max(1, min(len(observations[side]), len(observations[opposite])))
                wf = walk_forward(own_points)
                contradictions = []
                if own_rate is not None and conceded_rate is not None and max(own_rate, conceded_rate) > 0:
                    if min(own_rate, conceded_rate) / max(own_rate, conceded_rate) < .4:
                        contradictions.append('TEAM_PRODUCTION_AND_OPPONENT_CONCESSION_DIVERGE')
                if rate is None or min(len(own), len(conceded)) < 4 or coverage < .4:
                    state = 'INSUFFICIENT_EVIDENCE'
                elif contradictions:
                    state = 'CONTRADICTORY'
                # El walk-forward univariante no acredita el modelo completo
                # produccion propia + concesion rival: no promocionar a validado.
                elif min(len(own), len(conceded)) >= 8 and coverage >= .5:
                    state = 'RULE_BASED'
                else:
                    state = 'EXPLORATORY_UNVALIDATED'
                if state not in STATES:
                    raise AssertionError(state)
                uncertainty = None
                if rate is not None and pool:
                    variance = pvariance(pool) if len(pool) > 1 else rate
                    se = math.sqrt(max(variance, 0) / len(pool))
                    uncertainty = {'parameter_rate_interval_approx_95': [round(max(0, rate - 1.96 * se), 6), round(rate + 1.96 * se, 6)],
                                   'outcome_interval_90': [quantile(probs, .05), quantile(probs, .95)],
                                   'coverage_quality': round(coverage, 6),
                                   'historical_stability_cv': round(math.sqrt(variance) / rate, 6) if rate > 0 else None}
                model_id = f"{fixture['fixture_id']}:{side}:{family}:FULL_TIME"
                data_quality = round(100 * min(1, coverage) * min(1, min(len(own), len(conceded)) / 12), 2)
                evidence = round(100 * min(1, min(len(own), len(conceded)) / 20), 2)
                validation_component = 12 if wf['valid_predictions'] else 0
                uncertainty_component = 25 if uncertainty and (uncertainty['historical_stability_cv'] or 99) <= .8 else 10
                priority = round(.35 * data_quality + .25 * evidence + validation_component + uncertainty_component, 2)
                risks = ['NO_CALIBRATED_BETTING_VALUE', 'NO_BWIN_INPUT', 'OPPONENT_STRENGTH_PARTIALLY_CONTROLLED']
                if fixture.get('coach_change') or fixture.get(side + '_coach_change'):
                    risks.append('KNOWN_COACH_REGIME_CHANGE_REQUIRES_SEGMENTATION')
                models.append({
                    'model_id': model_id, 'fixture_id': fixture['fixture_id'], 'home': fixture.get('home'),
                    'away': fixture.get('away'), 'kickoff_madrid': fixture.get('kickoff_madrid'),
                    'league_id': fixture.get('league_id'), 'season': fixture.get('season'),
                    'subject': side, 'subject_team_id': fixture.get(side + '_id'), 'family': family,
                    'metric': metric, 'period': 'FULL_TIME', 'pattern_state': state,
                    'model': chosen, 'predictive_rate': round(rate, 6) if rate is not None else None,
                    'league_reference_rate': round(reference_rate, 6) if reference_rate is not None else None,
                    'attack_rate_shrunk': round(own_rate, 6) if own_rate is not None else None,
                    'opponent_concession_rate_shrunk': round(conceded_rate, 6) if conceded_rate is not None else None,
                    'distribution': {'support': [0, len(probs) - 1], 'probabilities': probs} if probs else None,
                    'uncertainty': uncertainty, 'data_quality_score': data_quality,
                    'statistical_evidence_score': evidence, 'experimental_priority_score': priority,
                    'ranking_label': 'EXPERIMENTAL_RANKING', 'contradictions': contradictions,
                    'risks': risks,
                    'contrastable_explanation': f'Contrastar producción {metric} de {fixture.get(side)} y concesión rival, sin inferir mecanismo táctico.',
                    'additional_data_that_could_change_assessment': ['larger_temporal_sample', 'coach_regime_id', 'comparable_opponent_strength', 'verified_period_splits'],
                    'source_fixture_ids': {'production': sorted(str(x[2]) for x in own_points),
                                           'opponent_concession': sorted(str(x[2]) for x in opp_points)},
                    'actionable_bet': False,
                })
                validations.append({'model_id': model_id, 'fixture_id': fixture['fixture_id'],
                                    'subject': side, 'family': family, 'metric': metric,
                                    'period': 'FULL_TIME', **wf})
    return models, validations


def diversified_global(models, limit=30):
    eligible = [m for m in models if m['pattern_state'] != 'INSUFFICIENT_EVIDENCE']
    by_family = defaultdict(list)
    for row in sorted(eligible, key=lambda x: (-x['experimental_priority_score'], str(x['fixture_id']), x['subject'])):
        by_family[row['family']].append(row)
    result = []
    while len(result) < limit and any(by_family.values()):
        for family in sorted(by_family):
            if by_family[family] and len(result) < limit:
                result.append(by_family[family].pop(0))
    return result


def request_ledger(pre, pre_path, pre_sha):
    entries = pre.get('api_request_ledger') or []
    if entries:
        normalized = entries
        limitation = None
    else:
        normalized = [{
            'request_id': 'legacy-preselection-summary', 'endpoint': 'MULTIPLE_ENDPOINTS_NOT_RECORDED_BY_LEGACY_SOURCE',
            'parameters': None, 'requested_at_utc': pre.get('generated_at_utc'),
            'status': 'LEGACY_AGGREGATE_ONLY', 'network_requests': (pre.get('api_usage') or {}).get('requests_sent'),
            'cache_hits': (pre.get('api_usage') or {}).get('cache_hits'), 'source_path': str(pre_path),
            'source_sha256': pre_sha,
        }]
        limitation = 'The frozen source predates per-request ledger fields; endpoint parameters cannot be reconstructed from cache hashes.'
    return {'schema_version': '1.0-api-request-ledger', 'engine_version': VERSION,
            'source_preselection': str(pre_path), 'source_preselection_sha256': pre_sha,
            'entries': normalized, 'counts': {'entries': len(normalized),
                'network_requests': (pre.get('api_usage') or {}).get('requests_sent', 0),
                'cache_hits': (pre.get('api_usage') or {}).get('cache_hits', 0),
                'errors': (pre.get('counts') or {}).get('api_errors', 0)},
            'limitation': limitation, 'api_key_recorded': False,
            'origin': 'SOURCE_PRESELECTION_LEDGER_NOT_CURRENT_ENGINE_EXECUTION',
            'predictive_engine_network_requests': 0,
            'offline_rehydration': pre.get('offline_rehydration')}


def summary_markdown(coverage, patterns, rankings, validation, ledger):
    family_counts = Counter(r['family'] for r in rankings['items'] if r['pattern_state'] != 'INSUFFICIENT_EVIDENCE')
    state_counts = Counter(r['pattern_state'] for r in rankings['items'])
    lines = ['# PREMATCH Predictive Engine V1.0 MVP', '',
             f"Corte: `{rankings['asof_madrid']}`. Fuente SHA-256: `{rankings['preselection_sha256']}`.", '',
             'Motor deportivo independiente de Bwin. No genera picks, stakes, EV ni probabilidades de rentabilidad.', '',
             '## Alcance', '',
             f"- Partidos en el filtro de origen: {rankings['fixture_stages']['source_filtered']}.",
             f"- Excluidos por inicio anterior o igual al corte: {rankings['fixture_stages']['excluded_temporal']}.",
             f"- Elegibles al corte y evaluados básicamente: {rankings['fixture_stages']['eligible']} / {rankings['fixture_stages']['basic_evaluated']}.",
             f"- Evaluados estadísticamente: {rankings['fixture_stages']['statistically_evaluated']}.",
             f"- Profundizados: {rankings['fixture_stages']['deep_evaluated']}.",
             f"- Estados de modelos: `{json.dumps(dict(sorted(state_counts.items())), ensure_ascii=False)}`.",
             f"- Familias con señal no insuficiente: `{json.dumps(dict(sorted(family_counts.items())), ensure_ascii=False)}`.", '',
             '## Validación', '',
             f"- Series con mejora preliminar univariante: {validation['counts']['preliminary_walk_forward_improvement']}.",
             f"- Series sin profundidad temporal suficiente: {validation['counts']['not_enough_temporal_evidence']}.",
             '- Ninguna serie univariante acredita TEMPORALLY_VALIDATED para el modelo de cruce de equipos.', '',
             '## Ranking global diversificado', '',
             '| # | Fixture | Sujeto | Familia | Estado | Modelo | Prioridad experimental |',
             '|---:|---:|---|---|---|---|---:|']
    for i, row in enumerate(rankings['global_diversified'][:15], 1):
        lines.append(f"| {i} | {row['fixture_id']} | {row['subject']} | {row['family']} | {row['pattern_state']} | {row['model']['name']} | {row['experimental_priority_score']} |")
    lines += ['', '## API y limitaciones', '',
              f"- Solicitudes nuevas declaradas por la fuente: {ledger['counts']['network_requests']}; caché: {ledger['counts']['cache_hits']}; errores: {ledger['counts']['errors']}.",
              '- Los splits de primera y segunda parte quedan INSUFFICIENT_EVIDENCE cuando el snapshot no contiene datos por periodo.',
              '- Entrenador, árbitro y fuerza de rivales solo afectan el modelo cuando la fuente aporta identidad y evidencia fechada; el MVP no los inventa.',
              '- El ranking es EXPERIMENTAL_RANKING y sus puntuaciones no son porcentajes de predictibilidad.', '']
    return '\n'.join(lines)


def run(preselection: Path, output_root: Path, asof=None):
    preselection = Path(preselection).resolve()
    pre = read(preselection)
    asof = asof or dt(pre.get('generated_at_utc'))
    if not asof:
        raise PredictiveError('ASOF_REQUIRED_WITH_TIMEZONE')
    validate_source(pre, asof)
    pre_sha = sha(preselection)
    run_id = f"{asof.astimezone(MADRID).strftime('%Y%m%dT%H%M%S%z')}_{pre_sha[:12]}"
    out = Path(output_root).resolve() / VERSION / run_id
    if out.exists():
        expected = out / 'run_manifest.json'
        if not expected.is_file() or read(expected).get('source_preselection_sha256') != pre_sha:
            raise PredictiveError(f'OUTPUT_RUN_COLLISION:{out}')
    out.mkdir(parents=True, exist_ok=True)
    coverage = coverage_rows(pre, pre_sha, asof)
    descriptive = discovery.build_discovery(pre, pre_sha, asof)
    references = league_reference(pre, asof)
    models, validations = fixture_models(pre, asof, references)
    patterns = {'schema_version': '1.0-predictive-patterns', 'engine_version': VERSION,
                'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
                'descriptive_discovery': descriptive, 'models': models,
                'states': sorted(STATES), 'actionable_bet': False}
    by_family = {family: sorted([m for m in models if m['family'] == family],
                                key=lambda x: (-x['experimental_priority_score'], str(x['fixture_id']), x['subject']))
                 for family in sorted(set(FAMILIES.values()))}
    fixture_ledger=pre.get('fixture_evaluation_ledger',[])
    filtered_count = len(fixture_ledger) if fixture_ledger else (pre.get('counts') or {}).get('input_filter', len(pre.get('matches', [])))
    excluded_temporal = sum(x.get('status') == 'EXCLUDED_TEMPORAL' for x in fixture_ledger)
    eligible_count = filtered_count - excluded_temporal
    stages = {'discovered': (pre.get('counts') or {}).get('input_filter', len(pre.get('matches', []))),
              'source_filtered': filtered_count, 'excluded_temporal': excluded_temporal,
              'eligible': eligible_count, 'basic_evaluated': len(pre.get('matches', [])),
              'statistically_evaluated': sum(any(m['fixture_id'] == f['fixture_id'] and m['predictive_rate'] is not None for m in models) for f in pre.get('matches', [])),
              'deep_evaluated': sum(bool(f.get('details_checked')) for f in pre.get('matches', [])),
              'pending_or_excluded': (sum(x.get('status')!='EVALUATED' for x in fixture_ledger) if fixture_ledger else
                 max(0, (pre.get('counts') or {}).get('input_filter', len(pre.get('matches', []))) - len(pre.get('matches', []))))}
    rankings = {'schema_version': '1.0-predictive-rankings', 'engine_version': VERSION,
                'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
                'ranking_label': 'EXPERIMENTAL_RANKING', 'fixture_stages': stages,
                'fixture_evaluation_ledger': fixture_ledger,
                'items': models, 'by_family': by_family, 'global_diversified': diversified_global(models),
                'manual_selection_supported_by_fixture_id': True, 'actionable_bet': False}
    val_counts = Counter(v['status'] for v in validations)
    validation = {'schema_version': '1.0-predictive-validation', 'engine_version': VERSION,
                  'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
                  'method': 'EXPANDING_WINDOW_WALK_FORWARD_NO_FUTURE_ROWS', 'series': validations,
                  'counts': {'series': len(validations), 'temporally_validated': 0,
                             'preliminary_walk_forward_improvement': val_counts['PRELIMINARY_WALK_FORWARD_IMPROVEMENT'],
                             'not_enough_temporal_evidence': val_counts['NOT_ENOUGH_TEMPORAL_EVIDENCE'],
                             'preliminary_no_improvement': val_counts['PRELIMINARY_NO_IMPROVEMENT']},
                  'actionable_bet': False}
    ledger = request_ledger(pre, preselection, pre_sha)
    for name, value in (('predictive_coverage.json', coverage), ('predictive_patterns.json', patterns),
                        ('predictive_rankings.json', rankings), ('predictive_validation.json', validation),
                        ('api_request_ledger.json', ledger)):
        dump(out / name, value)
    (out / 'predictive_summary.md').write_text(summary_markdown(coverage, patterns, rankings, validation, ledger), encoding='utf-8')
    artifacts = {p.name: {'sha256': sha(p), 'bytes': p.stat().st_size} for p in sorted(out.iterdir())
                 if p.is_file() and p.name != 'run_manifest.json'}
    manifest = {'schema_version': '1.0-predictive-run-manifest', 'engine_version': VERSION,
                'source_preselection': str(preselection), 'source_preselection_sha256': pre_sha,
                'asof_madrid': asof.astimezone(MADRID).isoformat(), 'run_id': run_id,
                'configuration': {'timezone': 'Europe/Madrid', 'window_default': '15:00-03:00',
                                  'horizon_hours_default': 0, 'bwin_used': False},
                'artifacts': artifacts, 'determinism_sha256': canonical_sha({'coverage': coverage, 'patterns': patterns, 'rankings': rankings, 'validation': validation, 'ledger': ledger})}
    dump(out / 'run_manifest.json', manifest)
    return out, coverage, patterns, rankings, validation, ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preselection', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, default=Path('outputs/predictive'))
    parser.add_argument('--asof', help='ISO8601 con offset; por defecto generated_at_utc del snapshot')
    parser.add_argument('--fixture-id', type=int, help='Mostrar la posición de un fixture sin alterar el ranking')
    args = parser.parse_args(argv)
    try:
        when = dt(args.asof) if args.asof else None
        if args.asof and not when:
            raise PredictiveError('ASOF_INVALID_OR_MISSING_OFFSET')
        out, coverage, patterns, rankings, validation, ledger = run(args.preselection, args.output_root, when)
        print(f"Fixtures: descubiertos={rankings['fixture_stages']['discovered']} elegibles={rankings['fixture_stages']['eligible']} evaluados={rankings['fixture_stages']['statistically_evaluated']}")
        states = Counter(m['pattern_state'] for m in rankings['items'])
        print('Patrones/modelos:', json.dumps(dict(sorted(states.items())), ensure_ascii=False))
        print(f"Mejoras preliminares univariantes: {validation['counts']['preliminary_walk_forward_improvement']} | validados matchup: {validation['counts']['temporally_validated']} | sin evidencia temporal: {validation['counts']['not_enough_temporal_evidence']}")
        for family, rows in rankings['by_family'].items():
            top = next((r for r in rows if r['pattern_state'] != 'INSUFFICIENT_EVIDENCE'), None)
            print(f"TOP {family}: " + (f"fixture={top['fixture_id']} sujeto={top['subject']} estado={top['pattern_state']}" if top else 'sin candidato'))
        print('TOP global:', [(x['fixture_id'], x['subject'], x['family']) for x in rankings['global_diversified'][:7]])
        print(f"API (snapshot de preselección): nuevas={ledger['counts']['network_requests']} "
              f"caché={ledger['counts']['cache_hits']} errores={ledger['counts']['errors']} "
              f"| motor predictivo nuevas={ledger['predictive_engine_network_requests']}")
        if args.fixture_id is not None:
            selected = [x for x in rankings['items'] if x['fixture_id'] == args.fixture_id]
            print(f'Selección manual fixture {args.fixture_id}: {len(selected)} filas deportivas; sin Bwin ni apuesta.')
        print('Salida:', out)
        return 0
    except (PredictiveError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
