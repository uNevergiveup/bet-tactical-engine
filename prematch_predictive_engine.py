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
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pvariance
from zoneinfo import ZoneInfo

import prematch_pattern_discovery as discovery

VERSION = '1.0-mvp'
MADRID = ZoneInfo('Europe/Madrid')
CONSERVATIVE_RESULT_AVAILABILITY = timedelta(hours=3)
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


def engine_code_sha() -> str:
    """Bind reusable output to the exact predictor implementation."""
    return sha(Path(__file__).resolve())


def read(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise PredictiveError(f'JSON raíz inválida: {path}')
    return value


def dump(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                 sort_keys=True) + '\n', encoding='utf-8')
    target.replace(path)


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


def result_available_at(row):
    """Fecha en que el resultado podía usarse, sin inventar hora oficial.

    Se prioriza evidencia fechada del proveedor. Si el snapshot no la incluye,
    se aplica kickoff + 3 horas: regla conservadora, explícita y auditable.
    """
    for key in ('result_available_at_utc', 'completed_at_utc', 'fixture_completed_at_utc'):
        value = dt(row.get(key))
        if value:
            return value, f'EXPLICIT_{key.upper()}'
    kickoff = dt(row.get('kickoff_utc'))
    return ((kickoff + CONSERVATIVE_RESULT_AVAILABILITY), 'CONSERVATIVE_KICKOFF_PLUS_3H') if kickoff else (None, 'UNAVAILABLE')


def observed_values(rows, block, metric, venue=None, include_availability=False):
    output = []
    for row in rows:
        if venue and row.get('venue') != venue:
            continue
        value = (row.get(block) or {}).get(metric)
        if finite(value):
            available_at, basis = result_available_at(row)
            output.append((dt(row.get('kickoff_utc')), float(value), row.get('fixture_id'), available_at, basis))
    valid = [(at, value, fid, available, basis) for at, value, fid, available, basis in output if at and available]
    return valid if include_availability else [(at, value, fid) for at, value, fid, _, _ in valid]


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
    """Expanding-window validation using only results available at forecast time."""
    normalized = {}
    for point in points:
        at, value, fid = point[:3]
        available = point[3] if len(point) > 3 else at + CONSERVATIVE_RESULT_AVAILABILITY
        basis = point[4] if len(point) > 4 else 'CONSERVATIVE_KICKOFF_PLUS_3H'
        if at and available:
            normalized[(at, fid)] = (value, available, basis)
    values = [(key[0], item[0], key[1], item[1], item[2])
              for key, item in sorted(normalized.items(), key=lambda x: (x[0][0], str(x[0][1])))]
    predictions = []
    # Los fixtures con el mismo kickoff pertenecen al mismo bloque temporal.
    # Ningun resultado del bloque puede entrar como entrenamiento de otro.
    for target_at, actual, target_id, _, _ in values:
        train_rows = [(at, value, fid, available, basis)
                      for at, value, fid, available, basis in values
                      if at < target_at and available <= target_at]
        train = [value for _, value, _, _, _ in train_rows]
        if len(train) < min_train:
            continue
        last_training_at = max(at for at, _, _, _, _ in train_rows)
        last_available_at = max(available for _, _, _, available, _ in train_rows)
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
            'training_last_result_available_at': last_available_at.isoformat(),
            'result_availability_basis': dict(Counter(basis for _, _, _, _, basis in train_rows)),
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
        'result_availability_policy': 'EXPLICIT_COMPLETION_WHEN_PRESENT_ELSE_KICKOFF_PLUS_3H',
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



def available_observations(fixture, side, asof):
    """Excluir resultados que no estaban disponibles al corte."""
    deadlines = [
        asof,
        dt(fixture.get("analysis_cutoff_utc")),
        dt(fixture.get("kickoff_madrid")),
    ]
    if any(value is None for value in deadlines):
        return []

    cutoff = min(deadlines)
    rows = discovery.clean_observations(fixture, side, asof)

    return [
        row for row in rows
        if (available := result_available_at(row)[0]) is not None
        and available <= cutoff
    ]

