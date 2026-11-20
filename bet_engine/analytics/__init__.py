"""Capa C: Núcleo de inteligencia analítica, registro de plugins e investigador táctico."""

from .discovery import (
    InteractionMinerPlugin,
    audit_feature_rows_temporal_integrity,
    benjamini_yekutieli_adjust,
    classify_dsl_availability,
    fisher_exact_two_sided,
    leave_one_out_range,
)
from .investigator import (
    HypothesisRegistry,
    LLMInvestigatorPort,
    SubprocessCLILlmAdapter,
    TacticalInvestigator,
    VerifiedHypothesisRecord,
    verify_hypotheses,
)
from .plugins import (
    MultiWindowMatchupPlugin,
    RedCardAndHalfDynamicsPlugin,
    RefereeAndScheduleContextPlugin,
    ScoreStateTimelinePlugin,
    SoSAdjustedPlugin,
    TacticalArchetypeDuelPlugin,
    build_default_registry,
)
from .registry import (
    AnalyticsEngineResult,
    PatternPlugin,
    PatternRegistry,
    PluginOutput,
    build_feature_rows,
    eval_declarative_dsl,
    extract_dsl_fields,
    reconstruct_score_state_timeline,
)


def build_stage3_registry() -> PatternRegistry:
    """Construye el registro completo de la Etapa 3 incluyendo minería de interacciones."""
    reg = build_default_registry()
    reg.register(InteractionMinerPlugin())
    return reg


__all__ = [
    "AnalyticsEngineResult",
    "HypothesisRegistry",
    "InteractionMinerPlugin",
    "LLMInvestigatorPort",
    "MultiWindowMatchupPlugin",
    "PatternPlugin",
    "PatternRegistry",
    "PluginOutput",
    "RedCardAndHalfDynamicsPlugin",
    "RefereeAndScheduleContextPlugin",
    "ScoreStateTimelinePlugin",
    "SoSAdjustedPlugin",
    "SubprocessCLILlmAdapter",
    "TacticalArchetypeDuelPlugin",
    "TacticalInvestigator",
    "VerifiedHypothesisRecord",
    "audit_feature_rows_temporal_integrity",
    "benjamini_yekutieli_adjust",
    "build_default_registry",
    "build_feature_rows",
    "build_stage3_registry",
    "classify_dsl_availability",
    "eval_declarative_dsl",
    "extract_dsl_fields",
    "fisher_exact_two_sided",
    "leave_one_out_range",
    "reconstruct_score_state_timeline",
    "verify_hypotheses",
]
