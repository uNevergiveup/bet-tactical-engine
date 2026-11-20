"""Contratos y máquina de estados V3.0 para orquestación multipartido por franjas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

MADRID_TZ = ZoneInfo("Europe/Madrid")
UTC = timezone.utc

MULTI_SLOT_SCHEMA_VERSION = "3.0-multi-slot-orchestration"

DEFAULT_SLOT_DEFINITIONS: tuple[tuple[str, str, str, str, int], ...] = (
    ("FRANJA_A", "Franja A (15:00-18:00)", "15:00", "18:00", 1),
    ("FRANJA_B", "Franja B (18:00-21:00)", "18:00", "21:00", 1),
    ("FRANJA_C", "Franja C (21:00-03:00)", "21:00", "03:00", 1),
)


class MultiSlotContractError(ValueError):
    """Error estructural, de unicidad o de transición inválida en el plan multipartido."""


class MultiMatchExecutionState(str, Enum):
    DISCOVERED = "DISCOVERED"
    PRESELECTED = "PRESELECTED"
    RANKED = "RANKED"
    SELECTED = "SELECTED"
    PREPARING = "PREPARING"
    WAITING_RESEARCH = "WAITING_RESEARCH"
    WAITING_LINEUPS = "WAITING_LINEUPS"
    READY_FOR_FINALIZATION = "READY_FOR_FINALIZATION"
    FINALIZED = "FINALIZED"
    NO_BET = "NO_BET"
    SKIPPED_INELIGIBLE = "SKIPPED_INELIGIBLE"
    REJECTED_MISSING_LINEUPS = "REJECTED_MISSING_LINEUPS"
    FAILED_TECHNICAL = "FAILED_TECHNICAL"
    EXPIRED = "EXPIRED"


TERMINAL_EXECUTION_STATES: frozenset[MultiMatchExecutionState] = frozenset(
    {
        MultiMatchExecutionState.FINALIZED,
        MultiMatchExecutionState.NO_BET,
        MultiMatchExecutionState.SKIPPED_INELIGIBLE,
        MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
        MultiMatchExecutionState.FAILED_TECHNICAL,
        MultiMatchExecutionState.EXPIRED,
    }
)

# Estados cuyo expediente ya fue cerrado operativamente y no deben reescribirse al reordenar.
CLOSED_DOSSIER_STATES: frozenset[MultiMatchExecutionState] = frozenset(
    {
        MultiMatchExecutionState.FINALIZED,
        MultiMatchExecutionState.NO_BET,
        MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
        MultiMatchExecutionState.FAILED_TECHNICAL,
        MultiMatchExecutionState.EXPIRED,
    }
)

ACTIVE_SLOT_STATES: frozenset[MultiMatchExecutionState] = frozenset(
    {
        MultiMatchExecutionState.SELECTED,
        MultiMatchExecutionState.PREPARING,
        MultiMatchExecutionState.WAITING_RESEARCH,
        MultiMatchExecutionState.WAITING_LINEUPS,
        MultiMatchExecutionState.READY_FOR_FINALIZATION,
    }
)

PROTECTED_DOSSIER_STATES: frozenset[MultiMatchExecutionState] = ACTIVE_SLOT_STATES | frozenset(
    {
        MultiMatchExecutionState.FINALIZED,
        MultiMatchExecutionState.NO_BET,
        MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
        MultiMatchExecutionState.FAILED_TECHNICAL,
    }
)

ALLOWED_STATE_TRANSITIONS: dict[MultiMatchExecutionState, frozenset[MultiMatchExecutionState]] = {
    MultiMatchExecutionState.DISCOVERED: frozenset(
        {
            MultiMatchExecutionState.PRESELECTED,
            MultiMatchExecutionState.RANKED,
            MultiMatchExecutionState.SKIPPED_INELIGIBLE,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.PRESELECTED: frozenset(
        {
            MultiMatchExecutionState.RANKED,
            MultiMatchExecutionState.SKIPPED_INELIGIBLE,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.RANKED: frozenset(
        {
            MultiMatchExecutionState.RANKED,
            MultiMatchExecutionState.SELECTED,
            MultiMatchExecutionState.SKIPPED_INELIGIBLE,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.SELECTED: frozenset(
        {
            MultiMatchExecutionState.PREPARING,
            MultiMatchExecutionState.WAITING_RESEARCH,
            MultiMatchExecutionState.WAITING_LINEUPS,
            MultiMatchExecutionState.SKIPPED_INELIGIBLE,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.PREPARING: frozenset(
        {
            MultiMatchExecutionState.WAITING_RESEARCH,
            MultiMatchExecutionState.WAITING_LINEUPS,
            MultiMatchExecutionState.READY_FOR_FINALIZATION,
            MultiMatchExecutionState.NO_BET,
            MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
            MultiMatchExecutionState.SKIPPED_INELIGIBLE,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.WAITING_RESEARCH: frozenset(
        {
            MultiMatchExecutionState.PREPARING,
            MultiMatchExecutionState.WAITING_RESEARCH,
            MultiMatchExecutionState.WAITING_LINEUPS,
            MultiMatchExecutionState.READY_FOR_FINALIZATION,
            MultiMatchExecutionState.NO_BET,
            MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.WAITING_LINEUPS: frozenset(
        {
            MultiMatchExecutionState.PREPARING,
            MultiMatchExecutionState.WAITING_LINEUPS,
            MultiMatchExecutionState.WAITING_RESEARCH,
            MultiMatchExecutionState.READY_FOR_FINALIZATION,
            MultiMatchExecutionState.NO_BET,
            MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.READY_FOR_FINALIZATION: frozenset(
        {
            MultiMatchExecutionState.PREPARING,
            MultiMatchExecutionState.READY_FOR_FINALIZATION,
            MultiMatchExecutionState.WAITING_RESEARCH,
            MultiMatchExecutionState.FINALIZED,
            MultiMatchExecutionState.NO_BET,
            MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
            MultiMatchExecutionState.FAILED_TECHNICAL,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.FINALIZED: frozenset(),
    MultiMatchExecutionState.NO_BET: frozenset(),
    MultiMatchExecutionState.SKIPPED_INELIGIBLE: frozenset(
        {
            MultiMatchExecutionState.RANKED,
            MultiMatchExecutionState.EXPIRED,
        }
    ),
    MultiMatchExecutionState.REJECTED_MISSING_LINEUPS: frozenset(),
    MultiMatchExecutionState.FAILED_TECHNICAL: frozenset(),
    MultiMatchExecutionState.EXPIRED: frozenset(),
}

FINANCIAL_DECISION_TAXONOMY: tuple[str, ...] = (
    "PICK_ACCIONABLE",
    "CANDIDATO_CONDICIONAL",
    "NO_BET_POR_PRECIO",
    "NO_BET_POR_EVIDENCIA",
    "BLOQUEO_TECNICO",
    "MERCADO_NO_MODELADO",
)


def parse_iso_utc(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        dt = raw
    else:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_clock_hhmm(raw: str) -> time:
    parts = str(raw).strip().split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise MultiSlotContractError(f"Horario HH:MM inválido: {raw!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise MultiSlotContractError(f"Horario fuera de rango: {raw!r}")
    return time(hour=hour, minute=minute)


def _madrid_local_to_utc(local_day: date, clock: time) -> datetime:
    # Conversión segura ante cambios CET/CEST: normaliza al instante real UTC y regresa a Europe/Madrid.
    naive = datetime.combine(local_day, clock)
    aware = naive.replace(tzinfo=MADRID_TZ)
    return aware.astimezone(UTC)


@dataclass(frozen=True)
class TimeSlotSpec:
    """Definición de una franja horaria semiabierta [start, end) dentro de la ventana diaria."""

    slot_id: str
    label: str
    start_hhmm: str
    end_hhmm: str
    slot_index: int
    start_utc: str
    end_utc: str
    start_madrid: str
    end_madrid: str
    max_active_matches: int = 1

    def contains(self, kickoff: str | datetime) -> bool:
        ko_utc = parse_iso_utc(kickoff)
        return parse_iso_utc(self.start_utc) <= ko_utc < parse_iso_utc(self.end_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimeSlotSpec":
        return cls(**data)


def build_window_slots(
    window_day: date,
    window_start_hhmm: str = "15:00",
    window_end_hhmm: str = "03:00",
    slot_definitions: list[tuple[str, str, str, str, int]] | tuple[tuple[str, str, str, str, int], ...] | None = None,
) -> tuple[datetime, datetime, list[TimeSlotSpec]]:
    """Construye la ventana operativa en Europe/Madrid (incluyendo cruce de medianoche y DST) y sus franjas [inicio, fin)."""
    w_start_t = parse_clock_hhmm(window_start_hhmm)
    w_end_t = parse_clock_hhmm(window_end_hhmm)
    w_start_utc = _madrid_local_to_utc(window_day, w_start_t)
    w_end_day = window_day + timedelta(days=1) if w_end_t <= w_start_t else window_day
    w_end_utc = _madrid_local_to_utc(w_end_day, w_end_t)
    if w_end_utc <= w_start_utc:
        raise MultiSlotContractError("La ventana diaria debe tener fin posterior al inicio")

    defs = list(slot_definitions or DEFAULT_SLOT_DEFINITIONS)
    specs: list[TimeSlotSpec] = []
    prev_end_utc: datetime | None = None
    for idx, item in enumerate(defs):
        slot_id, label, s_hhmm, e_hhmm, max_active = item
        s_t = parse_clock_hhmm(s_hhmm)
        e_t = parse_clock_hhmm(e_hhmm)
        s_day = window_day + timedelta(days=1) if s_t < w_start_t else window_day
        e_day = s_day + timedelta(days=1) if e_t <= s_t else s_day
        s_utc = _madrid_local_to_utc(s_day, s_t)
        e_utc = _madrid_local_to_utc(e_day, e_t)
        if e_utc <= s_utc:
            raise MultiSlotContractError(f"Franja {slot_id} inválida: fin <= inicio")
        if s_utc < w_start_utc or e_utc > w_end_utc:
            raise MultiSlotContractError(
                f"Franja {slot_id} [{s_utc.isoformat()}, {e_utc.isoformat()}) fuera de la ventana operativa"
            )
        if prev_end_utc is not None and s_utc < prev_end_utc:
            raise MultiSlotContractError(f"Franja {slot_id} solapada con la anterior")
        prev_end_utc = e_utc
        specs.append(
            TimeSlotSpec(
                slot_id=slot_id,
                label=label,
                start_hhmm=s_hhmm,
                end_hhmm=e_hhmm,
                slot_index=idx,
                start_utc=s_utc.isoformat(),
                end_utc=e_utc.isoformat(),
                start_madrid=s_utc.astimezone(MADRID_TZ).isoformat(),
                end_madrid=e_utc.astimezone(MADRID_TZ).isoformat(),
                max_active_matches=int(max_active),
            )
        )
    return w_start_utc, w_end_utc, specs


def parse_custom_slots_spec(raw: str) -> list[tuple[str, str, str, str, int]]:
    """Parsea formato compacto 'FRANJA_A=15:00-18:00,FRANJA_B=18:00-21:00,FRANJA_C=21:00-03:00'."""
    items: list[tuple[str, str, str, str, int]] = []
    for idx, chunk in enumerate(x.strip() for x in raw.split(",") if x.strip()):
        if "=" in chunk:
            slot_id, rng = chunk.split("=", 1)
        else:
            slot_id, rng = f"FRANJA_{chr(65 + idx)}", chunk
        if "-" not in rng:
            raise MultiSlotContractError(f"Rango de franja inválido: {chunk!r}")
        s_hhmm, e_hhmm = rng.split("-", 1)
        parse_clock_hhmm(s_hhmm)
        parse_clock_hhmm(e_hhmm)
        slot_id = slot_id.strip()
        items.append((slot_id, f"{slot_id} ({s_hhmm.strip()}-{e_hhmm.strip()})", s_hhmm.strip(), e_hhmm.strip(), 1))
    if not items:
        raise MultiSlotContractError("No se especificaron franjas válidas")
    return items


@dataclass
class SlotCandidateRecord:
    """Registro por partido dentro del planificador multipartido con dimensiones separadas."""

    slot_id: str | None
    fixture_id: int
    event_id: int | None
    kickoff_utc: str
    kickoff_madrid: str
    league_name: str
    global_rank: int | None = None
    slot_rank: int | None = None
    quality_score: float | None = None
    eligibility_status: str = "PENDING_VALIDATION"
    execution_state: MultiMatchExecutionState = MultiMatchExecutionState.DISCOVERED
    lineup_state: str = "LINEUPS_PENDING"
    odds_state: str = "NOT_CAPTURED"
    research_state: str = "NOT_REQUESTED"
    shadow_decision: str | None = None
    fallback_parent_fixture_id: int | None = None
    last_updated_utc: str = ""
    home_team: str = ""
    away_team: str = ""
    league_id: int | None = None
    season: int | None = None
    audit_outcome: str = "PENDING"
    blocking_reasons: list[str] = field(default_factory=list)
    quality_components: dict[str, Any] = field(default_factory=dict)
    fallback_chain_depth: int = 0
    attempts: int = 0
    candidate_selection: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def transition_to(self, new_state: MultiMatchExecutionState, now_utc: datetime, reason: str = "") -> None:
        cur = self.execution_state
        if cur == new_state:
            self.last_updated_utc = parse_iso_utc(now_utc).isoformat()
            if reason and reason not in self.notes:
                self.notes.append(reason)
            return
        allowed = ALLOWED_STATE_TRANSITIONS.get(cur, frozenset())
        if new_state not in allowed:
            raise MultiSlotContractError(
                f"Transición de estado inválida para fixture {self.fixture_id}: {cur.value} -> {new_state.value}"
            )
        self.execution_state = new_state
        self.last_updated_utc = parse_iso_utc(now_utc).isoformat()
        if reason:
            self.notes.append(f"{self.last_updated_utc}: {cur.value}->{new_state.value} ({reason})")

    def is_closed_dossier(self) -> bool:
        return self.execution_state in CLOSED_DOSSIER_STATES

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["execution_state"] = self.execution_state.value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlotCandidateRecord":
        payload = dict(data)
        payload["execution_state"] = MultiMatchExecutionState(payload["execution_state"])
        return cls(**payload)


@dataclass
class DynamicRankChange:
    timestamp_utc: str
    fixture_id: int
    slot_id: str | None
    reason: str
    previous_global_rank: int | None
    new_global_rank: int | None
    previous_slot_rank: int | None
    new_slot_rank: int | None
    previous_eligibility: str
    new_eligibility: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DynamicRankChange":
        return cls(**data)


@dataclass
class ResourceBudgetLedger:
    max_api_calls: int = 40
    max_scrapes: int = 12
    max_slot_fallbacks: int = 2
    max_attempts_per_match: int = 5
    api_calls_used: int = 0
    scrapes_used: int = 0
    slot_fallbacks_used: dict[str, int] = field(default_factory=dict)

    def can_spend_api(self, count: int = 1) -> bool:
        return self.api_calls_used + count <= self.max_api_calls

    def spend_api(self, count: int = 1) -> None:
        if not self.can_spend_api(count):
            raise MultiSlotContractError(
                f"Presupuesto de API agotado: {self.api_calls_used}+{count} > {self.max_api_calls}"
            )
        self.api_calls_used += count

    def can_spend_scrape(self, count: int = 1) -> bool:
        return self.scrapes_used + count <= self.max_scrapes

    def spend_scrape(self, count: int = 1) -> None:
        if not self.can_spend_scrape(count):
            raise MultiSlotContractError(
                f"Presupuesto de scraping agotado: {self.scrapes_used}+{count} > {self.max_scrapes}"
            )
        self.scrapes_used += count

    def can_fallback_slot(self, slot_id: str) -> bool:
        return self.slot_fallbacks_used.get(slot_id, 0) < self.max_slot_fallbacks

    def record_slot_fallback(self, slot_id: str) -> None:
        if not self.can_fallback_slot(slot_id):
            raise MultiSlotContractError(
                f"Presupuesto de fallback agotado para {slot_id}: max={self.max_slot_fallbacks}"
            )
        self.slot_fallbacks_used[slot_id] = self.slot_fallbacks_used.get(slot_id, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceBudgetLedger":
        return cls(**data)


@dataclass
class WindowOrchestrationPlan:
    """Estado maestro persistente de la ventana diaria multipartido V3.0."""

    window_label: str
    window_day: str
    window_start_utc: str
    window_end_utc: str
    window_start_madrid: str
    window_end_madrid: str
    timezone: str = "Europe/Madrid"
    schema_version: str = MULTI_SLOT_SCHEMA_VERSION
    max_total_active: int = 3
    slots: list[TimeSlotSpec] = field(default_factory=list)
    candidates: list[SlotCandidateRecord] = field(default_factory=list)
    active_by_slot: dict[str, int | None] = field(default_factory=dict)
    ranking_history: list[DynamicRankChange] = field(default_factory=list)
    events_log: list[dict[str, Any]] = field(default_factory=list)
    budget: ResourceBudgetLedger = field(default_factory=ResourceBudgetLedger)
    preselection_path: str | None = None
    preselection_sha256: str | None = None
    preselection_auto_generated: bool = False
    plan_id: str = ""
    plan_version: int = 1
    parent_plan_id: str | None = None
    parent_plan_path: str | None = None
    rebuild_reason: str | None = None
    filter_implementation: str | None = None
    policy_signature: dict[str, Any] = field(default_factory=dict)
    created_utc: str = ""
    last_updated_utc: str = ""

    def validate_invariants(self) -> None:
        seen_fixtures: set[int] = set()
        seen_events: set[int] = set()
        slot_ids = {s.slot_id for s in self.slots}
        active_counts: dict[str, int] = {sid: 0 for sid in slot_ids}

        for cand in self.candidates:
            if cand.fixture_id in seen_fixtures:
                raise MultiSlotContractError(f"fixture_id duplicado en plan: {cand.fixture_id}")
            seen_fixtures.add(cand.fixture_id)
            if cand.event_id is not None:
                if cand.event_id in seen_events:
                    raise MultiSlotContractError(f"event_id duplicado en plan: {cand.event_id}")
                seen_events.add(cand.event_id)
            if cand.execution_state in ACTIVE_SLOT_STATES:
                if not cand.slot_id or cand.slot_id not in slot_ids:
                    raise MultiSlotContractError(
                        f"Partido activo {cand.fixture_id} sin franja válida: {cand.slot_id}"
                    )
                active_counts[cand.slot_id] += 1
                if active_counts[cand.slot_id] > 1:
                    raise MultiSlotContractError(
                        f"Más de 1 partido activo en franja {cand.slot_id}"
                    )

        total_active = sum(active_counts.values())
        if total_active > self.max_total_active:
            raise MultiSlotContractError(
                f"Más de {self.max_total_active} partidos activos en la ventana: {total_active}"
            )

        for sid, fid in self.active_by_slot.items():
            if sid not in slot_ids:
                raise MultiSlotContractError(f"Franja desconocida en active_by_slot: {sid}")
            if fid is not None:
                cand = self.get_candidate(fid)
                if cand is None or cand.slot_id != sid:
                    raise MultiSlotContractError(
                        f"Inconsistencia active_by_slot[{sid}]={fid}"
                    )

    def get_candidate(self, fixture_id: int) -> SlotCandidateRecord | None:
        for cand in self.candidates:
            if cand.fixture_id == int(fixture_id):
                return cand
        return None

    def get_by_event_id(self, event_id: int) -> SlotCandidateRecord | None:
        for cand in self.candidates:
            if cand.event_id is not None and int(cand.event_id) == int(event_id):
                return cand
        return None

    def assign_slot_id(self, kickoff: str | datetime) -> str | None:
        ko_utc = parse_iso_utc(kickoff)
        w_start = parse_iso_utc(self.window_start_utc)
        w_end = parse_iso_utc(self.window_end_utc)
        if not (w_start <= ko_utc < w_end):
            return None
        for spec in self.slots:
            if spec.contains(ko_utc):
                return spec.slot_id
        return None

    def log_event(self, timestamp_utc: datetime, event_type: str, detail: dict[str, Any]) -> None:
        ts = parse_iso_utc(timestamp_utc).isoformat()
        self.events_log.append({"timestamp_utc": ts, "event_type": event_type, **detail})
        self.last_updated_utc = ts

    def to_dict(self) -> dict[str, Any]:
        self.validate_invariants()
        return {
            "schema_version": self.schema_version,
            "window_label": self.window_label,
            "window_day": self.window_day,
            "window_start_utc": self.window_start_utc,
            "window_end_utc": self.window_end_utc,
            "window_start_madrid": self.window_start_madrid,
            "window_end_madrid": self.window_end_madrid,
            "timezone": self.timezone,
            "max_total_active": self.max_total_active,
            "slots": [s.to_dict() for s in self.slots],
            "candidates": [c.to_dict() for c in self.candidates],
            "active_by_slot": dict(self.active_by_slot),
            "completed_fixture_ids": [
                c.fixture_id for c in self.candidates if c.execution_state == MultiMatchExecutionState.FINALIZED
            ],
            "discarded_fixture_ids": [
                c.fixture_id
                for c in self.candidates
                if c.execution_state
                in {
                    MultiMatchExecutionState.NO_BET,
                    MultiMatchExecutionState.SKIPPED_INELIGIBLE,
                    MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
                    MultiMatchExecutionState.FAILED_TECHNICAL,
                    MultiMatchExecutionState.EXPIRED,
                }
            ],
            "ranking_history": [r.to_dict() for r in self.ranking_history],
            "events_log": list(self.events_log),
            "budget": self.budget.to_dict(),
            "preselection_path": self.preselection_path,
            "preselection_sha256": self.preselection_sha256,
            "preselection_auto_generated": self.preselection_auto_generated,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "parent_plan_id": self.parent_plan_id,
            "parent_plan_path": self.parent_plan_path,
            "rebuild_reason": self.rebuild_reason,
            "filter_implementation": self.filter_implementation,
            "policy_signature": dict(self.policy_signature),
            "created_utc": self.created_utc,
            "last_updated_utc": self.last_updated_utc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowOrchestrationPlan":
        slots = [TimeSlotSpec.from_dict(s) for s in data.get("slots", [])]
        candidates = [SlotCandidateRecord.from_dict(c) for c in data.get("candidates", [])]
        history = [DynamicRankChange.from_dict(r) for r in data.get("ranking_history", [])]
        budget = ResourceBudgetLedger.from_dict(data.get("budget") or {})
        plan = cls(
            window_label=data["window_label"],
            window_day=data["window_day"],
            window_start_utc=data["window_start_utc"],
            window_end_utc=data["window_end_utc"],
            window_start_madrid=data["window_start_madrid"],
            window_end_madrid=data["window_end_madrid"],
            timezone=data.get("timezone", "Europe/Madrid"),
            schema_version=data.get("schema_version", MULTI_SLOT_SCHEMA_VERSION),
            max_total_active=int(data.get("max_total_active", 3)),
            slots=slots,
            candidates=candidates,
            active_by_slot=dict(data.get("active_by_slot") or {}),
            ranking_history=history,
            events_log=list(data.get("events_log") or []),
            budget=budget,
            preselection_path=data.get("preselection_path"),
            preselection_sha256=data.get("preselection_sha256"),
            preselection_auto_generated=bool(data.get("preselection_auto_generated", False)),
            plan_id=str(data.get("plan_id") or ""),
            plan_version=int(data.get("plan_version") or 1),
            parent_plan_id=data.get("parent_plan_id"),
            parent_plan_path=data.get("parent_plan_path"),
            rebuild_reason=data.get("rebuild_reason"),
            filter_implementation=data.get("filter_implementation"),
            policy_signature=dict(data.get("policy_signature") or {}),
            created_utc=data.get("created_utc", ""),
            last_updated_utc=data.get("last_updated_utc", ""),
        )
        plan.validate_invariants()
        return plan