def league_reference(pre, asof):
    buckets = defaultdict(dict)
    for fixture in pre.get('matches', []):
        for side in ('home', 'away'):
            rows = available_observations(fixture, side, asof)
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
            clean = available_observations(fixture, side, asof)
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
        observations = {side: available_observations(fixture, side, asof) for side in ('home', 'away')}
        for side in ('home', 'away'):
            opposite = 'away' if side == 'home' else 'home'
            venue = 'HOME' if side == 'home' else 'AWAY'
            opponent_venue = 'AWAY' if side == 'home' else 'HOME'
            for metric, family in FAMILIES.items():
                own_points = observed_values(observations[side], 'own', metric, include_availability=True)
                opp_points = observed_values(observations[opposite], 'conceded', metric, include_availability=True)
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
                # El componente conserva su peso, pero solo se concede cuando
                # existe mejora preliminar; disponer de folds no prueba mejora.
                validation_component = 12 if wf['status'] == 'PRELIMINARY_WALK_FORWARD_IMPROVEMENT' else 0
                uncertainty_component = 25 if uncertainty and (uncertainty['historical_stability_cv'] or 99) <= .8 else 10
                priority = round(.35 * data_quality + .25 * evidence + validation_component + uncertainty_component, 2)
                risks = ['NO_CALIBRATED_BETTING_VALUE', 'NO_BWIN_INPUT', 'OPPONENT_STRENGTH_PARTIALLY_CONTROLLED']
                usage_status = 'EXPERIMENTAL_TACTICAL_ONLY' if metric == 'offsides' else 'RESEARCH_ONLY'
                if metric == 'offsides':
                    risks.append('OFFSIDES_BWIN_AVAILABILITY_UNVERIFIED_NOT_ACTIONABLE')
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
                    'evidence_dimensions': {
                        'data_quality': {'score': data_quality, 'basis': 'COVERAGE_AND_MATCHED_SAMPLE'},
                        'temporal_depth': {'walk_forward_predictions': wf['valid_predictions'],
                                           'status': 'AVAILABLE' if wf['valid_predictions'] else 'INSUFFICIENT'},
                        'predictive_performance': {'status': wf['status'],
                                                   'improvement_vs_baseline': wf.get('improvement_vs_baseline', False)},
                    },
                    'ranking_label': 'EXPERIMENTAL_RANKING', 'contradictions': contradictions,
                    'risks': risks,
                    'contrastable_explanation': f'Contrastar producción {metric} de {fixture.get(side)} y concesión rival, sin inferir mecanismo táctico.',
                    'additional_data_that_could_change_assessment': ['larger_temporal_sample', 'coach_regime_id', 'comparable_opponent_strength', 'verified_period_splits'],
                    'source_fixture_ids': {'production': sorted(str(x[2]) for x in own_points),
                                           'opponent_concession': sorted(str(x[2]) for x in opp_points)},
                    'actionable_bet': False,
                    'calibration_status': 'NOT_CALIBRATED_FOR_BETTING',
                    'usage_status': usage_status,
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


def fixture_stages(pre, models):
    """Cobertura con universos explícitos; un ledger parcial nunca es el total."""
    matches = pre.get('matches') or []
    ledger = pre.get('fixture_evaluation_ledger') or []
    source_filtered = int((pre.get('counts') or {}).get('input_filter', len(matches)))
    excluded_temporal = sum(x.get('status') == 'EXCLUDED_TEMPORAL' for x in ledger)
    eligible = max(len(matches), source_filtered - excluded_temporal)
    modelled_ids = {m['fixture_id'] for m in models if m.get('predictive_rate') is not None}
    ledger_scope = 'COMPLETE' if len(ledger) >= source_filtered else 'PARTIAL'
    return {
        'discovered': source_filtered,
        'source_filtered': source_filtered,
        'excluded_temporal': excluded_temporal,
        'eligible': eligible,
        'basic_evaluated': len(matches),
        'statistically_evaluated': len({f['fixture_id'] for f in matches} & modelled_ids),
        'deep_evaluated': sum(bool(f.get('details_checked')) for f in matches),
        'pending_or_excluded': max(0, eligible - len(matches)) + excluded_temporal,
        'ledger_scope': ledger_scope,
        'ledger_entries': len(ledger),
        'ledger_universe': source_filtered,
        'ledger_coverage_pct': round(100 * len(ledger) / source_filtered, 2) if source_filtered else None,
        'universe_definitions': {
            'source_filtered': 'counts.input_filter from frozen preselection',
            'eligible': 'source_filtered minus explicit temporal exclusions; never inferred from a partial ledger',
            'basic_evaluated': 'rows physically present in matches',
            'deep_evaluated': 'matches rows with details_checked=true',
        },
    }


