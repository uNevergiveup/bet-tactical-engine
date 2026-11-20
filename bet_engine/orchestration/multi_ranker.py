"""Etapa 2 V3.0: Ranking global, ranking por franja y reordenación dinámica con auditoría."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bet_engine.contracts.multi_slot import (
    CLOSED_DOSSIER_STATES,
    DynamicRankChange,
    MultiMatchExecutionState,
    MultiSlotContractError,
    SlotCandidateRecord,
    WindowOrchestrationPlan,
    parse_iso_utc,
)


def _compute_fixture_quality(
    fixture: dict[str, Any],
    now_utc: datetime,
    *,
    quality_override: float | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Calcula puntuación descriptiva determinista sin inventar métricas ausentes."""
    if quality_override is not None:
        return round(float(quality_override), 4), {"source": "override", "quality_override": round(float(quality_override), 4)}

    components: dict[str, Any] = {}
    tactical_score: float | None = None
    contradictions_count = 0
    signal_count = 0

    if fixture.get("statistical_observations"):
        try:
            from prematch_tactical_signal_engine import analyze_fixture

            evaluated = analyze_fixture(fixture, now_utc, fixture.get("statistical_observations") or {})
            tactical_score = float(evaluated.get("tactical_research_score") or 0.0)
            contradictions_count = len(evaluated.get("contradictions") or [])
            signal_count = int(evaluated.get("signal_count") or 0)
            components["tactical_research_score"] = round(tactical_score, 4)
            components["signal_count"] = signal_count
            components["contradiction_penalty"] = min(20.0, 5.0 * contradictions_count)
            components["small_sample_penalty"] = 10.0 if not signal_count else 0.0
        except Exception:
            tactical_score = None

    readiness_raw = fixture.get("data_readiness_score")
    readiness = float(readiness_raw) if readiness_raw is not None else None
    if readiness is not None:
        components["data_readiness_score"] = round(readiness, 4)

    explicit_priority = fixture.get("research_priority")
    if explicit_priority is None:
        explicit_priority = fixture.get("research_priority_score")
    if explicit_priority is None:
        explicit_priority = fixture.get("quality_score")

    stat_bonus = float((fixture.get("statistical_priority") or {}).get("bonus") or 0.0)
    if stat_bonus:
        components["declared_coverage_bonus"] = round(stat_bonus, 4)

    if explicit_priority is not None and tactical_score is None:
        score = float(explicit_priority)
        components["source"] = "explicit_priority"
        components["explicit_priority"] = round(score, 4)
        return round(score, 4), components

    if tactical_score is not None:
        raw_score = (
            tactical_score
            + stat_bonus
            - float(components.get("contradiction_penalty", 0.0))
            - float(components.get("small_sample_penalty", 0.0))
        )
        # Cuando hay readiness además de score táctico bajo o parcial, preserva transparencia sin inflar.
        if readiness is not None and raw_score <= 0 and readiness > 0:
            components["readiness_fallback_used"] = True
            raw_score = round(readiness * 0.5, 4)
        components["source"] = "tactical_signal_engine"
        return round(max(0.0, raw_score), 4), components

    if readiness is not None:
        # Si sólo existe readiness en preselection, escalamos de forma conservadora (0-100 -> 0-100).
        components["source"] = "data_readiness"
        return round(readiness + stat_bonus, 4), components

    return None, {"source": "NONE", "reason": "NO_OBSERVABLE_QUALITY_EVIDENCE"}


