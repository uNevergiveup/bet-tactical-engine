"""Auxiliary descriptive rendering and diagnostic helpers for prematch tactical profiles.

Strictly read-only/in-memory transformations over verified historical observations.
Never performs network requests or converts descriptive statistics into calibrated probabilities,
fair odds, expected value (EV), or betting recommendations.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from statistics import mean, pstdev
from typing import Any


EXTENDED_METRICS = (
    'goals',
    'xg',
    'shots',
    'on_target',
    'corners',
    'fouls',
    'yellow',
    'offsides',
    'blocked_shots',
    'possession_pct',
    'goals_h1',
    'goals_h2',
)

METRIC_LABELS_ES = {
    'goals': 'Goles FT',
    'xg': 'xG',
    'shots': 'Tiros totales',
    'on_target': 'Tiros a puerta',
    'corners': 'Córners',
    'fouls': 'Faltas cometidas',
    'yellow': 'Tarjetas amarillas',
    'offsides': 'Fueras de juego',
    'blocked_shots': 'Tiros bloqueados',
    'possession_pct': 'Posesión (%)',
    'goals_h1': 'Goles 1ª parte (1H)',
    'goals_h2': 'Goles 2ª parte (2H)',
}


def is_finite_non_negative(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def basic_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {'n': 0, 'mean': None, 'cv': None}
    avg = mean(values)
    sd = pstdev(values)
    return {
        'n': len(values),
        'mean': round(avg, 4),
        'stdev': round(sd, 4),
        'cv': round(sd / avg, 4) if avg > 0 else (0.0 if sd == 0 else None),
    }


def enrich_series_denominators(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    detailed_records: list[dict[str, Any]],
    valid: list[tuple[dict[str, Any], float]],
    venue: str,
    metric: str,
) -> dict[str, Any]:
    """Attach explicit zero vs null vs not-requested counts and venue denominators."""
    zero_ids = [r['fixture_id'] for r, v in valid if v == 0]
    positive_ids = [r['fixture_id'] for r, v in valid if v > 0]
    valid_id_set = {r['fixture_id'] for r, _ in valid}
    detailed_id_set = {r['fixture_id'] for r in detailed_records}
    missing_ids = [r['fixture_id'] for r in detailed_records if r['fixture_id'] not in valid_id_set]
    not_detailed_ids = [r['fixture_id'] for r in records if r['fixture_id'] not in detailed_id_set]

    venue_records = [r for r in records if r.get('venue') == venue]
    venue_detailed = [r for r in detailed_records if r.get('venue') == venue]
    venue_valid = [(r, v) for r, v in valid if r.get('venue') == venue]
    venue_zero_n = sum(1 for _, v in venue_valid if v == 0)
    venue_positive_n = sum(1 for _, v in venue_valid if v > 0)

    venue_block = dict(summary.get('venue') or {})
    venue_block.update(
        {
            'venue_filter': venue,
            'history_n': len(venue_records),
            'expected_n': len(venue_detailed),
            'not_detailed_n': len(venue_records) - len(venue_detailed),
            'missing_n': len(venue_detailed) - len(venue_valid),
            'zero_n': venue_zero_n,
            'positive_n': venue_positive_n,
            'fixture_ids': [r['fixture_id'] for r, _ in venue_valid],
        }
    )

    recent_pairs = valid[:5]
    previous_pairs = valid[5:10]
    recent_block = dict(summary.get('recent') or {})
    recent_block.update(
        {
            'zero_n': sum(1 for _, v in recent_pairs if v == 0),
            'positive_n': sum(1 for _, v in recent_pairs if v > 0),
            'fixture_ids': [r['fixture_id'] for r, _ in recent_pairs],
        }
    )
    previous_block = dict(summary.get('previous') or {})
    previous_block.update(
        {
            'zero_n': sum(1 for _, v in previous_pairs if v == 0),
            'positive_n': sum(1 for _, v in previous_pairs if v > 0),
            'fixture_ids': [r['fixture_id'] for r, _ in previous_pairs],
        }
    )

    summary.update(
        {
            'zero_n': len(zero_ids),
            'positive_n': len(positive_ids),
            'metric_observations_zero': len(zero_ids),
            'metric_observations_positive': len(positive_ids),
            'unobserved_total_n': len(records) - len(valid),
            'zero_fixture_ids': zero_ids,
            'missing_fixture_ids': missing_ids,
            'not_detailed_fixture_ids': not_detailed_ids,
            'venue': venue_block,
            'recent': recent_block,
            'previous': previous_block,
            'denominator_rule': (
                'ALL_FT_FIXTURES_EXPECTED'
                if metric == 'goals'
                else 'DETAILED_STATISTICS_REQUESTED_SUBSET_VS_FULL_FT_HISTORY'
            ),
        }
    )
    return summary


def build_window_slice(
    sample_slice: list[dict[str, Any]],
    metrics: tuple[str, ...],
    stats_fn=basic_stats,
    finite_fn=is_finite_non_negative,
) -> dict[str, Any]:
    """Build a window slice with unambiguous denominators and both own/conceded metrics."""
    window_n = len(sample_slice)
    detailed_rows = [r for r in sample_slice if r.get('statistics_requested', True)]
    detailed_id_set = {r['fixture_id'] for r in detailed_rows}
    not_detailed_ids = [r['fixture_id'] for r in sample_slice if r['fixture_id'] not in detailed_id_set]

    def build_kind(kind: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for m in metrics:
            expected_rows = sample_slice if m == 'goals' else detailed_rows
            valid_pairs = [
                (r, float(r.get(kind, {}).get(m)))
                for r in expected_rows
                if finite_fn(r.get(kind, {}).get(m))
            ]
            vals = [v for _, v in valid_pairs]
            valid_id_set = {r['fixture_id'] for r, _ in valid_pairs}
            missing_ids = [r['fixture_id'] for r in expected_rows if r['fixture_id'] not in valid_id_set]
            zero_n = sum(1 for _, v in valid_pairs if v == 0)
            positive_n = sum(1 for _, v in valid_pairs if v > 0)
            entry = dict(stats_fn(vals))
            entry.update(
                {
                    'window_fixtures_n': window_n,
                    'expected_n': len(expected_rows),
                    'not_detailed_n': window_n - len(expected_rows),
                    'missing_n': len(expected_rows) - len(valid_pairs),
                    'zero_n': zero_n,
                    'positive_n': positive_n,
                    'valid_fixture_ids': [r['fixture_id'] for r, _ in valid_pairs],
                    'missing_fixture_ids': missing_ids,
                    'not_detailed_fixture_ids': [] if m == 'goals' else not_detailed_ids,
                }
            )
            out[m] = entry
        return out

    competitions = sorted({f"{r.get('league_id')}:{r.get('season')}" for r in sample_slice})
    return {
        'fixture_ids': [r['fixture_id'] for r in sample_slice],
        'window_fixtures_n': window_n,
        'detailed_fixtures_n': len(detailed_rows),
        'not_detailed_fixtures_n': window_n - len(detailed_rows),
        'competitions_included': competitions,
        'mixed_competitions': len(competitions) > 1,
        'metrics': build_kind('own'),
        'conceded_metrics': build_kind('conceded'),
    }


def summarize_extended_metric(
    records: list[dict[str, Any]],
    metric: str,
    kind: str,
    venue: str,
    stats_fn=basic_stats,
    finite_fn=is_finite_non_negative,
) -> dict[str, Any]:
    """Summarize period or support metrics using the same denominator contract as summarize_series."""
    all_n = len(records)
    if metric == 'goals':
        detailed_records = list(records)
    else:
        detailed_records = [
            r
            for r in records
            if r.get('statistics_requested', True) or finite_fn(r.get(kind, {}).get(metric))
        ]
    vals = [(r, r.get(kind, {}).get(metric)) for r in detailed_records]
    valid = [(r, float(v)) for r, v in vals if finite_fn(v)]
    values = [v for _, v in valid]
    summary = dict(stats_fn(values))
    venue_values = [v for r, v in valid if r.get('venue') == venue]
    recent = stats_fn(values[:5])
    previous = stats_fn(values[5:10])
    trend = (
        (recent['mean'] - previous['mean']) / max(previous['mean'], 1)
        if recent['n'] >= 3 and previous['n'] >= 3
        else None
    )
    loo = [mean(values[:i] + values[i + 1 :]) for i in range(len(values))] if len(values) >= 4 else []
    requested_coverage = round(100 * len(valid) / len(detailed_records), 2) if detailed_records else None
    effective_coverage = round(100 * len(valid) / all_n, 2) if all_n else None
    base = {
        **summary,
        'history_n': all_n,
        'history_not_detailed_n': all_n - len(detailed_records),
        'expected_n': len(detailed_records),
        'missing_n': len(detailed_records) - len(valid),
        'coverage_pct': requested_coverage,
        'historical_matches_available': all_n,
        'historical_matches_detailed': len(detailed_records),
        'historical_matches_not_detailed': all_n - len(detailed_records),
        'metric_observations_valid': len(valid),
        'metric_observations_missing': len(detailed_records) - len(valid),
        'requested_data_coverage_pct': requested_coverage,
        'historical_effective_coverage_pct': effective_coverage,
        'venue': stats_fn(venue_values),
        'recent': recent,
        'previous': previous,
        'trend_relative': trend,
        'leave_one_out_mean_range': [round(min(loo), 4), round(max(loo), 4)] if loo else None,
        'fixture_ids': [r['fixture_id'] for r, _ in valid],
    }
    return enrich_series_denominators(base, records, detailed_records, valid, venue, metric)


def build_period_and_support_metrics(
    same_records: list[dict[str, Any]],
    venue: str,
    stats_fn=basic_stats,
    finite_fn=is_finite_non_negative,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build H1/H2 period breakdown and support metrics (blocked_shots, possession_pct)."""
    period_metrics: dict[str, Any] = {}
    for m in ('goals_h1', 'goals_h2'):
        period_metrics[m] = {
            k: summarize_extended_metric(same_records, m, k, venue, stats_fn, finite_fn)
            for k in ('own', 'conceded')
        }

    half_comparison: dict[str, Any] = {}
    for k in ('own', 'conceded'):
        paired = []
        for r in same_records:
            h1 = r.get(k, {}).get('goals_h1')
            h2 = r.get(k, {}).get('goals_h2')
            if finite_fn(h1) and finite_fn(h2):
                paired.append((r, float(h1), float(h2)))
        h1_vals = [h1 for _, h1, _ in paired]
        h2_vals = [h2 for _, _, h2 in paired]
        diffs = [h2 - h1 for _, h1, h2 in paired]
        half_comparison[k] = {
            'paired_n': len(paired),
            'history_n': len(same_records),
            'unobserved_halftime_n': len(same_records) - len(paired),
            'h1_mean': round(mean(h1_vals), 4) if paired else None,
            'h2_mean': round(mean(h2_vals), 4) if paired else None,
            'h2_minus_h1_mean': round(mean(diffs), 4) if paired else None,
            'h2_greater_n': sum(1 for _, h1, h2 in paired if h2 > h1),
            'h1_greater_n': sum(1 for _, h1, h2 in paired if h1 > h2),
            'equal_halves_n': sum(1 for _, h1, h2 in paired if h1 == h2),
            'both_halves_zero_n': sum(1 for _, h1, h2 in paired if h1 == 0 and h2 == 0),
            'h2_greater_fixture_ids': [r['fixture_id'] for r, h1, h2 in paired if h2 > h1],
            'h1_greater_fixture_ids': [r['fixture_id'] for r, h1, h2 in paired if h1 > h2],
            'fixture_ids': [r['fixture_id'] for r, _, _ in paired],
        }

    match_half_totals = []
    for r in same_records:
        oh1 = r.get('own', {}).get('goals_h1')
        oh2 = r.get('own', {}).get('goals_h2')
        ch1 = r.get('conceded', {}).get('goals_h1')
        ch2 = r.get('conceded', {}).get('goals_h2')
        if all(finite_fn(v) for v in (oh1, oh2, ch1, ch2)):
            match_half_totals.append((r, float(oh1 + ch1), float(oh2 + ch2)))
    th1 = [x[1] for x in match_half_totals]
    th2 = [x[2] for x in match_half_totals]
    total_goals_sum = sum(th1) + sum(th2)
    half_comparison['match_total'] = {
        'paired_n': len(match_half_totals),
        'total_h1_mean': round(mean(th1), 4) if match_half_totals else None,
        'total_h2_mean': round(mean(th2), 4) if match_half_totals else None,
        'total_h2_minus_h1_mean': round(mean([b - a for _, a, b in match_half_totals]), 4)
        if match_half_totals
        else None,
        'h2_share_of_total_goals': round((sum(th2) / total_goals_sum), 4)
        if total_goals_sum > 0
        else None,
        'h2_higher_scoring_matches_n': sum(1 for _, a, b in match_half_totals if b > a),
        'h1_higher_scoring_matches_n': sum(1 for _, a, b in match_half_totals if a > b),
        'equal_half_scoring_matches_n': sum(1 for _, a, b in match_half_totals if a == b),
        'fixture_ids': [r['fixture_id'] for r, _, _ in match_half_totals],
    }
    period_metrics['half_comparison'] = half_comparison
    period_metrics['other_metrics_by_half'] = (
        'UNAVAILABLE_IN_PROVIDER_SNAPSHOT_ONLY_GOALS_H1_H2_RECORDED'
    )

    support_metrics: dict[str, Any] = {}
    for m in ('blocked_shots', 'possession_pct'):
        support_metrics[m] = {
            k: summarize_extended_metric(same_records, m, k, venue, stats_fn, finite_fn)
            for k in ('own', 'conceded')
        }
    return period_metrics, support_metrics


def enrich_observations_from_raw(
    observations: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    team_id: int,
) -> list[dict[str, Any]]:
    """Propagate recorded formation, coach_id and coach_name from pre-cutoff raw_rows onto observations."""
    raw_by_fid = {
        raw.get('fixture', {}).get('id'): raw
        for raw in raw_rows
        if isinstance(raw, dict) and isinstance(raw.get('fixture', {}).get('id'), int)
    }
    for row in observations:
        fid = row.get('fixture_id')
        raw = raw_by_fid.get(fid)
        if not raw:
            continue
        lineups = [
            lu
            for lu in (raw.get('lineups') or [])
            if (lu.get('team') or {}).get('id') == team_id
        ]
        if len(lineups) == 1:
            lu = lineups[0]
            if row.get('formation') is None and lu.get('formation'):
                row['formation'] = lu.get('formation')
            coach = lu.get('coach') or {}
            cid = coach.get('id')
            cname = coach.get('name')
            if row.get('coach_id') is None and isinstance(cid, int) and not isinstance(cid, bool):
                row['coach_id'] = cid
            if row.get('coach_name') is None and cname:
                row['coach_name'] = cname
    return observations