def temporal_metadata(pre, asof, executed_at):
    kickoffs = [dt(f.get('kickoff_madrid')) for f in pre.get('matches', [])]
    kickoffs = [x for x in kickoffs if x]
    retrospective = any(executed_at >= kickoff for kickoff in kickoffs)
    return {
        'source_cutoff_utc': asof.astimezone(timezone.utc).isoformat(),
        'run_executed_at_utc': executed_at.astimezone(timezone.utc).isoformat(),
        'execution_mode': 'RETROSPECTIVE_REPLAY' if retrospective else 'PROSPECTIVE',
        'prospective_claim_allowed': not retrospective,
    }


def verify_existing_run(out, source_path, source_sha, *, require_current_code=True):
    manifest_path = out / 'run_manifest.json'
    if not manifest_path.is_file():
        raise PredictiveError(f'EXISTING_RUN_MANIFEST_MISSING:{out}')
    manifest = read(manifest_path)
    required_temporal = ('source_cutoff_utc', 'run_executed_at_utc', 'execution_mode', 'prospective_claim_allowed')
    if manifest.get('integrity_policy') != 'WRITE_ONCE_VERIFY_ALL_ARTIFACT_SHA256' or any(k not in manifest for k in required_temporal):
        raise PredictiveError(f'EXISTING_RUN_LEGACY_OR_UNHARDENED_USE_NEW_OUTPUT_ROOT:{out}')
    if manifest.get('source_preselection_sha256') != source_sha or Path(manifest.get('source_preselection', '')).resolve() != source_path:
        raise PredictiveError(f'EXISTING_RUN_SOURCE_CONFLICT:{out}')
    recorded_code_sha = manifest.get('engine_code_sha256')
    if require_current_code and recorded_code_sha != engine_code_sha():
        raise PredictiveError(f'EXISTING_RUN_ENGINE_CODE_MISMATCH:{out}')
    if not source_path.is_file() or sha(source_path) != source_sha:
        raise PredictiveError('SOURCE_PRESELECTION_SHA_MISMATCH')
    artifacts = manifest.get('artifacts')
    if not isinstance(artifacts, dict) or not artifacts:
        raise PredictiveError(f'EXISTING_RUN_ARTIFACT_MANIFEST_INVALID:{out}')
    for name, expected in artifacts.items():
        path = out / name
        if not path.is_file():
            raise PredictiveError(f'EXISTING_ARTIFACT_MISSING:{name}')
        if sha(path) != expected.get('sha256') or path.stat().st_size != expected.get('bytes'):
            raise PredictiveError(f'EXISTING_ARTIFACT_SHA_MISMATCH:{name}')
    return manifest


def archive_interrupted_run(out: Path) -> Path:
    """Preserve a manifest-less partial run before retrying publication."""
    archive_root = out.parent / '_interrupted_runs'
    archive_root.mkdir(parents=True, exist_ok=True)
    target = archive_root / out.name
    suffix = 1
    while target.exists():
        suffix += 1
        target = archive_root / f'{out.name}.{suffix}'
    out.replace(target)
    return target


def compact_fixture_document(fixture_id, preselection, pre_sha, temporal, stages, models, validations):
    selected = [m for m in models if m['fixture_id'] == fixture_id]
    if not selected:
        raise PredictiveError(f'FIXTURE_ID_NOT_IN_FROZEN_PRESELECTION:{fixture_id}')
    validation_by_id = {v['model_id']: v for v in validations if v['fixture_id'] == fixture_id}
    records = []
    for model in selected:
        validation = validation_by_id.get(model['model_id']) or {}
        records.append({
            'model_id': model['model_id'], 'fixture_id': fixture_id,
            'kickoff_madrid': model['kickoff_madrid'], 'team_side': model['subject'],
            'team_id': model['subject_team_id'], 'metric': model['metric'], 'family': model['family'],
            'estimated_value': model['predictive_rate'], 'distribution': model['distribution'],
            'sample': {
                'production_n': len(model['source_fixture_ids']['production']),
                'opponent_concession_n': len(model['source_fixture_ids']['opponent_concession']),
                'production_fixture_ids': model['source_fixture_ids']['production'],
                'opponent_concession_fixture_ids': model['source_fixture_ids']['opponent_concession'],
            },
            'evidence_state': model['pattern_state'], 'evidence_dimensions': model['evidence_dimensions'],
            'source_cutoff_utc': temporal['source_cutoff_utc'],
            'run_executed_at_utc': temporal['run_executed_at_utc'],
            'emission_mode': ('RETROSPECTIVE' if dt(temporal['run_executed_at_utc']) >= dt(model['kickoff_madrid']) else 'PROSPECTIVE'),
            'source_preselection': str(preselection), 'source_preselection_sha256': pre_sha,
            'provenance': 'FROZEN_PRESELECTION_OFFLINE_NO_BWIN',
            'walk_forward_status': validation.get('status'),
            'usage_status': model['usage_status'], 'risks': model['risks'],
            'ranking_label': 'EXPERIMENTAL_RANKING', 'actionable_bet': False,
            'calibration_status': 'NOT_CALIBRATED_FOR_BETTING',
        })
    return {
        'schema_version': '1.0-frozen-fixture-predictions', 'engine_version': VERSION,
        'fixture_id': fixture_id, **temporal, 'fixture_stages': stages,
        'records': records, 'record_count': len(records),
        'warning': 'RULE_BASED is not a calibrated probability or demonstrated commercial edge.',
        'actionable_bet': False,
    }


