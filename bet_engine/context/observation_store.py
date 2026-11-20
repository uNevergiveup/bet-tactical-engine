"""ObservationStore: Almacén canónico de observaciones point-in-time (N=20+),
ventanas múltiples (L5/L10/L20), segmentos y extracción de ContextualEvidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from bet_engine.contracts import (
    ContextualEvidence,
    CoverageFlag,
    ObservedFact,
    VerificationStatus,
)
from bet_engine.contracts.epistemic import parse_utc_timestamp

STAT_LABEL_MAP: dict[str, str] = {
    "Corner Kicks": "corners",
    "Total Shots": "shots",
    "Shots on Goal": "on_target",
    "Blocked Shots": "blocked_shots",
    "Shots insidebox": "shots_insidebox",
    "Shots outsidebox": "shots_outsidebox",
    "Fouls": "fouls",
    "Yellow Cards": "yellow",
    "Red Cards": "red",
    "Offsides": "offsides",
    "expected_goals": "xg",
    "Ball Possession": "possession",
    "Total passes": "passes",
    "Passes accurate": "passes_accurate",
}

CORE_METRICS: tuple[str, ...] = (
    "goals",
    "goals_h1",
    "goals_h2",
    "corners",
    "shots",
    "on_target",
    "blocked_shots",
    "fouls",
    "yellow",
    "red",
    "offsides",
    "xg",
    "possession",
)


def _to_numeric(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip().rstrip("%")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


_NATIONAL_COMPETITION_LEAGUE_IDS: frozenset[int] = frozenset({1, 4, 5, 6, 9, 10})
_CLUB_FRIENDLY_LEAGUE_IDS: frozenset[int] = frozenset({667})
_NON_MATCHING_COMP_STRATA: frozenset[str] = frozenset({"", "UNKNOWN", "TIER_UNKNOWN"})
_NATIONAL_TEAM_NAME_RE = re.compile(
    r"(?:\bworld cup\b|\beuros?\b|\beuro championship\b|\bnations league\b|\bcopa america\b|"
    r"\bafrica cup of nations\b|\bafcon\b|\basian cup\b|\bgold cup\b|\bconfederations cup\b|"
    r"\bolympic\b|\bwc qualification\b)"
)
_FRIENDLY_NAME_RE = re.compile(r"\b(?:friendl(?:y|ies)|amistos[oa]s?)\b")


def _classify_stratum(
    league_name: str | None,
    league_type: str | None,
    *,
    league_id: int | None = None,
    country: str | None = None,
) -> str:
    name = (league_name or "").strip().lower()
    ltype = (league_type or "").strip().lower()
    ctry = (country or "").strip().lower()

    if "club world cup" in name or any(
        k in name
        for k in (
            "champions league",
            "europa league",
            "conference league",
            "libertadores",
            "sudamericana",
        )
    ):
        return "CONTINENTAL_CLUB"

    if league_id in _CLUB_FRIENDLY_LEAGUE_IDS or ("club" in name and _FRIENDLY_NAME_RE.search(name)):
        return "UNKNOWN"

    if league_id in _NATIONAL_COMPETITION_LEAGUE_IDS and "club" not in name:
        return "NATIONAL_TEAM"

    if _FRIENDLY_NAME_RE.search(name) and "club" not in name:
        if (
            "international" in name
            or "national" in name
            or ctry == "world"
            or name in {"friendlies", "friendly", "friendlies women", "friendly match"}
        ):
            return "NATIONAL_TEAM"

    if _NATIONAL_TEAM_NAME_RE.search(name) and "club" not in name:
        return "NATIONAL_TEAM"

    if ltype == "cup" or any(k in name for k in ("cup", "copa", "pokal", "coupe", "coppa", "fa cup", "efl")):
        return "DOMESTIC_CUP"
    if ltype == "league" or name:
        return "DOMESTIC_LEAGUE"
    return "UNKNOWN"


def _estimate_available_at_utc(kickoff_utc: str, status: str) -> str:
    """Estimación conservadora de disponibilidad cuando falta timestamp verificado de finalización."""
    ko = parse_utc_timestamp(kickoff_utc)
    st = str(status or "").strip().upper()
    if st == "FT":
        # Estimación conservadora para 90 min + descanso + descuento estándar.
        return (ko + timedelta(minutes=125)).isoformat()
    if st == "AET":
        # Estimación para prórroga (120 min + descansos + descuentos).
        return (ko + timedelta(minutes=165)).isoformat()
    if st == "PEN":
        # Estimación para prórroga + tanda de penaltis.
        return (ko + timedelta(minutes=180)).isoformat()
    # Regla conservadora ante estado desconocido/no estándar: +180 min estimado.
    return (ko + timedelta(minutes=180)).isoformat()


def _analyze_red_card_events(
    events: list[dict[str, Any]] | None,
    team_id: int,
    *,
    own_red_stat: float | None = None,
    opp_red_stat: float | None = None,
    early_cutoff_minute: float = 60.0,
    regulation_minutes: float = 90.0,
) -> dict[str, Any]:
    """Distingue rojas propias vs. rivales, minuto, exposición afectada y fiabilidad del dato."""
    own_minutes: list[int] = []
    opp_minutes: list[int] = []
    unknown_team_minutes: list[int] = []

    if events:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if str(ev.get("type") or "").strip().lower() != "card":
                continue
            detail = str(ev.get("detail") or "").strip().lower()
            if "red" not in detail:
                continue
            elapsed = _to_numeric((ev.get("time") or {}).get("elapsed"))
            if elapsed is None:
                continue
            minute = int(max(0, round(elapsed)))
            ev_team_id = (ev.get("team") or {}).get("id")
            if isinstance(ev_team_id, int):
                if ev_team_id == team_id:
                    own_minutes.append(minute)
                else:
                    opp_minutes.append(minute)
            else:
                unknown_team_minutes.append(minute)

    own_minutes.sort()
    opp_minutes.sort()
    unknown_team_minutes.sort()

    own_first = own_minutes[0] if own_minutes else None
    opp_first = opp_minutes[0] if opp_minutes else None
    any_minutes = sorted(own_minutes + opp_minutes + unknown_team_minutes)
    any_first = any_minutes[0] if any_minutes else None

    own_affected = round(max(0.0, regulation_minutes - min(regulation_minutes, float(own_first))), 2) if own_first is not None else 0.0
    opp_affected = round(max(0.0, regulation_minutes - min(regulation_minutes, float(opp_first))), 2) if opp_first is not None else 0.0
    own_share = round(own_affected / regulation_minutes, 4) if regulation_minutes > 0 else 0.0
    opp_share = round(opp_affected / regulation_minutes, 4) if regulation_minutes > 0 else 0.0

    early_own = bool(own_first is not None and own_first < early_cutoff_minute)
    early_opp = bool(opp_first is not None and opp_first < early_cutoff_minute)
    early_unknown = bool(unknown_team_minutes and unknown_team_minutes[0] < early_cutoff_minute)
    early_any = bool(early_own or early_opp or early_unknown)

    own_stat_n = int(round(own_red_stat)) if own_red_stat is not None else None
    opp_stat_n = int(round(opp_red_stat)) if opp_red_stat is not None else None
    has_stat_red = bool((own_stat_n or 0) > 0 or (opp_stat_n or 0) > 0)

    if events:
        stat_under_reported = bool(
            (own_stat_n is not None and len(own_minutes) < own_stat_n)
            or (opp_stat_n is not None and len(opp_minutes) < opp_stat_n)
        )
        data_availability = "PARTIAL_STAT_EVENT_MISMATCH" if stat_under_reported else "VERIFIED_EVENT_TIMELINE"
    elif has_stat_red:
        data_availability = "STAT_COUNT_ONLY_NO_MINUTE"
    elif own_stat_n is not None or opp_stat_n is not None:
        data_availability = "NO_EVENTS_TIMELINE"
    else:
        data_availability = "NO_STAT_AND_NO_EVENTS"

    if own_minutes or opp_minutes or unknown_team_minutes or has_stat_red:
        red_card_status = "RED_CARD_CONFIRMED"
        red_card_verified = True
    elif data_availability in ("VERIFIED_EVENT_TIMELINE", "NO_EVENTS_TIMELINE"):
        red_card_status = "NO_RED_CARD_VERIFIED"
        red_card_verified = True
    else:
        red_card_status = "RED_CARD_UNKNOWN"
        red_card_verified = False

    if early_own and early_opp:
        distortion_side = "BOTH_EARLY"
    elif early_own:
        distortion_side = "OWN_EARLY"
    elif early_opp:
        distortion_side = "OPPONENT_EARLY"
    elif early_unknown:
        distortion_side = "UNKNOWN_SIDE_EARLY"
    elif own_minutes or opp_minutes or unknown_team_minutes:
        distortion_side = "LATE_ONLY"
    elif has_stat_red:
        distortion_side = "STAT_ONLY_UNKNOWN_MINUTE"
    elif red_card_status == "RED_CARD_UNKNOWN":
        distortion_side = "UNKNOWN"
    else:
        distortion_side = "NONE"

    return {
        "red_card_status": red_card_status,
        "red_card_verified": red_card_verified,
        "distortion_side": distortion_side,
        "early_own_red": early_own,
        "early_opp_red": early_opp,
        "early_any_red": early_any,
        "own_red_count": len(own_minutes) if events else (own_stat_n or 0),
        "opp_red_count": len(opp_minutes) if events else (opp_stat_n or 0),
        "own_first_red_minute": own_first,
        "opp_first_red_minute": opp_first,
        "any_first_red_minute": any_first,
        "own_affected_minutes": own_affected,
        "opp_affected_minutes": opp_affected,
        "own_affected_share": own_share,
        "opp_affected_share": opp_share,
        "net_man_advantage_minutes": round(opp_affected - own_affected, 2),
        "regulation_minutes": regulation_minutes,
        "early_cutoff_minute": early_cutoff_minute,
        "data_availability": data_availability,
    }


def _detect_early_red_card(events: list[dict[str, Any]] | None, team_id: int) -> bool:
    return bool(_analyze_red_card_events(events, team_id)["early_any_red"])


@dataclass(frozen=True)
class HistoricalTeamMatch:
    """Representación normalizada de un partido histórico finalizado para un equipo."""

    fixture_id: int
    team_id: int
    opponent_id: int
    kickoff_utc: str
    available_at_utc: str
    league_id: int
    league_name: str
    season: int
    competition_stratum: str
    venue: str  # HOME | AWAY
    status: str
    coach_id: int | None
    coach_name: str | None
    red_card_distortion: bool
    own_metrics: dict[str, float | None]
    conceded_metrics: dict[str, float | None]
    has_detailed_stats: bool
    source: str
    events: tuple[dict[str, Any], ...] = ()
    red_card_detail: dict[str, Any] = field(default_factory=dict)

    @property
    def red_card_status(self) -> str:
        raw = (self.red_card_detail or {}).get("red_card_status")
        if raw:
            return str(raw)
        return "RED_CARD_CONFIRMED" if self.red_card_distortion else "RED_CARD_UNKNOWN"

    @property
    def red_card_verified(self) -> bool:
        return self.red_card_status in {"RED_CARD_CONFIRMED", "NO_RED_CARD_VERIFIED"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def same_competition_season(
    match: HistoricalTeamMatch, target_league_id: int, target_season: int
) -> bool:
    """Coincidencia real de liga y temporada excluyendo identificadores ausentes (<= 0)."""
    lid_m = int(getattr(match, "league_id", 0) or 0)
    lid_t = int(target_league_id or 0)
    sea_m = int(getattr(match, "season", 0) or 0)
    sea_t = int(target_season or 0)
    return lid_m > 0 and lid_t > 0 and lid_m == lid_t and sea_m > 0 and sea_t > 0 and sea_m == sea_t


def same_meaningful_stratum(
    match: HistoricalTeamMatch, target_stratum: str | None
) -> bool:
    """Coincidencia real de estrato excluyendo UNKNOWN / vacío / ausente."""
    st_t = str(target_stratum or "").strip()
    st_m = str(getattr(match, "competition_stratum", "") or "").strip()
    if st_t in _NON_MATCHING_COMP_STRATA or st_m in _NON_MATCHING_COMP_STRATA:
        return False
    return st_m == st_t


def build_league_catalog_from_preselection(
    matches: Iterable[dict[str, Any]],
    *,
    include_competition_registry: bool = True,
    competition_registry_path: Path | None = None,
) -> dict[int, dict[str, Any]]:
    """Construye catálogo determinista e invariante al orden: league_id -> metadatos."""
    best_by_lid: dict[int, tuple[tuple[int, int, int, str, str, str], dict[str, Any]]] = {}
    ordered_items = sorted(
        [m for m in matches if isinstance(m, dict)],
        key=lambda m: (
            int(m.get("fixture_id") or 0),
            int(m.get("league_id") or 0),
            str(m.get("league") or ""),
        ),
    )
    for m in ordered_items:
        lid = int(m.get("league_id") or 0)
        if lid <= 0:
            continue
        lname = str(m.get("league") or "").strip()
        ltype = str(m.get("league_type") or "").strip()
        ctry = str(m.get("country") or "").strip()
        if not (lname or ltype or ctry):
            continue
        score = (1 if lname else 0, 1 if ltype else 0, 1 if ctry else 0, lname, ltype, ctry)
        prev = best_by_lid.get(lid)
        if prev is None or score > prev[0]:
            best_by_lid[lid] = (
                score,
                {
                    "league_name": lname,
                    "league_type": ltype or None,
                    "country": ctry or None,
                },
            )

    catalog: dict[int, dict[str, Any]] = {lid: dict(val[1]) for lid, val in sorted(best_by_lid.items())}

    if include_competition_registry:
        reg_path = competition_registry_path or (
            Path(__file__).resolve().parents[2] / "config" / "competition_registry.json"
        )
        if reg_path.is_file():
            try:
                raw_reg = json.loads(reg_path.read_text(encoding="utf-8"))
                comps = raw_reg.get("competitions") or []
            except (OSError, ValueError):
                comps = []
            by_lid: dict[int, list[dict[str, Any]]] = {}
            for entry in comps:
                if not isinstance(entry, dict):
                    continue
                lid = int(entry.get("league_id") or 0)
                if lid > 0:
                    by_lid.setdefault(lid, []).append(entry)
            type_map = {
                "LEAGUE": "League",
                "NATIONAL_CUP": "Cup",
                "INTERNATIONAL_CUP": "Cup",
                "NATIONAL_FRIENDLY": "Cup",
            }
            for lid, entries_for_lid in sorted(by_lid.items()):
                if lid in catalog:
                    continue
                names = sorted({str((e.get("names") or [""])[0]).strip() for e in entries_for_lid if e.get("names")})
                types = sorted({str(e.get("type") or "").strip() for e in entries_for_lid if e.get("type")})
                countries = sorted({str(e.get("country") or "").strip() for e in entries_for_lid if e.get("country")})
                if len(names) == 1 and names[0] and len(types) == 1:
                    catalog[lid] = {
                        "league_name": names[0],
                        "league_type": type_map.get(types[0]),
                        "country": countries[0] if len(countries) == 1 and countries[0] else None,
                    }
    return catalog


@dataclass(frozen=True)
class WindowSummary:
    """Resumen multi-ventana con separación explícita de disponibles vs válidos por métrica."""

    window_name: str  # L5 | L10 | L20 | ALL
    segment_name: str  # all_competitions | exact_competition | other_competitions | venue_matching | current_coach_regime
    total_matches_available: int
    detailed_matches_valid: int
    competition_mix: dict[str, float]
    metrics: dict[str, dict[str, Any]]
    partial_or_score_only_count: int = 0
    oldest_kickoff_utc: str | None = None
    newest_kickoff_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MatchObservationBundle:
    """Paquete canónico de observaciones y evidencia contextual para un encuentro PREMATCH."""

    event_id: str
    fixture_id: int
    league_id: int
    season: int
    kickoff_utc: str
    cutoff_utc: str
    home_id: int
    away_id: int
    home_name: str
    away_name: str
    home_matches: tuple[HistoricalTeamMatch, ...]
    away_matches: tuple[HistoricalTeamMatch, ...]
    facts: tuple[ObservedFact, ...]
    contextual_evidence: tuple[ContextualEvidence, ...]
    rejected_records: tuple[dict[str, Any], ...]
    windows: dict[str, dict[str, WindowSummary]]  # {"home": {"L5_all": ...}, "away": {...}}
    source_sha256: str
    target_lineup_evaluation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "fixture_id": self.fixture_id,
            "league_id": self.league_id,
            "season": self.season,
            "kickoff_utc": self.kickoff_utc,
            "cutoff_utc": self.cutoff_utc,
            "home_id": self.home_id,
            "away_id": self.away_id,
            "home_name": self.home_name,
            "away_name": self.away_name,
            "home_matches_count": len(self.home_matches),
            "away_matches_count": len(self.away_matches),
            "facts_count": len(self.facts),
            "contextual_evidence": [c.to_dict() for c in self.contextual_evidence],
            "rejected_records": list(self.rejected_records),
            "windows": {
                side: {k: w.to_dict() for k, w in segs.items()}
                for side, segs in self.windows.items()
            },
            "source_sha256": self.source_sha256,
            "target_lineup_evaluation": dict(self.target_lineup_evaluation),
        }


def _extract_stats_from_bundle(bundle: dict[str, Any], team_id: int) -> tuple[dict[str, float | None], dict[str, float | None], bool]:
    own: dict[str, float | None] = {m: None for m in CORE_METRICS}
    conceded: dict[str, float | None] = {m: None for m in CORE_METRICS}

    teams = bundle.get("teams") or {}
    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}
    is_home = home_team.get("id") == team_id

    goals = bundle.get("goals") or {}
    gh = _to_numeric(goals.get("home"))
    ga = _to_numeric(goals.get("away"))
    own["goals"] = gh if is_home else ga
    conceded["goals"] = ga if is_home else gh

    score = bundle.get("score") or {}
    ht = score.get("halftime") or {}
    ht_h = _to_numeric(ht.get("home"))
    ht_a = _to_numeric(ht.get("away"))
    own["goals_h1"] = ht_h if is_home else ht_a
    conceded["goals_h1"] = ht_a if is_home else ht_h
    if own["goals"] is not None and own["goals_h1"] is not None:
        own["goals_h2"] = max(0.0, own["goals"] - own["goals_h1"])
    if conceded["goals"] is not None and conceded["goals_h1"] is not None:
        conceded["goals_h2"] = max(0.0, conceded["goals"] - conceded["goals_h1"])

    stats_list = bundle.get("statistics") or []
    has_detail = False
    for entry in stats_list:
        if not isinstance(entry, dict):
            continue
        tid = (entry.get("team") or {}).get("id")
        target = own if tid == team_id else conceded
        for st in entry.get("statistics") or []:
            if not isinstance(st, dict):
                continue
            mapped = STAT_LABEL_MAP.get(str(st.get("type") or ""))
            if mapped and mapped in target:
                val = _to_numeric(st.get("value"))
                target[mapped] = val
                if val is not None:
                    has_detail = True

    return own, conceded, has_detail


def _extract_coach(lineups: list[dict[str, Any]] | None, team_id: int) -> tuple[int | None, str | None]:
    for entry in lineups or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("team") or {}).get("id") == team_id:
            coach = entry.get("coach") or {}
            cid = coach.get("id")
            cname = coach.get("name")
            return (int(cid) if isinstance(cid, int) else None), (str(cname) if cname else None)
    return None, None


class ObservationStore:
    """Constructor unificado de observaciones históricas y evidencia contextual PREMATCH."""

    build_league_catalog = staticmethod(build_league_catalog_from_preselection)

    @staticmethod
    def _historical_match_richness_key(m: HistoricalTeamMatch) -> tuple[Any, ...]:
        has_lname = 1 if str(m.league_name or "").strip() else 0
        has_stratum = 1 if str(m.competition_stratum or "").strip() not in _NON_MATCHING_COMP_STRATA else 0
        has_lid = 1 if int(m.league_id or 0) > 0 else 0
        has_season = 1 if int(m.season or 0) > 0 else 0
        has_red_ver = 1 if m.red_card_verified else 0
        has_det = 1 if m.has_detailed_stats else 0
        has_coach = 1 if (m.coach_id is not None or bool(str(m.coach_name or "").strip())) else 0
        ev_n = len(m.events or ())
        return (
            has_lname,
            has_stratum,
            has_lid,
            has_season,
            has_red_ver,
            has_det,
            has_coach,
            ev_n,
            str(m.league_name or ""),
            str(m.competition_stratum or ""),
            str(m.red_card_status),
        )

    @classmethod
    def prefer_informative_historical_match(
        cls, existing: HistoricalTeamMatch, candidate: HistoricalTeamMatch
    ) -> HistoricalTeamMatch:
        """Combina de forma conmutativa e invariante al orden dos lecturas de un mismo (team_id, fixture_id)."""
        if (existing.fixture_id, existing.team_id) != (candidate.fixture_id, candidate.team_id):
            raise ValueError(
                f"HISTORICAL_MATCH_IDENTITY_MISMATCH:({existing.fixture_id},{existing.team_id})!=({candidate.fixture_id},{candidate.team_id})"
            )
        if cls._historical_match_richness_key(candidate) > cls._historical_match_richness_key(existing):
            winner, loser = candidate, existing
        else:
            winner, loser = existing, candidate

        new_lname = winner.league_name or loser.league_name
        new_stratum = (
            winner.competition_stratum
            if winner.competition_stratum not in _NON_MATCHING_COMP_STRATA
            else loser.competition_stratum
        )
        new_lid = winner.league_id if int(winner.league_id or 0) > 0 else loser.league_id
        new_season = winner.season if int(winner.season or 0) > 0 else loser.season
        new_red_detail = (
            winner.red_card_detail
            if winner.red_card_verified or not loser.red_card_verified
            else loser.red_card_detail
        )
        new_red_dist = bool(winner.red_card_distortion or loser.red_card_distortion)
        if (
            new_lname == winner.league_name
            and new_stratum == winner.competition_stratum
            and new_lid == winner.league_id
            and new_season == winner.season
            and new_red_detail == winner.red_card_detail
            and new_red_dist == winner.red_card_distortion
        ):
            return winner
        return replace(
            winner,
            league_name=new_lname,
            competition_stratum=new_stratum,
            league_id=new_lid,
            season=new_season,
            red_card_detail=new_red_detail,
            red_card_distortion=new_red_dist,
        )

    @staticmethod
    def build_window_summary(
        matches: list[HistoricalTeamMatch],
        *,
        window_name: str,
        segment_name: str,
        target_league_id: int,
        target_season: int,
    ) -> WindowSummary:
        total = len(matches)
        detailed = sum(1 for m in matches if m.has_detailed_stats)
        partial_only = total - detailed
        ko_sorted = sorted(parse_utc_timestamp(m.kickoff_utc) for m in matches) if matches else []
        oldest_ko = ko_sorted[0].isoformat() if ko_sorted else None
        newest_ko = ko_sorted[-1].isoformat() if ko_sorted else None

        exact_count = sum(1 for m in matches if same_competition_season(m, target_league_id, target_season))
        other_count = total - exact_count
        comp_mix = {
            "exact_competition_pct": round((exact_count / total) * 100.0, 2) if total > 0 else 0.0,
            "other_competitions_pct": round((other_count / total) * 100.0, 2) if total > 0 else 0.0,
        }

        metrics_summary: dict[str, dict[str, Any]] = {}
        for prefix in ("own", "conceded"):
            for metric in CORE_METRICS:
                key = f"{prefix}.{metric}"
                vals: list[float] = []
                for m in matches:
                    src = m.own_metrics if prefix == "own" else m.conceded_metrics
                    v = src.get(metric)
                    if v is not None:
                        vals.append(float(v))
                valid_n = len(vals)
                metrics_summary[key] = {
                    "valid_n": valid_n,
                    "missing_n": total - valid_n,
                    "coverage_pct": round((valid_n / total) * 100.0, 2) if total > 0 else 0.0,
                    "mean": round(statistics.fmean(vals), 4) if valid_n > 0 else None,
                    "median": round(statistics.median(vals), 4) if valid_n > 0 else None,
                    "std": round(statistics.pstdev(vals), 4) if valid_n > 1 else None,
                    "min": round(min(vals), 4) if valid_n > 0 else None,
                    "max": round(max(vals), 4) if valid_n > 0 else None,
                }

        return WindowSummary(
            window_name=window_name,
            segment_name=segment_name,
            total_matches_available=total,
            detailed_matches_valid=detailed,
            competition_mix=comp_mix,
            metrics=metrics_summary,
            partial_or_score_only_count=partial_only,
            oldest_kickoff_utc=oldest_ko,
            newest_kickoff_utc=newest_ko,
        )

    @classmethod
    def _build_all_windows(
        cls,
        matches: list[HistoricalTeamMatch],
        *,
        target_league_id: int,
        target_season: int,
        target_venue: str,
        current_coach_id: int | None,
    ) -> dict[str, WindowSummary]:
        # matches ordenados más reciente primero
        ordered = sorted(matches, key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
        segments: dict[str, list[HistoricalTeamMatch]] = {
            "all_competitions": ordered,
            "exact_competition": [m for m in ordered if same_competition_season(m, target_league_id, target_season)],
            "other_competitions": [m for m in ordered if not same_competition_season(m, target_league_id, target_season)],
            "venue_matching": [m for m in ordered if m.venue == target_venue],
        }
        if current_coach_id is not None:
            segments["current_coach_regime"] = [m for m in ordered if m.coach_id == current_coach_id]

        out: dict[str, WindowSummary] = {}
        for seg_name, seg_matches in segments.items():
            for w_name, limit in (("L5", 5), ("L6", 6), ("L10", 10), ("L20", 20)):
                key = f"{w_name}_{seg_name}"
                out[key] = cls.build_window_summary(
                    seg_matches[:limit],
                    window_name=w_name,
                    segment_name=seg_name,
                    target_league_id=target_league_id,
                    target_season=target_season,
                )
        return out

    @classmethod
    def from_match_engine_json(
        cls,
        payload: dict[str, Any],
        *,
        cutoff_utc: str | datetime | None = None,
        case_dir: Path | None = None,
        history_limit: int | None = None,
        coach_overrides: dict[str, dict[str, Any]] | None = None,
        qualitative_markdown: str | None = None,
    ) -> MatchObservationBundle:
        """Construye MatchObservationBundle desde match_engine_<event>.json (generate_context.py)
        preservando hasta N=20+ partidos sin el recorte P-1..P-6 ni parseo por regex.
        """
        if history_limit is not None and history_limit <= 0:
            raise ValueError(f"INVALID_HISTORY_LIMIT:{history_limit}")
        match_meta = payload.get("match") or {}
        fixture = payload.get("fixture") or {}
        ctx = payload.get("context") or {}

        event_id = str(match_meta.get("event_id") or "")
        fixture_id = int(fixture.get("fixture_id") or 0)
        league_id = int(fixture.get("league_id") or 0)
        season = int(fixture.get("season") or 0)
        kickoff_utc = str(fixture.get("date") or "")
        home_id = int(fixture.get("home_id") or 0)
        away_id = int(fixture.get("away_id") or 0)
        home_name = str(fixture.get("home_name") or match_meta.get("home_name") or "")
        away_name = str(fixture.get("away_name") or match_meta.get("away_name") or "")

        if cutoff_utc is not None:
            cut_dt = parse_utc_timestamp(cutoff_utc)
        elif payload.get("generated_at"):
            cut_dt = min(parse_utc_timestamp(payload["generated_at"]), parse_utc_timestamp(kickoff_utc))
        else:
            cut_dt = parse_utc_timestamp(kickoff_utc)

        source_sha = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        rejected: list[dict[str, Any]] = []
        facts: list[ObservedFact] = []
        contextual: list[ContextualEvidence] = []

        def parse_side(side_key: str, team_id: int) -> list[HistoricalTeamMatch]:
            deep = ctx.get(f"{side_key}_history_deep") or {}
            bundles = deep.get("detailed_fixtures") or []
            seen_ids: set[int] = set()
            parsed: list[HistoricalTeamMatch] = []

            for bundle in bundles:
                if not isinstance(bundle, dict):
                    continue
                fx = bundle.get("fixture") or {}
                lg = bundle.get("league") or {}
                tms = bundle.get("teams") or {}
                fid = fx.get("id")
                status = str((fx.get("status") or {}).get("short") or "")
                ko_raw = fx.get("date")
                h_id = (tms.get("home") or {}).get("id")
                a_id = (tms.get("away") or {}).get("id")

                if not isinstance(fid, int) or fid == fixture_id:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "TARGET_OR_INVALID_ID"})
                    continue
                if fid in seen_ids:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "DUPLICATE_FIXTURE"})
                    continue
                if status not in {"FT", "AET", "PEN"}:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": f"NON_FINAL_STATUS_{status}"})
                    continue
                if not ko_raw:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "MISSING_KICKOFF"})
                    continue
                avail_utc = _estimate_available_at_utc(str(ko_raw), status)
                if parse_utc_timestamp(avail_utc) > cut_dt or parse_utc_timestamp(str(ko_raw)) >= cut_dt:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "AFTER_CUTOFF_OR_NOT_FINISHED"})
                    continue
                if team_id not in (h_id, a_id) or h_id == a_id:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "TEAM_IDENTITY_MISMATCH"})
                    continue

                seen_ids.add(fid)
                venue = "HOME" if h_id == team_id else "AWAY"
                opp_id = int(a_id if venue == "HOME" else h_id)
                lid = int(lg.get("id") or 0)
                lname = str(lg.get("name") or "")
                lseason = int(lg.get("season") or 0)
                stratum = _classify_stratum(
                    lname,
                    lg.get("type"),
                    league_id=lid,
                    country=lg.get("country"),
                )
                own, conceded, has_detail = _extract_stats_from_bundle(bundle, team_id)
                coach_id, coach_name = _extract_coach(bundle.get("lineups"), team_id)
                raw_events = tuple(e for e in (bundle.get("events") or []) if isinstance(e, dict))
                red_detail = _analyze_red_card_events(
                    list(raw_events),
                    team_id,
                    own_red_stat=own.get("red"),
                    opp_red_stat=conceded.get("red"),
                )
                red_dist = bool(red_detail["early_any_red"])

                hm = HistoricalTeamMatch(
                    fixture_id=fid,
                    team_id=team_id,
                    opponent_id=opp_id,
                    kickoff_utc=parse_utc_timestamp(str(ko_raw)).isoformat(),
                    available_at_utc=avail_utc,
                    league_id=lid,
                    league_name=lname,
                    season=lseason,
                    competition_stratum=stratum,
                    venue=venue,
                    status=status,
                    coach_id=coach_id,
                    coach_name=coach_name,
                    red_card_distortion=red_dist,
                    own_metrics=own,
                    conceded_metrics=conceded,
                    has_detailed_stats=has_detail,
                    source="match_engine_json",
                    events=raw_events,
                    red_card_detail=red_detail,
                )
                parsed.append(hm)

            parsed.sort(key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
            if history_limit is not None and len(parsed) > history_limit:
                parsed = parsed[:history_limit]

            for hm in parsed:
                for prefix, metrics_map in (("own", hm.own_metrics), ("conceded", hm.conceded_metrics)):
                    for m_key, m_val in metrics_map.items():
                        facts.append(
                            ObservedFact(
                                fact_id=f"obs_{hm.fixture_id}_{team_id}_{prefix}_{m_key}",
                                entity_type="TEAM",
                                entity_id=team_id,
                                fixture_id=hm.fixture_id,
                                kickoff_utc=hm.kickoff_utc,
                                available_at_utc=hm.available_at_utc,
                                competition_id=hm.league_id,
                                season=hm.season,
                                competition_stratum=hm.competition_stratum,
                                period="FT",
                                metric_key=f"{prefix}.{m_key}",
                                value=m_val,
                                is_covered=(m_val is not None),
                                coverage_declared=CoverageFlag.UNKNOWN,
                                context_tags={
                                    "venue": hm.venue,
                                    "coach_id": hm.coach_id,
                                    "red_card_distortion": hm.red_card_distortion,
                                    "red_card_status": hm.red_card_detail.get("red_card_status"),
                                    "red_card_side": hm.red_card_detail.get("distortion_side"),
                                    "early_own_red": hm.red_card_detail.get("early_own_red"),
                                    "early_opp_red": hm.red_card_detail.get("early_opp_red"),
                                    "own_first_red_minute": hm.red_card_detail.get("own_first_red_minute"),
                                    "opp_first_red_minute": hm.red_card_detail.get("opp_first_red_minute"),
                                    "opponent_id": hm.opponent_id,
                                },
                                provenance={"source": "match_engine_json", "sha256": source_sha},
                            )
                        )
            return parsed

        home_matches = parse_side("home", home_id)
        away_matches = parse_side("away", away_id)

        # Entrenador actual: nunca promover histórico o coach=null a CURRENT_VERIFIED / CONFIRMADO_OFICIAL
        coachs = ctx.get("coachs") or {}
        raw_lineups_resp = ((ctx.get("lineups") or {}).get("response")) or ((ctx.get("target_fixture_full") or {}).get("lineups")) or []
        target_home_coach = _extract_coach(raw_lineups_resp, home_id)
        target_away_coach = _extract_coach(raw_lineups_resp, away_id)
        lineups_prov = (ctx.get("lineups") or {}).get("provenance") or {}
        target_coach_primary = bool(
            lineups_prov.get("primary_official_verified")
            or lineups_prov.get("primary_evidence_verified")
        )
        target_coach_source = str(lineups_prov.get("source") or "API_FOOTBALL_TARGET_LINEUP")
        target_coach_date = lineups_prov.get("captured_at_utc") or ((ctx.get("lineups") or {}).get("_acquired_at_utc"))

        qual_coach_hints = (
            extract_qualitative_coach_evidence(qualitative_markdown, home_name, away_name)
            if qualitative_markdown
            else {}
        )
        overrides = coach_overrides or {}
        home_ext_coach = overrides.get("home") or qual_coach_hints.get("home")
        away_ext_coach = overrides.get("away") or qual_coach_hints.get("away")

        home_coach_info = _resolve_coach_regime(
            coachs.get("home"),
            home_id,
            home_matches,
            cutoff_dt=cut_dt,
            target_coach=target_home_coach,
            target_coach_primary_verified=target_coach_primary,
            target_coach_source=target_coach_source,
            target_coach_date=target_coach_date,
            external_coach_evidence=home_ext_coach,
        )
        away_coach_info = _resolve_coach_regime(
            coachs.get("away"),
            away_id,
            away_matches,
            cutoff_dt=cut_dt,
            target_coach=target_away_coach,
            target_coach_primary_verified=target_coach_primary,
            target_coach_source=target_coach_source,
            target_coach_date=target_coach_date,
            external_coach_evidence=away_ext_coach,
        )
        home_coach_current = (
            home_coach_info["current_coach_id"]
            if home_coach_info["coach_status"] in ("CURRENT_VERIFIED", "NEW_COACH_VERIFIED")
            else None
        )
        away_coach_current = (
            away_coach_info["current_coach_id"]
            if away_coach_info["coach_status"] in ("CURRENT_VERIFIED", "NEW_COACH_VERIFIED")
            else None
        )

        for side_key, team_id, matches_list, coach_info in (
            ("home", home_id, home_matches, home_coach_info),
            ("away", away_id, away_matches, away_coach_info),
        ):
            ordered = sorted(matches_list, key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
            # Descanso real multicompetición
            rest_hours = None
            prior_comp = None
            if ordered:
                last_ko = parse_utc_timestamp(ordered[0].kickoff_utc)
                rest_hours = round((parse_utc_timestamp(kickoff_utc) - last_ko).total_seconds() / 3600.0, 2)
                prior_comp = ordered[0].league_name

            # Lookahead (próximo partido tras kickoff objetivo) verificando sede/confirmación
            next_block = (ctx.get(f"{side_key}_next_5") or {}).get("response") or []
            next_hours = None
            next_comp = None
            next_fixture_id = None
            next_fixture_status = None
            next_fixture_venue_present = None
            next_fixture_confirmed = None
            target_ko_dt = parse_utc_timestamp(kickoff_utc)
            future_candidates = []
            for nf in next_block:
                fx = nf.get("fixture") or {}
                n_ko = fx.get("date")
                if not n_ko:
                    continue
                n_dt = parse_utc_timestamp(str(n_ko))
                if n_dt > target_ko_dt:
                    st_short = str((fx.get("status") or {}).get("short") or "").upper()
                    ven = fx.get("venue") or {}
                    v_present = bool(ven.get("id") or ven.get("name"))
                    lg_obj = nf.get("league") or {}
                    lg_name = str(lg_obj.get("name") or "")
                    lg_type = str(lg_obj.get("type") or "")
                    is_cup = _classify_stratum(
                        lg_name,
                        lg_type,
                        league_id=lg_obj.get("id"),
                        country=lg_obj.get("country"),
                    ) in {"DOMESTIC_CUP", "CONTINENTAL_CLUB"}
                    confirmed = (st_short == "NS") and (v_present or not is_cup)
                    future_candidates.append((n_dt, lg_name, fx.get("id"), st_short, v_present, confirmed))
            if future_candidates:
                future_candidates.sort(key=lambda x: x[0])
                nxt = future_candidates[0]
                next_hours = round((nxt[0] - target_ko_dt).total_seconds() / 3600.0, 2)
                next_comp = nxt[1]
                next_fixture_id = nxt[2]
                next_fixture_status = nxt[3]
                next_fixture_venue_present = nxt[4]
                next_fixture_confirmed = nxt[5]

            contextual.append(
                ContextualEvidence(
                    evidence_id=f"ctx_{fixture_id}_{side_key}_schedule",
                    fixture_id=fixture_id,
                    category="SCHEDULE_FATIGUE",
                    verification_status=VerificationStatus.CONFIRMADO_OFICIAL,
                    recorded_at_utc=cut_dt.isoformat(),
                    facts={
                        "side": side_key,
                        "team_id": team_id,
                        "rest_hours_all_comps": rest_hours,
                        "prior_competition": prior_comp,
                        "lookahead_next_fixture_hours": next_hours,
                        "next_competition": next_comp,
                        "next_fixture_id": next_fixture_id,
                        "next_fixture_status": next_fixture_status,
                        "next_fixture_venue_present": next_fixture_venue_present,
                        "next_fixture_confirmed": next_fixture_confirmed,
                    },
                    provenance={"source": "match_engine_json", "sha256": source_sha},
                )
            )

            coach_verif = VerificationStatus(str(coach_info["verification_status"]))
            contextual.append(
                ContextualEvidence(
                    evidence_id=f"ctx_{fixture_id}_{side_key}_coach_regime",
                    fixture_id=fixture_id,
                    category="COACH_REGIME",
                    verification_status=coach_verif,
                    recorded_at_utc=cut_dt.isoformat(),
                    facts={
                        "side": side_key,
                        "team_id": team_id,
                        "current_coach_id": coach_info["current_coach_id"],
                        "current_coach_name": coach_info["current_coach_name"],
                        "coach_status": coach_info["coach_status"],
                        "coach_provenance": coach_info["coach_provenance"],
                        "source_tier": coach_info["source_tier"],
                        "primary_official_verified": coach_info["primary_official_verified"],
                        "verifiable_reference": coach_info["verifiable_reference"],
                        "publication_date": coach_info["publication_date"],
                        "continuity_verified": coach_info["continuity_verified"],
                        "last_historical_coach_id": coach_info["last_historical_coach_id"],
                        "last_historical_coach_name": coach_info["last_historical_coach_name"],
                        "last_historical_coach_source": coach_info["last_historical_coach_source"],
                        "last_historical_match_kickoff_utc": coach_info["last_historical_match_kickoff_utc"],
                        "target_coach_id": coach_info["target_coach_id"],
                        "target_coach_name": coach_info["target_coach_name"],
                        "suspected_coach_name": coach_info["suspected_coach_name"],
                        "coach_conflict_detail": coach_info["coach_conflict_detail"],
                        "career_spell_start": coach_info["career_spell_start"],
                        "coach_ids_in_window": coach_info["coach_ids_in_window"],
                        "canonical_coaches_in_window": coach_info["canonical_coaches_in_window"],
                        "historical_coaches": coach_info["historical_coaches"],
                        "regime_change_detected": coach_info["regime_change_detected"],
                        "regime_change_suspected": coach_info["regime_change_suspected"],
                        "historical_matches_under_current_coach_n": coach_info["historical_matches_under_current_coach_n"],
                        "historical_sample_belongs_to_prior_regime": coach_info["historical_sample_belongs_to_prior_regime"],
                    },
                    provenance={"source": coach_info["coach_provenance"], "sha256": source_sha},
                )
            )

        # Perfil disciplinario del árbitro (antes ignorado por los motores!)
        ref_hist = ctx.get("referee_history") or {}
        if ref_hist.get("available") and isinstance(ref_hist.get("profile"), dict):
            contextual.append(
                ContextualEvidence(
                    evidence_id=f"ctx_{fixture_id}_referee_profile",
                    fixture_id=fixture_id,
                    category="REFEREE_PROFILE",
                    verification_status=VerificationStatus.PUBLICADO_PREPARTIDO,
                    recorded_at_utc=cut_dt.isoformat(),
                    facts={
                        "referee": ref_hist.get("referee"),
                        "profile": ref_hist.get("profile"),
                        "coverage": ref_hist.get("coverage"),
                    },
                    provenance={"source": "match_engine_json", "sha256": source_sha},
                )
            )

        # Lesiones (injuries)
        inj_resp = (ctx.get("injuries") or {}).get("response") or []
        if inj_resp:
            contextual.append(
                ContextualEvidence(
                    evidence_id=f"ctx_{fixture_id}_injuries",
                    fixture_id=fixture_id,
                    category="INJURY_ABSENCE",
                    verification_status=VerificationStatus.PUBLICADO_PREPARTIDO,
                    recorded_at_utc=cut_dt.isoformat(),
                    facts={
                        "total_entries": len(inj_resp),
                        "home_entries": sum(1 for r in inj_resp if (r.get("team") or {}).get("id") == home_id),
                        "away_entries": sum(1 for r in inj_resp if (r.get("team") or {}).get("id") == away_id),
                    },
                    provenance={"source": "match_engine_json", "sha256": source_sha},
                )
            )

        from .quick_context import load_quick_context_bundle

        qc_candidates = [case_dir] if case_dir is not None else [
            Path.cwd() / "outputs" / "matches" / str(event_id),
            Path.cwd() / "outputs" / "matches" / f"fixture_{fixture_id}",
        ]
        seen_qc_ids = {c.evidence_id for c in contextual}
        for qdir in qc_candidates:
            if not qdir.is_dir():
                continue
            qc_evs, _ = load_quick_context_bundle(qdir)
            for ev in qc_evs:
                if ev.fixture_id == fixture_id and ev.evidence_id not in seen_qc_ids and parse_utc_timestamp(ev.recorded_at_utc) <= cut_dt:
                    contextual.append(ev)
                    seen_qc_ids.add(ev.evidence_id)

        windows = {
            "home": cls._build_all_windows(
                home_matches,
                target_league_id=league_id,
                target_season=season,
                target_venue="HOME",
                current_coach_id=home_coach_current,
            ),
            "away": cls._build_all_windows(
                away_matches,
                target_league_id=league_id,
                target_season=season,
                target_venue="AWAY",
                current_coach_id=away_coach_current,
            ),
        }

        from .lineup_manager import evaluate_fixture_lineup_state, extract_fixture_team_identity

        team_ident = extract_fixture_team_identity(payload)
        raw_lineups_resp = ((payload.get("context") or {}).get("lineups") or {}).get("response")
        manual_lineup_doc = payload.get("manual_lineup")
        # Distinguir entre context.lineups proveniente de API-Football vs inserción manual preservada
        lineups_prov = ((payload.get("context") or {}).get("lineups") or {}).get("provenance") or {}
        diff_meta = payload.get("differential_refresh") or {}
        api_acq_ts = (
            diff_meta.get("acquired_at_utc")
            or ((payload.get("context") or {}).get("lineups") or {}).get("_acquired_at_utc")
            or payload.get("generated_at")
        )
        if manual_lineup_doc is None and lineups_prov.get("lineup_status") in {
            "LINEUPS_MANUAL_VERIFIED",
            "LINEUPS_EXTERNAL_CONFIRMED",
        }:
            manual_lineup_doc = {
                "lineup_status": lineups_prov["lineup_status"],
                "source": lineups_prov.get("source"),
                "source_url": lineups_prov.get("source_url"),
                "source_kind": lineups_prov.get("source_kind"),
                "primary_evidence_verified": lineups_prov.get("primary_evidence_verified", False),
                "captured_at_utc": lineups_prov.get("captured_at_utc"),
                "sides": {"home": {"starters_count": 11}, "away": {"starters_count": 11}},
            }
            api_resp_for_eval = None
        else:
            api_resp_for_eval = raw_lineups_resp

        lineup_eval = evaluate_fixture_lineup_state(
            api_lineups_response=api_resp_for_eval,
            manual_lineup=manual_lineup_doc,
            fixture_id=fixture_id,
            event_id=event_id,
            home_id=team_ident.get("home_id") or home_id,
            away_id=team_ident.get("away_id") or away_id,
            home_name=str(team_ident.get("home_name") or home_name),
            away_name=str(team_ident.get("away_name") or away_name),
            home_aliases=team_ident.get("home_aliases"),
            away_aliases=team_ident.get("away_aliases"),
            is_national_team=bool(team_ident.get("is_national_team")),
            kickoff_utc=parse_utc_timestamp(kickoff_utc),
            now_utc=cut_dt,
            api_acquired_at_utc=api_acq_ts,
        )

        return MatchObservationBundle(
            event_id=event_id,
            fixture_id=fixture_id,
            league_id=league_id,
            season=season,
            kickoff_utc=parse_utc_timestamp(kickoff_utc).isoformat(),
            cutoff_utc=cut_dt.isoformat(),
            home_id=home_id,
            away_id=away_id,
            home_name=home_name,
            away_name=away_name,
            home_matches=tuple(home_matches),
            away_matches=tuple(away_matches),
            facts=tuple(facts),
            contextual_evidence=tuple(contextual),
            rejected_records=tuple(rejected),
            windows=windows,
            source_sha256=source_sha,
            target_lineup_evaluation=lineup_eval,
        )

    @classmethod
    def from_preselection_match(
        cls,
        target: dict[str, Any],
        *,
        cutoff_utc: str | datetime,
        history_limit: int | None = None,
        league_catalog: dict[int, dict[str, Any]] | None = None,
    ) -> MatchObservationBundle:
        """Construye MatchObservationBundle desde una entrada de preselection.json (N=20)."""
        if history_limit is not None and history_limit <= 0:
            raise ValueError(f"INVALID_HISTORY_LIMIT:{history_limit}")
        cut_dt = parse_utc_timestamp(cutoff_utc)
        fixture_id = int(target.get("fixture_id") or 0)
        league_id = int(target.get("league_id") or 0)
        season = int(target.get("season") or 0)
        kickoff_utc = str(target.get("kickoff_madrid") or target.get("kickoff_utc") or "")
        home_id = int(target.get("home_id") or 0)
        away_id = int(target.get("away_id") or 0)
        home_name = str(target.get("home") or "")
        away_name = str(target.get("away") or "")

        source_sha = hashlib.sha256(
            json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        rejected: list[dict[str, Any]] = []
        facts: list[ObservedFact] = []

        def parse_pre_side(side_key: str, team_id: int) -> list[HistoricalTeamMatch]:
            obs_list = (target.get("statistical_observations") or {}).get(side_key) or []
            parsed: list[HistoricalTeamMatch] = []
            seen: set[int] = set()
            for row in obs_list:
                fid = row.get("fixture_id")
                ko = row.get("kickoff_utc")
                status = str(row.get("status") or "")
                if not isinstance(fid, int) or fid == fixture_id or fid in seen:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "INVALID_OR_DUP_FIXTURE"})
                    continue
                if row.get("team_id") != team_id or status != "FT" or not ko:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "IDENTITY_OR_STATUS_INVALID"})
                    continue
                avail_utc = _estimate_available_at_utc(str(ko), status)
                if parse_utc_timestamp(avail_utc) > cut_dt or parse_utc_timestamp(str(ko)) >= cut_dt:
                    rejected.append({"fixture_id": fid, "side": side_key, "reason": "AFTER_CUTOFF"})
                    continue
                seen.add(fid)

                own = {m: _to_numeric((row.get("own") or {}).get(m)) for m in CORE_METRICS}
                conceded = {m: _to_numeric((row.get("conceded") or {}).get(m)) for m in CORE_METRICS}
                has_detail = any(
                    own.get(k) is not None for k in ("corners", "shots", "fouls", "yellow", "xg")
                )
                lid = int(row.get("league_id") or 0)
                lseason = int(row.get("season") or 0)
                same_target_league = lid > 0 and league_id > 0 and lid == league_id
                cat_info = (league_catalog or {}).get(lid) if lid > 0 else None
                lname = str(
                    row.get("league_name")
                    or (target.get("league") if same_target_league else "")
                    or (cat_info.get("league_name") if cat_info else "")
                    or ""
                )
                ltype = (
                    row.get("league_type")
                    if row.get("league_type") is not None
                    else (
                        target.get("league_type")
                        if same_target_league and target.get("league_type") is not None
                        else (cat_info.get("league_type") if cat_info else None)
                    )
                )
                lcountry = (
                    row.get("country")
                    if row.get("country") is not None
                    else (
                        target.get("country")
                        if same_target_league and target.get("country") is not None
                        else (cat_info.get("country") if cat_info else None)
                    )
                )
                venue = str(row.get("venue") or "UNKNOWN")
                stratum = _classify_stratum(
                    lname,
                    ltype,
                    league_id=lid,
                    country=lcountry,
                )
                pre_red_detail = _analyze_red_card_events(
                    None,
                    team_id,
                    own_red_stat=own.get("red"),
                    opp_red_stat=conceded.get("red"),
                )

                hm = HistoricalTeamMatch(
                    fixture_id=fid,
                    team_id=team_id,
                    opponent_id=int(row.get("opponent_id") or 0),
                    kickoff_utc=parse_utc_timestamp(str(ko)).isoformat(),
                    available_at_utc=avail_utc,
                    league_id=lid,
                    league_name=lname,
                    season=lseason,
                    competition_stratum=stratum,
                    venue=venue,
                    status=status,
                    coach_id=row.get("coach_id"),
                    coach_name=row.get("coach_name"),
                    red_card_distortion=bool((own.get("red") or 0) > 0 or (conceded.get("red") or 0) > 0),
                    own_metrics=own,
                    conceded_metrics=conceded,
                    has_detailed_stats=has_detail,
                    source="preselection_json",
                    red_card_detail=pre_red_detail,
                )
                parsed.append(hm)

            parsed.sort(key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
            if history_limit is not None and len(parsed) > history_limit:
                parsed = parsed[:history_limit]

            for hm in parsed:
                for prefix, metrics_map in (("own", hm.own_metrics), ("conceded", hm.conceded_metrics)):
                    for m_key, m_val in metrics_map.items():
                        facts.append(
                            ObservedFact(
                                fact_id=f"obs_{hm.fixture_id}_{team_id}_{prefix}_{m_key}",
                                entity_type="TEAM",
                                entity_id=team_id,
                                fixture_id=hm.fixture_id,
                                kickoff_utc=hm.kickoff_utc,
                                available_at_utc=hm.available_at_utc,
                                competition_id=hm.league_id,
                                season=hm.season,
                                competition_stratum=hm.competition_stratum,
                                period="FT",
                                metric_key=f"{prefix}.{m_key}",
                                value=m_val,
                                is_covered=(m_val is not None),
                                coverage_declared=CoverageFlag.UNKNOWN,
                                context_tags={"venue": hm.venue, "opponent_id": hm.opponent_id},
                                provenance={"source": "preselection_json", "sha256": source_sha},
                            )
                        )
            return parsed

        home_matches = parse_pre_side("home", home_id)
        away_matches = parse_pre_side("away", away_id)
        pre_contextual: list[ContextualEvidence] = []
        target_ko_dt = parse_utc_timestamp(kickoff_utc)
        for side_key, team_id, matches_list in (
            ("home", home_id, home_matches),
            ("away", away_id, away_matches),
        ):
            ordered = sorted(matches_list, key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
            rest_hours = None
            prior_comp = None
            if ordered:
                last_ko = parse_utc_timestamp(ordered[0].kickoff_utc)
                rest_hours = round((target_ko_dt - last_ko).total_seconds() / 3600.0, 2)
                prior_comp = ordered[0].league_name
            pre_contextual.append(
                ContextualEvidence(
                    evidence_id=f"ctx_{fixture_id}_{side_key}_schedule",
                    fixture_id=fixture_id,
                    category="SCHEDULE_FATIGUE",
                    verification_status=VerificationStatus.PUBLICADO_PREPARTIDO,
                    recorded_at_utc=cut_dt.isoformat(),
                    facts={
                        "side": side_key,
                        "team_id": team_id,
                        "rest_hours_all_comps": rest_hours,
                        "prior_competition": prior_comp,
                        "lookahead_next_fixture_hours": None,
                        "next_competition": None,
                    },
                    provenance={"source": "preselection_json", "sha256": source_sha},
                )
            )
        windows = {
            "home": cls._build_all_windows(
                home_matches,
                target_league_id=league_id,
                target_season=season,
                target_venue="HOME",
                current_coach_id=None,
            ),
            "away": cls._build_all_windows(
                away_matches,
                target_league_id=league_id,
                target_season=season,
                target_venue="AWAY",
                current_coach_id=None,
            ),
        }
        return MatchObservationBundle(
            event_id=str(target.get("event_id") or fixture_id),
            fixture_id=fixture_id,
            league_id=league_id,
            season=season,
            kickoff_utc=parse_utc_timestamp(kickoff_utc).isoformat(),
            cutoff_utc=cut_dt.isoformat(),
            home_id=home_id,
            away_id=away_id,
            home_name=home_name,
            away_name=away_name,
            home_matches=tuple(home_matches),
            away_matches=tuple(away_matches),
            facts=tuple(facts),
            contextual_evidence=tuple(pre_contextual),
            rejected_records=tuple(rejected),
            windows=windows,
            source_sha256=source_sha,
        )


def _norm_coach_tokens(name: str | None) -> list[str]:
    if not name:
        return []
    decomposed = unicodedata.normalize("NFKD", str(name))
    clean = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z]+", clean.lower())
    return [t for t in tokens if len(t) >= 1]


def _same_coach_person(cid_a: int | None, name_a: str | None, cid_b: int | None, name_b: str | None) -> bool:
    """Reconcilia alias de entrenador (p.ej. 'M. Corujo' / 'Mathias Corujo' o id=0 / 'Sergio Blanco')."""
    if isinstance(cid_a, int) and isinstance(cid_b, int) and cid_a > 0 and cid_b > 0 and cid_a == cid_b:
        return True
    toks_a = _norm_coach_tokens(name_a)
    toks_b = _norm_coach_tokens(name_b)
    if not toks_a or not toks_b:
        return False
    set_a, set_b = set(toks_a), set(toks_b)
    # Coincidencia por subconjunto completo (>=2 tokens significativos, p.ej. 'sergio blanco' en 'ruben blanco soto sergio')
    if len(set_a) >= 2 and set_a <= set_b:
        return True
    if len(set_b) >= 2 and set_b <= set_a:
        return True
    # Inicial + apellido(s): 'm corujo' <-> 'mathias corujo'
    non_single_a = [t for t in toks_a if len(t) > 1]
    non_single_b = [t for t in toks_b if len(t) > 1]
    if set(non_single_a) & set(non_single_b):
        shared_surnames = set(non_single_a) & set(non_single_b)
        rem_a = [t for t in toks_a if t not in shared_surnames]
        rem_b = [t for t in toks_b if t not in shared_surnames]
        if rem_a and rem_b and any(ra[0] == rb[0] and (len(ra) == 1 or len(rb) == 1 or ra == rb) for ra in rem_a for rb in rem_b):
            return True
    return False


def _canonical_coach_list(matches: list[HistoricalTeamMatch]) -> tuple[list[dict[str, Any]], bool]:
    """Construye secuencia cronológica de entrenadores canónicos y detecta cambios reales de régimen."""
    ordered_asc = sorted(matches, key=lambda m: parse_utc_timestamp(m.kickoff_utc))
    canon_groups: list[dict[str, Any]] = []
    era_sequence: list[int] = []
    for m in ordered_asc:
        if m.coach_id is None and not m.coach_name:
            continue
        matched_idx = None
        for idx, g in enumerate(canon_groups):
            if _same_coach_person(g["coach_id"], g["coach_name"], m.coach_id, m.coach_name):
                matched_idx = idx
                break
        if matched_idx is None:
            canon_groups.append(
                {
                    "coach_id": m.coach_id if (m.coach_id or 0) > 0 else None,
                    "coach_name": m.coach_name,
                    "observed_ids": [m.coach_id] if m.coach_id is not None else [],
                    "first_kickoff_utc": m.kickoff_utc,
                    "last_kickoff_utc": m.kickoff_utc,
                    "matches_count": 1,
                }
            )
            matched_idx = len(canon_groups) - 1
        else:
            g = canon_groups[matched_idx]
            if (g["coach_id"] is None or g["coach_id"] <= 0) and m.coach_id and m.coach_id > 0:
                g["coach_id"] = m.coach_id
            if m.coach_name and len(m.coach_name) > len(str(g["coach_name"] or "")):
                g["coach_name"] = m.coach_name
            if m.coach_id is not None and m.coach_id not in g["observed_ids"]:
                g["observed_ids"].append(m.coach_id)
            g["last_kickoff_utc"] = m.kickoff_utc
            g["matches_count"] += 1
        era_sequence.append(matched_idx)

    # Cambio real de régimen requiere al menos 2 técnicos canónicos distintos con transición temporal (no alternancia puntual sin transición)
    transitions = sum(1 for i in range(1, len(era_sequence)) if era_sequence[i] != era_sequence[i - 1])
    regime_shift = len(canon_groups) > 1 and transitions >= 1
    return canon_groups, regime_shift


def _resolve_coach_regime(
    coach_block: Any,
    team_id: int | None,
    matches: list[HistoricalTeamMatch],
    *,
    cutoff_dt: datetime | None = None,
    target_coach: tuple[int | None, str | None] | None = None,
    target_coach_primary_verified: bool = False,
    target_coach_source: str | None = None,
    target_coach_date: str | None = None,
    external_coach_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resuelve régimen de entrenador diferenciando primaria oficial, secundaria documentada, inferencia LLM y rumor."""
    ordered_desc = sorted(matches, key=lambda m: parse_utc_timestamp(m.kickoff_utc), reverse=True)
    canon_groups, window_shift = _canonical_coach_list(matches)
    coach_ids_seen = sorted({m.coach_id for m in ordered_desc if m.coach_id is not None})

    latest_lineup = next(
        (m for m in ordered_desc if (m.coach_id is not None and m.coach_id > 0) or m.coach_name),
        None,
    )
    last_hist_id = latest_lineup.coach_id if (latest_lineup and (latest_lineup.coach_id or 0) > 0) else None
    last_hist_name = latest_lineup.coach_name if latest_lineup else None
    last_hist_ko = latest_lineup.kickoff_utc if latest_lineup else None
    cut_date_str = cutoff_dt.date().isoformat() if cutoff_dt is not None else "9999-12-31"

    career_candidates: list[dict[str, Any]] = []
    historical_list: list[dict[str, Any]] = []
    if isinstance(coach_block, dict):
        resp = coach_block.get("response")
        if isinstance(resp, list):
            for entry in resp:
                cid = entry.get("id")
                cname = entry.get("name")
                career = entry.get("career") or []
                team_spells = [
                    sp for sp in career
                    if team_id is None or (sp.get("team") or {}).get("id") == team_id
                ]
                if not team_spells and team_id is not None:
                    t_entry = entry.get("team") or {}
                    if t_entry.get("id") == team_id:
                        team_spells = [{"start": "", "end": None}]
                active_spells = [
                    sp for sp in team_spells
                    if (sp.get("end") is None or str(sp.get("end")) >= cut_date_str)
                    and (not sp.get("start") or str(sp.get("start")) <= cut_date_str)
                ]
                best_start = max((str(sp.get("start") or "") for sp in (active_spells or team_spells)), default="")
                lineup_hits = sum(
                    1 for m in ordered_desc
                    if _same_coach_person(cid, cname, m.coach_id, m.coach_name)
                )
                row = {
                    "coach_id": cid if isinstance(cid, int) else None,
                    "coach_name": str(cname) if cname else None,
                    "active_spell": bool(active_spells),
                    "latest_start": best_start,
                    "lineup_hits": lineup_hits,
                    "status": "ACTIVE_CANDIDATE" if active_spells else "HISTORICAL",
                }
                if active_spells:
                    career_candidates.append(row)
                else:
                    historical_list.append(row)
        elif isinstance(coach_block.get("id"), int) and coach_block["id"] > 0:
            career_candidates.append({
                "coach_id": coach_block["id"],
                "coach_name": coach_block.get("name"),
                "active_spell": True,
                "latest_start": "",
                "lineup_hits": 0,
                "status": "ACTIVE_CANDIDATE",
            })

    career_candidates.sort(key=lambda r: (r["latest_start"], r["lineup_hits"], r["coach_id"] or 0), reverse=True)
    top_career = career_candidates[0] if career_candidates else None
    for old_cand in career_candidates[1:]:
        historical_list.append({**old_cand, "status": "HISTORICAL"})

    if last_hist_name is None and last_hist_id is None and top_career is not None:
        last_hist_id = top_career["coach_id"] if (top_career["coach_id"] or 0) > 0 else None
        last_hist_name = top_career["coach_name"]
        last_hist_source = "API_COACHS_CAREER_ONLY_UNVERIFIED" if (last_hist_name or last_hist_id) else None
    elif latest_lineup is not None:
        if last_hist_id is None and top_career is not None and (top_career["coach_id"] or 0) > 0:
            if _same_coach_person(top_career["coach_id"], top_career["coach_name"], None, last_hist_name):
                last_hist_id = top_career["coach_id"]
        last_hist_source = "HISTORICAL_LINEUPS"
    else:
        last_hist_source = None

    t_cid, t_cname = target_coach or (None, None)
    t_cid_clean = int(t_cid) if isinstance(t_cid, int) and t_cid > 0 else None
    t_cname_clean = str(t_cname).strip() if isinstance(t_cname, str) and str(t_cname).strip() else None
    has_target_coach = bool(t_cid_clean is not None or t_cname_clean)

    ext = external_coach_evidence or {}
    ext_tier = ext.get("verification_tier")
    ext_cid = int(ext["coach_id"]) if isinstance(ext.get("coach_id"), int) and ext["coach_id"] > 0 else None
    ext_cname = str(ext["coach_name"]).strip() if isinstance(ext.get("coach_name"), str) and ext["coach_name"].strip() else None
    ext_source = str(ext.get("source") or "EXTERNAL_RESEARCH")
    ext_ref = ext.get("verifiable_reference") or ext.get("source_url")
    ext_date = ext.get("publication_date")
    ext_source_tier = str(ext.get("source_tier") or ext_tier or "NONE")
    # Primaria oficial exige explicitación de verificación primaria + referencia verificable + fecha
    ext_primary_verified = bool(
        ext.get("primary_verified") is True
        and ext_ref
        and ext_date
        and ext_source != "QUALITATIVE_REPORT"
    )

    cur_id: int | None = None
    cur_name: str | None = None
    suspected_coach_name: str | None = None
    coach_conflict_detail: dict[str, Any] | None = None
    continuity_verified = False
    regime_shift = window_shift
    regime_change_suspected = False
    matches_under_current: int | None = None
    belongs_to_prior = False
    primary_official_verified = False
    source_tier = "NONE"
    verifiable_reference: str | None = None
    publication_date: str | None = None

    has_ext_substantive = bool(
        ext_tier in ("VERIFIED_SOURCE", "SECONDARY_DOCUMENTED", "SECONDARY_SUSPECTED")
        and (ext_cid is not None or ext_cname)
    )
    api_vs_ext_conflict = bool(
        has_target_coach
        and has_ext_substantive
        and not _same_coach_person(t_cid_clean, t_cname_clean, ext_cid, ext_cname)
    )

    if api_vs_ext_conflict and not (ext_primary_verified and not target_coach_primary_verified):
        coach_status = "COACH_IDENTITY_CONFLICT"
        verification_status = VerificationStatus.NO_VERIFICADO.value
        source_tier = "CONFLICT_API_VS_EXTERNAL"
        regime_change_suspected = True
        suspected_coach_name = ext_cname
        verifiable_reference = str(ext_ref) if ext_ref else None
        publication_date = str(ext_date) if ext_date else None
        coach_provenance = f"CONFLICT_API_TARGET_VS_EXTERNAL:{ext_source}"
        coach_conflict_detail = {
            "target_api_coach_id": t_cid_clean,
            "target_api_coach_name": t_cname_clean,
            "external_coach_name": ext_cname,
            "external_source": ext_source,
            "external_reference": verifiable_reference,
            "external_publication_date": publication_date,
        }
    elif has_target_coach or ext_primary_verified:
        if ext_primary_verified and not target_coach_primary_verified:
            cur_id = ext_cid
            cur_name = ext_cname
            primary_official_verified = True
            source_tier = "PRIMARY_OFFICIAL"
            verifiable_reference = str(ext_ref)
            publication_date = str(ext_date)
            verification_status = VerificationStatus.CONFIRMADO_OFICIAL.value
            if api_vs_ext_conflict:
                coach_conflict_detail = {
                    "overridden_api_target_coach_name": t_cname_clean,
                    "primary_official_coach_name": cur_name,
                    "reference": verifiable_reference,
                }
        else:
            cur_id = t_cid_clean
            cur_name = t_cname_clean
            primary_official_verified = bool(target_coach_primary_verified)
            source_tier = "PRIMARY_OFFICIAL" if primary_official_verified else "STRUCTURED_PROVIDER"
            verifiable_reference = target_coach_source or "API_FOOTBALL_FIXTURES_LINEUPS"
            publication_date = target_coach_date
            verification_status = (
                VerificationStatus.CONFIRMADO_OFICIAL.value
                if primary_official_verified
                else VerificationStatus.PUBLICADO_PREPARTIDO.value
            )

        if latest_lineup is not None:
            same_as_hist = _same_coach_person(cur_id, cur_name, last_hist_id, last_hist_name)
            if same_as_hist:
                coach_status = "CURRENT_VERIFIED"
                continuity_verified = True
                coach_provenance = (
                    "PRIMARY_OFFICIAL_CONTINUITY_VERIFIED"
                    if primary_official_verified
                    else "TARGET_MATCH_LINEUP_CONTINUITY_VERIFIED"
                )
            else:
                coach_status = "NEW_COACH_VERIFIED"
                continuity_verified = False
                regime_shift = True
                regime_change_suspected = True
                coach_provenance = (
                    f"PRIMARY_OFFICIAL_NEW_COACH:{ext_source}"
                    if ext_primary_verified
                    else "TARGET_MATCH_LINEUP_NEW_COACH"
                )
        else:
            coach_status = "CURRENT_VERIFIED"
            continuity_verified = False
            coach_provenance = (
                f"PRIMARY_OFFICIAL_VERIFIED:{ext_source}"
                if ext_primary_verified
                else "TARGET_MATCH_LINEUP_VERIFIED"
            )
    elif ext_tier == "SECONDARY_DOCUMENTED" and (ext_cid is not None or ext_cname) and ext_ref and ext_date:
        cur_id = ext_cid
        cur_name = ext_cname
        if cur_id is None and cur_name:
            matched_group = next(
                (g for g in canon_groups if _same_coach_person(None, cur_name, g["coach_id"], g["coach_name"])),
                None,
            )
            if matched_group and (matched_group.get("coach_id") or 0) > 0:
                cur_id = matched_group["coach_id"]
        primary_official_verified = False
        source_tier = "SECONDARY_DOCUMENTED"
        verifiable_reference = str(ext_ref)
        publication_date = str(ext_date)
        # Fuente secundaria documentada o citada por informe nunca se eleva a CONFIRMADO_OFICIAL
        verification_status = VerificationStatus.PUBLICADO_PREPARTIDO.value
        if latest_lineup is not None and not _same_coach_person(cur_id, cur_name, last_hist_id, last_hist_name):
            coach_status = "NEW_COACH_SECONDARY_DOCUMENTED"
            continuity_verified = False
            regime_shift = True
            regime_change_suspected = True
            coach_provenance = f"SECONDARY_DOCUMENTED_NEW_COACH:{ext_source}"
        else:
            coach_status = "CURRENT_SECONDARY_DOCUMENTED"
            continuity_verified = bool(latest_lineup is not None)
            coach_provenance = f"SECONDARY_DOCUMENTED_CURRENT:{ext_source}"
    elif ext_tier in ("VERIFIED_SOURCE",) and (ext_cid is not None or ext_cname) and ext_ref and ext_date:
        # Override explícito no primario pero sí documentado con URL/referencia y fecha
        cur_id = ext_cid
        cur_name = ext_cname
        primary_official_verified = False
        source_tier = "SECONDARY_DOCUMENTED"
        verifiable_reference = str(ext_ref)
        publication_date = str(ext_date)
        verification_status = VerificationStatus.PUBLICADO_PREPARTIDO.value
        if latest_lineup is not None and not _same_coach_person(cur_id, cur_name, last_hist_id, last_hist_name):
            coach_status = "NEW_COACH_SECONDARY_DOCUMENTED"
            continuity_verified = False
            regime_shift = True
            regime_change_suspected = True
            coach_provenance = f"SECONDARY_DOCUMENTED_NEW_COACH:{ext_source}"
        else:
            coach_status = "CURRENT_SECONDARY_DOCUMENTED"
            continuity_verified = bool(latest_lineup is not None)
            coach_provenance = f"SECONDARY_DOCUMENTED_CURRENT:{ext_source}"
    elif ext_tier in ("SECONDARY_SUSPECTED", "LLM_CLAIM_UNVERIFIED", "UNCONFIRMED_RUMOR", "VERIFIED_SOURCE") or (
        top_career is not None
        and latest_lineup is not None
        and bool(top_career["latest_start"])
        and last_hist_ko is not None
        and top_career["latest_start"] > last_hist_ko[:10]
        and not _same_coach_person(top_career["coach_id"], top_career["coach_name"], last_hist_id, last_hist_name)
    ):
        # Si es afirmación sin fuente o rumor, nunca acreditar vigencia
        suspected_coach_name = ext_cname or (top_career["coach_name"] if top_career else None)
        verifiable_reference = str(ext_ref) if ext_ref else None
        publication_date = str(ext_date) if ext_date else None
        if ext_tier in ("LLM_CLAIM_UNVERIFIED", "VERIFIED_SOURCE") or ext_source_tier == "LLM_CLAIM_UNVERIFIED":
            if latest_lineup is not None and suspected_coach_name and _same_coach_person(
                None, suspected_coach_name, last_hist_id, last_hist_name
            ):
                coach_status = "HISTORICAL_ONLY_UNVERIFIED"
                suspected_coach_name = None
                coach_provenance = "LLM_CLAIM_UNVERIFIED_FALLBACK_HISTORICAL"
            else:
                coach_status = "REGIME_CHANGE_SUSPECTED"
                regime_change_suspected = True
                coach_provenance = f"LLM_CLAIM_UNVERIFIED:{ext_source}"
            source_tier = "LLM_CLAIM_UNVERIFIED"
            verification_status = VerificationStatus.NO_VERIFICADO.value
        elif ext_tier == "UNCONFIRMED_RUMOR":
            coach_status = "REGIME_CHANGE_SUSPECTED"
            source_tier = "RUMOR"
            verification_status = VerificationStatus.NO_VERIFICADO.value
            regime_change_suspected = True
            coach_provenance = f"UNCONFIRMED_RUMOR:{ext_source}"
        else:
            coach_status = "REGIME_CHANGE_SUSPECTED"
            source_tier = "SECONDARY_UNLINKED" if ext_tier else "API_COACHS_CAREER"
            verification_status = VerificationStatus.PROBABLE.value
            regime_change_suspected = True
            coach_provenance = f"SECONDARY_SUSPECTED_SHIFT:{ext_source if ext_tier else 'API_COACHS_CAREER'}"
    elif last_hist_id is not None or last_hist_name is not None:
        coach_status = "HISTORICAL_ONLY_UNVERIFIED"
        source_tier = "HISTORICAL_API_ONLY"
        verification_status = VerificationStatus.NO_VERIFICADO.value
        coach_provenance = (
            "HISTORICAL_LINEUPS_ONLY_TARGET_COACH_UNVERIFIED"
            if latest_lineup is not None
            else "API_COACHS_CAREER_ONLY_UNVERIFIED"
        )
    else:
        coach_status = "UNKNOWN"
        source_tier = "NONE"
        verification_status = VerificationStatus.NO_VERIFICADO.value
        coach_provenance = "NO_COACH_EVIDENCE"

    if coach_status in (
        "CURRENT_VERIFIED",
        "NEW_COACH_VERIFIED",
        "CURRENT_SECONDARY_DOCUMENTED",
        "NEW_COACH_SECONDARY_DOCUMENTED",
    ) and (cur_id is not None or cur_name):
        matches_under_current = sum(
            1 for m in ordered_desc if _same_coach_person(cur_id, cur_name, m.coach_id, m.coach_name)
        )
        belongs_to_prior = bool(ordered_desc) and matches_under_current == 0

    return {
        "current_coach_id": cur_id,
        "current_coach_name": cur_name,
        "coach_status": coach_status,
        "verification_status": verification_status,
        "coach_provenance": coach_provenance,
        "source_tier": source_tier,
        "primary_official_verified": primary_official_verified,
        "verifiable_reference": verifiable_reference,
        "publication_date": publication_date,
        "continuity_verified": continuity_verified,
        "last_historical_coach_id": last_hist_id,
        "last_historical_coach_name": last_hist_name,
        "last_historical_coach_source": last_hist_source,
        "last_historical_match_kickoff_utc": last_hist_ko,
        "target_coach_id": t_cid_clean,
        "target_coach_name": t_cname_clean,
        "suspected_coach_name": suspected_coach_name,
        "coach_conflict_detail": coach_conflict_detail,
        "career_spell_start": top_career["latest_start"] if top_career else None,
        "coach_ids_in_window": coach_ids_seen,
        "canonical_coaches_in_window": canon_groups,
        "historical_coaches": historical_list[:10],
        "regime_change_detected": regime_shift,
        "regime_change_suspected": regime_change_suspected,
        "historical_matches_under_current_coach_n": matches_under_current,
        "historical_sample_belongs_to_prior_regime": belongs_to_prior,
    }