def build_coach_regime_summary(
    team_id: int,
    same_records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    longitudinal_coaches: list[dict[str, Any]],
    stats_fn=basic_stats,
    finite_fn=is_finite_non_negative,
) -> dict[str, Any]:
    """Separate team statistics by recorded coach without merging unverified IDs."""
    coach_lookup: dict[int, tuple[int | None, str | None]] = {}
    for item in longitudinal_coaches or []:
        fid = item.get('fixture_id')
        coach = item.get('coach') or {}
        cid = coach.get('id') if isinstance(coach.get('id'), int) and not isinstance(coach.get('id'), bool) else None
        cname = coach.get('name') or None
        if isinstance(fid, int) and (cid is not None or cname):
            coach_lookup[fid] = (cid, cname)

    def resolve_coach(row: dict[str, Any]) -> tuple[str, int | None, str | None, bool]:
        cid = row.get('coach_id')
        cname = row.get('coach_name')
        if cid is None and cname is None and row.get('fixture_id') in coach_lookup:
            cid, cname = coach_lookup[row['fixture_id']]
        if isinstance(cid, int) and not isinstance(cid, bool) and cid > 0:
            label = f"id={cid}:{cname}" if cname else f"id={cid}"
            return label, cid, cname, False
        if (cid == 0 and cname) or (cid is None and cname):
            return f"id=0:{cname}", 0, cname, True
        return 'UNRECORDED_NO_LINEUP_IN_CACHE', None, None, False

    def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        metrics_out: dict[str, Any] = {}
        for m in (
            'goals',
            'xg',
            'shots',
            'on_target',
            'corners',
            'fouls',
            'yellow',
            'offsides',
            'blocked_shots',
            'possession_pct',
            'goals_h1',
            'goals_h2',
        ):
            metrics_out[m] = {}
            for kind in ('own', 'conceded'):
                expected_rows = (
                    rows
                    if m == 'goals'
                    else [
                        r
                        for r in rows
                        if r.get('statistics_requested', True) or finite_fn(r.get(kind, {}).get(m))
                    ]
                )
                valid_vals = [
                    float(r.get(kind, {}).get(m))
                    for r in expected_rows
                    if finite_fn(r.get(kind, {}).get(m))
                ]
                s = dict(stats_fn(valid_vals))
                s.update(
                    {
                        'sample_n': len(rows),
                        'expected_n': len(expected_rows),
                        'not_detailed_n': len(rows) - len(expected_rows),
                        'missing_n': len(expected_rows) - len(valid_vals),
                        'zero_n': sum(1 for v in valid_vals if v == 0),
                        'positive_n': sum(1 for v in valid_vals if v > 0),
                    }
                )
                metrics_out[m][kind] = s
        return metrics_out

    grouped_same: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta_by_key: dict[str, dict[str, Any]] = {}
    for r in same_records:
        key, cid, cname, id_zero = resolve_coach(r)
        grouped_same[key].append(r)
        meta_by_key[key] = {
            'coach_key': key,
            'coach_id': cid,
            'coach_name': cname,
            'provider_id_zero': id_zero,
        }

    grouped_all: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in all_records:
        key, cid, cname, id_zero = resolve_coach(r)
        grouped_all[key].append(r)
        meta_by_key.setdefault(
            key,
            {
                'coach_key': key,
                'coach_id': cid,
                'coach_name': cname,
                'provider_id_zero': id_zero,
            },
        )

    regimes = []
    for key, rows in grouped_same.items():
        ordered = sorted(rows, key=lambda x: str(x.get('kickoff_utc', '')), reverse=True)
        meta = meta_by_key[key]
        all_rows = sorted(grouped_all.get(key, []), key=lambda x: str(x.get('kickoff_utc', '')), reverse=True)
        formations = Counter(r.get('formation') for r in ordered if r.get('formation'))
        venues = Counter(r.get('venue') for r in ordered if r.get('venue'))
        identity_status = (
            'UNRECORDED_LINEUP_NOT_IN_DETAILED_CACHE'
            if meta['coach_id'] is None
            else 'PROVIDER_COACH_ID_ZERO_UNLINKED_POSSIBLE_ALIAS_DO_NOT_AUTO_MERGE'
            if meta['provider_id_zero']
            else 'PROVIDER_RECORDED_COACH_ID_NOT_CONFIRMED_CURRENT'
        )
        regimes.append(
            {
                **meta,
                'identity_status': identity_status,
                'same_competition_n': len(ordered),
                'same_competition_fixture_ids': [r['fixture_id'] for r in ordered],
                'all_competitions_n': len(all_rows),
                'all_competitions_fixture_ids': [r['fixture_id'] for r in all_rows],
                'first_kickoff_utc': ordered[-1].get('kickoff_utc') if ordered else None,
                'last_kickoff_utc': ordered[0].get('kickoff_utc') if ordered else None,
                'venues': dict(sorted(venues.items())),
                'formations_observed': dict(sorted(formations.items())),
                'metrics': summarize_rows(ordered),
            }
        )

    regimes.sort(
        key=lambda x: (
            x['coach_id'] is not None,
            str(x.get('last_kickoff_utc') or ''),
            x['same_competition_n'],
        ),
        reverse=True,
    )

    recorded_regimes = [r for r in regimes if r['coach_id'] is not None]
    positive_regimes = [r for r in recorded_regimes if r['coach_id'] and r['coach_id'] > 0]
    zero_id_regimes = [r for r in recorded_regimes if r['provider_id_zero']]

    most_recent = recorded_regimes[0] if recorded_regimes else None
    contrast = None
    if len(positive_regimes) >= 2:
        current_reg = positive_regimes[0]
        prior_ids = set(
            fid for reg in positive_regimes[1:] for fid in reg['same_competition_fixture_ids']
        )
        prior_rows = [r for r in same_records if r['fixture_id'] in prior_ids]
        prior_metrics = summarize_rows(prior_rows)
        metric_deltas = {}
        for m in ('goals', 'xg', 'shots', 'on_target', 'corners', 'fouls', 'yellow', 'offsides', 'possession_pct'):
            c_own = current_reg['metrics'][m]['own']['mean']
            p_own = prior_metrics[m]['own']['mean']
            c_con = current_reg['metrics'][m]['conceded']['mean']
            p_con = prior_metrics[m]['conceded']['mean']
            metric_deltas[m] = {
                'recent_coach_own_mean': c_own,
                'prior_coaches_own_mean': p_own,
                'own_delta': round(c_own - p_own, 4) if finite_fn(c_own) and finite_fn(p_own) else None,
                'recent_coach_conceded_mean': c_con,
                'prior_coaches_conceded_mean': p_con,
                'conceded_delta': round(c_con - p_con, 4) if finite_fn(c_con) and finite_fn(p_con) else None,
            }
        contrast = {
            'recent_coach_key': current_reg['coach_key'],
            'recent_coach_id': current_reg['coach_id'],
            'recent_coach_name': current_reg['coach_name'],
            'recent_coach_n': current_reg['same_competition_n'],
            'recent_coach_fixture_ids': current_reg['same_competition_fixture_ids'],
            'prior_positive_coaches': [
                {
                    'coach_key': reg['coach_key'],
                    'coach_id': reg['coach_id'],
                    'coach_name': reg['coach_name'],
                    'n': reg['same_competition_n'],
                    'fixture_ids': reg['same_competition_fixture_ids'],
                }
                for reg in positive_regimes[1:]
            ],
            'prior_coaches_combined_n': len(prior_rows),
            'metric_deltas': metric_deltas,
            'warning': (
                'RECENT_COACH_SAMPLE_SMALL_DO_NOT_PROJECT_PRIOR_REGIME_AS_STABLE'
                if current_reg['same_competition_n'] < 5
                else 'MULTIPLE_COACH_REGIMES_OBSERVED_IN_SEASON'
            ),
        }

    if len(positive_regimes) >= 2:
        regime_state = 'MULTIPLE_POSITIVE_COACH_IDS_RECORDED_REGIME_SPLIT_REQUIRED'
    elif len(positive_regimes) == 1 and zero_id_regimes:
        regime_state = 'SINGLE_POSITIVE_COACH_ID_PLUS_UNLINKED_ID_ZERO_ENTRY'
    elif len(positive_regimes) == 1:
        regime_state = 'SINGLE_RECORDED_COACH_ID_IN_DETAILED_SAMPLE'
    elif zero_id_regimes:
        regime_state = 'ONLY_UNLINKED_COACH_ID_ZERO_RECORDED'
    else:
        regime_state = 'NO_COACH_LINEUPS_RECORDED_IN_SAMPLE'

    return {
        'team_id': team_id,
        'regime_state': regime_state,
        'same_competition_total_n': len(same_records),
        'recorded_lineup_matches_n': sum(r['same_competition_n'] for r in recorded_regimes),
        'unrecorded_lineup_matches_n': sum(
            r['same_competition_n'] for r in regimes if r['coach_id'] is None
        ),
        'distinct_positive_coach_ids': [r['coach_id'] for r in positive_regimes],
        'has_unlinked_zero_coach_id': bool(zero_id_regimes),
        'multiple_positive_coaches_detected': len(positive_regimes) >= 2,
        'most_recent_recorded_coach': {
            'coach_key': most_recent['coach_key'],
            'coach_id': most_recent['coach_id'],
            'coach_name': most_recent['coach_name'],
            'same_competition_n': most_recent['same_competition_n'],
            'fixture_ids': most_recent['same_competition_fixture_ids'],
            'last_kickoff_utc': most_recent['last_kickoff_utc'],
        }
        if most_recent
        else None,
        'regimes': regimes,
        'recent_vs_prior_coach_contrast': contrast,
        'governance_note': (
            'Provider coach IDs are separated descriptively; id=0 entries are flagged as unlinked '
            'provider records and never merged automatically by name similarity.'
        ),
    }


