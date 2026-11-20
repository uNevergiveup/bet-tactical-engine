"""Capa D: Modelización probabilística coherente y validación temporal fuera de muestra."""

from .calibration import (
    WalkForwardComparisonResult,
    WalkForwardEvaluator,
    brier_score,
    expected_calibration_error,
    log_loss,
    multiclass_1x2_log_loss,
    paired_bootstrap_delta_ci,
    ranked_probability_score_1x2,
    summarize_probability_calibration,
)
from .distributions import (
    CoherentMatchModeler,
    FamilyDistributionEstimate,
    MatchProbabilisticModel,
    build_count_pmf,
    convolve_pmfs,
    dixon_coles_joint_grid,
    negbin_pmf,
    poisson_pmf,
)
from .prospective_cand_2b3b import (
    CANDIDATE_2B3B_VERSION,
    evaluate_2b3b_prospective_records,
    evaluate_parallel_bundle_2b3b,
    record_2b3b_batch,
    record_2b3b_prospective_prediction,
    settle_2b3b_prospective_record,
)

__all__ = [
    "CANDIDATE_2B3B_VERSION",
    "CoherentMatchModeler",
    "FamilyDistributionEstimate",
    "MatchProbabilisticModel",
    "WalkForwardComparisonResult",
    "WalkForwardEvaluator",
    "brier_score",
    "build_count_pmf",
    "convolve_pmfs",
    "dixon_coles_joint_grid",
    "evaluate_2b3b_prospective_records",
    "evaluate_parallel_bundle_2b3b",
    "expected_calibration_error",
    "log_loss",
    "multiclass_1x2_log_loss",
    "negbin_pmf",
    "paired_bootstrap_delta_ci",
    "poisson_pmf",
    "ranked_probability_score_1x2",
    "record_2b3b_batch",
    "record_2b3b_prospective_prediction",
    "settle_2b3b_prospective_record",
    "summarize_probability_calibration",
]
