"""Capa E: Evaluación de mercados Bwin y comparación frente al motor anterior."""

from .bwin_evaluator import (
    FORMAL_DECISION_CLASSIFICATIONS,
    BwinMarketEvaluator,
    EvaluatedMarketOption,
    classify_option_formal_decision,
)
from .market_equivalence import (
    CanonicalContract,
    are_contracts_equivalent,
    audit_snapshot_equivalences,
    canonicalize_selection_key,
)
from .staking_and_policy import (
    DEFAULT_BANKROLL,
    DEFAULT_CURRENCY,
    DEFAULT_KELLY_FRACTION,
    MAX_MATCH_EXPOSURE_PCT,
    MAX_PICKS_PER_MATCH,
    MAX_SINGLE_STAKE_PCT,
    MIN_SINGLE_STAKE_PCT,
    allocate_match_portfolio_stakes,
    compute_kelly_stake_pct,
    compute_market_devig_map,
    correlation_axis_for_family,
    evaluate_collective_quant_policy,
    evaluate_player_prop_quant,
    extract_min_acceptable_odd,
    is_cross_league_club_competition,
)

__all__ = [
    "BwinMarketEvaluator",
    "CanonicalContract",
    "DEFAULT_BANKROLL",
    "DEFAULT_CURRENCY",
    "DEFAULT_KELLY_FRACTION",
    "EvaluatedMarketOption",
    "FORMAL_DECISION_CLASSIFICATIONS",
    "MAX_MATCH_EXPOSURE_PCT",
    "MAX_PICKS_PER_MATCH",
    "MAX_SINGLE_STAKE_PCT",
    "MIN_SINGLE_STAKE_PCT",
    "allocate_match_portfolio_stakes",
    "are_contracts_equivalent",
    "audit_snapshot_equivalences",
    "canonicalize_selection_key",
    "classify_option_formal_decision",
    "compute_kelly_stake_pct",
    "compute_market_devig_map",
    "correlation_axis_for_family",
    "evaluate_collective_quant_policy",
    "evaluate_player_prop_quant",
    "extract_min_acceptable_odd",
    "is_cross_league_club_competition",
]