def result_rows(document):
    """Normaliza resultados offline; nunca consulta red ni altera el run original."""
    if isinstance(document.get('fixtures'), list):  # retrospective audit output
        for row in document['fixtures']:
            yield row.get('fixture', {}).get('fixture_id'), row.get('observed') or {}
    elif isinstance(document.get('results'), list):
        for row in document['results']:
            yield row.get('fixture_id'), row.get('observed') or {'home': row.get('home', {}), 'away': row.get('away', {})}
    elif isinstance(document.get('response'), list):  # API-Football response; goals only unless embedded stats exist
        for row in document['response']:
            fixture_id = (row.get('fixture') or {}).get('id')
            goals = row.get('goals') or {}
            yield fixture_id, {'home': {'goals': goals.get('home')}, 'away': {'goals': goals.get('away')}}


def evaluate_run(run_dir: Path, results_path: Path, output_root: Path):
    run_dir, results_path = Path(run_dir).resolve(), Path(results_path).resolve()
    manifest = read(run_dir / 'run_manifest.json')
    source = Path(manifest['source_preselection']).resolve()
    # Retrospective evaluation verifies the frozen historical run and its
    # artifacts; it must not require the currently installed implementation.
    verify_existing_run(run_dir, source, manifest['source_preselection_sha256'],
                        require_current_code=False)
    patterns_path = run_dir / 'predictive_patterns.json'
    if not patterns_path.is_file():
        raise PredictiveError('RETROSPECTIVE_EVALUATION_REQUIRES_FULL_RUN')
    results_sha = sha(results_path)
    observed = {int(fid): row for fid, row in result_rows(read(results_path)) if fid is not None}
    models = read(patterns_path)['models']
    rows, missing = [], 0
    for model in models:
        value = (observed.get(int(model['fixture_id']), {}).get(model['subject']) or {}).get(model['metric'])
        if not finite(value) or not finite(model.get('predictive_rate')):
            missing += 1
            continue
        estimate, baseline = model['predictive_rate'], model.get('league_reference_rate')
        rows.append({
            'model_id': model['model_id'], 'fixture_id': model['fixture_id'], 'family': model['family'],
            'metric': model['metric'], 'subject': model['subject'], 'observed': float(value),
            'estimated': estimate, 'error': round(estimate - float(value), 8),
            'absolute_error': round(abs(estimate - float(value)), 8),
            'baseline': baseline,
            'baseline_error': round(baseline - float(value), 8) if finite(baseline) else None,
            'baseline_absolute_error': round(abs(baseline - float(value)), 8) if finite(baseline) else None,
        })
    groups = []
    for family in sorted({r['family'] for r in rows}):
        selected = [r for r in rows if r['family'] == family]
        with_baseline = [r for r in selected if r['baseline_absolute_error'] is not None]
        groups.append({
            'family': family, 'n': len(selected),
            'mae_model': round(mean(r['absolute_error'] for r in selected), 8),
            'bias_model': round(mean(r['error'] for r in selected), 8),
            'baseline_n': len(with_baseline),
            'mae_baseline': round(mean(r['baseline_absolute_error'] for r in with_baseline), 8) if with_baseline else None,
            'bias_baseline': round(mean(r['baseline_error'] for r in with_baseline), 8) if with_baseline else None,
        })
    evaluation = {
        'schema_version': '1.0-retrospective-predictive-evaluation', 'engine_version': VERSION,
        'evaluated_run': str(run_dir), 'evaluated_run_manifest_sha256': sha(run_dir / 'run_manifest.json'),
        'results_source': str(results_path), 'results_source_sha256': results_sha,
        'evaluation_mode': 'RETROSPECTIVE_OFFLINE_SEPARATE_ARTIFACT',
        'counts': {'evaluable': len(rows), 'not_evaluable_missing': missing},
        'by_market_family': groups, 'rows': rows, 'actionable_bet': False,
        'warning': 'No odds, picks, EV or profitability are inferred.',
    }
    out = Path(output_root).resolve() / 'evaluations' / f"{manifest['run_id']}_{results_sha[:12]}"
    target = out / 'retrospective_evaluation.json'
    if out.exists():
        existing = read(target) if target.is_file() else None
        if existing != evaluation:
            raise PredictiveError(f'EXISTING_EVALUATION_CONFLICT:{out}')
        return out
    out.mkdir(parents=True)
    dump(target, evaluation)
    dump(out / 'evaluation_manifest.json', {'artifact': target.name, 'artifact_sha256': sha(target),
         'source_run_manifest_sha256': sha(run_dir / 'run_manifest.json'), 'results_source_sha256': results_sha})
    return out