def enrich_signal_diagnostics(signal: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    """Attach explicit explanation of stability/contradictions and own-vs-conceded cross-analysis."""
    side = signal['team_side']
    opposite = 'away' if side == 'home' else 'home'
    acting_name = fixture.get(side) or str(fixture.get(side + '_id'))
    defending_name = fixture.get(opposite) or str(fixture.get(opposite + '_id'))
    venue = 'HOME' if side == 'home' else 'AWAY'
    opp_venue = 'AWAY' if venue == 'HOME' else 'HOME'

    own = signal['own']
    conceded = signal['opponent_conceded']
    own_cv = own.get('cv')
    con_cv = conceded.get('cv')
    cvs = [v for v in (own_cv, con_cv) if is_finite_non_negative(v)]
    max_cv = max(cvs) if len(cvs) == 2 else None
    unstable_sides = []
    if is_finite_non_negative(own_cv) and own_cv > 0.8:
        unstable_sides.append('own')
    if is_finite_non_negative(con_cv) and con_cv > 0.8:
        unstable_sides.append('opponent_conceded')

    contradiction_details = []
    for code in signal.get('contradictions', []):
        if code == 'OWN_PRODUCTION_VS_RIVAL_CONCESSION_DISAGREE':
            om, cm = own.get('mean'), conceded.get('mean')
            gap = round(om - cm, 4) if is_finite_non_negative(om) and is_finite_non_negative(cm) else None
            agr = round(signal['components'].get('agreement', 0.0), 4)
            contradiction_details.append(
                {
                    'code': code,
                    'own_mean': om,
                    'opponent_conceded_mean': cm,
                    'own_minus_conceded_gap': gap,
                    'agreement': agr,
                    'threshold': 0.60,
                    'higher_side': 'own' if (gap or 0) > 0 else 'opponent_conceded',
                    'explanation_es': (
                        f"Desacuerdo entre producción propia de {acting_name} (media={om}, n={own.get('n')}) "
                        f"y concesión rival de {defending_name} (media={cm}, n={conceded.get('n')}): "
                        f"acuerdo={agr} (< 0.60, brecha={gap})."
                    ),
                }
            )
        elif code == 'OPPOSITE_RECENT_TRENDS':
            ot = own.get('trend_relative')
            ct = conceded.get('trend_relative')
            contradiction_details.append(
                {
                    'code': code,
                    'own_recent_l5_mean': (own.get('recent') or {}).get('mean'),
                    'own_previous_l5_mean': (own.get('previous') or {}).get('mean'),
                    'own_trend_relative': round(ot, 4) if ot is not None else None,
                    'conceded_recent_l5_mean': (conceded.get('recent') or {}).get('mean'),
                    'conceded_previous_l5_mean': (conceded.get('previous') or {}).get('mean'),
                    'conceded_trend_relative': round(ct, 4) if ct is not None else None,
                    'threshold_abs': 0.25,
                    'explanation_es': (
                        f"Tendencias recientes opuestas (L5 vs L5 previo): {acting_name} propio "
                        f"{(own.get('recent') or {}).get('mean')} vs {(own.get('previous') or {}).get('mean')} "
                        f"(tendencia={round(ot, 4) if ot is not None else None}) frente a {defending_name} concedido "
                        f"{(conceded.get('recent') or {}).get('mean')} vs {(conceded.get('previous') or {}).get('mean')} "
                        f"(tendencia={round(ct, 4) if ct is not None else None})."
                    ),
                }
            )
        elif code == 'VENUE_VS_GLOBAL_DISAGREE':
            divergent_sides = []
            for label_key, block, v_label, team_label in (
                ('own', own, venue, acting_name),
                ('opponent_conceded', conceded, opp_venue, defending_name),
            ):
                vn = (block.get('venue') or {}).get('n', 0)
                vm = (block.get('venue') or {}).get('mean')
                gm = block.get('mean')
                if vn >= 3 and is_finite_non_negative(vm) and is_finite_non_negative(gm):
                    rel = abs(vm - gm) / max(gm, 1)
                    if rel > 0.4:
                        divergent_sides.append(
                            {
                                'series': label_key,
                                'team': team_label,
                                'venue': v_label,
                                'venue_n': vn,
                                'venue_mean': vm,
                                'global_n': block.get('n'),
                                'global_mean': gm,
                                'relative_divergence': round(rel, 4),
                            }
                        )
            contradiction_details.append(
                {
                    'code': code,
                    'threshold_relative': 0.40,
                    'divergent_series': divergent_sides,
                    'explanation_es': (
                        'Divergencia entre media por localía y media global (>40%): '
                        + '; '.join(
                            f"{d['team']} ({d['series']} en {d['venue']}: {d['venue_mean']} [n={d['venue_n']}] vs global {d['global_mean']} [n={d['global_n']}], div={d['relative_divergence']})"
                            for d in divergent_sides
                        )
                    ),
                }
            )

    status = signal['status']
    if status == 'SUPPORTED_DESCRIPTIVE_SIGNAL':
        summary_es = (
            f"Señal descriptiva estable y sin contradicciones: {acting_name} propio media={own.get('mean')} "
            f"(CV={own_cv}, n={own.get('n')}/{own.get('expected_n')}/{own.get('history_n')}) vs "
            f"{defending_name} concedido media={conceded.get('mean')} (CV={con_cv}, n={conceded.get('n')}/{conceded.get('expected_n')}/{conceded.get('history_n')}); "
            f"score={signal['priority_score']}."
        )
    elif status == 'UNSTABLE_PATTERN':
        culprits = ', '.join(
            f"{acting_name} propio (CV={own_cv})"
            if s == 'own'
            else f"{defending_name} concedido (CV={con_cv})"
            for s in unstable_sides
        )
        summary_es = (
            f"Patrón inestable (max CV={max_cv} > 0.80) originado en: {culprits}. "
            f"Aunque el otro lado pueda tener menor dispersión, la alta variabilidad impide tratar la media "
            f"({own.get('mean')} vs {conceded.get('mean')}) como punto fijo."
        )
    elif status == 'CONTRADICTORY_SIGNAL':
        summary_es = (
            f"Señal contradictoria ({', '.join(signal.get('contradictions', []))}): "
            + ' '.join(d['explanation_es'] for d in contradiction_details)
        )
    elif status == 'PARTIAL_EVIDENCE':
        summary_es = (
            f"Evidencia parcial (score={signal['priority_score']}<55 o cobertura efectiva<50%): "
            f"{acting_name} propio n={own.get('n')}/{own.get('history_n')} ({own.get('historical_effective_coverage_pct')}%) vs "
            f"{defending_name} concedido n={conceded.get('n')}/{conceded.get('history_n')} ({conceded.get('historical_effective_coverage_pct')}%)."
        )
    elif status == 'SMALL_SAMPLE':
        summary_es = (
            f"Muestra insuficiente (n mínimo={min(own.get('n', 0), conceded.get('n', 0))} < 4) "
            f"para inferencia táctica robusta."
        )
    else:
        summary_es = f"Estado {status}: sin datos válidos suficientes (n={own.get('n', 0)}/{conceded.get('n', 0)})."

    signal['status_explanation'] = {
        'status': status,
        'stability_class': (
            'STABLE'
            if max_cv is not None and max_cv <= 0.8
            else 'UNSTABLE'
            if max_cv is not None and max_cv > 0.8
            else 'VARIABILITY_UNAVAILABLE'
        ),
        'cv_threshold': 0.80,
        'own_cv': own_cv,
        'opponent_conceded_cv': con_cv,
        'max_cv': max_cv,
        'unstable_sides': unstable_sides,
        'contradiction_details': contradiction_details,
        'summary_es': summary_es,
    }

    om, cm = own.get('mean'), conceded.get('mean')
    ovm, cvm = (own.get('venue') or {}).get('mean'), (conceded.get('venue') or {}).get('mean')
    orm, crm = (own.get('recent') or {}).get('mean'), (conceded.get('recent') or {}).get('mean')

    signal['cross_comparison'] = {
        'acting_team': acting_name,
        'acting_team_id': fixture.get(side + '_id'),
        'defending_team': defending_name,
        'defending_team_id': fixture.get(opposite + '_id'),
        'acting_venue': venue,
        'defending_venue': opp_venue,
        'global_ft': {
            'own_n': own.get('n'),
            'own_expected_n': own.get('expected_n'),
            'own_history_n': own.get('history_n'),
            'own_zero_n': own.get('zero_n'),
            'own_missing_n': own.get('missing_n'),
            'own_mean': om,
            'own_stdev': own.get('stdev'),
            'own_cv': own_cv,
            'conceded_n': conceded.get('n'),
            'conceded_expected_n': conceded.get('expected_n'),
            'conceded_history_n': conceded.get('history_n'),
            'conceded_zero_n': conceded.get('zero_n'),
            'conceded_missing_n': conceded.get('missing_n'),
            'conceded_mean': cm,
            'conceded_stdev': conceded.get('stdev'),
            'conceded_cv': con_cv,
            'combined_midpoint_descriptive': (
                round((om + cm) / 2, 4)
                if is_finite_non_negative(om) and is_finite_non_negative(cm)
                else None
            ),
            'own_minus_conceded_gap': (
                round(om - cm, 4)
                if is_finite_non_negative(om) and is_finite_non_negative(cm)
                else None
            ),
            'agreement': round(signal['components'].get('agreement', 0.0), 4),
        },
        'comparable_locality': {
            'own_venue': venue,
            'own_venue_n': (own.get('venue') or {}).get('n'),
            'own_venue_history_n': (own.get('venue') or {}).get('history_n'),
            'own_venue_mean': ovm,
            'own_venue_cv': (own.get('venue') or {}).get('cv'),
            'conceded_venue': opp_venue,
            'conceded_venue_n': (conceded.get('venue') or {}).get('n'),
            'conceded_venue_history_n': (conceded.get('venue') or {}).get('history_n'),
            'conceded_venue_mean': cvm,
            'conceded_venue_cv': (conceded.get('venue') or {}).get('cv'),
            'locality_midpoint_descriptive': (
                round((ovm + cvm) / 2, 4)
                if is_finite_non_negative(ovm) and is_finite_non_negative(cvm)
                else None
            ),
            'locality_gap': (
                round(ovm - cvm, 4)
                if is_finite_non_negative(ovm) and is_finite_non_negative(cvm)
                else None
            ),
        },
        'recent_l5_vs_previous_l5': {
            'own_recent_n': (own.get('recent') or {}).get('n'),
            'own_recent_mean': orm,
            'own_previous_n': (own.get('previous') or {}).get('n'),
            'own_previous_mean': (own.get('previous') or {}).get('mean'),
            'own_trend_relative': (
                round(own['trend_relative'], 4) if own.get('trend_relative') is not None else None
            ),
            'conceded_recent_n': (conceded.get('recent') or {}).get('n'),
            'conceded_recent_mean': crm,
            'conceded_previous_n': (conceded.get('previous') or {}).get('n'),
            'conceded_previous_mean': (conceded.get('previous') or {}).get('mean'),
            'conceded_trend_relative': (
                round(conceded['trend_relative'], 4)
                if conceded.get('trend_relative') is not None
                else None
            ),
            'recent_midpoint_descriptive': (
                round((orm + crm) / 2, 4)
                if is_finite_non_negative(orm) and is_finite_non_negative(crm)
                else None
            ),
        },
        'statistical_notice': 'DESCRIPTIVE_CROSS_COMPARISON_ONLY_NOT_CALIBRATED_PROBABILITY_OR_FAIR_LINE',
    }
    return signal


def build_matchup_cross_matrix(
    fixture: dict[str, Any],
    home_profile: dict[str, Any],
    away_profile: dict[str, Any],
    signals: list[dict[str, Any]],
    stats_fn=basic_stats,
    finite_fn=is_finite_non_negative,
) -> dict[str, Any]:
    """Cross own production vs rival concession across all metrics, venues, windows, periods, and coaches."""
    sig_index = {(s['team_side'], s['metric']): s for s in signals}
    home_name = fixture.get('home') or str(fixture.get('home_id'))
    away_name = fixture.get('away') or str(fixture.get('away_id'))

    def get_metric_series(profile: dict[str, Any], metric: str, kind: str) -> dict[str, Any]:
        if metric in profile.get('metrics', {}):
            return profile['metrics'][metric][kind]
        if metric in profile.get('period_metrics', {}):
            return profile['period_metrics'][metric][kind]
        if metric in profile.get('support_metrics', {}):
            return profile['support_metrics'][metric][kind]
        return {'n': 0, 'mean': None, 'cv': None, 'venue': {'n': 0, 'mean': None}}

    def observed_match_totals(profile: dict[str, Any], metric: str, venue_filter: str | None = None) -> dict[str, Any]:
        same_comp = [
            r
            for r in profile.get('observations', [])
            if r.get('league_id') == profile.get('league_id')
            and r.get('season') == profile.get('season')
            and (venue_filter is None or r.get('venue') == venue_filter)
        ]
        totals = []
        for r in same_comp:
            a = r.get('own', {}).get(metric)
            b = r.get('conceded', {}).get(metric)
            if finite_fn(a) and finite_fn(b):
                totals.append(float(a + b))
        return stats_fn(totals)

    by_metric: dict[str, Any] = {}
    for m in EXTENDED_METRICS:
        h_own = get_metric_series(home_profile, m, 'own')
        a_con = get_metric_series(away_profile, m, 'conceded')
        a_own = get_metric_series(away_profile, m, 'own')
        h_con = get_metric_series(home_profile, m, 'conceded')

        h_sig = sig_index.get(('home', m))
        a_sig = sig_index.get(('away', m))

        hom, acm = h_own.get('mean'), a_con.get('mean')
        aom, hcm = a_own.get('mean'), h_con.get('mean')
        h_mid = round((hom + acm) / 2, 4) if finite_fn(hom) and finite_fn(acm) else None
        a_mid = round((aom + hcm) / 2, 4) if finite_fn(aom) and finite_fn(hcm) else None

        hovm = (h_own.get('venue') or {}).get('mean')
        acvm = (a_con.get('venue') or {}).get('mean')
        aovm = (a_own.get('venue') or {}).get('mean')
        hcvm = (h_con.get('venue') or {}).get('mean')
        h_loc_mid = round((hovm + acvm) / 2, 4) if finite_fn(hovm) and finite_fn(acvm) else None
        a_loc_mid = round((aovm + hcvm) / 2, 4) if finite_fn(aovm) and finite_fn(hcvm) else None

        h_rec_reg = ((home_profile.get('coach_regime_summary') or {}).get('regimes') or [None])[0]
        a_rec_reg = ((away_profile.get('coach_regime_summary') or {}).get('regimes') or [None])[0]
        h_coach_own = (
            h_rec_reg['metrics'][m]['own']['mean']
            if h_rec_reg and h_rec_reg.get('coach_id') is not None and m in h_rec_reg.get('metrics', {})
            else None
        )
        a_coach_con = (
            a_rec_reg['metrics'][m]['conceded']['mean']
            if a_rec_reg and a_rec_reg.get('coach_id') is not None and m in a_rec_reg.get('metrics', {})
            else None
        )
        a_coach_own = (
            a_rec_reg['metrics'][m]['own']['mean']
            if a_rec_reg and a_rec_reg.get('coach_id') is not None and m in a_rec_reg.get('metrics', {})
            else None
        )
        h_coach_con = (
            h_rec_reg['metrics'][m]['conceded']['mean']
            if h_rec_reg and h_rec_reg.get('coach_id') is not None and m in h_rec_reg.get('metrics', {})
            else None
        )

        by_metric[m] = {
            'metric': m,
            'label_es': METRIC_LABELS_ES.get(m, m),
            'home_attack_vs_away_defense': {
                'home_team': home_name,
                'away_team': away_name,
                'signal_status': h_sig['status'] if h_sig else 'NOT_SIGNAL_METRIC',
                'priority_score': h_sig['priority_score'] if h_sig else None,
                'contradictions': h_sig['contradictions'] if h_sig else [],
                'home_own_n': h_own.get('n'),
                'home_own_expected_n': h_own.get('expected_n'),
                'home_own_history_n': h_own.get('history_n'),
                'home_own_zero_n': h_own.get('zero_n'),
                'home_own_missing_n': h_own.get('missing_n'),
                'home_own_mean': hom,
                'home_own_stdev': h_own.get('stdev'),
                'home_own_cv': h_own.get('cv'),
                'away_conceded_n': a_con.get('n'),
                'away_conceded_expected_n': a_con.get('expected_n'),
                'away_conceded_history_n': a_con.get('history_n'),
                'away_conceded_zero_n': a_con.get('zero_n'),
                'away_conceded_missing_n': a_con.get('missing_n'),
                'away_conceded_mean': acm,
                'away_conceded_stdev': a_con.get('stdev'),
                'away_conceded_cv': a_con.get('cv'),
                'global_midpoint_descriptive': h_mid,
                'global_gap_own_minus_conceded': (
                    round(hom - acm, 4) if finite_fn(hom) and finite_fn(acm) else None
                ),
                'home_venue_n': (h_own.get('venue') or {}).get('n'),
                'home_venue_mean': hovm,
                'away_venue_n': (a_con.get('venue') or {}).get('n'),
                'away_venue_mean': acvm,
                'locality_midpoint_descriptive': h_loc_mid,
                'home_recent_l5_mean': (h_own.get('recent') or {}).get('mean'),
                'away_conceded_recent_l5_mean': (a_con.get('recent') or {}).get('mean'),
                'home_latest_coach_own_mean': h_coach_own,
                'away_latest_coach_conceded_mean': a_coach_con,
            },
            'away_attack_vs_home_defense': {
                'away_team': away_name,
                'home_team': home_name,
                'signal_status': a_sig['status'] if a_sig else 'NOT_SIGNAL_METRIC',
                'priority_score': a_sig['priority_score'] if a_sig else None,
                'contradictions': a_sig['contradictions'] if a_sig else [],
                'away_own_n': a_own.get('n'),
                'away_own_expected_n': a_own.get('expected_n'),
                'away_own_history_n': a_own.get('history_n'),
                'away_own_zero_n': a_own.get('zero_n'),
                'away_own_missing_n': a_own.get('missing_n'),
                'away_own_mean': aom,
                'away_own_stdev': a_own.get('stdev'),
                'away_own_cv': a_own.get('cv'),
                'home_conceded_n': h_con.get('n'),
                'home_conceded_expected_n': h_con.get('expected_n'),
                'home_conceded_history_n': h_con.get('history_n'),
                'home_conceded_zero_n': h_con.get('zero_n'),
                'home_conceded_missing_n': h_con.get('missing_n'),
                'home_conceded_mean': hcm,
                'home_conceded_stdev': h_con.get('stdev'),
                'home_conceded_cv': h_con.get('cv'),
                'global_midpoint_descriptive': a_mid,
                'global_gap_own_minus_conceded': (
                    round(aom - hcm, 4) if finite_fn(aom) and finite_fn(hcm) else None
                ),
                'away_venue_n': (a_own.get('venue') or {}).get('n'),
                'away_venue_mean': aovm,
                'home_venue_n': (h_con.get('venue') or {}).get('n'),
                'home_venue_mean': hcvm,
                'locality_midpoint_descriptive': a_loc_mid,
                'away_recent_l5_mean': (a_own.get('recent') or {}).get('mean'),
                'home_conceded_recent_l5_mean': (h_con.get('recent') or {}).get('mean'),
                'away_latest_coach_own_mean': a_coach_own,
                'home_latest_coach_conceded_mean': h_coach_con,
            },
            'combined_match_totals_descriptive': {
                'sum_of_global_midpoints_descriptive': (
                    round(h_mid + a_mid, 4)
                    if m != 'possession_pct' and finite_fn(h_mid) and finite_fn(a_mid)
                    else None
                ),
                'sum_of_locality_midpoints_descriptive': (
                    round(h_loc_mid + a_loc_mid, 4)
                    if m != 'possession_pct' and finite_fn(h_loc_mid) and finite_fn(a_loc_mid)
                    else None
                ),
                'home_matches_observed_total': (
                    observed_match_totals(home_profile, m) if m != 'possession_pct' else None
                ),
                'away_matches_observed_total': (
                    observed_match_totals(away_profile, m) if m != 'possession_pct' else None
                ),
                'home_venue_matches_observed_total': (
                    observed_match_totals(home_profile, m, 'HOME') if m != 'possession_pct' else None
                ),
                'away_venue_matches_observed_total': (
                    observed_match_totals(away_profile, m, 'AWAY') if m != 'possession_pct' else None
                ),
            },
        }

    return {
        'fixture_id': fixture.get('fixture_id'),
        'home_team': home_name,
        'home_id': fixture.get('home_id'),
        'away_team': away_name,
        'away_id': fixture.get('away_id'),
        'league_id': fixture.get('league_id'),
        'season': fixture.get('season'),
        'metrics': by_metric,
        'methodology_warning': (
            'DESCRIPTIVE_CROSS_MATRIX_ONLY: Midpoints and observed match totals summarize historical '
            'distributions; they are never calibrated probabilities (p_model), fair odds, or EV.'
        ),
    }


def build_tactical_scenarios(
    fixture: dict[str, Any],
    home_profile: dict[str, Any],
    away_profile: dict[str, Any],
    signals: list[dict[str, Any]],
    cross_matrix: dict[str, Any],
) -> dict[str, Any]:
    """Generate opposing tactical scenarios and falsifiers strictly when empirical sample n >= 4."""
    home_name = fixture.get('home') or str(fixture.get('home_id'))
    away_name = fixture.get('away') or str(fixture.get('away_id'))
    asof_str = fixture.get('analysis_cutoff_utc') or 'PREMATCH_CUTOFF'
    scenarios = []
    omitted = []

    sig_by_key = {(s['team_side'], s['metric']): s for s in signals}
    cm_metrics = cross_matrix.get('metrics', {})

    # 1. Goals & Half Distribution (1H vs 2H + FT Goals/xG)
    h_half = (home_profile.get('period_metrics') or {}).get('half_comparison', {}).get('match_total', {})
    a_half = (away_profile.get('period_metrics') or {}).get('half_comparison', {}).get('match_total', {})
    h_goals_sig = sig_by_key.get(('home', 'goals'))
    a_goals_sig = sig_by_key.get(('away', 'goals'))
    if (
        h_goals_sig
        and a_goals_sig
        and min(h_goals_sig['own']['n'], a_goals_sig['opponent_conceded']['n']) >= 4
        and min(a_goals_sig['own']['n'], h_goals_sig['opponent_conceded']['n']) >= 4
    ):
        g_cross = cm_metrics.get('goals', {})
        xg_cross = cm_metrics.get('xg', {})
        h_obs = [
            r
            for r in home_profile.get('observations', [])
            if r.get('league_id') == home_profile.get('league_id')
            and r.get('season') == home_profile.get('season')
        ]
        a_obs = [
            r
            for r in away_profile.get('observations', [])
            if r.get('league_id') == away_profile.get('league_id')
            and r.get('season') == away_profile.get('season')
        ]
        low_goal_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('goals'))
            and is_finite_non_negative(r.get('conceded', {}).get('goals'))
            and (r['own']['goals'] + r['conceded']['goals']) <= 2
        ]
        high_goal_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('goals'))
            and is_finite_non_negative(r.get('conceded', {}).get('goals'))
            and (r['own']['goals'] + r['conceded']['goals']) >= 3
        ]
        h1_heavy_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('goals_h1'))
            and is_finite_non_negative(r.get('conceded', {}).get('goals_h1'))
            and is_finite_non_negative(r.get('own', {}).get('goals_h2'))
            and is_finite_non_negative(r.get('conceded', {}).get('goals_h2'))
            and (r['own']['goals_h1'] + r['conceded']['goals_h1'])
            > (r['own']['goals_h2'] + r['conceded']['goals_h2'])
        ]
        h2_heavy_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('goals_h1'))
            and is_finite_non_negative(r.get('conceded', {}).get('goals_h1'))
            and is_finite_non_negative(r.get('own', {}).get('goals_h2'))
            and is_finite_non_negative(r.get('conceded', {}).get('goals_h2'))
            and (r['own']['goals_h2'] + r['conceded']['goals_h2'])
            > (r['own']['goals_h1'] + r['conceded']['goals_h1'])
        ]

        h_h1_mean = h_half.get('total_h1_mean')
        h_h2_mean = h_half.get('total_h2_mean')
        a_h1_mean = a_half.get('total_h1_mean')
        a_h2_mean = a_half.get('total_h2_mean')
        tot_mid = g_cross.get('combined_match_totals_descriptive', {}).get(
            'sum_of_global_midpoints_descriptive'
        )
        loc_mid = g_cross.get('combined_match_totals_descriptive', {}).get(
            'sum_of_locality_midpoints_descriptive'
        )
        xg_mid = xg_cross.get('combined_match_totals_descriptive', {}).get(
            'sum_of_global_midpoints_descriptive'
        )

        scenarios.append(
            {
                'scenario_id': f"{fixture['fixture_id']}:SCENARIO_GOALS_AND_HALVES",
                'family': 'goals_and_half_distribution',
                'title': f"Ritmo de goles FT y asimetría temporal 1H vs 2H ({home_name} vs {away_name})",
                'epistemic_status': (
                    'UNSTABLE_HIGH_VARIANCE'
                    if 'UNSTABLE_PATTERN' in (h_goals_sig['status'], a_goals_sig['status'])
                    else 'SUPPORTED_DESCRIPTIVE'
                ),
                'evidence_chain': (
                    f"CORTE {asof_str} -> HECHO OBSERVADO: Punto medio global FT={tot_mid} goles "
                    f"(localía={loc_mid}, xG combinado={xg_mid}); goles totales por mitad en partidos de "
                    f"{home_name} (1H={h_h1_mean} vs 2H={h_h2_mean}, n={h_half.get('paired_n')}) y de "
                    f"{away_name} (1H={a_h1_mean} vs 2H={a_h2_mean}, n={a_half.get('paired_n')}), pero con "
                    f"dispersión alta en concesión/producción (CV goles home={h_goals_sig['own']['cv']}/{h_goals_sig['opponent_conceded']['cv']}, "
                    f"away={a_goals_sig['own']['cv']}/{a_goals_sig['opponent_conceded']['cv']}) -> "
                    f"HIPÓTESIS PRINCIPAL: Tramo inicial contenido (1H de tanteo y control de transición) "
                    f"con mayor apertura de espacios y volumen de goles en la 2H o marcador FT moderado -> "
                    f"ESCENARIO CONTRAPUESTO: Ruptura temprana del guion (gol en primeros 20', penalti o "
                    f"sobreconversión respecto a xG) que estira bloques desde el 1H y reproduce las colas "
                    f"de 3+ goles observadas en la muestra -> EVENTO MEDIBLE: Goles en 1H vs 2H y total FT -> "
                    f"CONTRAEVIDENCIA HISTÓRICA: Partidos con >=3 goles FT {high_goal_fids[:6]} y partidos con 1H > 2H {h1_heavy_fids[:6]} -> "
                    f"FALSADOR: Gol antes del minuto 20 o >=2 goles al descanso (1H)."
                ),
                'primary_hypothesis': (
                    f"Contención inicial y mayor peso goleador en la 2ª mitad (1H={h_h1_mean}/{a_h1_mean} vs "
                    f"2H={h_h2_mean}/{a_h2_mean}), con punto medio descriptivo FT de {tot_mid} goles "
                    f"(localía={loc_mid}) y xG combinado={xg_mid}."
                ),
                'counter_scenario': (
                    f"Partido roto temprano o festival de conversión: la alta varianza en goles "
                    f"(CV máx={max(v for v in (h_goals_sig['own']['cv'], h_goals_sig['opponent_conceded']['cv'], a_goals_sig['own']['cv'], a_goals_sig['opponent_conceded']['cv']) if is_finite_non_negative(v))}) "
                    f"refleja que cuando cualquiera encaja pronto el bloque se descompone y se alcanzan 3+ goles FT."
                ),
                'quantitative_basis': {
                    'ft_goals_midpoint_global': tot_mid,
                    'ft_goals_midpoint_locality': loc_mid,
                    'xg_midpoint_global': xg_mid,
                    'home_matches_h1_vs_h2_total_mean': [h_h1_mean, h_h2_mean],
                    'away_matches_h1_vs_h2_total_mean': [a_h1_mean, a_h2_mean],
                    'home_goals_signal_status': h_goals_sig['status'],
                    'away_goals_signal_status': a_goals_sig['status'],
                },
                'supporting_fixture_ids': sorted(set(low_goal_fids + h2_heavy_fids), reverse=True)[:10],
                'counter_evidence_fixture_ids': sorted(set(high_goal_fids + h1_heavy_fids), reverse=True)[:10],
                'falsifier': (
                    'Dos o más goles antes del descanso (1H >= 2), gol en los primeros 15 minutos que obligue '
                    'a abandonar el bloque medio/bajo, o expulsión temprana.'
                ),
                'actionable_bet': False,
            }
        )
    else:
        omitted.append({'family': 'goals_and_half_distribution', 'reason': 'INSUFFICIENT_SAMPLE_BELOW_4'})

    # 2. Shots & Shots on Target Volume vs Block/Concession
    h_shots = sig_by_key.get(('home', 'shots'))
    a_shots = sig_by_key.get(('away', 'shots'))
    h_sot = sig_by_key.get(('home', 'on_target'))
    a_sot = sig_by_key.get(('away', 'on_target'))
    if (
        h_shots
        and a_shots
        and min(h_shots['own']['n'], h_shots['opponent_conceded']['n']) >= 4
        and min(a_shots['own']['n'], a_shots['opponent_conceded']['n']) >= 4
    ):
        s_cross = cm_metrics.get('shots', {})
        sot_cross = cm_metrics.get('on_target', {})
        blk_cross = cm_metrics.get('blocked_shots', {})
        h_obs = [
            r
            for r in home_profile.get('observations', [])
            if r.get('league_id') == home_profile.get('league_id')
            and r.get('season') == home_profile.get('season')
        ]
        a_obs = [
            r
            for r in away_profile.get('observations', [])
            if r.get('league_id') == away_profile.get('league_id')
            and r.get('season') == away_profile.get('season')
        ]
        high_shot_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('shots'))
            and is_finite_non_negative(r.get('conceded', {}).get('shots'))
            and (r['own']['shots'] + r['conceded']['shots']) >= 23
        ]
        low_shot_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('shots'))
            and is_finite_non_negative(r.get('conceded', {}).get('shots'))
            and (r['own']['shots'] + r['conceded']['shots']) <= 20
        ]
        s_mid = s_cross.get('combined_match_totals_descriptive', {}).get('sum_of_global_midpoints_descriptive')
        s_loc_mid = s_cross.get('combined_match_totals_descriptive', {}).get('sum_of_locality_midpoints_descriptive')
        sot_mid = sot_cross.get('combined_match_totals_descriptive', {}).get('sum_of_global_midpoints_descriptive')
        sot_loc_mid = sot_cross.get('combined_match_totals_descriptive', {}).get('sum_of_locality_midpoints_descriptive')

        scenarios.append(
            {
                'scenario_id': f"{fixture['fixture_id']}:SCENARIO_SHOTS_AND_ON_TARGET",
                'family': 'shots_and_finishing_volume',
                'title': f"Volumen de remate total y tiros a puerta vs oposición ({home_name} vs {away_name})",
                'epistemic_status': (
                    'SUPPORTED_DESCRIPTIVE'
                    if h_sot and a_sot and h_sot['status'] == 'SUPPORTED_DESCRIPTIVE_SIGNAL'
                    else 'PLAUSIBLE_WITH_CONTRADICTIONS'
                ),
                'evidence_chain': (
                    f"CORTE {asof_str} -> HECHO OBSERVADO: {home_name} produce {h_shots['own']['mean']} tiros "
                    f"(local={h_shots['own']['venue']['mean']}, L5={h_shots['own']['recent']['mean']}) frente a "
                    f"{away_name} que concede {h_shots['opponent_conceded']['mean']} (visitante={h_shots['opponent_conceded']['venue']['mean']}, "
                    f"L5={h_shots['opponent_conceded']['recent']['mean']}); a la inversa, {away_name} produce "
                    f"{a_shots['own']['mean']} tiros (visitante={a_shots['own']['venue']['mean']}) frente a "
                    f"{home_name} que concede {a_shots['opponent_conceded']['mean']} (local={a_shots['opponent_conceded']['venue']['mean']}). "
                    f"En tiros a puerta: {home_name} {h_sot['own']['mean'] if h_sot else None} vs {h_sot['opponent_conceded']['mean'] if h_sot else None} "
                    f"y {away_name} {a_sot['own']['mean'] if a_sot else None} vs {a_sot['opponent_conceded']['mean'] if a_sot else None} -> "
                    f"HIPÓTESIS PRINCIPAL: Volumen agregado de remate en torno a {s_mid} tiros (localía={s_loc_mid}) "
                    f"y {sot_mid} tiros a puerta (localía={sot_loc_mid}), impulsado por el intercambio de ataques exteriores/transiciones -> "
                    f"ESCENARIO CONTRAPUESTO: Neutralización táctica y bloqueo en frontal (tiros bloqueados medios: "
                    f"{home_name}={blk_cross.get('home_attack_vs_away_defense', {}).get('home_own_mean')}, "
                    f"{away_name}={blk_cross.get('away_attack_vs_home_defense', {}).get('away_own_mean')}) o "
                    f"tendencias recientes opuestas ({h_shots['contradictions'] + a_shots['contradictions']}) que deprimen el volumen real -> "
                    f"EVENTO MEDIBLE: Total de tiros y tiros a puerta FT -> "
                    f"CONTRAEVIDENCIA HISTÓRICA: Partidos cerrados <=20 tiros totales {low_shot_fids[:6]} -> "
                    f"FALSADOR: Ritmo de posesión estéril sin profundidad (<8 tiros totales al descanso o bloqueo sistemático en banda)."
                ),
                'primary_hypothesis': (
                    f"Convergencia descriptiva en tiros a puerta ({sot_mid} global / {sot_loc_mid} por localía) "
                    f"y volumen de tiros totales ({s_mid} global / {s_loc_mid} por localía)."
                ),
                'counter_scenario': (
                    f"Fricción táctica o imposición de la tendencia defensiva reciente: en {home_name} y {away_name} "
                    f"existen divergencias de tendencia reciente/localía ({sorted(set(h_shots['contradictions'] + a_shots['contradictions']))}) "
                    f"que pueden comprimir el partido por debajo de 20 remates totales."
                ),
                'quantitative_basis': {
                    'shots_midpoint_global': s_mid,
                    'shots_midpoint_locality': s_loc_mid,
                    'on_target_midpoint_global': sot_mid,
                    'on_target_midpoint_locality': sot_loc_mid,
                    'home_shots_status': h_shots['status'],
                    'away_shots_status': a_shots['status'],
                    'home_on_target_status': h_sot['status'] if h_sot else None,
                    'away_on_target_status': a_sot['status'] if a_sot else None,
                },
                'supporting_fixture_ids': sorted(set(high_shot_fids), reverse=True)[:10],
                'counter_evidence_fixture_ids': sorted(set(low_shot_fids), reverse=True)[:10],
                'falsifier': (
                    'Menos de 8 tiros totales combinados al descanso (1H) o dominio posicional (>65%) de un lado '
                    'frente a bloque bajo sin conceder transiciones ni remates claros.'
                ),
                'actionable_bet': False,
            }
        )
    else:
        omitted.append({'family': 'shots_and_finishing_volume', 'reason': 'INSUFFICIENT_SAMPLE_BELOW_4'})

    # 3. Corners: Asymmetry & High Variance
    h_cor = sig_by_key.get(('home', 'corners'))
    a_cor = sig_by_key.get(('away', 'corners'))
    if (
        h_cor
        and a_cor
        and min(h_cor['own']['n'], h_cor['opponent_conceded']['n']) >= 4
        and min(a_cor['own']['n'], a_cor['opponent_conceded']['n']) >= 4
    ):
        c_cross = cm_metrics.get('corners', {})
        h_obs = [
            r
            for r in home_profile.get('observations', [])
            if r.get('league_id') == home_profile.get('league_id')
            and r.get('season') == home_profile.get('season')
        ]
        a_obs = [
            r
            for r in away_profile.get('observations', [])
            if r.get('league_id') == away_profile.get('league_id')
            and r.get('season') == away_profile.get('season')
        ]
        low_cor_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('corners'))
            and is_finite_non_negative(r.get('conceded', {}).get('corners'))
            and (r['own']['corners'] + r['conceded']['corners']) <= 8
        ]
        high_cor_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('corners'))
            and is_finite_non_negative(r.get('conceded', {}).get('corners'))
            and (r['own']['corners'] + r['conceded']['corners']) >= 10
        ]
        c_mid = c_cross.get('combined_match_totals_descriptive', {}).get('sum_of_global_midpoints_descriptive')
        c_loc_mid = c_cross.get('combined_match_totals_descriptive', {}).get('sum_of_locality_midpoints_descriptive')

        scenarios.append(
            {
                'scenario_id': f"{fixture['fixture_id']}:SCENARIO_CORNERS_ASYMMETRY",
                'family': 'corners_and_block_resistance',
                'title': f"Choque entre producción de córners y contención rival ({home_name} vs {away_name})",
                'epistemic_status': (
                    'PLAUSIBLE_WITH_CONTRADICTIONS'
                    if any(s['contradictions'] for s in (h_cor, a_cor))
                    or 'UNSTABLE_PATTERN' in (h_cor['status'], a_cor['status'])
                    else 'SUPPORTED_DESCRIPTIVE'
                ),
                'evidence_chain': (
                    f"CORTE {asof_str} -> HECHO OBSERVADO: {home_name} promedia {h_cor['own']['mean']} córners "
                    f"(local={h_cor['own']['venue']['mean']}, CV={h_cor['own']['cv']}) pero {away_name} concede "
                    f"{h_cor['opponent_conceded']['mean']} (visitante={h_cor['opponent_conceded']['venue']['mean']}, "
                    f"CV={h_cor['opponent_conceded']['cv']}); mientras {away_name} promedia {a_cor['own']['mean']} "
                    f"(visitante={a_cor['own']['venue']['mean']}, CV={a_cor['own']['cv']}) frente a {home_name} que "
                    f"concede {a_cor['opponent_conceded']['mean']} (local={a_cor['opponent_conceded']['venue']['mean']}, "
                    f"CV={a_cor['opponent_conceded']['cv']}). Asociaciones descriptivas córners-tiros bloqueados: "
                    f"home={h_cor.get('volume_associations')}, away={a_cor.get('volume_associations')} -> "
                    f"HIPÓTESIS PRINCIPAL: Moderación del total de saques de esquina (punto medio global={c_mid}, "
                    f"localía={c_loc_mid}) si se impone la baja producción propia del local y la contención exterior visitante -> "
                    f"ESCENARIO CONTRAPUESTO: Asedio por marcador adverso o acumulación de tiros bloqueados/rechaces "
                    f"en banda que dispara la cola alta (>=10 córners) observada en el historial de concesión -> "
                    f"EVENTO MEDIBLE: Córners por equipo y totales FT -> "
                    f"CONTRAEVIDENCIA HISTÓRICA: Partidos con >=10 córners totales {high_cor_fids[:6]} -> "
                    f"FALSADOR: Marcador desigual temprano que fuerce centros laterales continuos (>=6 córners al descanso)."
                ),
                'primary_hypothesis': (
                    f"Tensión entre baja generación propia de {home_name} ({h_cor['own']['mean']}) y concesión "
                    f"de {away_name} ({h_cor['opponent_conceded']['mean']}), con punto medio agregado de "
                    f"{c_mid} córners (localía={c_loc_mid})."
                ),
                'counter_scenario': (
                    f"Desbordamiento por guion de marcador o rechaces: la correlación descriptiva con tiros/bloqueos "
                    f"y la alta dispersión en {away_name} (CV={a_cor['own']['cv']}) permiten picos de >=10 córners si un equipo asedia en bloque bajo."
                ),
                'quantitative_basis': {
                    'corners_midpoint_global': c_mid,
                    'corners_midpoint_locality': c_loc_mid,
                    'home_corners_status': h_cor['status'],
                    'away_corners_status': a_cor['status'],
                    'home_contradictions': h_cor['contradictions'],
                    'away_contradictions': a_cor['contradictions'],
                },
                'supporting_fixture_ids': sorted(set(low_cor_fids), reverse=True)[:10],
                'counter_evidence_fixture_ids': sorted(set(high_cor_fids), reverse=True)[:10],
                'falsifier': (
                    'Seis o más córners en la 1ª mitad (1H >= 6) o guion de asedio unilateral tras gol temprano.'
                ),
                'actionable_bet': False,
            }
        )
    else:
        omitted.append({'family': 'corners_and_block_resistance', 'reason': 'INSUFFICIENT_SAMPLE_BELOW_4'})

    # 4. Fouls & Yellow Cards Discipline
    h_fouls = sig_by_key.get(('home', 'fouls'))
    a_fouls = sig_by_key.get(('away', 'fouls'))
    h_yel = sig_by_key.get(('home', 'yellow'))
    a_yel = sig_by_key.get(('away', 'yellow'))
    if (
        h_fouls
        and a_fouls
        and min(h_fouls['own']['n'], h_fouls['opponent_conceded']['n']) >= 4
        and min(a_fouls['own']['n'], a_fouls['opponent_conceded']['n']) >= 4
    ):
        f_cross = cm_metrics.get('fouls', {})
        y_cross = cm_metrics.get('yellow', {})
        h_obs = [
            r
            for r in home_profile.get('observations', [])
            if r.get('league_id') == home_profile.get('league_id')
            and r.get('season') == home_profile.get('season')
        ]
        a_obs = [
            r
            for r in away_profile.get('observations', [])
            if r.get('league_id') == away_profile.get('league_id')
            and r.get('season') == away_profile.get('season')
        ]
        high_foul_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('fouls'))
            and is_finite_non_negative(r.get('conceded', {}).get('fouls'))
            and (r['own']['fouls'] + r['conceded']['fouls']) >= 26
        ]
        low_foul_fids = [
            r['fixture_id']
            for r in h_obs + a_obs
            if is_finite_non_negative(r.get('own', {}).get('fouls'))
            and is_finite_non_negative(r.get('conceded', {}).get('fouls'))
            and (r['own']['fouls'] + r['conceded']['fouls']) < 26
        ]
        f_mid = f_cross.get('combined_match_totals_descriptive', {}).get('sum_of_global_midpoints_descriptive')
        f_loc_mid = f_cross.get('combined_match_totals_descriptive', {}).get('sum_of_locality_midpoints_descriptive')
        y_mid = y_cross.get('combined_match_totals_descriptive', {}).get('sum_of_global_midpoints_descriptive')
        y_loc_mid = y_cross.get('combined_match_totals_descriptive', {}).get('sum_of_locality_midpoints_descriptive')

        scenarios.append(
            {
                'scenario_id': f"{fixture['fixture_id']}:SCENARIO_DISCIPLINE_FOULS_AND_CARDS",
                'family': 'duels_discipline',
                'title': f"Intensidad de duelos, faltas y tarjetas amarillas ({home_name} vs {away_name})",
                'epistemic_status': (
                    'SUPPORTED_DESCRIPTIVE'
                    if h_fouls['status'] == 'SUPPORTED_DESCRIPTIVE_SIGNAL'
                    and a_fouls['status'] == 'SUPPORTED_DESCRIPTIVE_SIGNAL'
                    else 'PLAUSIBLE_WITH_CONTRADICTIONS'
                ),
                'evidence_chain': (
                    f"CORTE {asof_str} -> HECHO OBSERVADO: Faltas muy consistentes en ambos lados: "
                    f"{home_name} propio={h_fouls['own']['mean']} (CV={h_fouls['own']['cv']}) vs concedido por "
                    f"{away_name}={h_fouls['opponent_conceded']['mean']} (CV={h_fouls['opponent_conceded']['cv']}), y "
                    f"{away_name} propio={a_fouls['own']['mean']} (CV={a_fouls['own']['cv']}) vs concedido por "
                    f"{home_name}={a_fouls['opponent_conceded']['mean']} (CV={a_fouls['opponent_conceded']['cv']}); "
                    f"punto medio FT={f_mid} faltas (localía={f_loc_mid}). En amarillas, punto medio={y_mid} "
                    f"(localía={y_loc_mid}), pero con divergencia por localía en {away_name} "
                    f"({a_yel['contradictions'] if a_yel else []}) y sin modelo arbitral verificado -> "
                    f"HIPÓTESIS PRINCIPAL: Partido de fricción media-alta en mediocampo (~{f_mid} faltas) -> "
                    f"ESCENARIO CONTRAPUESTO: Desacople entre volumen de faltas y sanción disciplinaria (umbral arbitral "
                    f"permisivo o marcador cómodo que reduce faltas tácticas en el tramo final, o bien pico de amarillas "
                    f"visitantes como en la submuestra AWAY de {away_name}) -> "
                    f"EVENTO MEDIBLE: Total de faltas y tarjetas amarillas FT -> "
                    f"CONTRAEVIDENCIA HISTÓRICA: Partidos de baja fricción (<26 faltas) {low_foul_fids[:6]} -> "
                    f"FALSADOR: Menos de 10 faltas al descanso o marcador resuelto por >=2 goles antes del minuto 60."
                ),
                'primary_hypothesis': (
                    f"Estabilidad alta en volumen de faltas ({f_mid} global / {f_loc_mid} por localía, CV <= 0.30 "
                    f"en todas las series), frente a mayor sensibilidad por localía en tarjetas amarillas ({y_mid} global / {y_loc_mid} localía)."
                ),
                'counter_scenario': (
                    f"Divergencia entre faltas y tarjetas: sin historial del árbitro en el snapshot y con "
                    f"contradicción de localía en amarillas ({a_yel['contradictions'] if a_yel else []}), un volumen "
                    f"normal de faltas no garantiza traslación lineal a tarjetas."
                ),
                'quantitative_basis': {
                    'fouls_midpoint_global': f_mid,
                    'fouls_midpoint_locality': f_loc_mid,
                    'yellow_midpoint_global': y_mid,
                    'yellow_midpoint_locality': y_loc_mid,
                    'home_fouls_status': h_fouls['status'],
                    'away_fouls_status': a_fouls['status'],
                    'home_yellow_status': h_yel['status'] if h_yel else None,
                    'away_yellow_status': a_yel['status'] if a_yel else None,
                },
                'supporting_fixture_ids': sorted(set(high_foul_fids), reverse=True)[:10],
                'counter_evidence_fixture_ids': sorted(set(low_foul_fids), reverse=True)[:10],
                'falsifier': (
                    'Ritmo sin interrupciones (<10 faltas combinadas en 1H) o criterio arbitral extremo no modelado.'
                ),
                'actionable_bet': False,
            }
        )
    else:
        omitted.append({'family': 'duels_discipline', 'reason': 'INSUFFICIENT_SAMPLE_BELOW_4'})

    # 5. Coach Regime Transition Scenario (only when evidence of multiple coaches or unlinked IDs exists)
    h_coach_sum = home_profile.get('coach_regime_summary') or {}
    a_coach_sum = away_profile.get('coach_regime_summary') or {}
    if (
        h_coach_sum.get('multiple_positive_coaches_detected')
        or a_coach_sum.get('multiple_positive_coaches_detected')
        or h_coach_sum.get('has_unlinked_zero_coach_id')
        or a_coach_sum.get('has_unlinked_zero_coach_id')
    ):
        a_contrast = a_coach_sum.get('recent_vs_prior_coach_contrast')
        h_contrast = h_coach_sum.get('recent_vs_prior_coach_contrast')
        scenarios.append(
            {
                'scenario_id': f"{fixture['fixture_id']}:SCENARIO_COACH_REGIME_STABILITY",
                'family': 'coach_adaptations',
                'title': f"Sensibilidad por cambios de entrenador y registros de banquillo ({home_name} vs {away_name})",
                'epistemic_status': 'INDETERMINATE_REGIME_CHANGE',
                'evidence_chain': (
                    f"CORTE {asof_str} -> HECHO OBSERVADO: En {home_name} estado de banquillo="
                    f"{h_coach_sum.get('regime_state')} (IDs positivos={h_coach_sum.get('distinct_positive_coach_ids')}, "
                    f"id=0 sin vincular={h_coach_sum.get('has_unlinked_zero_coach_id')}); en {away_name} estado="
                    f"{a_coach_sum.get('regime_state')} (IDs positivos={a_coach_sum.get('distinct_positive_coach_ids')}, "
                    f"último técnico registrado={a_coach_sum.get('most_recent_recorded_coach')}) -> "
                    f"HIPÓTESIS PRINCIPAL: Las medias de temporada completa mezclan etapas de distintos entrenadores "
                    f"cuando existen múltiples IDs positivos; el tramo más reciente puede alterar ritmo de posesión, "
                    f"altura de bloque y concesión -> "
                    f"ESCENARIO CONTRAPUESTO: Inercia estructural de plantilla (mismos titulares y roles nominales) "
                    f"donde la muestra corta del técnico reciente (n<5) refleja varianza de rival/localía más que "
                    f"un cambio táctico consolidado -> "
                    f"EVENTO MEDIBLE: Desviación de posesión, tiros y goles concedidos respecto a la media anual -> "
                    f"CONTRAEVIDENCIA HISTÓRICA: Contraste reciente vs previo home={bool(h_contrast)}, away={bool(a_contrast)} -> "
                    f"FALSADOR: Alineación y dibujo idénticos a la etapa previa con retorno a los promedios globales de temporada."
                ),
                'primary_hypothesis': (
                    f"No proyectar promedios agregados de temporada como régimen único sin ponderar la separación por "
                    f"entrenador ({home_name}: {h_coach_sum.get('regime_state')}; {away_name}: {a_coach_sum.get('regime_state')})."
                ),
                'counter_scenario': (
                    'Las submuestras por entrenador reciente con n < 5 están expuestas a sesgo de rival y localía; '
                    'además, los registros con coach_id=0 del proveedor pueden ser alias sin vincular y no cambios reales de técnico.'
                ),
                'quantitative_basis': {
                    'home_regime_state': h_coach_sum.get('regime_state'),
                    'away_regime_state': a_coach_sum.get('regime_state'),
                    'home_recent_vs_prior_contrast': h_contrast,
                    'away_recent_vs_prior_contrast': a_contrast,
                },
                'supporting_fixture_ids': (
                    (a_coach_sum.get('most_recent_recorded_coach') or {}).get('fixture_ids', [])
                    + (h_coach_sum.get('most_recent_recorded_coach') or {}).get('fixture_ids', [])
                )[:10],
                'counter_evidence_fixture_ids': [
                    fid
                    for reg in (a_coach_sum.get('regimes') or [])[1:]
                    if reg.get('coach_id')
                    for fid in reg.get('same_competition_fixture_ids', [])
                ][:10],
                'falsifier': (
                    'Confirmación oficial prepartido del cuerpo técnico y observación en los primeros 25 minutos '
                    'de si se mantiene la estructura de los últimos 2 partidos o la media histórica de la plantilla.'
                ),
                'actionable_bet': False,
            }
        )

    return {
        'fixture_id': fixture.get('fixture_id'),
        'minimum_sample_required': 4,
        'scenarios_count': len(scenarios),
        'scenarios': scenarios,
        'omitted_due_to_insufficient_evidence': omitted,
        'epistemic_guardrail': (
            'All scenarios are descriptive falsifiable hypotheses grounded in historical observations; '
            'none authorize commercial execution or imply calibrated edge.'
        ),
    }


