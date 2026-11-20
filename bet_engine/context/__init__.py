"""Capa B: Contexto deportivo canónico, ObservationStore y evidencia contextual."""

from .history_quality import (
    audit_historical_window_quality,
    audit_match_history_signals,
    build_point_in_time_opponent_lookup,
)
from .lineup_manager import (
    CONFIRMED_LINEUP_STATUSES,
    DEFAULT_LINEUP_CUTOFF_MINUTES,
    MANUAL_LINEUP_SCHEMA,
    LineupStatus,
    compare_probable_vs_confirmed_absences,
    evaluate_fixture_lineup_state,
    extract_confirmed_lineup_from_research,
    is_confirmed_complete,
    load_and_validate_manual_lineup_file,
    prepare_manual_lineup_inbox,
    scan_manual_lineup_inbox,
    select_next_eligible_ranked_candidate,
    validate_manual_lineup_payload,
)
from .observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    ObservationStore,
    WindowSummary,
)
from .quick_context import (
    QUICK_CONTEXT_SCHEMA,
    import_quick_context_markdown,
    load_quick_context_bundle,
    parse_quick_context_markdown,
)

__all__ = [
    "CONFIRMED_LINEUP_STATUSES",
    "DEFAULT_LINEUP_CUTOFF_MINUTES",
    "HistoricalTeamMatch",
    "LineupStatus",
    "MANUAL_LINEUP_SCHEMA",
    "MatchObservationBundle",
    "ObservationStore",
    "QUICK_CONTEXT_SCHEMA",
    "WindowSummary",
    "audit_historical_window_quality",
    "audit_match_history_signals",
    "build_point_in_time_opponent_lookup",
    "compare_probable_vs_confirmed_absences",
    "evaluate_fixture_lineup_state",
    "extract_confirmed_lineup_from_research",
    "import_quick_context_markdown",
    "is_confirmed_complete",
    "load_and_validate_manual_lineup_file",
    "load_quick_context_bundle",
    "parse_quick_context_markdown",
    "prepare_manual_lineup_inbox",
    "scan_manual_lineup_inbox",
    "select_next_eligible_ranked_candidate",
    "validate_manual_lineup_payload",
]