def _evaluate_candidate_eligibility(
    plan: WindowOrchestrationPlan,
    fixture: dict[str, Any],
    *,
    slot_id: str | None,
    quality_score: float | None,
    lineup_state: str,
    bwin_info: dict[str, Any] | None,
    now_utc: datetime,
    min_lead_minutes: int,
    lineup_cutoff_minutes: int,
    min_quality_score: float,
    min_readiness_score: float,
    require_bwin: bool,
    already_in_progress: bool = False,
) -> tuple[str, list[str], int | None]:
    """Evalúa elegibilidad PREMATCH separada de la calidad y de la decisión probabilística."""
    reasons: list[str] = []
    ko_raw = fixture.get("kickoff_utc") or fixture.get("kickoff_madrid")
    if not ko_raw:
        return "INELIGIBLE_IDENTITY", ["MISSING_KICKOFF"], None
    ko_utc = parse_iso_utc(ko_raw)

    if now_utc >= ko_utc:
        reasons.append("MATCH_ALREADY_STARTED")
        return "INELIGIBLE_STARTED", reasons, _extract_event_id(fixture, bwin_info)

    if slot_id is None:
        reasons.append("OUTSIDE_DAILY_SLOTS_OR_WINDOW")
        return "INELIGIBLE_OUTSIDE_SLOT", reasons, _extract_event_id(fixture, bwin_info)

    lead_seconds = (ko_utc - now_utc).total_seconds()
    confirmed_states = {"LINEUPS_API_CONFIRMED", "LINEUPS_EXTERNAL_CONFIRMED", "LINEUPS_MANUAL_VERIFIED"}
    if not already_in_progress and lead_seconds < min_lead_minutes * 60:
        reasons.append(f"INSUFFICIENT_LEAD_TIME_{int(lead_seconds // 60)}M_LT_{min_lead_minutes}M")
        return "INELIGIBLE_LEAD_TIME", reasons, _extract_event_id(fixture, bwin_info)
    if not already_in_progress and lead_seconds <= lineup_cutoff_minutes * 60 and lineup_state not in confirmed_states:
        reasons.append(f"PAST_LINEUP_CUTOFF_T_MINUS_{lineup_cutoff_minutes}M")
        return "INELIGIBLE_LEAD_TIME", reasons, _extract_event_id(fixture, bwin_info)

    if fixture.get("identity_blocked") or str(fixture.get("status", "")).startswith("BLOCK"):
        reasons.append(str(fixture.get("block_reason") or "IDENTITY_OR_POLICY_BLOCK"))
        return "INELIGIBLE_IDENTITY", reasons, _extract_event_id(fixture, bwin_info)

    readiness_raw = fixture.get("data_readiness_score")
    if readiness_raw is not None and float(readiness_raw) < min_readiness_score:
        reasons.append(f"LOW_DATA_READINESS_{readiness_raw}_LT_{min_readiness_score}")

    if quality_score is None:
        reasons.append("MISSING_QUALITY_EVIDENCE")
    elif quality_score < min_quality_score:
        reasons.append(f"QUALITY_BELOW_THRESHOLD_{quality_score}_LT_{min_quality_score}")

    if fixture.get("details_checked") is False:
        reasons.append("FIXTURE_DETAILS_NOT_CHECKED")

    if reasons:
        return "INELIGIBLE_DATA_QUALITY", reasons, _extract_event_id(fixture, bwin_info)

    event_id = _extract_event_id(fixture, bwin_info)
    bwin_status = str((bwin_info or {}).get("status") or fixture.get("bwin_status") or "")
    if (
        bwin_status.startswith("BLOCK")
        or "REVIEW" in bwin_status
        or "CONFLICT" in bwin_status
        or bwin_status == "PROVIDER_FAILED"
    ):
        reasons.append(f"BWIN_UNAVAILABLE_OR_BLOCKED:{bwin_status}")
        return "INELIGIBLE_NO_BWIN", reasons, event_id
    if require_bwin and event_id is None:
        reasons.append("MISSING_VERIFIED_BWIN_EVENT_ID")
        return "INELIGIBLE_NO_BWIN", reasons, None

    return "ELIGIBLE", [], event_id