def build_pattern_library_diagnostics(
    profiles: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    library_result: dict[str, Any],
    interpretation_supplied: bool,
    tactical_rule_fn,
) -> dict[str, Any]:
    """Explain why pattern_library.evaluations is empty or populated, without inventing library results."""
    evaluations = library_result.get('evaluations', [])
    accepted_ids = library_result.get('accepted_pattern_ids', [])

    causes = []
    if not interpretation_supplied:
        causes.append(
            {
                'code': 'NO_INTERPRETATION_FILE_SUPPLIED',
                'detail_es': (
                    'No se suministró archivo --interpretation con propuestas de team_patterns; '
                    'el motor opera en modo de extracción determinista sin inyectar reglas ad-hoc.'
                ),
            }
        )
    elif not proposals:
        causes.append(
            {
                'code': 'INTERPRETATION_CONTAINED_ZERO_TEAM_PATTERNS',
                'detail_es': 'El archivo de interpretación no incluyó propuestas en team_patterns.',
            }
        )
    if not evaluations:
        causes.append(
            {
                'code': 'NO_PREEXISTING_VALIDATED_PATTERNS_IN_LIBRARY_FOR_TEAMS',
                'detail_es': (
                    'El directorio pattern_library/patterns no contenía patrones previamente persistidos '
                    'con fecha evidence_asof <= asof para los equipos/temporada evaluados.'
                ),
            }
        )
        causes.append(
            {
                'code': 'ENGINE_POLICY_FORBIDS_FABRICATING_UNVALIDATED_PATTERNS',
                'detail_es': (
                    'Por contrato de integridad (evolve_tactical_library), un patrón solo entra en '
                    'evaluations si define predicados formales (condition/outcome), mecanismo, '
                    'contraescenario, falsador y supera las puertas de muestra (n>=6 para DESCRIPTIVE_REGULARITY).'
                ),
            }
        )

    sample_readiness = []
    candidate_regularities = []

    candidate_specs = [
        (
            'CAND_H2_GOALS_GE_H1_GOALS',
            'offensive_organization',
            'Goles propios en 2H >= goles propios en 1H',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'own.goals_h2', 'op': 'ge', 'value': {'field': 'own.goals_h1'}},
        ),
        (
            'CAND_OWN_SHOTS_GE_10',
            'finishing_area',
            'Producción de al menos 10 tiros totales en tiempo reglamentario',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'own.shots', 'op': 'ge', 'value': 10},
        ),
        (
            'CAND_CONCEDED_SHOTS_GE_10',
            'defensive_organization',
            'Concesión de al menos 10 tiros totales al rival',
            {'field': 'status', 'eq_override': True, 'op': 'eq', 'value': 'FT'},
            {'field': 'conceded.shots', 'op': 'ge', 'value': 10},
        ),
        (
            'CAND_OWN_ON_TARGET_GE_3',
            'finishing_area',
            'Producción de al menos 3 tiros a puerta',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'own.on_target', 'op': 'ge', 'value': 3},
        ),
        (
            'CAND_CONCEDED_ON_TARGET_GE_3',
            'defensive_organization',
            'Concesión de al menos 3 tiros a puerta',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'conceded.on_target', 'op': 'ge', 'value': 3},
        ),
        (
            'CAND_OWN_CORNERS_LE_5',
            'corners',
            'Producción propia contenida en <= 5 córners',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'own.corners', 'op': 'le', 'value': 5},
        ),
        (
            'CAND_OWN_FOULS_GE_11',
            'duels_discipline',
            'Intensidad de duelos con >= 11 faltas cometidas',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'own.fouls', 'op': 'ge', 'value': 11},
        ),
        (
            'CAND_OWN_YELLOW_GE_2',
            'duels_discipline',
            'Al menos 2 tarjetas amarillas propias en el encuentro',
            {'field': 'status', 'op': 'eq', 'value': 'FT'},
            {'field': 'own.yellow', 'op': 'ge', 'value': 2},
        ),
    ]

    for prof in profiles:
        tid = prof['team_id']
        lid = prof.get('league_id')
        season = prof.get('season')
        all_obs = prof.get('observations', [])
        same_obs = [
            r for r in all_obs if r.get('league_id') == lid and r.get('season') == season
        ]
        detailed_same = [r for r in same_obs if r.get('statistics_requested', True)]
        sample_readiness.append(
            {
                'team_id': tid,
                'league_id': lid,
                'season': season,
                'all_observations_n': len(all_obs),
                'same_competition_n': len(same_obs),
                'same_competition_detailed_n': len(detailed_same),
                'meets_min_n_6_for_regularity': len(detailed_same) >= 6,
            }
        )

        for code, fam, title, _, outcome_rule in candidate_specs:
            cond_rule = {'all': [{'field': 'league_id', 'op': 'eq', 'value': lid}, {'field': 'season', 'op': 'eq', 'value': season}]}
            selected = []
            success = []
            counter = []
            missing = []
            for row in same_obs:
                c_val = tactical_rule_fn(cond_rule, row)
                if c_val is not True:
                    continue
                o_val = tactical_rule_fn(outcome_rule, row)
                if o_val is None:
                    missing.append(row['fixture_id'])
                    continue
                selected.append(row['fixture_id'])
                (success if o_val else counter).append(row['fixture_id'])
            n_obs = len(selected)
            freq = round(len(success) / n_obs, 4) if n_obs else None
            if n_obs >= 6 and freq is not None and freq >= 0.70:
                candidate_regularities.append(
                    {
                        'candidate_code': f"{tid}:{season}:{code}",
                        'team_id': tid,
                        'league_id': lid,
                        'season': season,
                        'family': fam,
                        'title': title,
                        'condition': cond_rule,
                        'outcome': outcome_rule,
                        'n': n_obs,
                        'successes': len(success),
                        'frequency': freq,
                        'fixture_ids': selected,
                        'counterexamples': counter,
                        'missing_outcomes': missing,
                        'status': 'CANDIDATE_FOR_INTERPRETATION_NOT_PERSISTED_IN_LIBRARY',
                        'would_qualify_as': (
                            'DESCRIPTIVE_REGULARITY' if freq >= 0.75 else 'MIXED_EVIDENCE_HIGH'
                        ),
                        'predictive_use_authorized': False,
                    }
                )

    return {
        'library_state': (
            'EMPTY_NO_VALIDATED_PATTERNS_PERSISTED'
            if not evaluations
            else 'VALIDATED_EVALUATIONS_PRESENT'
        ),
        'interpretation_supplied': bool(interpretation_supplied),
        'proposals_submitted_count': len(proposals),
        'accepted_patterns_count': len(accepted_ids),
        'evaluations_count': len(evaluations),
        'empty_state_causes': causes,
        'team_sample_readiness': sample_readiness,
        'descriptive_candidate_regularities_not_persisted': candidate_regularities,
        'governance_notice': (
            'Candidate regularities are computed read-only to aid human/LLM scouting when the formal '
            'pattern_library is empty; they are NOT persisted as validated patterns and never authorize bets.'
        ),
    }


