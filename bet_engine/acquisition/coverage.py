"""Evaluador de cobertura declarada (TRUE/FALSE/UNKNOWN) y cobertura observada por familia."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bet_engine.contracts import CoverageFlag


def to_coverage_flag(raw: Any) -> CoverageFlag:
    """Normaliza cobertura declarada distinguiendo estrictamente TRUE, FALSE y UNKNOWN."""
    if raw is True:
        return CoverageFlag.TRUE
    if raw is False:
        return CoverageFlag.FALSE
    if isinstance(raw, str):
        norm = raw.strip().upper()
        if norm in {"TRUE", "1", "YES"}:
            return CoverageFlag.TRUE
        if norm in {"FALSE", "0", "NO"}:
            return CoverageFlag.FALSE
    return CoverageFlag.UNKNOWN


@dataclass(frozen=True)
class CoverageAuditResult:
    """Resultado de auditoría de cobertura declarada vs observada por familia."""

    league_id: int | None
    season: int | None
    declared_statistics_fixtures: CoverageFlag
    declared_events: CoverageFlag
    declared_lineups: CoverageFlag
    declared_players: CoverageFlag
    declared_injuries: CoverageFlag
    declared_standings: CoverageFlag
    declared_tier: str
    declared_score: float
    total_matches: int
    detailed_matches_requested: int
    not_requested_count: int
    observed_by_metric: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "league_id": self.league_id,
            "season": self.season,
            "declared": {
                "statistics_fixtures": self.declared_statistics_fixtures.value,
                "events": self.declared_events.value,
                "lineups": self.declared_lineups.value,
                "players": self.declared_players.value,
                "injuries": self.declared_injuries.value,
                "standings": self.declared_standings.value,
            },
            "declared_tier": self.declared_tier,
            "declared_score": self.declared_score,
            "total_matches": self.total_matches,
            "detailed_matches_requested": self.detailed_matches_requested,
            "not_requested_count": self.not_requested_count,
            "observed_by_metric": self.observed_by_metric,
        }


class CoveragePolicyEvaluator:
    """Carga config/coverage_priority.json y evalúa cobertura declarada + observada."""

    def __init__(self, policy_path: Path | None = None) -> None:
        if policy_path is None:
            policy_path = Path(__file__).resolve().parents[2] / "config" / "coverage_priority.json"
        self.policy_path = policy_path
        self.policy: dict[str, Any] = {}
        if policy_path.is_file():
            self.policy = json.loads(policy_path.read_text(encoding="utf-8"))

    def evaluate_declared(self, league_coverage: dict[str, Any] | None) -> dict[str, Any]:
        cov = league_coverage or {}
        fx = cov.get("fixtures") if isinstance(cov.get("fixtures"), dict) else {}
        stats_fx = to_coverage_flag(fx.get("statistics_fixtures"))
        events = to_coverage_flag(fx.get("events"))
        lineups = to_coverage_flag(fx.get("lineups"))
        players = to_coverage_flag(cov.get("players"))
        injuries = to_coverage_flag(cov.get("injuries"))
        standings = to_coverage_flag(cov.get("standings"))

        weights = self.policy.get("endpoint_weights", {
            "statistics_fixtures": 0.30,
            "events": 0.20,
            "lineups": 0.15,
            "players": 0.15,
            "standings": 0.10,
            "injuries": 0.10,
        })
        flags = {
            "statistics_fixtures": stats_fx,
            "events": events,
            "lineups": lineups,
            "players": players,
            "standings": standings,
            "injuries": injuries,
        }
        score = 0.0
        for key, weight in weights.items():
            if flags.get(key) == CoverageFlag.TRUE:
                score += float(weight) * 100.0
            elif flags.get(key) == CoverageFlag.UNKNOWN:
                score += float(weight) * 35.0  # Incertidumbre explícita, no asumir TRUE ni 0 absoluto

        tiers = self.policy.get("tiers", {"A_FULL": 85.0, "B_PARTIAL": 60.0, "C_LOW": 35.0})
        if score >= float(tiers.get("A_FULL", 85.0)):
            tier = "A_FULL"
        elif score >= float(tiers.get("B_PARTIAL", 60.0)):
            tier = "B_PARTIAL"
        elif score >= float(tiers.get("C_LOW", 35.0)):
            tier = "C_LOW"
        else:
            tier = "D_MINIMAL"

        return {
            "flags": flags,
            "score": round(score, 2),
            "tier": tier,
        }

    def audit_observations(
        self,
        *,
        league_id: int | None,
        season: int | None,
        league_coverage: dict[str, Any] | None,
        total_matches: int,
        detailed_matches_requested: int,
        metric_valid_counts: dict[str, int],
    ) -> CoverageAuditResult:
        decl = self.evaluate_declared(league_coverage)
        flags: dict[str, CoverageFlag] = decl["flags"]
        not_requested = max(0, total_matches - detailed_matches_requested)

        observed: dict[str, dict[str, Any]] = {}
        for metric, valid_n in sorted(metric_valid_counts.items()):
            obs_pct_total = round((valid_n / total_matches) * 100.0, 2) if total_matches > 0 else 0.0
            obs_pct_req = (
                round((valid_n / detailed_matches_requested) * 100.0, 2)
                if detailed_matches_requested > 0
                else 0.0
            )
            observed[metric] = {
                "valid_n": valid_n,
                "total_matches": total_matches,
                "detailed_requested": detailed_matches_requested,
                "missing_within_requested": max(0, detailed_matches_requested - valid_n),
                "not_requested": not_requested,
                "observed_pct_over_total": obs_pct_total,
                "observed_pct_over_requested": obs_pct_req,
            }

        return CoverageAuditResult(
            league_id=league_id,
            season=season,
            declared_statistics_fixtures=flags["statistics_fixtures"],
            declared_events=flags["events"],
            declared_lineups=flags["lineups"],
            declared_players=flags["players"],
            declared_injuries=flags["injuries"],
            declared_standings=flags["standings"],
            declared_tier=decl["tier"],
            declared_score=decl["score"],
            total_matches=total_matches,
            detailed_matches_requested=detailed_matches_requested,
            not_requested_count=not_requested,
            observed_by_metric=observed,
        )