def _extract_event_id(fixture: dict[str, Any], bwin_info: dict[str, Any] | None) -> int | None:
    raw = (bwin_info or {}).get("event_id") or (bwin_info or {}).get("bwin_event_id") or fixture.get("bwin_event_id") or fixture.get("event_id")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def populate_and_rank_plan(
    plan: WindowOrchestrationPlan,
    matches: list[dict[str, Any]],
    *,
    now_utc: datetime,
    bwin_links: dict[int, dict[str, Any]] | None = None,
    quality_overrides: dict[int, float] | None = None,
    min_lead_minutes: int = 45,
    lineup_cutoff_minutes: int = 15,
    min_quality_score: float = 20.0,
    min_readiness_score: float = 40.0,
    require_bwin: bool = False,
) -> WindowOrchestrationPlan:
    """Construye los candidatos por franja desde preselección, calcula ranking global/por franja y preserva invariantes."""
    now_utc = parse_iso_utc(now_utc)
    seen_fids: set[int] = set()
    seen_events: set[int] = set()
    records: list[SlotCandidateRecord] = []

    for fixture in matches:
        fid = int(fixture["fixture_id"])
        if fid in seen_fids:
            raise MultiSlotContractError(f"fixture_id duplicado en entrada de ranking: {fid}")
        seen_fids.add(fid)

        ko_raw = fixture.get("kickoff_utc") or fixture.get("kickoff_madrid")
        ko_utc = parse_iso_utc(ko_raw) if ko_raw else now_utc
        slot_id = plan.assign_slot_id(ko_utc)
        q_override = (quality_overrides or {}).get(fid)
        quality_score, components = _compute_fixture_quality(fixture, now_utc, quality_override=q_override)
        bwin_info = (bwin_links or {}).get(fid)
        lineup_state = str(fixture.get("lineup_state") or "LINEUPS_PENDING")

        elig, reasons, event_id = _evaluate_candidate_eligibility(
            plan,
            fixture,
            slot_id=slot_id,
            quality_score=quality_score,
            lineup_state=lineup_state,
            bwin_info=bwin_info,
            now_utc=now_utc,
            min_lead_minutes=min_lead_minutes,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
            min_quality_score=min_quality_score,
            min_readiness_score=min_readiness_score,
            require_bwin=require_bwin,
        )

        if event_id is not None:
            if event_id in seen_events:
                raise MultiSlotContractError(f"event_id Bwin duplicado en ranking: {event_id}")
            seen_events.add(event_id)

        if elig == "INELIGIBLE_STARTED":
            exec_state = MultiMatchExecutionState.EXPIRED
            audit_outcome = "EXPIRED_WINDOW"
        elif elig != "ELIGIBLE":
            exec_state = MultiMatchExecutionState.SKIPPED_INELIGIBLE
            audit_outcome = "SKIPPED_ELIGIBILITY"
        else:
            exec_state = MultiMatchExecutionState.RANKED
            audit_outcome = "PENDING"

        records.append(
            SlotCandidateRecord(
                slot_id=slot_id,
                fixture_id=fid,
                event_id=event_id,
                kickoff_utc=ko_utc.isoformat(),
                kickoff_madrid=ko_utc.astimezone(plan_tz := plan_tz_from_plan(plan)).isoformat(),
                league_name=str(fixture.get("league_name") or fixture.get("league") or "UNKNOWN_LEAGUE"),
                home_team=str(fixture.get("home") or fixture.get("home_name") or ""),
                away_team=str(fixture.get("away") or fixture.get("away_name") or ""),
                league_id=int(fixture["league_id"]) if fixture.get("league_id") is not None else None,
                season=int(fixture["season"]) if fixture.get("season") is not None else None,
                quality_score=quality_score,
                quality_components=components,
                eligibility_status=elig,
                execution_state=exec_state,
                lineup_state=lineup_state,
                odds_state=str(fixture.get("odds_state") or ("CAPTURED_VALID" if fixture.get("odds_captured") else "NOT_CAPTURED")),
                research_state=str(fixture.get("research_state") or "NOT_REQUESTED"),
                audit_outcome=audit_outcome,
                blocking_reasons=reasons,
                last_updated_utc=now_utc.isoformat(),
            )
        )

    plan.candidates = records
    _assign_ranks(plan, reason="INITIAL_WINDOW_RANKING", now_utc=now_utc, record_history=False)
    plan.active_by_slot = {spec.slot_id: None for spec in plan.slots}
    plan.log_event(
        now_utc,
        "WINDOW_RANKING_BUILT",
        {
            "total_candidates": len(plan.candidates),
            "eligible_candidates": sum(1 for c in plan.candidates if c.eligibility_status == "ELIGIBLE"),
        },
    )
    plan.validate_invariants()
    return plan


def plan_tz_from_plan(plan: WindowOrchestrationPlan):
    from zoneinfo import ZoneInfo

    return ZoneInfo(plan.timezone)