def build_matchup_diagnostics(
    match: dict[str, Any],
    pattern_library_doc: dict[str, Any],
    interpretation_supplied: bool,
) -> dict[str, Any]:
    """Explain empty states in pattern_matchup (home, away, contrasts, codex_review)."""
    pm = match.get('pattern_matchup') or {}
    home_evals = pm.get('home') or []
    away_evals = pm.get('away') or []
    contrasts = pm.get('contrasts') or []
    has_review = 'codex_review' in pm

    reasons = []
    if not home_evals and not away_evals:
        reasons.append(
            'NO_PERSISTED_LIBRARY_EVALUATIONS_FOR_HOME_OR_AWAY_TEAM: '
            'Al no existir patrones persistidos en pattern_library.evaluations para ninguno de los dos '
            'equipos, las listas pattern_matchup.home, pattern_matchup.away y pattern_matchup.contrasts '
            'quedan vacías legítimamente sin inventar cruces.'
        )
    elif not home_evals or not away_evals:
        reasons.append(
            'SINGLE_SIDE_LIBRARY_EVALUATIONS_ONLY: '
            'Se requieren patrones evaluados en ambos clubes para poblar pattern_matchup.contrasts.'
        )
    if not has_review:
        reasons.append(
            'NO_MATCHUP_REVIEWS_IN_INTERPRETATION_INPUT: '
            'No se suministró bloque matchup_reviews en --interpretation (o no existían pattern_ids '
            'previamente persistidos a los que vincular la revisión).'
        )

    lib_diag = (pattern_library_doc or {}).get('diagnostics') or {}
    team_ids = set(match.get('teams') or [])
    relevant_candidates = [
        c
        for c in lib_diag.get('descriptive_candidate_regularities_not_persisted', [])
        if c.get('team_id') in team_ids
    ]

    return {
        'matchup_state': (
            'EMPTY_LIBRARY_EVALUATIONS_USE_EMPIRICAL_CROSS_MATRIX_AND_SCENARIOS'
            if not contrasts
            else 'LIBRARY_CONTRASTS_AVAILABLE'
        ),
        'home_library_patterns_n': len(home_evals),
        'away_library_patterns_n': len(away_evals),
        'contrasts_n': len(contrasts),
        'codex_review_present': has_review,
        'interpretation_supplied': bool(interpretation_supplied),
        'empty_state_explanations': reasons,
        'fallback_empirical_sections': [
            'matchup_cross_matrix',
            'tactical_scenarios',
            'signals',
        ],
        'candidate_regularities_available_for_scouting_n': len(relevant_candidates),
    }


