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

__all__ = [
    "BwinMarketEvaluator",
    "CanonicalContract",
    "EvaluatedMarketOption",
    "FORMAL_DECISION_CLASSIFICATIONS",
    "are_contracts_equivalent",
    "audit_snapshot_equivalences",
    "canonicalize_selection_key",
    "classify_option_formal_decision",
]
