"""Cliente unificado de acceso a API-Football con reutilización de las 3 cachés existentes,
telemetría por familia/endpoint y bloqueo estricto offline salvo autorización explícita.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.contracts.epistemic import TemporalLeakageError, assert_prematch_cutoff, parse_utc_timestamp
from prematch_phase1 import CacheMissOffline, Phase1ApiClient


class CacheMissOfflineError(RuntimeError):
    """Error cuando se solicita un endpoint no presente en caché en modo offline."""


@dataclass
class UnifiedApiTelemetry:
    """Telemetría de adquisición exigida por la Decisión 11A."""

    cache_hits: int = 0
    legacy_cache_hits: int = 0
    api_calls_effective: int = 0
    empty_results: int = 0
    cache_misses: int = 0
    coverage_by_family: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, endpoint: str, source: str, response: list[Any]) -> None:
        if source == "cache":
            self.cache_hits += 1
        elif source.startswith("legacy_"):
            self.legacy_cache_hits += 1
        elif source == "api":
            self.api_calls_effective += 1

        family = self.coverage_by_family.setdefault(
            endpoint,
            {"hits": 0, "non_empty": 0, "empty": 0},
        )
        family["hits"] += 1
        if response:
            family["non_empty"] += 1
        else:
            family["empty"] += 1
            self.empty_results += 1

    def to_dict(self, max_new_calls: int = 0) -> dict[str, Any]:
        return {
            "cache_hits": self.cache_hits,
            "legacy_cache_hits": self.legacy_cache_hits,
            "api_calls_effective": self.api_calls_effective,
            "empty_results": self.empty_results,
            "cache_misses": self.cache_misses,
            "budget_remaining": max(0, max_new_calls - self.api_calls_effective),
            "coverage_by_family": self.coverage_by_family,
        }


class UnifiedApiClient:
    """Fachada unificada sobre Phase1ApiClient + cachés legacy (generate_context & preselect_api).

    - Reutiliza outputs/.api_football_history_cache, outputs/data/cache y outputs/data/cache/preselect_api.
    - Garantiza offline_only=True por defecto en Etapa 2 para proteger cuota real.
    - Verifica cutoff_utc sobre fetched_at_utc para evitar fuga temporal.
    """

    def __init__(
        self,
        root: Path,
        *,
        offline_only: bool = True,
        cutoff_utc: str | datetime | None = None,
        max_new_calls: int = 0,
        api_key: str = "",
    ) -> None:
        self.root = Path(root)
        self.offline_only = offline_only
        self.cutoff_utc = parse_utc_timestamp(cutoff_utc) if cutoff_utc is not None else None
        self.max_new_calls = 0 if offline_only else max(0, int(max_new_calls))
        self.telemetry = UnifiedApiTelemetry()

        cache_dir = self.root / "outputs" / ".api_football_history_cache"
        clock = (lambda: self.cutoff_utc) if self.cutoff_utc is not None else (lambda: datetime.now(timezone.utc))
        self._client = Phase1ApiClient(
            cache_dir=cache_dir,
            key=api_key,
            offline=offline_only,
            max_new_calls=self.max_new_calls,
            clock=clock,
        )

    def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """Obtiene el resultado desde caché unificada/legacy o API (si offline_only=False)."""
        try:
            res = self._client.get(endpoint, **params)
        except CacheMissOffline as exc:
            self.telemetry.cache_misses += 1
            raise CacheMissOfflineError(str(exc)) from exc

        if self.cutoff_utc is not None and res.fetched_at_utc:
            assert_prematch_cutoff(
                res.fetched_at_utc,
                self.cutoff_utc,
                label=f"{endpoint}:{res.params}",
            )

        self.telemetry.record(endpoint, res.source, res.response)
        return {
            "endpoint": res.endpoint,
            "parameters": res.params,
            "response": res.response,
            "source": res.source,
            "cache_path": str(res.cache_path) if res.cache_path else None,
            "fetched_at_utc": res.fetched_at_utc,
            "completeness": res.completeness,
        }

    def snapshot_telemetry(self) -> dict[str, Any]:
        return self.telemetry.to_dict(max_new_calls=self.max_new_calls)