def _fmt_num(val: Any, digits: int = 2) -> str:
    if val is None:
        return 'N/D'
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if not math.isfinite(val):
            return 'N/D'
        return f"{val:.{digits}f}"
    return str(val)


def render_research_profiles_md(doc: dict[str, Any]) -> str:
    """Render a comprehensive, self-contained Markdown tactical report from research_profiles.json."""
    lines: list[str] = []
    lines.append(f"# Investigación táctica: {doc.get('mode', 'PREMATCH_RESEARCH')}")
    lines.append('')
    lines.append(
        'Sin cuotas, EV ni autorización comercial. Fuerza y cobertura son indicadores descriptivos, no probabilidades.'
    )
    lines.append('')
    lines.append('## 1. Resumen Ejecutivo y Control de Integridad')
    lines.append('')
    lines.append(f"- **Esquema**: `{doc.get('schema_version')}` | **Modo**: `{doc.get('mode')}`")
    lines.append(f"- **Corte temporal (`asof`)**: `{doc.get('asof')}`")
    lines.append(f"- **Fuente preselección**: `{doc.get('source')}` (`sha256={doc.get('source_sha256')}`)")
    lines.append(f"- **Llamadas API en ejecución**: `{doc.get('api_calls', 0)}` (100% offline sobre snapshots locales)")
    lines.append(
        "- **Guardarraíl metodológico**: Ninguna frecuencia, media, punto medio ni `priority_score` "
        "en este documento representa una probabilidad calibrada (`p_model`), cuota justa ni ventaja financiera (EV). "
        "`actionable_bet = False` en todos los niveles."
    )
    lines.append('')
    lines.append('### Ranking de partidos evaluados')
    lines.append('')
    for m in doc.get('ranking', []):
        lines.append(
            f"- Fixture {m['fixture_id']}: fuerza {m['pattern_strength']}; cobertura {m['data_quality']}; compatibilidad y riesgo pendientes."
        )
    lines.append('')

    teams_by_key = doc.get('teams') or {}
    for match in doc.get('matches', []):
        fid = match.get('fixture_id')
        profile_keys = match.get('profile_ids') or []
        home_prof = teams_by_key.get(profile_keys[0], {}) if len(profile_keys) >= 1 else {}
        away_prof = teams_by_key.get(profile_keys[1], {}) if len(profile_keys) >= 2 else {}
        t_names = match.get('team_names') or {}
        home_id = match.get('teams', [None, None])[0]
        away_id = match.get('teams', [None, None])[1]
        home_name = t_names.get('home') or f"Team {home_id}"
        away_name = t_names.get('away') or f"Team {away_id}"
        lid = match.get('league_id') or home_prof.get('league_id')
        season = match.get('season') or home_prof.get('season')

        lines.append('---')
        lines.append('')
        lines.append(
            f"## 2. Expediente del Encuentro: {home_name} (`id={home_id}`) vs {away_name} (`id={away_id}`) — Fixture `{fid}`"
        )
        lines.append('')
        lines.append(
            f"- **Kickoff**: `{match.get('kickoff')}` | **Competición/Temporada objetivo**: `league_id={lid}`, `season={season}`"
        )
        lines.append(
            f"- **Calidad de datos (cobertura efectiva media)**: `{match.get('data_quality')}%` | "
            f"**Tactical Research Score (fuerza descriptiva)**: `{match.get('pattern_strength')}`"
        )
        lines.append(
            f"- **Estado de alineaciones (`lineup_status`)**: `{match.get('lineup_status')}` | "
            f"**Familias con señal soportada**: `{match.get('potential_market_families')}` | "
            f"**Apuesta ejecutable (`actionable_bet`)**: `{match.get('actionable_bet')}`"
        )
        lines.append('')

        # Section 2.1: Unambiguous Denominators & Missing vs Zero
        lines.append('### 2.1 Auditoría de Muestras, Denominadores y Datos Ausentes vs Valores Cero (`0`)')
        lines.append('')
        lines.append(
            '> **Nota de denominadores**: En `goals` el denominador esperado (`expected_n`) comprende todos los '
            'partidos finalizados (`FT`) de la misma competición y temporada (`history_n`). En las métricas detalladas '
            '(`shots`, `on_target`, `corners`, `fouls`, `yellow`, `offsides`, `xg`, `blocked_shots`, `possession_pct`), '
            '`expected_n` son los partidos donde se solicitó/recuperó el bloque `statistics` (`historical_matches_detailed`), '
            'mientras que `not_detailed_n` (`history_not_detailed_n`) son partidos FT conocidos sin bloque detallado. '
            'Un valor `0` observado se contabiliza en `zero_n` y entra en la media; un dato ausente (`null` / no solicitado) '
            'se contabiliza en `missing_n` / `not_detailed_n` y **nunca** se convierte en `0`.'
        )
        lines.append('')
        lines.append(
            '| Equipo | Rol | Métrica | `history_n` (FT Liga) | `expected_n` (Detallados) | `not_detailed_n` (Sin detalle) | `valid_n` (`n`) | `missing_n` (`null` en detalle) | `zero_n` (`0` real) | `positive_n` (`>0`) | Cobertura s/detalle | Cobertura efectiva s/historial |'
        )
        lines.append('|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
        for side_label, prof, t_name in (
            ('LOCAL', home_prof, home_name),
            ('VISITANTE', away_prof, away_name),
        ):
            for m in EXTENDED_METRICS:
                series = None
                if m in (prof.get('metrics') or {}):
                    series = prof['metrics'][m]['own']
                elif m in (prof.get('period_metrics') or {}):
                    series = prof['period_metrics'][m]['own']
                elif m in (prof.get('support_metrics') or {}):
                    series = prof['support_metrics'][m]['own']
                if not series:
                    continue
                lines.append(
                    f"| {t_name} | {side_label} (`own`) | `{m}` ({METRIC_LABELS_ES.get(m, m)}) | "
                    f"{series.get('history_n')} | {series.get('expected_n')} | {series.get('history_not_detailed_n')} | "
                    f"**{series.get('n')}** | {series.get('missing_n')} | {series.get('zero_n')} | {series.get('positive_n')} | "
                    f"{_fmt_num(series.get('requested_data_coverage_pct'))}% | {_fmt_num(series.get('historical_effective_coverage_pct'))}% |"
                )
        lines.append('')

        # Rejections & Multi-competition windows explanation
        for side_label, prof, t_name in (
            ('Local', home_prof, home_name),
            ('Visitante', away_prof, away_name),
        ):
            rejs = prof.get('rejections') or []
            comp_samples = (prof.get('longitudinal') or {}).get('competition_season_samples') or {}
            rej_counts = Counter(r.get('reason') for r in rejs)
            lines.append(
                f"- **{t_name} ({side_label}) — Composición del historial bruto (`observations={len(prof.get('observations', []))}`)**: "
                f"Muestras por competición/temporada: `{ {k: len(v) for k, v in comp_samples.items()} }`. "
                f"Excluidos del cálculo de señales de liga (`rejections={len(rejs)}`): `{dict(rej_counts)}` "
                f"(fixtures excluidos: `{[r.get('fixture_id') for r in rejs]}`)."
            )
        lines.append('')

        # Section 2.2: Full Statistical Table (Both teams, Own & Conceded, Global, Locality, L5 vs Prev L5)
        lines.append('### 2.2 Estadísticas Completas de Ambos Equipos (Global FT, Localía Comparable y Ventanas Recientes)')
        lines.append('')
        lines.append(
            '| Métrica | Equipo | Sentido | `n` (`val/det/hist`) | Media Global | SD | CV | `zero_n` | Localía Comparable (`n`) | Media Localía | CV Localía | Reciente `L5` (`n`) | Previo `5-10` (`n`) | Tendencia Relativa | Rango LOO |'
        )
        lines.append('|---|---|---|---|---:|---:|---:|---:|---|---:|---:|---|---|---:|---|')
        for m in EXTENDED_METRICS:
            for prof, t_name, side_tag in (
                (home_prof, home_name, 'HOME'),
                (away_prof, away_name, 'AWAY'),
            ):
                for kind, kind_label in (('own', 'Propio'), ('conceded', 'Concedido')):
                    series = None
                    if m in (prof.get('metrics') or {}):
                        series = prof['metrics'][m][kind]
                    elif m in (prof.get('period_metrics') or {}):
                        series = prof['period_metrics'][m][kind]
                    elif m in (prof.get('support_metrics') or {}):
                        series = prof['support_metrics'][m][kind]
                    if not series:
                        continue
                    v_block = series.get('venue') or {}
                    rec_block = series.get('recent') or {}
                    prev_block = series.get('previous') or {}
                    v_filter = v_block.get('venue_filter') or side_tag
                    loo = series.get('leave_one_out_mean_range')
                    loo_str = f"[{_fmt_num(loo[0])}, {_fmt_num(loo[1])}]" if loo else 'N/D'
                    lines.append(
                        f"| `{m}` | {t_name} | {kind_label} (`{kind}`) | "
                        f"`{series.get('n')}/{series.get('expected_n')}/{series.get('history_n')}` | "
                        f"**{_fmt_num(series.get('mean'))}** | {_fmt_num(series.get('stdev'))} | {_fmt_num(series.get('cv'))} | "
                        f"{series.get('zero_n')} | `{v_filter}` (`n={v_block.get('n')}/{v_block.get('history_n')}`) | "
                        f"**{_fmt_num(v_block.get('mean'))}** | {_fmt_num(v_block.get('cv'))} | "
                        f"{_fmt_num(rec_block.get('mean'))} (`n={rec_block.get('n')}`) | "
                        f"{_fmt_num(prev_block.get('mean'))} (`n={prev_block.get('n')}`) | "
                        f"{_fmt_num(series.get('trend_relative'))} | {loo_str} |"
                    )
        lines.append('')

        # Section 2.3: Periods (1H vs 2H)
        lines.append('### 2.3 Desglose por Periodos (`1ª Parte [1H]` vs `2ª Parte [2H]`)')
        lines.append('')
        lines.append(
            '| Equipo | Ámbito | `paired_n` / `history_n` | Media 1H | Media 2H | Diferencia (`2H - 1H`) | Partidos `2H > 1H` | Partidos `1H > 2H` | Partidos `1H == 2H` | Partidos `0-0` en ambas mitades | Cuota goles en 2H |'
        )
        lines.append('|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
        for prof, t_name in ((home_prof, home_name), (away_prof, away_name)):
            hc = ((prof.get('period_metrics') or {}).get('half_comparison') or {})
            for k, k_label in (
                ('own', 'Goles propios'),
                ('conceded', 'Goles concedidos'),
            ):
                b = hc.get(k) or {}
                lines.append(
                    f"| {t_name} | {k_label} | `{b.get('paired_n')}/{b.get('history_n')}` | "
                    f"{_fmt_num(b.get('h1_mean'))} | {_fmt_num(b.get('h2_mean'))} | **{_fmt_num(b.get('h2_minus_h1_mean'))}** | "
                    f"{b.get('h2_greater_n')} | {b.get('h1_greater_n')} | {b.get('equal_halves_n')} | "
                    f"{b.get('both_halves_zero_n')} | N/A |"
                )
            mt = hc.get('match_total') or {}
            share_pct = (
                f"{mt['h2_share_of_total_goals'] * 100:.1f}%"
                if mt.get('h2_share_of_total_goals') is not None
                else 'N/D'
            )
            lines.append(
                f"| {t_name} | **Total partido (`own + conceded`)** | `{mt.get('paired_n')}/{len(prof.get('observations', []))}` | "
                f"**{_fmt_num(mt.get('total_h1_mean'))}** | **{_fmt_num(mt.get('total_h2_mean'))}** | "
                f"**{_fmt_num(mt.get('total_h2_minus_h1_mean'))}** | {mt.get('h2_higher_scoring_matches_n')} | "
                f"{mt.get('h1_higher_scoring_matches_n')} | {mt.get('equal_half_scoring_matches_n')} | - | **{share_pct}** |"
            )
        lines.append('')
        lines.append(
            '- **Limitación de periodos**: El proveedor API-Football registra marcador al descanso (`score.halftime`), '
            'lo que permite verificar `goals_h1` y `goals_h2` con exactitud. Las demás métricas (`shots`, `corners`, '
            '`fouls`, `yellow`, `offsides`) solo constan a nivel `FT` en el snapshot.'
        )
        lines.append('')

        # Section 2.4: Multi-Window Comparison (Pure League Windows vs Mixed Windows)
        lines.append('### 2.4 Ventanas Temporales (`5`, `10`, `20`, `PREVIOUS_L5`) y Advertencia de Mezcla de Competición')
        lines.append('')
        lines.append(
            '> **Control de contaminación entre torneos**: Las ventanas `GLOBAL`, `HOME` y `AWAY` agrupan todas las '
            'observaciones `FT` del historial bruto (incluyendo Copa, amistosos o temporada previa si existen en el '
            'dataset), mientras que las ventanas `'
            + f"{lid}:{season}"
            + '`, `'
            + f"{lid}:{season}:HOME"
            + '` y `'
            + f"{lid}:{season}:AWAY"
            + '` aíslan estrictamente la competición y temporada objetivo.'
        )
        lines.append('')
        lines.append(
            '| Equipo | Ventana | Corte | Fixtures (`window_n` / `detailed_n`) | Competiciones incluidas | Mezcla torneos | Goles (`own` / `con`) | Tiros (`own` / `con`) | A puerta (`own` / `con`) | Córners (`own` / `con`) | Faltas (`own` / `con`) | Amarillas (`own` / `con`) |'
        )
        lines.append('|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|')
        target_comp_key = f"{lid}:{season}"
        window_order = [
            target_comp_key,
            f"{target_comp_key}:HOME",
            f"{target_comp_key}:AWAY",
            'GLOBAL',
            'HOME',
            'AWAY',
        ]
        for prof, t_name in ((home_prof, home_name), (away_prof, away_name)):
            wins = prof.get('windows') or {}
            for w_label in window_order:
                if w_label not in wins:
                    continue
                for slice_key in ('5', '10', '20', 'PREVIOUS_L5'):
                    w_slice = wins[w_label].get(slice_key)
                    if not w_slice or not w_slice.get('fixture_ids'):
                        continue
                    wm = w_slice.get('metrics') or {}
                    wc = w_slice.get('conceded_metrics') or {}
                    w_n = w_slice.get('window_fixtures_n', len(w_slice.get('fixture_ids', [])))
                    d_n = w_slice.get('detailed_fixtures_n')
                    comps = ', '.join(w_slice.get('competitions_included') or [])
                    mixed = 'SÍ (MIXTA)' if w_slice.get('mixed_competitions') else 'NO (PURA)'

                    def _pair(m_key: str) -> str:
                        om_val = (wm.get(m_key) or {}).get('mean')
                        on_val = (wm.get(m_key) or {}).get('n')
                        cm_val = (wc.get(m_key) or {}).get('mean')
                        return f"{_fmt_num(om_val)} / {_fmt_num(cm_val)} (`n={on_val}`)"

                    lines.append(
                        f"| {t_name} | `{w_label}` | `{slice_key}` | `{w_n}` / `{d_n}` | `{comps}` | {mixed} | "
                        f"{_pair('goals')} | {_pair('shots')} | {_pair('on_target')} | {_pair('corners')} | "
                        f"{_pair('fouls')} | {_pair('yellow')} |"
                    )
        lines.append('')

        # Section 3: Coach Regimes, Formations & Players
        lines.append('## 3.Separación por Entrenador, Dibujos Nominales y Exposición Longitudinal de Jugadores')
        lines.append('')
        for prof, t_name, side_label in (
            (home_prof, home_name, 'LOCAL'),
            (away_prof, away_name, 'VISITANTE'),
        ):
            crs = prof.get('coach_regime_summary') or {}
            longi = prof.get('longitudinal') or {}
            lines.append(f"### 3.1 {t_name} ({side_label}, `team_id={prof.get('team_id')}`)")
            lines.append('')
            lines.append(
                f"- **Estado del régimen de entrenador**: `{crs.get('regime_state')}` | "
                f"**IDs positivos distintos en liga**: `{crs.get('distinct_positive_coach_ids')}` | "
                f"**Registro con `coach_id=0` sin vincular**: `{crs.get('has_unlinked_zero_coach_id')}`"
            )
            lines.append(
                f"- **Cobertura de banquillo en liga (`{lid}:{season}`)**: `{crs.get('recorded_lineup_matches_n')}` partidos "
                f"con alineación/entrenador registrado vs `{crs.get('unrecorded_lineup_matches_n')}` partidos sin bloque `lineups`."
            )
            if crs.get('most_recent_recorded_coach'):
                mrc = crs['most_recent_recorded_coach']
                lines.append(
                    f"- **Último entrenador registrado antes del corte**: `{mrc.get('coach_name')}` "
                    f"(`coach_id={mrc.get('coach_id')}`, `n={mrc.get('same_competition_n')}` en liga, "
                    f"último partido `{mrc.get('last_kickoff_utc')}`, fixtures `{mrc.get('fixture_ids')}`)."
                )
            lines.append('')
            lines.append(
                '| Segmento Entrenador | Estado Identidad | `n` Liga (`all_n`) | Fechas (Primero -> Último) | Formaciones | Goles (`own`/`con`) | xG (`own`/`con`) | Tiros (`own`/`con`) | A puerta (`own`/`con`) | Córners (`own`/`con`) | Faltas (`own`/`con`) | Amarillas (`own`/`con`) | Posesión % (`own`) | Fixtures Liga |'
            )
            lines.append('|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|')
            for reg in crs.get('regimes', []):
                rm = reg.get('metrics') or {}

                def _rm_pair(m_key: str) -> str:
                    if m_key not in rm:
                        return 'N/D'
                    o = rm[m_key]['own']
                    c = rm[m_key]['conceded']
                    return f"{_fmt_num(o.get('mean'))} / {_fmt_num(c.get('mean'))} (`n={o.get('n')}`)"

                pos_o = (rm.get('possession_pct') or {}).get('own', {})
                forms_str = ', '.join(f"{k}({v})" for k, v in (reg.get('formations_observed') or {}).items()) or 'Sin datos'
                lines.append(
                    f"| `{reg.get('coach_key')}` | `{reg.get('identity_status')}` | "
                    f"**{reg.get('same_competition_n')}** (`{reg.get('all_competitions_n')}`) | "
                    f"`{str(reg.get('first_kickoff_utc') or '')[:10]}` -> `{str(reg.get('last_kickoff_utc') or '')[:10]}` | "
                    f"`{forms_str}` | {_rm_pair('goals')} | {_rm_pair('xg')} | {_rm_pair('shots')} | "
                    f"{_rm_pair('on_target')} | {_rm_pair('corners')} | {_rm_pair('fouls')} | {_rm_pair('yellow')} | "
                    f"{_fmt_num(pos_o.get('mean'))}% (`n={pos_o.get('n')}`) | `{reg.get('same_competition_fixture_ids')}` |"
                )
            lines.append('')

            contrast = crs.get('recent_vs_prior_coach_contrast')
            if contrast:
                lines.append(
                    f"- **Contraste entre etapa reciente (`{contrast.get('recent_coach_key')}`, `n={contrast.get('recent_coach_n')}`) "
                    f"y entrenadores previos con ID positivo (`n={contrast.get('prior_coaches_combined_n')}`)**: "
                    f"Advertencia `{contrast.get('warning')}`."
                )
                deltas = contrast.get('metric_deltas') or {}
                delta_parts = []
                for m_k in ('goals', 'shots', 'on_target', 'corners', 'fouls', 'yellow', 'possession_pct'):
                    d_obj = deltas.get(m_k) or {}
                    delta_parts.append(
                        f"`{m_k}` propio {_fmt_num(d_obj.get('recent_coach_own_mean'))} vs {_fmt_num(d_obj.get('prior_coaches_own_mean'))} "
                        f"(delta={_fmt_num(d_obj.get('own_delta'))}), concedido {_fmt_num(d_obj.get('recent_coach_conceded_mean'))} "
                        f"vs {_fmt_num(d_obj.get('prior_coaches_conceded_mean'))} (delta={_fmt_num(d_obj.get('conceded_delta'))})"
                    )
                lines.append('  - ' + '; '.join(delta_parts) + '.')
                lines.append('')

            # Top Players Exposure Table (showing null vs zero explicitly)
            players = longi.get('players') or []
            if players:
                lines.append(
                    f"#### Exposición Longitudinal de Jugadores de {t_name} (Diferenciación `null` vs `0` observado)"
                )
                lines.append('')
                lines.append(
                    '| Jugador (`player_id`) | Apariciones (`titular`) | Minutos Totales | Tiros (`tot` / `per90` / `obs:null:zero`) | A Puerta (`tot` / `per90` / `obs:null:zero`) | Pases Clave (`tot` / `per90` / `obs:null:zero`) | Entradas (`tot` / `per90` / `obs:null:zero`) | Goles (`tot` / `obs:null:zero`) |'
                )
                lines.append('|---|---|---:|---|---|---|---|---|')
                ranked_players = sorted(
                    players,
                    key=lambda p: (
                        sum(o.get('minutes', 0) for o in p.get('observations', [])),
                        len(p.get('observations', [])),
                    ),
                    reverse=True,
                )
                for pl in ranked_players[:15]:
                    obs_list = pl.get('observations') or []
                    app_n = len(obs_list)
                    start_n = sum(1 for o in obs_list if o.get('starter') is True)
                    tot_min = sum(o.get('minutes', 0) for o in obs_list)
                    pm = pl.get('metrics') or {}

                    def _pl_cell(m_k: str, include_p90: bool = True) -> str:
                        m_obj = pm.get(m_k) or {}
                        obs_m = m_obj.get('observed_matches', 0)
                        null_m = m_obj.get('missing_matches', app_n - obs_m)
                        zero_m = m_obj.get('zero_matches', 0)
                        tot_v = m_obj.get('total')
                        p90_v = m_obj.get('per90')
                        if include_p90:
                            return f"{_fmt_num(tot_v, 0)} / {_fmt_num(p90_v)} (`{obs_m}:{null_m}:{zero_m}`)"
                        return f"{_fmt_num(tot_v, 0)} (`{obs_m}:{null_m}:{zero_m}`)"

                    lines.append(
                        f"| {pl.get('name')} (`{pl.get('player_id')}`) | {app_n} (`{start_n}`) | {tot_min} | "
                        f"{_pl_cell('shots')} | {_pl_cell('on_target')} | {_pl_cell('key_passes')} | "
                        f"{_pl_cell('tackles')} | {_pl_cell('goals', False)} |"
                    )
                lines.append('')
            else:
                lines.append(
                    f"- **Exposición de jugadores de {t_name}**: Sin bloques `players` detallados en las fuentes cargadas."
                )
                lines.append('')

        # Section 4: Explanation of Stable, Unstable, and Contradictory Signals
        lines.append('## 4. Auditoría de Señales Tácticas: Estables, Inestables y Contradictorias')
        lines.append('')
        lines.append(
            '| `signal_id` | Estado (`status`) | Score | `own` (`media`, `CV`, `n`) | `opponent_conceded` (`media`, `CV`, `n`) | Acuerdo | Lados inestables (`CV>0.80`) | Contradicciones detectadas | Explicación diagnóstica |'
        )
        lines.append('|---|---|---:|---|---|---:|---|---|---|')
        for sig in match.get('signals', []):
            exp = sig.get('status_explanation') or {}
            own = sig.get('own') or {}
            con = sig.get('opponent_conceded') or {}
            agr = (sig.get('components') or {}).get('agreement')
            unstable_str = ', '.join(exp.get('unstable_sides') or []) or 'Ninguno (`<=0.80`)'
            contra_str = ', '.join(sig.get('contradictions') or []) or 'Ninguna'
            lines.append(
                f"| `{sig.get('signal_id')}` | **{sig.get('status')}** | {_fmt_num(sig.get('priority_score'))} | "
                f"{_fmt_num(own.get('mean'))} (`CV={_fmt_num(own.get('cv'))}`, `n={own.get('n')}`) | "
                f"{_fmt_num(con.get('mean'))} (`CV={_fmt_num(con.get('cv'))}`, `n={con.get('n')}`) | "
                f"{_fmt_num(agr)} | {unstable_str} | `{contra_str}` | {exp.get('summary_es', '')} |"
            )
        lines.append('')

        # Section 5: Cross-Matrix (Own Production vs Rival Concession)
        lines.append('## 5. Cruce Directo: Producción Propia vs Concesión Rival (`matchup_cross_matrix`)')
        lines.append('')
        cm = match.get('matchup_cross_matrix') or {}
        lines.append(
            '| Métrica | Ataque Local vs Defensa Visitante (`Global` / `Localía` / `L5` / `Últ. Técnico`) | Punto Medio Local | Ataque Visitante vs Defensa Local (`Global` / `Localía` / `L5` / `Últ. Técnico`) | Punto Medio Visitante | Suma Puntos Medios (`Global` / `Localía`) | Total Observado en Partidos de `Home` vs `Away` |'
        )
        lines.append('|---|---|---:|---|---:|---:|---|')
        for m in EXTENDED_METRICS:
            m_row = (cm.get('metrics') or {}).get(m)
            if not m_row:
                continue
            ha = m_row['home_attack_vs_away_defense']
            aa = m_row['away_attack_vs_home_defense']
            tot = m_row['combined_match_totals_descriptive']
            h_tot_obs = (tot.get('home_matches_observed_total') or {}).get('mean')
            a_tot_obs = (tot.get('away_matches_observed_total') or {}).get('mean')
            lines.append(
                f"| `{m}` ({m_row.get('label_es')}) | "
                f"Global: {_fmt_num(ha.get('home_own_mean'))} vs {_fmt_num(ha.get('away_conceded_mean'))} "
                f"| Loc: {_fmt_num(ha.get('home_venue_mean'))} vs {_fmt_num(ha.get('away_venue_mean'))} "
                f"| L5: {_fmt_num(ha.get('home_recent_l5_mean'))} vs {_fmt_num(ha.get('away_conceded_recent_l5_mean'))} "
                f"| Téc: {_fmt_num(ha.get('home_latest_coach_own_mean'))} vs {_fmt_num(ha.get('away_latest_coach_conceded_mean'))} | "
                f"**{_fmt_num(ha.get('global_midpoint_descriptive'))}** (Loc: {_fmt_num(ha.get('locality_midpoint_descriptive'))}) | "
                f"Global: {_fmt_num(aa.get('away_own_mean'))} vs {_fmt_num(aa.get('home_conceded_mean'))} "
                f"| Loc: {_fmt_num(aa.get('away_venue_mean'))} vs {_fmt_num(aa.get('home_venue_mean'))} "
                f"| L5: {_fmt_num(aa.get('away_recent_l5_mean'))} vs {_fmt_num(aa.get('home_conceded_recent_l5_mean'))} "
                f"| Téc: {_fmt_num(aa.get('away_latest_coach_own_mean'))} vs {_fmt_num(aa.get('home_latest_coach_conceded_mean'))} | "
                f"**{_fmt_num(aa.get('global_midpoint_descriptive'))}** (Loc: {_fmt_num(aa.get('locality_midpoint_descriptive'))}) | "
                f"**{_fmt_num(tot.get('sum_of_global_midpoints_descriptive'))}** / {_fmt_num(tot.get('sum_of_locality_midpoints_descriptive'))} | "
                f"{home_name}: {_fmt_num(h_tot_obs)} / {away_name}: {_fmt_num(a_tot_obs)} |"
            )
        lines.append('')

        # Section 6: Opposing Tactical Scenarios & Falsifiers
        lines.append('## 6. Escenarios Tácticos Contrapuestos y Falsadores Basados en Evidencia (`tactical_scenarios`)')
        lines.append('')
        ts_block = match.get('tactical_scenarios') or {}
        for sc in ts_block.get('scenarios', []):
            lines.append(f"### {sc.get('title')} (`{sc.get('scenario_id')}`)")
            lines.append('')
            lines.append(
                f"- **Familia**: `{sc.get('family')}` | **Estado epistémico**: `{sc.get('epistemic_status')}` | "
                f"**Apuesta accionable**: `{sc.get('actionable_bet')}`"
            )
            lines.append(f"- **Cadena de evidencia**: {sc.get('evidence_chain')}")
            lines.append(f"- **Hipótesis principal**: {sc.get('primary_hypothesis')}")
            lines.append(f"- **Escenario contrapuesto**: {sc.get('counter_scenario')}")
            lines.append(f"- **Fixtures de apoyo en muestra**: `{sc.get('supporting_fixture_ids')}`")
            lines.append(f"- **Contraevidencia histórica (fixtures)**: `{sc.get('counter_evidence_fixture_ids')}`")
            lines.append(f"- **Falsador verificable**: {sc.get('falsifier')}")
            lines.append('')

        # Matchup diagnostics
        pm_diag = (match.get('pattern_matchup') or {}).get('diagnostics') or {}
        lines.append('## 7. Diagnóstico de `pattern_matchup` y Biblioteca de Patrones')
        lines.append('')
        lines.append(f"- **Estado del cruce de patrones (`matchup_state`)**: `{pm_diag.get('matchup_state')}`")
        for reason_txt in pm_diag.get('empty_state_explanations', []):
            lines.append(f"  - {reason_txt}")
        lines.append('')

    # Preserve exact rendering of any LLM interpretation claims if present
    for c in doc.get('interpretation', {}).get('claims', []):
        lines.append(f"## {c['fixture_id']} — {c.get('title', c['status'])}")
        lines.append('')
        lines.append(f"{c['status']} | Fixtures: {c['fixture_ids']}")
        lines.append('')
        lines.append(str(c['mechanism']))
        lines.append('')
        lines.append(f"Contrario: {c['counter_scenario']}")
        lines.append('')
        lines.append(f"Comprobación: {c['falsifier']}")
        lines.append('')

    # Pattern Library Diagnostics Summary
    lib_diag = (doc.get('pattern_library') or {}).get('diagnostics') or {}
    if lib_diag:
        lines.append('### 7.1 Diagnóstico de `pattern_library` y Regularidades Descriptivas Candidatas (No Persistidas)')
        lines.append('')
        lines.append(
            f"- **Estado de biblioteca**: `{lib_diag.get('library_state')}` | "
            f"**Propuestas recibidas**: `{lib_diag.get('proposals_submitted_count')}` | "
            f"**Evaluaciones persistidas**: `{lib_diag.get('evaluations_count')}`"
        )
        for cause in lib_diag.get('empty_state_causes', []):
            lines.append(f"  - `{cause.get('code')}`: {cause.get('detail_es')}")
        lines.append('')
        cands = lib_diag.get('descriptive_candidate_regularities_not_persisted') or []
        if cands:
            lines.append(
                '| Código Candidato | Equipo (`team_id`) | Familia | Regla Descriptiva | `n` | Éxitos | Frecuencia | Fixtures Cumplen | Contraejemplos | Estado | Uso Predictivo Autorizado |'
            )
            lines.append('|---|---:|---|---|---:|---:|---:|---|---|---|---|')
            for cand in cands:
                lines.append(
                    f"| `{cand.get('candidate_code')}` | `{cand.get('team_id')}` | `{cand.get('family')}` | "
                    f"{cand.get('title')} | {cand.get('n')} | {cand.get('successes')} | "
                    f"**{_fmt_num(cand.get('frequency'))}** | `{cand.get('fixture_ids')}` | "
                    f"`{cand.get('counterexamples')}` | `{cand.get('status')}` | `{cand.get('predictive_use_authorized')}` |"
                )
            lines.append('')

    return '\n'.join(lines) + '\n'


def render_pattern_library_summary_md(doc: dict[str, Any]) -> str:
    """Render pattern_library_summary.md including full profiles summary, reviews, evaluations, and empty-state diagnostics."""
    body = render_research_profiles_md(doc)
    extra: list[str] = []
    for match in doc.get('matches', []):
        review = (match.get('pattern_matchup') or {}).get('codex_review')
        if review:
            extra.append(f"\n## Cruce {match['fixture_id']}\n")
            extra.append(f"{review['scenarios']}\n")
            extra.append(f"Contrario: {review['counter_scenario']}\n")
            extra.append(f"Falsador: {review['falsifier']}\n")
    for e in (doc.get('pattern_library') or {}).get('evaluations', []):
        extra.append(
            f"\n- Patrón {e['team_id']}: {e['title']} | {e['state']} | n={e['n']} | contraejemplos={e['counterexamples']}\n"
        )
    return body + ''.join(extra)
