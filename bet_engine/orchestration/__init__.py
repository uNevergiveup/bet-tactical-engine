"""Capa F: Orquestación en Shadow Mode, ciclo T-24h (preanalyze/status/finalize)
y planificación secuencial por proximidad al kickoff.
"""

from .adapters import (
    run_daily_shadow_lifecycle,
    run_shadow_daily_optin,
    run_shadow_for_event_dir,
)
from .multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorAdapters,
    OrchestratorConfig,
    resolve_or_generate_preselection,
)
from .multi_ranker import (
    apply_dynamic_ranking_update,
    populate_and_rank_plan,
    select_best_eligible_for_slot,
)
from .scheduler import PrematchLifecycleController, compute_analytical_quality_score
from .shadow_pipeline import ShadowPipelineRunner, ShadowRunReport, parse_bwin_odds_txt

__all__ = [
    "MultiSlotOrchestrator",
    "OrchestratorAdapters",
    "OrchestratorConfig",
    "PrematchLifecycleController",
    "ShadowPipelineRunner",
    "ShadowRunReport",
    "apply_dynamic_ranking_update",
    "compute_analytical_quality_score",
    "parse_bwin_odds_txt",
    "populate_and_rank_plan",
    "resolve_or_generate_preselection",
    "run_daily_shadow_lifecycle",
    "run_shadow_daily_optin",
    "run_shadow_for_event_dir",
    "select_best_eligible_for_slot",
]
