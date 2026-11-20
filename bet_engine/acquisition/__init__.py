"""Capa A: Adquisición unificada, reutilización de cachés y evaluación de cobertura."""

from .api_client import CacheMissOfflineError, UnifiedApiClient, UnifiedApiTelemetry
from .coverage import CoverageAuditResult, CoveragePolicyEvaluator, to_coverage_flag

__all__ = [
    "CacheMissOfflineError",
    "CoverageAuditResult",
    "CoveragePolicyEvaluator",
    "UnifiedApiClient",
    "UnifiedApiTelemetry",
    "to_coverage_flag",
]
