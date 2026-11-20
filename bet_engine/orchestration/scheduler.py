"""Controlador del ciclo PREMATCH (Etapa 5.1):
- preanalyze (T-24h): investiga históricos y patrones preliminares, crea ranking y guarda en caché sin picks finales.
- status: presenta candidatos, disponibilidad (default 13:00 Europe/Madrid), empates simultáneos y siguiente ejecutable.
- finalize: reutiliza T-24h, actualiza datos sensibles al tiempo (cuotas/bajas/XI), ejecuta ShadowPipelineRunner
  para un partido a la vez y recalcula la programación restante.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from bet_engine.analytics import build_stage3_registry
from bet_engine.context import MatchObservationBundle, ObservationStore
from bet_engine.contracts import (
    ContextualEvidence,
    MatchScheduleCandidate,
    PreAnalysisT24Snapshot,
    ProximityPlanState,
    ScheduleExecutionStatus,
    VerificationStatus,
    select_next_proximity_candidate,
)
from bet_engine.contracts.epistemic import parse_utc_timestamp

from .shadow_pipeline import ShadowPipelineRunner, ShadowRunReport


def compute_analytical_quality_score(bundle: MatchObservationBundle) -> tuple[float, str, bool]:
    """Calcula calidad analítica (0..100) estrictamente independiente de la cercanía del kickoff."""
    l20_h = bundle.windows["home"]["L20_all_competitions"]
    l20_a = bundle.windows["away"]["L20_all_competitions"]

    avail_score = min(1.0, (l20_h.total_matches_available + l20_a.total_matches_available) / 32.0) * 40.0
    detail_score = min(1.0, (l20_h.detailed_matches_valid + l20_a.detailed_matches_valid) / 10.0) * 40.0
    exact_pct = (
        l20_h.competition_mix.get("exact_competition_pct", 0.0)
        + l20_a.competition_mix.get("exact_competition_pct", 0.0)
    ) / 2.0
    comp_score = (exact_pct / 100.0) * 20.0

    score = round(avail_score + detail_score + comp_score, 2)
    if score >= 80.0:
        tier = "A_FULL"
    elif score >= 58.0:
        tier = "B_PARTIAL"
    elif score >= 35.0:
        tier = "C_LOW"
    else:
        tier = "D_MINIMAL"

    min_passed = (
        l20_h.total_matches_available >= 4
        and l20_a.total_matches_available >= 4
        and score >= 45.0
    )
    return score, tier, min_passed


class PrematchLifecycleController:
    """Gestiona preanalyze (T-24h) -> status -> finalize secuencial en Shadow Mode."""

    def __init__(
        self,
        workspace_dir: Path,
        *,
        available_from_local: str = "13:00",
        timezone_name: str = "Europe/Madrid",
        min_quality_score: float = 50.0,
        min_lead_minutes: int = 45,
        safety_margin_minutes: int = 10,
        lineup_cutoff_minutes: int = 15,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.available_from_local = available_from_local
        self.timezone_name = timezone_name
        self.min_quality_score = min_quality_score
        self.min_lead_minutes = min_lead_minutes
        self.safety_margin_minutes = safety_margin_minutes
        self.lineup_cutoff_minutes = lineup_cutoff_minutes
        self.shadow_runner = ShadowPipelineRunner(
            min_lead_minutes=min_lead_minutes,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
        )

    def preanalyze(
        self,
        *,
        target_date_local: str,
        bundles: list[MatchObservationBundle],
        generated_at_utc: str,
    ) -> dict[str, Any]:
        """Paso 1 (T-24h): investiga históricos y patrones preliminares, crea ranking y guarda en caché.
        No genera picks definitivos (final_picks_generated=False).
        """
        t24_dir = self.workspace_dir / "t24_cache"
        t24_dir.mkdir(parents=True, exist_ok=True)

        reg = build_stage3_registry()
        candidates: list[MatchScheduleCandidate] = []
        cache_manifest: dict[str, Any] = {}

        for b in bundles:
            q_score, cov_tier, min_ok = compute_analytical_quality_score(b)
            prelim_analytics = reg.run_all(b)
            ev_path = t24_dir / f"t24_evidence_{b.event_id}.json"
            ev_payload = {
                "event_id": b.event_id,
                "fixture_id": b.fixture_id,
                "t24_generated_at_utc": generated_at_utc,
                "analytical_quality_score": q_score,
                "coverage_tier": cov_tier,
                "preliminary_analytics": prelim_analytics.to_dict(),
                "bundle_summary": b.to_dict(),
                "final_picks_generated": False,
            }
            ev_path.write_text(json.dumps(ev_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            cache_manifest[b.event_id] = str(ev_path)

            candidates.append(
                MatchScheduleCandidate(
                    event_id=b.event_id,
                    fixture_id=b.fixture_id,
                    league_id=b.league_id,
                    season=b.season,
                    home_name=b.home_name,
                    away_name=b.away_name,
                    kickoff_utc=b.kickoff_utc,
                    coverage_tier=cov_tier,
                    analytical_quality_score=q_score,
                    min_quality_passed=min_ok,
                    min_lead_minutes=self.min_lead_minutes,
                    safety_margin_minutes=self.safety_margin_minutes,
                    t24_snapshot_id=f"t24_{target_date_local}",
                )
            )

        # Orden de ranking de calidad para la vista T-24h
        candidates_ranked = tuple(
            sorted(candidates, key=lambda c: (-c.analytical_quality_score, c.kickoff_utc, c.event_id))
        )
        t24_snap = PreAnalysisT24Snapshot(
            snapshot_id=f"t24_{target_date_local}",
            target_date_local=target_date_local,
            generated_at_utc=generated_at_utc,
            candidates=candidates_ranked,
            cache_manifest=cache_manifest,
            final_picks_generated=False,
        )
        plan_state = ProximityPlanState(
            plan_id=f"plan_{target_date_local}",
            target_date_local=target_date_local,
            available_from_local=self.available_from_local,
            timezone_name=self.timezone_name,
            min_quality_score=self.min_quality_score,
            candidates=candidates_ranked,
        )

        (self.workspace_dir / "t24_preanalysis.json").write_text(
            json.dumps(t24_snap.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._save_plan(plan_state)
        return {
            "t24_snapshot": t24_snap.to_dict(),
            "plan_state": plan_state.to_dict(),
        }

    def status(
        self,
        *,
        plan_state: ProximityPlanState,
        now_utc: str | datetime,
        manual_override_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Paso 2: presenta candidatos, ranking, empates simultáneos y el siguiente partido a ejecutar."""
        selection = select_next_proximity_candidate(
            plan_state,
            now_utc,
            manual_override_event_id=manual_override_event_id,
        )
        now_dt = parse_utc_timestamp(now_utc)
        done = set(plan_state.completed_event_ids) | set(plan_state.failed_event_ids)
        eligible = [
            c
            for c in plan_state.candidates
            if c.event_id not in done
            and c.min_quality_passed
            and c.analytical_quality_score >= plan_state.min_quality_score
            and now_dt <= c.latest_start_utc()
        ]
        eligible_by_ko = sorted(eligible, key=lambda c: (parse_utc_timestamp(c.kickoff_utc), -c.analytical_quality_score))

        # Detecta si los dos primeros elegibles son simultáneos (<=15m) y de calidad equivalente (<=5 pts)
        simultaneous_tie: list[str] = []
        if len(eligible_by_ko) >= 2:
            c1, c2 = eligible_by_ko[0], eligible_by_ko[1]
            ko_diff_m = abs((parse_utc_timestamp(c2.kickoff_utc) - parse_utc_timestamp(c1.kickoff_utc)).total_seconds()) / 60.0
            qual_diff = abs(c1.analytical_quality_score - c2.analytical_quality_score)
            if ko_diff_m <= 15.0 and qual_diff <= 5.0:
                simultaneous_tie = [c1.event_id, c2.event_id]

        return {
            "now_utc": now_dt.isoformat(),
            "next_action": selection,
            "manual_selection_available_for_tie": simultaneous_tie,
            "completed_event_ids": list(plan_state.completed_event_ids),
            "failed_event_ids": list(plan_state.failed_event_ids),
            "remaining_eligible_by_proximity": [c.to_dict() for c in eligible_by_ko],
        }

    def finalize(
        self,
        *,
        plan_state: ProximityPlanState,
        bundle: MatchObservationBundle,
        now_utc: str | datetime,
        target_status: str = "NS",
        updated_odds_map: dict[str, float] | None = None,
        updated_contextual_evidence: tuple[ContextualEvidence, ...] = (),
        codex_hypotheses: list[dict[str, Any]] | None = None,
        simulate_codex_failure: bool = False,
        manual_override_event_id: str | None = None,
        qualitative_markdown: str | None = None,
        manual_lineup: dict[str, Any] | None = None,
        lineup_cutoff_minutes: int | None = None,
        fallback_on_missing_lineups: bool = False,
        user_manual_absent: bool = False,
    ) -> tuple[ShadowRunReport, ProximityPlanState, dict[str, Any]]:
        """Paso 3: reutiliza preanálisis T-24h, actualiza cuotas/contexto en now_utc, ejecuta 1 partido
        en Shadow Mode y recalcula inmediatamente la programación de los restantes.
        """
        now_dt = parse_utc_timestamp(now_utc)
        t24_ev_path = self.workspace_dir / "t24_cache" / f"t24_evidence_{bundle.event_id}.json"
        reused_t24 = t24_ev_path.is_file()

        # Actualiza cutoff_utc al instante de ejecución PREMATCH y fusiona evidencia sensible al tiempo
        merged_context = tuple(bundle.contextual_evidence) + tuple(updated_contextual_evidence)
        if reused_t24:
            merged_context = merged_context + (
                ContextualEvidence(
                    evidence_id=f"ctx_{bundle.fixture_id}_t24_reused",
                    fixture_id=bundle.fixture_id,
                    category="T24_CACHE_REUSE",
                    verification_status=VerificationStatus.CONFIRMADO_OFICIAL,
                    recorded_at_utc=now_dt.isoformat(),
                    facts={"t24_evidence_path": str(t24_ev_path), "reused_historical_matches": len(bundle.home_matches) + len(bundle.away_matches)},
                ),
            )

        updated_bundle = replace(
            bundle,
            cutoff_utc=now_dt.isoformat(),
            contextual_evidence=merged_context,
        )

        event_out_dir = self.workspace_dir / "events" / str(bundle.event_id)
        try:
            report = self.shadow_runner.run_shadow_on_bundle(
                updated_bundle,
                target_status=target_status,
                odds_map=updated_odds_map,
                codex_hypotheses=codex_hypotheses,
                simulate_codex_failure=simulate_codex_failure,
                output_dir=event_out_dir,
                qualitative_markdown=qualitative_markdown,
                manual_lineup=manual_lineup,
                lineup_cutoff_minutes=lineup_cutoff_minutes if lineup_cutoff_minutes is not None else self.lineup_cutoff_minutes,
                user_manual_absent=user_manual_absent,
            )
            missing_lineup_discard = (
                fallback_on_missing_lineups
                and not report.eligible_for_final_action
                and (
                    (report.lineup_gate or {}).get("status") == "LINEUPS_UNAVAILABLE_FINAL"
                    or user_manual_absent
                )
            )
            if report.status == "BLOCKED_PREMATCH_GUARD":
                new_failed = tuple(dict.fromkeys(list(plan_state.failed_event_ids) + [bundle.event_id]))
                new_completed = plan_state.completed_event_ids
                new_status = ScheduleExecutionStatus.REJECTED_LEAD_TIME
            elif missing_lineup_discard:
                new_failed = tuple(dict.fromkeys(list(plan_state.failed_event_ids) + [bundle.event_id]))
                new_completed = plan_state.completed_event_ids
                new_status = ScheduleExecutionStatus.REJECTED_MISSING_LINEUPS
                self._append_discarded_lineup_audit(
                    {
                        "event_id": bundle.event_id,
                        "fixture_id": bundle.fixture_id,
                        "discarded_at_utc": now_dt.isoformat(),
                        "lineup_status": (report.lineup_gate or {}).get("status"),
                        "reason": (report.lineup_gate or {}).get("reason") or "MISSING_CONFIRMED_22_STARTERS",
                        "user_manual_absent": bool(user_manual_absent),
                        "original_snapshots_preserved": True,
                        "actionable_bet": False,
                    }
                )
            else:
                new_completed = tuple(dict.fromkeys(list(plan_state.completed_event_ids) + [bundle.event_id]))
                new_failed = plan_state.failed_event_ids
                new_status = ScheduleExecutionStatus.COMPLETED
        except Exception:
            # Aislamiento de fallos: un candidato fallido jamás bloquea el resto de la jornada
            new_failed = tuple(dict.fromkeys(list(plan_state.failed_event_ids) + [bundle.event_id]))
            new_completed = plan_state.completed_event_ids
            new_status = ScheduleExecutionStatus.FAILED_ISOLATED
            raise

        updated_candidates = tuple(
            replace(c, execution_status=new_status) if c.event_id == bundle.event_id else c
            for c in plan_state.candidates
        )
        updated_plan = replace(
            plan_state,
            candidates=updated_candidates,
            active_event_id=None,
            completed_event_ids=new_completed,
            failed_event_ids=new_failed,
        )
        self._save_plan(updated_plan)

        # Recalcula la programación restante tras finalizar este partido
        next_status = self.status(
            plan_state=updated_plan,
            now_utc=now_dt,
            manual_override_event_id=manual_override_event_id,
        )
        return report, updated_plan, next_status

    def _append_discarded_lineup_audit(self, record: dict[str, Any]) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self.workspace_dir / "discarded_lineups_audit.json"
        existing: list[dict[str, Any]] = []
        if audit_path.is_file():
            try:
                existing = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                existing = []
        existing.append(record)
        audit_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_plan(self, plan: ProximityPlanState) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "proximity_plan_state.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