def extract_qualitative_coach_evidence(
    qualitative_markdown: str | None,
    home_name: str,
    away_name: str,
) -> dict[str, dict[str, Any]]:
    """Extrae evidencia de entrenador desde informe cualitativo sin promover afirmaciones LLM a oficial."""
    if not qualitative_markdown:
        return {}

    def _norm_txt(s: str) -> str:
        dec = unicodedata.normalize("NFKD", s)
        clean = "".join(ch for ch in dec if not unicodedata.combining(ch))
        return " ".join(re.findall(r"[a-z0-9]+", clean.lower()))

    def _match_side(label: str) -> str | None:
        nl = _norm_txt(label)
        if not nl:
            return None
        for side, team in (("home", home_name), ("away", away_name)):
            nt = _norm_txt(team)
            if not nt:
                continue
            toks = [t for t in nt.split() if len(t) >= 3 and t not in {"clube", "associacao", "futebol", "sport", "club", "real"}]
            if nl == nt or nl in nt or nt in nl or (toks and all(t in nl for t in toks)):
                return side
        return None

    # Índice de referencias externas con URL real dentro del propio informe (p.ej. tabla ARG-S004)
    source_registry_urls: dict[str, str] = {}
    for line in qualitative_markdown.splitlines():
        ref_m = re.search(r"\b(ARG-S\d+)\b", line)
        url_m = re.search(r"https?://[^\s\])>\\]+", line)
        if ref_m and url_m:
            source_registry_urls[ref_m.group(1)] = url_m.group(0).rstrip(".,;>")

    out: dict[str, dict[str, Any]] = {}
    sections = re.split(r"(?m)^(?=#{2,4}\s+)", qualitative_markdown)
    for sec in sections:
        m = re.match(r"^#{2,4}\s+(?:\d+(?:\.\d+)?\.?\s*)?([^\n]+)", sec)
        if not m:
            continue
        raw_heading = m.group(1).strip()
        heading = re.sub(r"\([^)]*\)", "", raw_heading).strip()
        if "—" not in heading and " - " not in heading:
            continue
        parts = re.split(r"\s+[—-]\s+", heading, maxsplit=1)
        if len(parts) != 2:
            continue
        team_part, coach_part = parts[0].strip(), parts[1].strip()
        side = _match_side(team_part)
        if side is None or side in out:
            continue
        body = sec[m.end():]
        coach_clean = coach_part.strip(" *`")
        if (
            not coach_clean
            or re.search(r"\d", coach_clean)
            or len(coach_clean.split()) > 6
            or any(x in coach_clean.lower() for x in ("xi ", "publicado", "probable", "alineacion", "muestra", "perfil", "estado"))
        ):
            continue
        body_norm = _norm_txt(body)

        # Buscar URL directa, referencia resuelta en tabla de fuentes y fecha explícita
        urls_in_sec = [u.rstrip(".,;>") for u in re.findall(r"https?://[^\s\])>\\]+", body)]
        ref_ids_in_sec = re.findall(r"\b(ARG-S\d+)\b", body)
        for rid in ref_ids_in_sec:
            if rid in source_registry_urls and source_registry_urls[rid] not in urls_in_sec:
                urls_in_sec.append(source_registry_urls[rid])
        dates_in_sec = re.findall(r"\b(?:20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/20\d{2})\b", body)
        domains_in_sec = re.findall(r"\b[a-z0-9-]+\.(?:com|es|org|net|br|uk|fr|de|it)(?:\.[a-z]{2})?\b", body.lower())

        has_verifiable_url = bool(urls_in_sec)
        has_date = bool(dates_in_sec)
        has_named_source = bool(domains_in_sec or any(k in body.lower() for k in ("ge.globo", "uol", "o liberal", "roma news", "dol", "itatiaia", "marca", "as ", "bbc", "guardian", "l'equipe")))

        is_rumor = (
            "no verificado" in body_norm
            or "rumor" in body_norm
            or "sin confirmar" in body_norm
            or "pendiente de confirmar" in body_norm
        )
        is_confirmed_claim = (
            "confirmado oficial" in body_norm
            or ("confirmado" in body_norm and ("oficializado" in body_norm or "publicado prepartido" in body_norm or "lineups" in body_norm or "acta" in body_norm or "vigencia" in body_norm))
        )
        is_probable = "probable" in body_norm or "sospecha" in body_norm or "posible cambio" in body_norm
        is_debut = any(k in _norm_txt(raw_heading + " " + body) for k in ("debut", "debuta", "nuevo entrenador", "nuevo dt", "entrante", "oficializado"))

        # Un informe cualitativo LLM NUNCA acredita verificación primaria directa por sí solo
        if is_rumor:
            tier = "UNCONFIRMED_RUMOR"
            s_tier = "RUMOR"
        elif is_confirmed_claim and has_verifiable_url and has_date:
            tier = "SECONDARY_DOCUMENTED"
            s_tier = "SECONDARY_DOCUMENTED"
        elif (is_confirmed_claim or is_probable) and has_named_source and has_date:
            tier = "SECONDARY_SUSPECTED"
            s_tier = "SECONDARY_UNLINKED"
        elif is_confirmed_claim or is_probable:
            # Afirmación del LLM sin fuente verificable ni fecha -> nunca eleva a oficial ni verificado
            tier = "LLM_CLAIM_UNVERIFIED"
            s_tier = "LLM_CLAIM_UNVERIFIED"
        else:
            continue

        out[side] = {
            "coach_name": coach_clean,
            "verification_tier": tier,
            "source_tier": s_tier,
            "primary_verified": False,
            "verifiable_reference": urls_in_sec[0] if urls_in_sec else (domains_in_sec[0] if domains_in_sec else None),
            "publication_date": dates_in_sec[0] if dates_in_sec else None,
            "is_debut": is_debut,
            "source": "QUALITATIVE_REPORT",
        }
    return out


