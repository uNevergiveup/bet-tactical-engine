"""Contratos de planificación PREMATCH: preanálisis T-24h y planificador secuencial
por proximidad al kickoff, manteniendo separado el ranking de calidad de la urgencia temporal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from .epistemic import (
    CONTRACT_SCHEMA_VERSION,
    EpistemicContractError,
    parse_utc_timestamp,
)


class ScheduleExecutionStatus(str, Enum):
    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    REJECTED_QUALITY = "REJECTED_QUALITY"
    REJECTED_LEAD_TIME = "REJECTED_LEAD_TIME"
    REJECTED_MISSING_LINEUPS = "REJECTED_MISSING_LINEUPS"
    RESERVED_WAIT_SUPERIOR = "RESERVED_WAIT_SUPERIOR"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED_ISOLATED = "FAILED_ISOLATED"


@dataclass(frozen=True)
class MatchScheduleCandidate:
    """Candidato planificable. La proximidad temporal NO altera analytical_quality_score."""

    event_id: str
    fixture_id: int
    league_id: int
    season: int
    home_name: str
    away_name: str
    kickoff_utc: str
    coverage_tier: str  # A_FULL | B_PARTIAL | C_LOW | D_MINIMAL
    analytical_quality_score: float  # 0..100, independiente del reloj
    min_quality_passed: bool
    estimated_duration_minutes: int = 12
    min_lead_minutes: int = 45
    safety_margin_minutes: int = 10
    execution_status: ScheduleExecutionStatus = ScheduleExecutionStatus.PENDING
    t24_snapshot_id: str | None = None
    preliminary_risk_score: float | None = None
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        parse_utc_timestamp(self.kickoff_utc)
        if not (0.0 <= self.analytical_quality_score <= 100.0):
            raise EpistemicContractError("ANALYTICAL_QUALITY_SCORE_OUT_OF_RANGE")

    def latest_start_utc(self) -> datetime:
        """Último instante permitido para iniciar análisis respetando lead + margen de seguridad."""
        kickoff = parse_utc_timestamp(self.kickoff_utc)
        return kickoff - timedelta(minutes=self.min_lead_minutes + self.safety_margin_minutes)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["execution_status"] = self.execution_status.value
        return data


@dataclass(frozen=True)
class PreAnalysisT24Snapshot:
    """Snapshot de preanálisis T-24h: investiga históricos/patrones sin emitir picks definitivos."""

    snapshot_id: str
    target_date_local: str
    generated_at_utc: str
    candidates: tuple[MatchScheduleCandidate, ...]
    cache_manifest: dict[str, Any] = field(default_factory=dict)
    final_picks_generated: bool = False
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        parse_utc_timestamp(self.generated_at_utc)
        if self.final_picks_generated:
            raise EpistemicContractError(
                "T24_PREANALYSIS_CANNOT_EMIT_FINAL_PICKS: los picks definitivos requieren ejecución cerca del kickoff"
            )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [c.to_dict() for c in self.candidates]
        return data


@dataclass(frozen=True)
class ProximityPlanState:
    """Estado del planificador secuencial por proximidad desde una hora de inicio configurable."""

    plan_id: str
    target_date_local: str
    available_from_local: str = "13:00"
    timezone_name: str = "Europe/Madrid"
    min_quality_score: float = 55.0
    superior_quality_delta: float = 15.0
    candidates: tuple[MatchScheduleCandidate, ...] = ()
    active_event_id: str | None = None
    completed_event_ids: tuple[str, ...] = ()
    failed_event_ids: tuple[str, ...] = ()
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def available_from_utc(self) -> datetime:
        tz = ZoneInfo(self.timezone_name)
        hour_str, min_str = self.available_from_local.split(":")
        day = datetime.fromisoformat(self.target_date_local).date()
        local_dt = datetime(day.year, day.month, day.day, int(hour_str), int(min_str), tzinfo=tz)
        return local_dt.astimezone(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [c.to_dict() for c in self.candidates]
        return data


def select_next_proximity_candidate(
    plan: ProximityPlanState,
    now_utc: str | datetime,
    manual_override_event_id: str | None = None,
) -> dict[str, Any]:
    """Selecciona el siguiente candidato ejecutable (1 partido a la vez) respetando:
    1) Hora de disponibilidad (default 13:00 Europe/Madrid)
    2) Calidad analítica mínima y ventana PREMATCH (lead + margen de seguridad)
    3) Reserva de capacidad si un partido próximo es claramente superior en calidad
    4) Orden por proximidad al kickoff; desempate por calidad y selección manual opcional.
    """
    now_dt = parse_utc_timestamp(now_utc)
    avail_dt = plan.available_from_utc()
    if now_dt < avail_dt:
        return {
            "status": "WAITING_AVAILABLE_TIME",
            "available_from_utc": avail_dt.isoformat(),
            "selected_event_id": None,
            "eligible_count": 0,
        }

    if plan.active_event_id is not None:
        return {
            "status": "BUSY_RUNNING_MATCH",
            "selected_event_id": None,
            "active_event_id": plan.active_event_id,
            "eligible_count": 0,
        }

    done = set(plan.completed_event_ids) | set(plan.failed_event_ids)
    eligible: list[MatchScheduleCandidate] = []
    rejected: list[dict[str, str]] = []

    for c in plan.candidates:
        if c.event_id in done:
            continue
        if not c.min_quality_passed or c.analytical_quality_score < plan.min_quality_score:
            rejected.append({"event_id": c.event_id, "reason": "REJECTED_QUALITY"})
            continue
        if now_dt > c.latest_start_utc():
            rejected.append({"event_id": c.event_id, "reason": "REJECTED_LEAD_TIME"})
            continue
        eligible.append(c)

    if not eligible:
        return {
            "status": "NO_ELIGIBLE_CANDIDATES",
            "selected_event_id": None,
            "eligible_count": 0,
            "rejected": rejected,
        }

    if manual_override_event_id:
        for c in eligible:
            if c.event_id == manual_override_event_id:
                return {
                    "status": "SELECTED_MANUAL_OVERRIDE",
                    "selected_event_id": c.event_id,
                    "kickoff_utc": c.kickoff_utc,
                    "analytical_quality_score": c.analytical_quality_score,
                    "eligible_count": len(eligible),
                    "rejected": rejected,
                }

    # Ordena primero por proximidad al kickoff, luego por mayor calidad analítica.
    eligible_sorted = sorted(
        eligible,
        key=lambda c: (parse_utc_timestamp(c.kickoff_utc), -c.analytical_quality_score, c.event_id),
    )

    first = eligible_sorted[0]
    first_finish = now_dt + timedelta(minutes=first.estimated_duration_minutes)

    # Reserva de capacidad: si un partido cercano en ventana es claramente superior en calidad
    # y ejecutar el primero invadiría el latest_start_utc del partido superior, reservar para el superior.
    for later in eligible_sorted[1:]:
        if (later.analytical_quality_score - first.analytical_quality_score) >= plan.superior_quality_delta:
            if first_finish > later.latest_start_utc():
                return {
                    "status": "RESERVED_FOR_SUPERIOR_MATCH",
                    "selected_event_id": later.event_id,
                    "deferred_event_id": first.event_id,
                    "kickoff_utc": later.kickoff_utc,
                    "analytical_quality_score": later.analytical_quality_score,
                    "eligible_count": len(eligible),
                    "rejected": rejected,
                }

    return {
        "status": "SELECTED_BY_PROXIMITY",
        "selected_event_id": first.event_id,
        "kickoff_utc": first.kickoff_utc,
        "analytical_quality_score": first.analytical_quality_score,
        "eligible_count": len(eligible),
        "rejected": rejected,
    }
