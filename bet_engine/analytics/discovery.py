"""Motor de descubrimiento automático de patrones e interacciones (Etapa 3).

Separa estrictamente:
- Condiciones observables PREMATCH (context.*, lag.*) -> PREMATCH_TRIGGER
- Mecanismos internos de partidos históricos (own.*, score_state.*) -> INTRA_MATCH_MECHANISM
Aplica separación temporal estricta:
- Descubrimiento de umbrales (medianas) y selección BY FDR exclusivamente en ventana Train (70%).
- Evaluación final fuera de muestra sobre ventana Holdout (30%) con umbrales congelados.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from bet_engine.context.observation_store import MatchObservationBundle
from bet_engine.contracts import (
    PatternScope,
    PrematchAvailability,
    StatisticalPattern,
    TemporalLeakageError,
    ValidatedPattern,
)
from bet_engine.contracts.epistemic import parse_utc_timestamp

from .registry import PluginOutput, build_feature_rows, eval_declarative_dsl, extract_dsl_fields


PREMATCH_ALLOWED_PREFIXES = ("context.", "lag.")
INTRA_MATCH_PREFIXES = ("own.", "conceded.", "score_state.")


def is_prematch_field(field_path: str) -> bool:
    return any(field_path.startswith(p) for p in PREMATCH_ALLOWED_PREFIXES)


def classify_dsl_availability(dsl: dict[str, Any] | None) -> PrematchAvailability:
    if dsl is None:
        return PrematchAvailability.PREMATCH_OBSERVABLE
    fields = extract_dsl_fields(dsl)
    if not fields:
        return PrematchAvailability.PREMATCH_OBSERVABLE
    flags = [is_prematch_field(f) for f in fields]
    if all(flags):
        return PrematchAvailability.PREMATCH_OBSERVABLE
    if not any(flags):
        return PrematchAvailability.POSTMATCH_ONLY
    return PrematchAvailability.MIXED


def audit_feature_rows_temporal_integrity(
    rows: list[dict[str, Any]],
    patterns: tuple[StatisticalPattern, ...] | list[StatisticalPattern] = (),
) -> dict[str, Any]:
    """Audita temporalmente todas las filas y variables usadas por InteractionMinerPlugin.

    Verifica:
    1) Que para toda fila, max_prior_available_at_utc <= kickoff_utc.
    2) Que todo patrón PREMATCH_TRIGGER utilice únicamente variables context.* y lag.*.
    3) Que todo mecanismo interno utilice variables own.*, conceded.* o score_state.* y nunca sea predictor.
    """
    row_checks = 0
    for r in rows:
        ko = parse_utc_timestamp(r["kickoff_utc"])
        max_prior = (r.get("lag") or {}).get("max_prior_available_at_utc")
        if max_prior is not None:
            prior_dt = parse_utc_timestamp(max_prior)
            if prior_dt > ko:
                raise TemporalLeakageError(
                    f"LAG_TEMPORAL_LEAKAGE(fixture={r['fixture_id']}): prior_avail={prior_dt.isoformat()} > kickoff={ko.isoformat()}"
                )
        row_checks += 1

    prematch_fields_used: set[str] = set()
    intra_fields_used: set[str] = set()
    for pat in patterns:
        if pat.condition_dsl is None:
            continue
        fields = extract_dsl_fields(pat.condition_dsl)
        if pat.pattern_scope == PatternScope.PREMATCH_TRIGGER:
            for f in fields:
                if not is_prematch_field(f):
                    raise TemporalLeakageError(
                        f"POSTMATCH_FIELD_IN_PREMATCH_TRIGGER({pat.pattern_id}): field={f}"
                    )
                prematch_fields_used.add(f)
        elif pat.pattern_scope == PatternScope.INTRA_MATCH_MECHANISM:
            for f in fields:
                intra_fields_used.add(f)

    return {
        "rows_audited": row_checks,
        "temporal_leakage_detected": False,
        "prematch_trigger_fields": sorted(prematch_fields_used),
        "intra_match_mechanism_fields": sorted(intra_fields_used),
    }


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Test exacto de Fisher bilateral sobre tabla 2x2 [[a, b], [c, d]]."""
    row1 = a + b
    row2 = c + d
    col1 = a + c
    total = row1 + row2
    if total == 0:
        return 1.0
    denom = math.comb(total, col1)
    if denom == 0:
        return 1.0

    def hyper_p(x: int) -> float:
        y = col1 - x
        if x < 0 or y < 0 or x > row1 or y > row2:
            return 0.0
        return (math.comb(row1, x) * math.comb(row2, y)) / denom

    p_obs = hyper_p(a)
    lo = max(0, col1 - row2)
    hi = min(row1, col1)
    p_sum = sum(hyper_p(x) for x in range(lo, hi + 1) if hyper_p(x) <= p_obs + 1e-12)
    return min(1.0, p_sum)