def reconcile_bundle_coach_regime(
    bundle: MatchObservationBundle,
    *,
    qualitative_markdown: str | None = None,
    coach_overrides: dict[str, dict[str, Any]] | None = None,
) -> MatchObservationBundle:
    """Reconcilia ContextualEvidence(COACH_REGIME) sin tocar observaciones históricas ni p_model."""
    hints = extract_qualitative_coach_evidence(qualitative_markdown, bundle.home_name, bundle.away_name)
    merged_ext = {
        "home": (coach_overrides or {}).get("home") or hints.get("home"),
        "away": (coach_overrides or {}).get("away") or hints.get("away"),
    }
    if not merged_ext["home"] and not merged_ext["away"]:
        return bundle

    new_ctx: list[ContextualEvidence] = []
    changed = False
    for ev in bundle.contextual_evidence:
        if ev.category != "COACH_REGIME":
            new_ctx.append(ev)
            continue
        side = str(ev.facts.get("side") or "")
        ext = merged_ext.get(side)
        if not ext:
            new_ctx.append(ev)
            continue
        matches = list(bundle.home_matches if side == "home" else bundle.away_matches)
        cut_dt = parse_utc_timestamp(bundle.cutoff_utc)
        t_cid = ev.facts.get("target_coach_id")
        t_cname = ev.facts.get("target_coach_name")
        resolved = _resolve_coach_regime(
            None,
            bundle.home_id if side == "home" else bundle.away_id,
            matches,
            cutoff_dt=cut_dt,
            target_coach=(t_cid, t_cname),
            target_coach_primary_verified=bool(
                ev.facts.get("primary_official_verified")
                and ev.facts.get("source_tier") == "PRIMARY_OFFICIAL"
            ),
            target_coach_source=str(ev.facts.get("verifiable_reference") or ""),
            target_coach_date=ev.facts.get("publication_date"),
            external_coach_evidence=ext,
        )
        merged_facts = dict(ev.facts)
        for k in (
            "current_coach_id",
            "current_coach_name",
            "coach_status",
            "coach_provenance",
            "source_tier",
            "primary_official_verified",
            "verifiable_reference",
            "publication_date",
            "continuity_verified",
            "last_historical_coach_id",
            "last_historical_coach_name",
            "last_historical_coach_source",
            "last_historical_match_kickoff_utc",
            "target_coach_id",
            "target_coach_name",
            "suspected_coach_name",
            "coach_conflict_detail",
            "regime_change_detected",
            "regime_change_suspected",
            "historical_matches_under_current_coach_n",
            "historical_sample_belongs_to_prior_regime",
        ):
            merged_facts[k] = resolved[k]
        new_ctx.append(
            ContextualEvidence(
                evidence_id=ev.evidence_id,
                fixture_id=ev.fixture_id,
                category=ev.category,
                verification_status=VerificationStatus(str(resolved["verification_status"])),
                recorded_at_utc=ev.recorded_at_utc,
                facts=merged_facts,
                regime_invalidation=dict(ev.regime_invalidation),
                provenance={**ev.provenance, "reconciled_with": ext.get("source", "EXTERNAL")},
            )
        )
        changed = True

    if not changed:
        return bundle
    return replace(bundle, contextual_evidence=tuple(new_ctx))


def _extract_current_coach_id(coach_block: Any, matches: list[HistoricalTeamMatch]) -> int | None:
    """Wrapper compatible hacia atrás que devuelve el ID resuelto solo si el técnico actual está verificado."""
    res = _resolve_coach_regime(coach_block, None, matches)
    return res["current_coach_id"] if res["coach_status"] in ("CURRENT_VERIFIED", "NEW_COACH_VERIFIED") else None