def _rank_sort_key(c: SlotCandidateRecord) -> tuple[Any, ...]:
    is_eligible = 0 if c.eligibility_status == "ELIGIBLE" else 1
    q = -(c.quality_score if c.quality_score is not None else -1.0)
    return (is_eligible, q, c.kickoff_utc, c.fixture_id)


def _assign_ranks(
    plan: WindowOrchestrationPlan,
    *,
    reason: str,
    now_utc: datetime,
    record_history: bool,
) -> None:
    ts = parse_iso_utc(now_utc).isoformat()
    ordered_global = sorted(plan.candidates, key=_rank_sort_key)
    new_global_map: dict[int, int] = {c.fixture_id: idx for idx, c in enumerate(ordered_global, start=1)}

    new_slot_map: dict[int, int | None] = {}
    for spec in plan.slots:
        in_slot = [c for c in ordered_global if c.slot_id == spec.slot_id]
        for s_idx, cand in enumerate(in_slot, start=1):
            new_slot_map[cand.fixture_id] = s_idx
    for cand in ordered_global:
        if cand.slot_id is None:
            new_slot_map[cand.fixture_id] = None

    for cand in plan.candidates:
        old_g = cand.global_rank
        old_s = cand.slot_rank
        new_g = new_global_map[cand.fixture_id]
        new_s = new_slot_map[cand.fixture_id]
        if record_history and (old_g != new_g or old_s != new_s):
            plan.ranking_history.append(
                DynamicRankChange(
                    timestamp_utc=ts,
                    fixture_id=cand.fixture_id,
                    slot_id=cand.slot_id,
                    reason=reason,
                    previous_global_rank=old_g,
                    new_global_rank=new_g,
                    previous_slot_rank=old_s,
                    new_slot_rank=new_s,
                    previous_eligibility=cand.eligibility_status,
                    new_eligibility=cand.eligibility_status,
                )
            )
        if not cand.is_closed_dossier():
            cand.global_rank = new_g
            cand.slot_rank = new_s
            cand.last_updated_utc = ts
        else:
            # Expedientes cerrados no mutan su estado operativo; se preservan sus rangos congelados salvo inicialización.
            if cand.global_rank is None:
                cand.global_rank = new_g
            if cand.slot_rank is None:
                cand.slot_rank = new_s


