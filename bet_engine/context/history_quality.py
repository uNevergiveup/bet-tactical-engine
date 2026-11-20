"""Auditoría point-in-time de calidad, antigüedad, régimen y cobertura histórica (P1-B1)."""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Callable

from bet_engine.contracts.epistemic import parse_utc_timestamp

from .observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    _same_coach_person,
    same_competition_season,
    same_meaningful_stratum,
)


def build_point_in_time_opponent_lookup(
    matches_by_team: dict[int, list[HistoricalTeamMatch] | tuple[HistoricalTeamMatch, ...]],
    *,
    min_matches: int = 4,
) -> Callable[[int, datetime], dict[str, float] | None]:
    """Construye un resolvedor as-of de fuerza del rival sin fuga temporal futura."""
    ordered_by_team: dict[int, list[HistoricalTeamMatch]] = {
        int(tid): sorted(ms, key=lambda m: parse_utc_timestamp(m.kickoff_utc))
        for tid, ms in matches_by_team.items()
    }

    def lookup(opponent_id: int, as_of_dt: datetime) -> dict[str, float] | None:
        if opponent_id <= 0:
            return None
        prior = [
            m
            for m in ordered_by_team.get(opponent_id, ())
            if parse_utc_timestamp(m.available_at_utc) <= as_of_dt
            and parse_utc_timestamp(m.kickoff_utc) < as_of_dt
            and m.own_metrics.get("goals") is not None
            and m.conceded_metrics.get("goals") is not None
        ]
        n_prior = len(prior)
        if n_prior < min_matches:
            return None
        gf_vals = [float(m.own_metrics["goals"]) for m in prior]
        ga_vals = [float(m.conceded_metrics["goals"]) for m in prior]
        pts_vals = [
            3.0 if gf > ga else (1.0 if gf == ga else 0.0)
            for gf, ga in zip(gf_vals, ga_vals)
        ]
        gf_pg = statistics.fmean(gf_vals)
        ga_pg = statistics.fmean(ga_vals)
        return {
            "n_prior": float(n_prior),
            "goals_for_pg": round(gf_pg, 4),
            "goals_against_pg": round(ga_pg, 4),
            "goal_diff_pg": round(gf_pg - ga_pg, 4),
            "ppg": round(statistics.fmean(pts_vals), 4),
        }

    return lookup