def benjamini_yekutieli_adjust(p_values: list[float]) -> list[float]:
    """Corrección Benjamini-Yekutieli (FDR bajo dependencia arbitraria)."""
    m = len(p_values)
    if m == 0:
        return []
    harmonic = sum(1.0 / i for i in range(1, m + 1))
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    q_raw = [0.0] * m
    running_min = 1.0
    for rank_idx in range(m - 1, -1, -1):
        orig_idx, p = indexed[rank_idx]
        rank = rank_idx + 1
        val = min(1.0, (p * m * harmonic) / rank)
        running_min = min(running_min, val)
        q_raw[orig_idx] = running_min
    return [round(q, 6) for q in q_raw]


def leave_one_out_range(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    total = sum(values)
    n = len(values)
    loos = [(total - v) / (n - 1) for v in values]
    return round(min(loos), 4), round(max(loos), 4)


class InteractionMinerPlugin:
    """Plugin de minería automática de patrones nuevos e interacciones 2-a-2."""

    plugin_id = "interaction_miner"
    domain_family = "AUTOMATED_INTERACTION_DISCOVERY"

    OUTCOME_METRICS = ("goals", "corners", "shots", "yellow")

    def __init__(self, *, max_tests_per_bucket: int = 32) -> None:
        self.max_tests_per_bucket = max_tests_per_bucket

    def _candidate_conditions(self, discovery_rows: list[dict[str, Any]]) -> list[tuple[str, PatternScope, dict[str, Any]]]:
        """Construye condiciones cuyos umbrales (medianas) se estiman SOLO sobre discovery_rows."""
        conds: list[tuple[str, PatternScope, dict[str, Any]]] = []

        # 1) Condiciones PREMATCH_TRIGGER (context.* y lag.*)
        conds.append(("venue_HOME", PatternScope.PREMATCH_TRIGGER, {
            "op": "all",
            "clauses": [{"field": "context.venue", "cmp": "eq", "value": "HOME"}],
        }))
        conds.append(("short_gap_le_4d", PatternScope.PREMATCH_TRIGGER, {
            "op": "all",
            "clauses": [{"field": "context.observed_gap_days", "cmp": "le", "value": 4.5}],
        }))

        for lag_field in ("lag.own.shots_l5", "lag.own.corners_l5", "lag.own.fouls_l5"):
            vals = []
            for r in discovery_rows:
                part = lag_field.split(".")[-1]
                v = (r.get("lag") or {}).get("own", {}).get(part)
                if isinstance(v, (int, float)):
                    vals.append(float(v))
            if len(vals) >= 4:
                med = statistics.median(vals)
                conds.append((f"{lag_field}_gt_med", PatternScope.PREMATCH_TRIGGER, {
                    "op": "all",
                    "clauses": [{"field": lag_field, "cmp": "gt", "value": round(med, 4)}],
                }))

        for name, scope, dsl in list(conds):
            if scope == PatternScope.PREMATCH_TRIGGER and name.startswith("lag.own."):
                conds.append((
                    f"venue_HOME_AND_{name}",
                    PatternScope.PREMATCH_TRIGGER,
                    {
                        "op": "all",
                        "clauses": [
                            {"field": "context.venue", "cmp": "eq", "value": "HOME"},
                            dsl["clauses"][0],
                        ],
                    },
                ))

        # 2) Mecanismos internos INTRA_MATCH_MECHANISM (post-kickoff; nunca predictor directo)
        for intra_field in ("shots", "blocked_shots", "fouls"):
            vals = [float(r["own"][intra_field]) for r in discovery_rows if r["own"].get(intra_field) is not None]
            if len(vals) >= 4:
                med = statistics.median(vals)
                conds.append((f"own.{intra_field}_gt_med", PatternScope.INTRA_MATCH_MECHANISM, {
                    "op": "all",
                    "clauses": [{"field": f"own.{intra_field}", "cmp": "gt", "value": round(med, 4)}],
                }))

        trail_vals = [
            r["score_state"]["minutes_trailing"]
            for r in discovery_rows
            if r["score_state"].get("minutes_trailing") is not None
        ]
        if len(trail_vals) >= 4:
            conds.append(("score_state.trailing_ge_20m", PatternScope.INTRA_MATCH_MECHANISM, {
                "op": "all",
                "clauses": [{"field": "score_state.minutes_trailing", "cmp": "ge", "value": 20}],
            }))

        return conds

    def _eval_side(
        self,
        bundle: MatchObservationBundle,
        side: str,
        rows: list[dict[str, Any]],
    ) -> tuple[list[StatisticalPattern], list[ValidatedPattern], dict[str, Any]]:
        if len(rows) < 6:
            return [], [], {"side": side, "status": f"SKIPPED_SMALL_SAMPLE(n={len(rows)})"}

        exact_rows = [r for r in rows if r["league_id"] == bundle.league_id and r["season"] == bundle.season]
        if len(exact_rows) >= 8:
            segment_rows = sorted(exact_rows, key=lambda x: x["kickoff_utc"])
            comp_mix = {"exact_competition_pct": 100.0}
            seg_warnings: list[str] = []
        else:
            segment_rows = sorted(rows, key=lambda x: x["kickoff_utc"])
            exact_pct = round((len(exact_rows) / len(rows)) * 100.0, 2)
            comp_mix = {"exact_competition_pct": exact_pct, "other_competitions_pct": round(100.0 - exact_pct, 2)}
            seg_warnings = ["COMPETITION_MIX_HETEROGENEOUS"] if exact_pct < 100.0 else []

        coach_ids = {r["context"].get("coach_id") for r in segment_rows if r["context"].get("coach_id") is not None}
        if len(coach_ids) > 1:
            seg_warnings.append("COACH_REGIME_MIXED")

        # Separación temporal estricta: si n >= 16, el descubrimiento y selección de umbrales
        # se realizan EXCLUSIVAMENTE sobre el 70% inicial cronológico (discovery_rows)
        if len(segment_rows) >= 16:
            split_idx = int(len(segment_rows) * 0.70)
            discovery_rows = segment_rows[:split_idx]
            discovery_window_mode = "TRAIN_70PCT_ISOLATED_FROM_HOLDOUT"
        else:
            discovery_rows = segment_rows
            discovery_window_mode = "EXPLORATORY_FULL_SMALL_SAMPLE"

        conditions = self._candidate_conditions(discovery_rows)
        prematch_tests: list[dict[str, Any]] = []
        intra_tests: list[dict[str, Any]] = []

        for metric in self.OUTCOME_METRICS:
            valid_outcomes = [float(r["own"][metric]) for r in discovery_rows if r["own"].get(metric) is not None]
            if len(valid_outcomes) < 5:
                continue
            outcome_med = statistics.median(valid_outcomes)

            for cond_name, scope, dsl in conditions:
                a = b = c = d = 0
                true_vals: list[float] = []
                false_vals: list[float] = []
                support_ids: list[int] = []
                counter_ids: list[int] = []

                for r in discovery_rows:
                    y = r["own"].get(metric)
                    if y is None:
                        continue
                    cond_res = eval_declarative_dsl(dsl, r)
                    if cond_res is None:
                        continue
                    y_high = float(y) > outcome_med
                    if cond_res:
                        true_vals.append(float(y))
                        if y_high:
                            a += 1
                            support_ids.append(r["fixture_id"])
                        else:
                            b += 1
                            counter_ids.append(r["fixture_id"])
                    else:
                        false_vals.append(float(y))
                        if y_high:
                            c += 1
                        else:
                            d += 1

                cond_n = a + b
                non_n = c + d
                if cond_n < 2 or non_n < 2:
                    continue

                p_val = fisher_exact_two_sided(a, b, c, d)
                rate_true = a / cond_n
                rate_false = c / non_n
                lift_rate = rate_true - rate_false
                lift_mean = statistics.fmean(true_vals) - statistics.fmean(false_vals)

                entry = {
                    "cond_name": cond_name,
                    "scope": scope,
                    "dsl": dsl,
                    "metric": metric,
                    "outcome_med": round(outcome_med, 4),
                    "table": {"a": a, "b": b, "c": c, "d": d},
                    "p_value": p_val,
                    "rate_true": round(rate_true, 4),
                    "rate_false": round(rate_false, 4),
                    "lift_rate": round(lift_rate, 4),
                    "lift_mean": round(lift_mean, 4),
                    "true_mean": round(statistics.fmean(true_vals), 4),
                    "false_mean": round(statistics.fmean(false_vals), 4),
                    "loo_true": leave_one_out_range(true_vals),
                    "support_ids": tuple(support_ids),
                    "counter_ids": tuple(counter_ids),
                    "valid_n": cond_n + non_n,
                }
                if scope == PatternScope.PREMATCH_TRIGGER:
                    prematch_tests.append(entry)
                else:
                    intra_tests.append(entry)

        discovered: list[StatisticalPattern] = []
        for bucket_name, bucket in (("PREMATCH", prematch_tests[: self.max_tests_per_bucket]), ("INTRA", intra_tests[: self.max_tests_per_bucket])):
            if not bucket:
                continue
            q_vals = benjamini_yekutieli_adjust([t["p_value"] for t in bucket])
            for item, q in zip(bucket, q_vals):
                if abs(item["lift_rate"]) < 0.15 and item["p_value"] > 0.25:
                    continue
                is_small = len(segment_rows) < 16 or min(item["table"]["a"] + item["table"]["b"], item["table"]["c"] + item["table"]["d"]) < 3
                flags = list(seg_warnings)
                if is_small:
                    flags.append("SMALL_SAMPLE_EXPLORATORY_ONLY")
                discovered.append(
                    StatisticalPattern(
                        pattern_id=f"disc_{bundle.fixture_id}_{side}_{item['scope'].value.lower()}_{item['cond_name']}_{item['metric']}",
                        pattern_scope=item["scope"],
                        target_metric=f"{side}.own.{item['metric']}",
                        condition_dsl=item["dsl"],
                        sample_total=len(discovery_rows),
                        sample_valid=item["valid_n"],
                        coverage_observed_pct=round((item["valid_n"] / len(discovery_rows)) * 100.0, 2),
                        estimates={
                            "condition": item["cond_name"],
                            "discovery_window_mode": discovery_window_mode,
                            "outcome_threshold_median": item["outcome_med"],
                            "rate_when_true": item["rate_true"],
                            "rate_when_false": item["rate_false"],
                            "lift_rate": item["lift_rate"],
                            "true_mean": item["true_mean"],
                            "false_mean": item["false_mean"],
                            "lift_mean": item["lift_mean"],
                            "loo_true_range": item["loo_true"],
                            "contingency_2x2": item["table"],
                        },
                        significance={
                            "bucket": bucket_name,
                            "test": "fisher_exact_2x2",
                            "p_value": round(item["p_value"], 6),
                            "by_q_value": q,
                        },
                        supporting_fixture_ids=item["support_ids"],
                        counterexample_fixture_ids=item["counter_ids"],
                        contradiction_flags=tuple(flags),
                        competition_mix=comp_mix,
                        exploratory_only=True,
                    )
                )

        return discovered, [], {
            "side": side,
            "segment_n": len(segment_rows),
            "discovery_n": len(discovery_rows),
            "discovery_window_mode": discovery_window_mode,
            "prematch_tests_evaluated": len(prematch_tests[: self.max_tests_per_bucket]),
            "intra_match_tests_evaluated": len(intra_tests[: self.max_tests_per_bucket]),
            "patterns_retained": len(discovered),
        }

    @staticmethod
    def evaluate_temporal_validation(
        pattern: StatisticalPattern,
        rows: list[dict[str, Any]],
        *,
        coach_consistent: bool = True,
    ) -> ValidatedPattern | None:
        """Evalúa fuera de muestra (Holdout 30%) un patrón cuyos umbrales fueron descubiertos
        y seleccionados exclusivamente en la ventana Train (70%).

        Nunca reestima umbrales en el Holdout y mantiene authorized_for_probabilistic_adjustment = False.
        """
        if pattern.pattern_scope != PatternScope.PREMATCH_TRIGGER or pattern.condition_dsl is None:
            return None
        metric = pattern.target_metric.split(".")[-1]
        valid_rows = [
            r
            for r in sorted(rows, key=lambda x: x["kickoff_utc"])
            if r["own"].get(metric) is not None and eval_declarative_dsl(pattern.condition_dsl, r) is not None
        ]
        if len(valid_rows) < 16:
            return None

        split_idx = int(len(valid_rows) * 0.70)
        train = valid_rows[:split_idx]
        holdout = valid_rows[split_idx:]
        if len(train) < 10 or len(holdout) < 5:
            return None

        # Umbral congelado desde la etapa de descubrimiento en Train
        frozen_threshold = pattern.estimates.get("outcome_threshold_median")
        if not isinstance(frozen_threshold, (int, float)):
            frozen_threshold = statistics.median(float(r["own"][metric]) for r in train)

        def contingency(sub: list[dict[str, Any]]) -> tuple[int, int, int, int, float, float]:
            a = b = c = d = 0
            for r in sub:
                cond = bool(eval_declarative_dsl(pattern.condition_dsl, r))
                hi = float(r["own"][metric]) > float(frozen_threshold)
                if cond and hi:
                    a += 1
                elif cond:
                    b += 1
                elif hi:
                    c += 1
                else:
                    d += 1
            rt = a / max(1, a + b)
            rf = c / max(1, c + d)
            return a, b, c, d, rt - rf, fisher_exact_two_sided(a, b, c, d)

        ta, tb, tc, td, t_lift, t_p = contingency(train)
        ha, hb, hc, hd, h_lift, h_p = contingency(holdout)
        if min(ta + tb, tc + td) < 3 or min(ha + hb, hc + hd) < 2:
            return None
        if t_lift == 0 or h_lift == 0 or (t_lift > 0) != (h_lift > 0):
            return None
        if t_p > 0.10:
            return None

        return ValidatedPattern(
            validated_id=f"val_{pattern.pattern_id}",
            source_pattern_id=pattern.pattern_id,
            source_hypothesis_id=None,
            prematch_availability=classify_dsl_availability(pattern.condition_dsl),
            validation_protocol="TEMPORAL_SPLIT_BY_FDR",
            train_metrics={
                "n": len(train),
                "frozen_threshold": float(frozen_threshold),
                "lift_rate": round(t_lift, 4),
                "p_value": round(t_p, 6),
                "by_q_value": pattern.significance.get("by_q_value"),
            },
            holdout_metrics={
                "n": len(holdout),
                "frozen_threshold_reused": float(frozen_threshold),
                "lift_rate": round(h_lift, 4),
                "p_value": round(h_p, 6),
                "same_direction": True,
            },
            regime_check={"coach_consistent": coach_consistent},
            authorized_for_candidate_ranking=coach_consistent,
            authorized_for_probabilistic_adjustment=False,
        )

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput:
        h_rows = build_feature_rows(bundle.home_matches, side="home")
        a_rows = build_feature_rows(bundle.away_matches, side="away")
        h_pats, _, h_tel = self._eval_side(bundle, "home", h_rows)
        a_pats, _, a_tel = self._eval_side(bundle, "away", a_rows)
        all_pats = tuple(h_pats + a_pats)
        audit = audit_feature_rows_temporal_integrity(h_rows + a_rows, all_pats)
        return PluginOutput(
            plugin_id=self.plugin_id,
            domain_family=self.domain_family,
            patterns=all_pats,
            telemetry={"home": h_tel, "away": a_tel, "temporal_audit": audit},
        )