def apply_dynamic_ranking_update(
    plan: WindowOrchestrationPlan,
    *,
    now_utc: datetime,
    reason: str,
    bwin_links: dict[int, dict[str, Any]] | None = None,
    quality_overrides: dict[int, float] | None = None,
    fixture_metadata_by_id: dict[int, dict[str, Any]] | None = None,
    min_lead_minutes: int = 45,
    lineup_cutoff_minutes: int = 15,
    min_quality_score: float = 20.0,
    min_readiness_score: float = 40.0,
    require_bwin: bool = False,
) -> WindowOrchestrationPlan:
    """Actualiza elegibilidad y ranking dinámico con marca temporal sin tocar expedientes cerrados."""
    now_utc = parse_iso_utc(now_utc)
    ts = now_utc.isoformat()
    seen_events: dict[int, int] = {
        c.event_id: c.fixture_id for c in plan.candidates if c.event_id is not None
    }

    for cand in plan.candidates:
        if cand.is_closed_dossier():
            continue

        prev_elig = cand.eligibility_status
        prev_g = cand.global_rank
        prev_s = cand.slot_rank

        if quality_overrides and cand.fixture_id in quality_overrides:
            cand.quality_score = round(float(quality_overrides[cand.fixture_id]), 4)
            cand.quality_components = {"source": "dynamic_override", "quality_override": cand.quality_score}

        bwin_info = (bwin_links or {}).get(cand.fixture_id)
        if bwin_info:
            ev = _extract_event_id({}, bwin_info)
            if ev is not None:
                owner = seen_events.get(ev)
                if owner is not None and owner != cand.fixture_id:
                    raise MultiSlotContractError(f"Conflicto event_id duplicado al reordenar: {ev}")
                cand.event_id = ev
                seen_events[ev] = cand.fixture_id

        meta = dict((fixture_metadata_by_id or {}).get(cand.fixture_id) or {})
        meta.setdefault("fixture_id", cand.fixture_id)
        meta.setdefault("kickoff_utc", cand.kickoff_utc)
        meta.setdefault("bwin_event_id", cand.event_id)
        meta.setdefault("lineup_state", cand.lineup_state)

        already_in_progress = cand.execution_state in {
            MultiMatchExecutionState.WAITING_RESEARCH,
            MultiMatchExecutionState.WAITING_LINEUPS,
            MultiMatchExecutionState.READY_FOR_FINALIZATION,
        }
        new_elig, reasons, _ = _evaluate_candidate_eligibility(
            plan,
            meta,
            slot_id=cand.slot_id,
            quality_score=cand.quality_score,
            lineup_state=cand.lineup_state,
            bwin_info=bwin_info,
            now_utc=now_utc,
            min_lead_minutes=min_lead_minutes,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
            min_quality_score=min_quality_score,
            min_readiness_score=min_readiness_score,
            require_bwin=require_bwin,
            already_in_progress=already_in_progress,
        )
        cand.eligibility_status = new_elig
        cand.blocking_reasons = reasons

        if new_elig == "INELIGIBLE_STARTED":
            cand.transition_to(MultiMatchExecutionState.EXPIRED, now_utc, f"RE_RANK:{reason}")
            cand.audit_outcome = "EXPIRED_WINDOW"
            if plan.active_by_slot.get(cand.slot_id or "") == cand.fixture_id:
                plan.active_by_slot[cand.slot_id or ""] = None
        elif new_elig != "ELIGIBLE":
            if cand.execution_state in {
                MultiMatchExecutionState.DISCOVERED,
                MultiMatchExecutionState.PRESELECTED,
                MultiMatchExecutionState.RANKED,
                MultiMatchExecutionState.SELECTED,
                MultiMatchExecutionState.PREPARING,
            }:
                cand.transition_to(MultiMatchExecutionState.SKIPPED_INELIGIBLE, now_utc, f"RE_RANK:{reason}")
                cand.audit_outcome = "SKIPPED_ELIGIBILITY"
                if plan.active_by_slot.get(cand.slot_id or "") == cand.fixture_id:
                    plan.active_by_slot[cand.slot_id or ""] = None
        elif cand.execution_state == MultiMatchExecutionState.SKIPPED_INELIGIBLE:
            cand.transition_to(MultiMatchExecutionState.RANKED, now_utc, f"RECOVERED_ELIGIBILITY:{reason}")
            cand.audit_outcome = "PENDING"

        if prev_elig != new_elig:
            plan.ranking_history.append(
                DynamicRankChange(
                    timestamp_utc=ts,
                    fixture_id=cand.fixture_id,
                    slot_id=cand.slot_id,
                    reason=reason,
                    previous_global_rank=prev_g,
                    new_global_rank=cand.global_rank,
                    previous_slot_rank=prev_s,
                    new_slot_rank=cand.slot_rank,
                    previous_eligibility=prev_elig,
                    new_eligibility=new_elig,
                )
            )

    _assign_ranks(plan, reason=reason, now_utc=now_utc, record_history=True)
    plan.log_event(now_utc, "DYNAMIC_RERANK_APPLIED", {"reason": reason})
    plan.validate_invariants()
    return plan


def select_best_eligible_for_slot(
    plan: WindowOrchestrationPlan,
    slot_id: str,
    *,
    exclude_fixture_ids: set[int] | None = None,
) -> SlotCandidateRecord | None:
    """Devuelve el candidato ELIGIBLE mejor rankeado en `slot_id` sin duplicar activos."""
    excluded = exclude_fixture_ids or set()
    in_slot = [
        c
        for c in plan.candidates
        if c.slot_id == slot_id
        and c.fixture_id not in excluded
        and c.eligibility_status == "ELIGIBLE"
        and c.execution_state == MultiMatchExecutionState.RANKED
    ]
    in_slot.sort(key=lambda c: (c.slot_rank if c.slot_rank is not None else 999999, _rank_sort_key(c)))
    return in_slot[0] if in_slot else None