def audit_match_history_signals(
    match: HistoricalTeamMatch,
    *,
    target_ko: datetime,
    target_league_id: int,
    target_season: int,
    target_venue: str,
    target_stratum: str | None = None,
    current_coach_id: int | None = None,
    current_coach_name: str | None = None,
    opponent_strength: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Evalúa antigüedad, competición, sede, entrenador, roja y completitud de un antecedente."""
    ko_dt = parse_utc_timestamp(match.kickoff_utc)
    age_days = round(max(0.0, (target_ko - ko_dt).total_seconds() / 86400.0), 2)

    has_target_coach = current_coach_id is not None or bool(current_coach_name)
    has_match_coach = match.coach_id is not None or bool(match.coach_name)
    if has_target_coach and has_match_coach:
        is_same = _same_coach_person(
            match.coach_id,
            match.coach_name,
            current_coach_id,
            current_coach_name,
        )
        coach_status = "SAME_COACH" if is_same else "DIFFERENT_COACH"
    else:
        coach_status = "UNKNOWN"

    red_detail = match.red_card_detail or {}
    return {
        "fixture_id": match.fixture_id,
        "kickoff_utc": match.kickoff_utc,
        "age_days": age_days,
        "league_id": match.league_id,
        "league_name": match.league_name,
        "season": match.season,
        "competition_stratum": match.competition_stratum,
        "venue": match.venue,
        "opponent_id": match.opponent_id,
        "same_competition_season": same_competition_season(
            match, target_league_id, target_season
        ),
        "same_stratum": same_meaningful_stratum(match, target_stratum),
        "same_venue": bool(match.venue == target_venue),
        "coach_id": match.coach_id,
        "coach_name": match.coach_name,
        "coach_status": coach_status,
        "red_card_distortion": bool(match.red_card_distortion),
        "red_card_status": str(
            red_detail.get("red_card_status")
            or ("RED_CARD_CONFIRMED" if match.red_card_distortion else "RED_CARD_UNKNOWN")
        ),
        "red_card_side": str(red_detail.get("distortion_side") or "UNKNOWN"),
        "early_own_red": bool(red_detail.get("early_own_red")),
        "early_opp_red": bool(red_detail.get("early_opp_red")),
        "own_first_red_minute": red_detail.get("own_first_red_minute"),
        "opp_first_red_minute": red_detail.get("opp_first_red_minute"),
        "completeness_tier": (
            "COMPLETE_DETAILED" if match.has_detailed_stats else "SCORE_ONLY_NO_STATS"
        ),
        "goals_own": match.own_metrics.get("goals"),
        "goals_conceded": match.conceded_metrics.get("goals"),
        "corners_own": match.own_metrics.get("corners"),
        "yellow_own": match.own_metrics.get("yellow"),
        "opponent_strength": opponent_strength,
    }


def _coach_from_bundle(bundle: MatchObservationBundle, side: str) -> tuple[int | None, str | None]:
    for item in bundle.contextual_evidence:
        if item.category == "COACH_REGIME" and item.facts.get("side") == side:
            cid = item.facts.get("current_coach_id")
            cname = item.facts.get("current_coach_name")
            return (
                int(cid) if isinstance(cid, int) else None,
                str(cname) if cname else None,
            )
    return None, None


def audit_historical_window_quality(
    bundle: MatchObservationBundle,
    *,
    windows: tuple[int, ...] = (6, 10, 20),
    coach_overrides: dict[str, tuple[int | None, str | None]] | None = None,
    opponent_strength_fn: Callable[[int, datetime], dict[str, float] | None] | None = None,
) -> dict[str, Any]:
    """Audita ventanas comparables (6, 10, 20) sin inventar ni mezclar datos pospartido."""
    target_ko = parse_utc_timestamp(bundle.kickoff_utc)
    target_stratum = next(
        (
            m.competition_stratum
            for m in (bundle.home_matches + bundle.away_matches)
            if m.league_id == bundle.league_id
            and m.competition_stratum
            and m.competition_stratum != "UNKNOWN"
        ),
        None,
    )

    sides_report: dict[str, Any] = {}
    for side, matches, venue in (
        ("home", bundle.home_matches, "HOME"),
        ("away", bundle.away_matches, "AWAY"),
    ):
        ordered = sorted(
            matches,
            key=lambda m: parse_utc_timestamp(m.kickoff_utc),
            reverse=True,
        )
        cid, cname = (
            coach_overrides[side]
            if coach_overrides and side in coach_overrides
            else _coach_from_bundle(bundle, side)
        )

        rows = [
            audit_match_history_signals(
                m,
                target_ko=target_ko,
                target_league_id=bundle.league_id,
                target_season=bundle.season,
                target_venue=venue,
                target_stratum=target_stratum,
                current_coach_id=cid,
                current_coach_name=cname,
                opponent_strength=(
                    opponent_strength_fn(m.opponent_id, parse_utc_timestamp(m.kickoff_utc))
                    if opponent_strength_fn is not None
                    else None
                ),
            )
            for m in ordered
        ]

        per_window: dict[str, dict[str, Any]] = {}
        for w in windows:
            sub = rows[:w]
            n_avail = len(sub)
            ages = [float(r["age_days"]) for r in sub]
            opp_ppg = [
                float(r["opponent_strength"]["ppg"])
                for r in sub
                if isinstance(r.get("opponent_strength"), dict)
            ]
            opp_gd = [
                float(r["opponent_strength"]["goal_diff_pg"])
                for r in sub
                if isinstance(r.get("opponent_strength"), dict)
            ]
            per_window[f"L{w}"] = {
                "window": w,
                "n_available": n_avail,
                "shortfall": max(0, w - n_avail),
                "complete_detailed_n": sum(
                    1 for r in sub if r["completeness_tier"] == "COMPLETE_DETAILED"
                ),
                "score_only_n": sum(
                    1 for r in sub if r["completeness_tier"] == "SCORE_ONLY_NO_STATS"
                ),
                "goals_valid_n": sum(1 for r in sub if r["goals_own"] is not None),
                "corners_valid_n": sum(1 for r in sub if r["corners_own"] is not None),
                "yellow_valid_n": sum(1 for r in sub if r["yellow_own"] is not None),
                "same_competition_season_n": sum(1 for r in sub if r["same_competition_season"]),
                "same_stratum_n": sum(1 for r in sub if r["same_stratum"]),
                "same_venue_n": sum(1 for r in sub if r["same_venue"]),
                "same_coach_n": sum(1 for r in sub if r["coach_status"] == "SAME_COACH"),
                "different_coach_n": sum(1 for r in sub if r["coach_status"] == "DIFFERENT_COACH"),
                "unknown_coach_n": sum(1 for r in sub if r["coach_status"] == "UNKNOWN"),
                "red_card_any_n": sum(1 for r in sub if r["red_card_distortion"]),
                "red_card_confirmed_n": sum(1 for r in sub if r["red_card_status"] == "RED_CARD_CONFIRMED"),
                "red_card_verified_no_red_n": sum(1 for r in sub if r["red_card_status"] == "NO_RED_CARD_VERIFIED"),
                "red_card_unknown_n": sum(1 for r in sub if r["red_card_status"] == "RED_CARD_UNKNOWN"),
                "early_own_red_n": sum(1 for r in sub if r["early_own_red"]),
                "early_opp_red_n": sum(1 for r in sub if r["early_opp_red"]),
                "opponent_strength_known_n": len(opp_ppg),
                "mean_opponent_ppg": round(statistics.fmean(opp_ppg), 4) if opp_ppg else None,
                "mean_opponent_gd_pg": round(statistics.fmean(opp_gd), 4) if opp_gd else None,
                "min_age_days": round(min(ages), 2) if ages else None,
                "median_age_days": round(statistics.median(ages), 2) if ages else None,
                "max_age_days": round(max(ages), 2) if ages else None,
                "newest_kickoff_utc": sub[0]["kickoff_utc"] if sub else None,
                "oldest_kickoff_utc": sub[-1]["kickoff_utc"] if sub else None,
            }

        sides_report[side] = {
            "team_id": bundle.home_id if side == "home" else bundle.away_id,
            "team_name": bundle.home_name if side == "home" else bundle.away_name,
            "current_coach_id": cid,
            "current_coach_name": cname,
            "total_available": len(rows),
            "windows": per_window,
            "matches": rows,
        }

    return {
        "event_id": bundle.event_id,
        "fixture_id": bundle.fixture_id,
        "league_id": bundle.league_id,
        "season": bundle.season,
        "kickoff_utc": bundle.kickoff_utc,
        "cutoff_utc": bundle.cutoff_utc,
        "target_stratum": target_stratum,
        "sides": sides_report,
    }