def summary_markdown(coverage, patterns, rankings, validation, ledger):
    family_counts = Counter(r['family'] for r in rankings['items'] if r['pattern_state'] != 'INSUFFICIENT_EVIDENCE')
    state_counts = Counter(r['pattern_state'] for r in rankings['items'])
    lines = ['# PREMATCH Predictive Engine V1.0 MVP', '',
             f"Corte de fuente: `{rankings['source_cutoff_utc']}`. Ejecución real: `{rankings['run_executed_at_utc']}`. Modo: `{rankings['execution_mode']}`.",
             f"Fuente SHA-256: `{rankings['preselection_sha256']}`.", '',
             'Motor deportivo independiente de Bwin. No genera picks, stakes, EV ni probabilidades de rentabilidad.', '',
             '## Alcance', '',
             f"- Partidos en el filtro de origen: {rankings['fixture_stages']['source_filtered']}.",
             f"- Excluidos por inicio anterior o igual al corte: {rankings['fixture_stages']['excluded_temporal']}.",
             f"- Elegibles al corte y evaluados básicamente: {rankings['fixture_stages']['eligible']} / {rankings['fixture_stages']['basic_evaluated']}.",
             f"- Evaluados estadísticamente: {rankings['fixture_stages']['statistically_evaluated']}.",
             f"- Profundizados: {rankings['fixture_stages']['deep_evaluated']}.",
             f"- Ledger de evaluación: {rankings['fixture_stages']['ledger_scope']} ({rankings['fixture_stages']['ledger_entries']}/{rankings['fixture_stages']['ledger_universe']}).",
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
    lines += ['- `RULE_BASED` no equivale a probabilidad calibrada ni a ventaja comercial demostrada.', '']
    return '\n'.join(lines)


def run(preselection: Path, output_root: Path, asof=None, *, executed_at=None, fixture_id=None):
    preselection = Path(preselection).resolve()
    pre = read(preselection)
    asof = asof or dt(pre.get('generated_at_utc'))
    executed_at = executed_at or datetime.now(timezone.utc)
    if executed_at.tzinfo is None:
        raise PredictiveError('RUN_EXECUTED_AT_REQUIRES_TIMEZONE')
    if not asof:
        raise PredictiveError('ASOF_REQUIRED_WITH_TIMEZONE')
    validate_source(pre, asof)
    pre_sha = sha(preselection)
    code_sha = engine_code_sha()
    run_id = (f"{asof.astimezone(MADRID).strftime('%Y%m%dT%H%M%S%z')}_{pre_sha[:12]}"
              f"_code-{code_sha[:12]}")
    if fixture_id is not None:
        run_id += f'_fixture-{fixture_id}'
    out = Path(output_root).resolve() / VERSION / run_id
    recovered_interrupted_run = None
    if out.exists() and not (out / 'run_manifest.json').is_file():
        recovered_interrupted_run = str(archive_interrupted_run(out).resolve())
    if out.exists():
        verify_existing_run(out, preselection, pre_sha)
        if fixture_id is None:
            return (out, read(out / 'predictive_coverage.json'), read(out / 'predictive_patterns.json'),
                    read(out / 'predictive_rankings.json'), read(out / 'predictive_validation.json'),
                    read(out / 'api_request_ledger.json'))
        compact = read(out / f'frozen_predictions_{fixture_id}.json')
        return out, {}, {'models': compact['records']}, {'items': compact['records'], 'fixture_stages': compact['fixture_stages']}, {'counts': {}}, {}
    temporal = temporal_metadata(pre, asof, executed_at)

    # Vista temporal segura; nunca modifica el snapshot original.
    safe_matches = []
    for fixture in pre.get('matches', []):
        safe_fixture = dict(fixture)
        observations = dict(fixture.get('statistical_observations') or {})
        for side in ('home', 'away'):
            observations[side] = available_observations(
                fixture, side, asof
            )
        safe_fixture['statistical_observations'] = observations
        safe_matches.append(safe_fixture)

    safe_pre = dict(pre, matches=safe_matches)

    coverage = coverage_rows(safe_pre, pre_sha, asof)
    descriptive = discovery.build_discovery(safe_pre, pre_sha, asof)
    references = league_reference(safe_pre, asof)
    models, validations = fixture_models(safe_pre, asof, references)
    patterns = {'schema_version': '1.0-predictive-patterns', 'engine_version': VERSION,
                'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
                'descriptive_discovery': descriptive, 'models': models,
                'states': sorted(STATES), 'actionable_bet': False}
    by_family = {family: sorted([m for m in models if m['family'] == family],
                                key=lambda x: (-x['experimental_priority_score'], str(x['fixture_id']), x['subject']))
                 for family in sorted(set(FAMILIES.values()))}
    fixture_ledger=pre.get('fixture_evaluation_ledger',[])
    stages = fixture_stages(pre, models)
    rankings = {'schema_version': '1.0-predictive-rankings', 'engine_version': VERSION,
                'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
                **temporal,
                'ranking_label': 'EXPERIMENTAL_RANKING', 'fixture_stages': stages,
                'fixture_evaluation_ledger': fixture_ledger,
                'items': models, 'by_family': by_family, 'global_diversified': diversified_global(models),
                'manual_selection_supported_by_fixture_id': True, 'actionable_bet': False}
    val_counts = Counter(v['status'] for v in validations)
    validation = {'schema_version': '1.0-predictive-validation', 'engine_version': VERSION,
                  'asof_madrid': asof.astimezone(MADRID).isoformat(), 'preselection_sha256': pre_sha,
                  **temporal,
                  'method': 'EXPANDING_WINDOW_RESULTS_AVAILABLE_BEFORE_TARGET', 'series': validations,
                  'counts': {'series': len(validations), 'temporally_validated': 0,
                             'preliminary_walk_forward_improvement': val_counts['PRELIMINARY_WALK_FORWARD_IMPROVEMENT'],
                             'not_enough_temporal_evidence': val_counts['NOT_ENOUGH_TEMPORAL_EVIDENCE'],
                             'preliminary_no_improvement': val_counts['PRELIMINARY_NO_IMPROVEMENT']},
                  'actionable_bet': False}
    ledger = request_ledger(pre, preselection, pre_sha)
    for document in (coverage, patterns, ledger):
        document.update(temporal)
    if fixture_id is not None:
        compact = compact_fixture_document(fixture_id, preselection, pre_sha, temporal, stages, models, validations)
        compact_name = f'frozen_predictions_{fixture_id}.json'
        out.mkdir(parents=True, exist_ok=False)
        dump(out / compact_name, compact)
        artifacts = {compact_name: {'sha256': sha(out / compact_name), 'bytes': (out / compact_name).stat().st_size}}
        manifest = {'schema_version': '1.1-predictive-run-manifest-hardening', 'engine_version': VERSION,
                    'engine_code_sha256': code_sha,
                    'integrity_policy': 'WRITE_ONCE_VERIFY_ALL_ARTIFACT_SHA256',
                    'source_preselection': str(preselection), 'source_preselection_sha256': pre_sha,
                    'asof_madrid': asof.astimezone(MADRID).isoformat(), **temporal, 'run_id': run_id,
                    'configuration': {'timezone': 'Europe/Madrid', 'window_default': '15:00-03:00',
                                      'horizon_hours_default': 0, 'bwin_used': False,
                                      'fixture_id_filter': fixture_id, 'global_references_preserved': True},
                    'recovered_interrupted_run': recovered_interrupted_run,
                    'artifacts': artifacts,
                    'determinism_sha256': canonical_sha({'fixture_id': fixture_id, 'records': compact['records'],
                                                         'source_cutoff_utc': temporal['source_cutoff_utc']})}
        dump(out / 'run_manifest.json', manifest)
        return out, {}, {'models': compact['records']}, {'items': compact['records'], 'fixture_stages': stages}, {'counts': {}}, ledger
    out.mkdir(parents=True, exist_ok=False)
    for name, value in (('predictive_coverage.json', coverage), ('predictive_patterns.json', patterns),
                        ('predictive_rankings.json', rankings), ('predictive_validation.json', validation),
                        ('api_request_ledger.json', ledger)):
        dump(out / name, value)
    (out / 'predictive_summary.md').write_text(summary_markdown(coverage, patterns, rankings, validation, ledger), encoding='utf-8')
    artifacts = {p.name: {'sha256': sha(p), 'bytes': p.stat().st_size} for p in sorted(out.iterdir())
                 if p.is_file() and p.name != 'run_manifest.json'}
    manifest = {'schema_version': '1.1-predictive-run-manifest-hardening', 'engine_version': VERSION,
                'engine_code_sha256': code_sha,
                'integrity_policy': 'WRITE_ONCE_VERIFY_ALL_ARTIFACT_SHA256',
                'source_preselection': str(preselection), 'source_preselection_sha256': pre_sha,
                'asof_madrid': asof.astimezone(MADRID).isoformat(), **temporal, 'run_id': run_id,
                'configuration': {'timezone': 'Europe/Madrid', 'window_default': '15:00-03:00',
                                  'horizon_hours_default': 0, 'bwin_used': False},
                'recovered_interrupted_run': recovered_interrupted_run,
                'artifacts': artifacts, 'determinism_sha256': canonical_sha({'coverage': coverage, 'patterns': patterns, 'rankings': rankings, 'validation': validation, 'ledger': ledger})}
    dump(out / 'run_manifest.json', manifest)
    return out, coverage, patterns, rankings, validation, ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preselection', type=Path)
    parser.add_argument('--output-root', type=Path, default=Path('outputs/predictive'))
    parser.add_argument('--asof', help='ISO8601 con offset; por defecto generated_at_utc del snapshot')
    parser.add_argument('--fixture-id', type=int, help='Generar artefacto congelado compacto para un fixture, conservando referencias globales')
    parser.add_argument('--evaluate-run-dir', type=Path, help='Run completo ya congelado para evaluación retrospectiva offline')
    parser.add_argument('--evaluate-results', type=Path, help='Resultados finales JSON; se escriben en un directorio separado')
    args = parser.parse_args(argv)
    try:
        if args.evaluate_results or args.evaluate_run_dir:
            if not args.evaluate_results or not args.evaluate_run_dir:
                raise PredictiveError('EVALUATION_REQUIRES_RUN_DIR_AND_RESULTS')
            out = evaluate_run(args.evaluate_run_dir, args.evaluate_results, args.output_root)
            print('Evaluación retrospectiva separada:', out)
            return 0
        if not args.preselection:
            raise PredictiveError('PRESELECTION_REQUIRED')
        when = dt(args.asof) if args.asof else None
        if args.asof and not when:
            raise PredictiveError('ASOF_INVALID_OR_MISSING_OFFSET')
        out, coverage, patterns, rankings, validation, ledger = run(
            args.preselection, args.output_root, when, fixture_id=args.fixture_id)
        if args.fixture_id is not None:
            compact = read(out / f'frozen_predictions_{args.fixture_id}.json')
            print(f"Fixture {args.fixture_id}: {compact['record_count']} filas congeladas; modo={compact['execution_mode']}; sin Bwin ni apuesta.")
            print('Salida para compartir:', out / f'frozen_predictions_{args.fixture_id}.json')
            return 0
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
        print('Salida:', out)
        return 0
    except (PredictiveError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())


def joint_ft_goal_baseline(fixture, observations, cutoff, *, min_train=6, bootstrap_samples=200):
    """Opt-in joint baseline; existing V1 models and rankings are unchanged.

    Four separately identified production/concession series. Venue estimates
    shrink to their own global series with the existing strength=6 rule.
    Independence, stationarity and Poisson variance are diagnostics, not facts.
    """
    import random
    from statistics import pvariance
    if min_train<6:raise ValueError('JOINT_DIAGNOSTIC_MINIMUM_6')
    clean={}
    for side in ('home','away'):
        unique={}
        for row in observations.get(side,[]):
            available,_=result_available_at(row)
            if (row.get('team_id')!=fixture[side+'_id'] or row.get('fixture_id')==fixture['fixture_id']
                or row.get('league_id')!=fixture['league_id'] or row.get('season')!=fixture['season']
                or row.get('status')!='FT' or not available or available>cutoff):continue
            if row['fixture_id'] in unique and unique[row['fixture_id']]!=row:raise ValueError('JOINT_DUPLICATE_IDENTITY_CONFLICT')
            unique[row['fixture_id']]=row
        clean[side]=list(unique.values())
    series={}
    for side in ('home','away'):
        for kind in ('own','conceded'):
            rows=[r for r in clean[side] if finite(r.get(kind,{}).get('goals')) and float(r[kind]['goals']).is_integer()]
            values=[r[kind]['goals'] for r in rows];venue='HOME' if side=='home' else 'AWAY'
            local=[r[kind]['goals'] for r in rows if r['venue']==venue]
            avg=safe_mean(values);var=pvariance(values) if len(values)>1 else None
            series[side+'_'+kind]={'n':len(values),'venue_n':len(local),'fixture_ids':[r['fixture_id'] for r in rows],
                'mean':avg,'variance':var,'variance_to_mean':var/avg if avg and var is not None else None,
                'venue_shrunk_rate':shrink(local,avg,6.0),'alternative_count_model':choose_count_model(values),
                '_values':values,'_local':local}
    result={'status':'DATA_INSUFFICIENT','series':{k:{x:y for x,y in v.items() if not x.startswith('_')} for k,v in series.items()},
            'min_train_required':min_train,'validated':False,'predictive_use_authorized':False,
            'assumptions':['INDEPENDENCE_NOT_VERIFIED','STATIONARITY_NOT_VERIFIED','VENUE_SHRINK_STRENGTH_6_EXISTING_RULE'],
            'probabilities_calibrated':False,'statistical_reliability':'NO_PROSPECTIVE_OR_APPROVED_OUT_OF_SAMPLE_VALIDATION'}
    if min(v['n'] for v in series.values())<min_train:return result
    rates={k:v['venue_shrunk_rate'] for k,v in series.items()}
    lh=(rates['home_own']+rates['away_conceded'])/2;la=(rates['away_own']+rates['home_conceded'])/2
    total=lh+la;probs=distribution(total,'POISSON')
    # Empirical mixture per scoring side, then independent convolution. Never
    # pool heterogeneous means to manufacture negative-binomial dispersion.
    empirical={}
    for key,v in series.items():
        vals=v['_values'];local=v['_local'];w=len(local)/(len(local)+6)
        empirical[key]={i:(1-w)*vals.count(i)/len(vals)+(w*local.count(i)/len(local) if local else 0) for i in set(vals+local)}
    def mixture(a,b):return {i:(a.get(i,0)+b.get(i,0))/2 for i in set(a)|set(b)}
    hp=mixture(empirical['home_own'],empirical['away_conceded']);ap=mixture(empirical['away_own'],empirical['home_conceded']);ep={}
    for h,ph in hp.items():
        for a,pa in ap.items():ep[h+a]=ep.get(h+a,0)+ph*pa
    rng=random.Random(str(fixture['fixture_id']));draws=[]
    for _ in range(bootstrap_samples):
        rr={}
        for key,v in series.items():
            # Resample complete rows: venue/global overlap is preserved.
            side=key.split('_')[0];kind=key.split('_')[1];rows=[r for r in clean[side] if finite(r.get(kind,{}).get('goals')) and float(r[kind]['goals']).is_integer()]
            sampled=[rng.choice(rows) for _ in rows];allv=[r[kind]['goals'] for r in sampled];lv=[r[kind]['goals'] for r in sampled if r['venue']==('HOME' if side=='home' else 'AWAY')]
            rr[key]=shrink(lv,safe_mean(allv),6)
        draws.append(sum(rr.values())/2)
    draws.sort();bounds=[draws[int((len(draws)-1)*q)] for q in (.05,.95)] if draws else None
    result.update(status='MODEL_EVALUABLE',model='INDEPENDENT_POISSON_JOINT_BASELINE',lambda_home=lh,lambda_away=la,lambda_total=total,
                  poisson_total_distribution=probs,empirical_total_distribution={str(k):v for k,v in ep.items()},
                  predictive_interval_90=[quantile(probs,.05),quantile(probs,.95)],
                  parameter_uncertainty={'lambda_total_interval_90':bounds,'method':'DETERMINISTIC_ROW_BOOTSTRAP_IID_WITHIN_SERIES','draws':bootstrap_samples,'calibrated_interval':False},
                  poisson_assumption_diagnostics={k:v['alternative_count_model'] for k,v in series.items()},
                  alternatives={'empirical':'VENUE_SHRUNK_SERIES_MIXTURE_INDEPENDENT_CONVOLUTION','negative_binomial':'Existing choose_count_model diagnostics per homogeneous series; no pooled heterogeneity fit'})
    return result
