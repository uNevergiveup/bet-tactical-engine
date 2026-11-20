"""PatternRegistry extensible y evaluador seguro de DSL declarativo (Etapa 3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from bet_engine.context.observation_store import (
    CORE_METRICS,
    HistoricalTeamMatch,
    MatchObservationBundle,
)
from bet_engine.contracts import (
    EpistemicContractError,
    PatternScope,
    StatisticalPattern,
    TacticalHypothesis,
)
from bet_engine.contracts.epistemic import parse_utc_timestamp, validate_declarative_dsl


def _resolve_field(row: dict[str, Any], field_path: str) -> Any:
    if field_path in row:
        return row[field_path]
    cur: Any = row
    for part in field_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _num_cmp(a: Any, b: Any, cmp_op: str) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return (a == b) if cmp_op == "eq" else (a != b) if cmp_op == "ne" else False
    try:
        fa = float(a)
        fb = float(b)
    except (TypeError, ValueError):
        return (a == b) if cmp_op == "eq" else (a != b) if cmp_op == "ne" else False
    if cmp_op == "gt":
        return fa > fb
    if cmp_op == "ge":
        return fa >= fb
    if cmp_op == "lt":
        return fa < fb
    if cmp_op == "le":
        return fa <= fb
    if cmp_op == "eq":
        return fa == fb
    if cmp_op == "ne":
        return fa != fb
    return False


def eval_declarative_dsl(dsl: dict[str, Any], row: dict[str, Any]) -> bool | None:
    """Evalúa un predicado DSL declarativo sobre una fila histórica.

    Retorna None si faltan datos requeridos para evaluar una cláusula (null != False/0).
    """
    validate_declarative_dsl(dsl)
    op = dsl["op"]
    results: list[bool | None] = []
    for item in dsl["clauses"]:
        if "op" in item:
            results.append(eval_declarative_dsl(item, row))
            continue
        left = _resolve_field(row, item["field"])
        if left is None:
            results.append(None)
            continue
        right = _resolve_field(row, item["value_field"]) if "value_field" in item else item.get("value")
        if right is None:
            results.append(None)
            continue
        cmp_op = item["cmp"]
        if cmp_op == "in":
            results.append(left in right if isinstance(right, (list, tuple, set, str)) else False)
        elif cmp_op == "not_in":
            results.append(left not in right if isinstance(right, (list, tuple, set, str)) else False)
        elif cmp_op in ("eq", "ne") and not isinstance(left, (int, float)) and not isinstance(right, (int, float)):
            results.append((left == right) if cmp_op == "eq" else (left != right))
        else:
            results.append(_num_cmp(left, right, cmp_op))

    if op == "all":
        if any(r is False for r in results):
            return False
        if any(r is None for r in results):
            return None
        return True
    if any(r is True for r in results):
        return True
    if any(r is None for r in results):
        return None
    return False


def extract_dsl_fields(dsl: dict[str, Any]) -> set[str]:
    """Extrae todos los campos referenciados por un DSL para comprobar disponibilidad PREMATCH."""
    validate_declarative_dsl(dsl)
    fields: set[str] = set()
    for item in dsl["clauses"]:
        if "op" in item:
            fields |= extract_dsl_fields(item)
        else:
            fields.add(str(item["field"]))
            if "value_field" in item:
                fields.add(str(item["value_field"]))
    return fields


def _effective_minute(elapsed: int, extra: int, h1_added: int) -> int:
    """Mapea (elapsed, extra) a minuto continuo incluyendo tiempo añadido de 1H y 2H."""
    if elapsed <= 45:
        return max(0, elapsed) + (max(0, extra) if elapsed == 45 else 0)
    return max(46, elapsed) + h1_added + (max(0, extra) if elapsed >= 90 else 0)


def reconstruct_score_state_timeline(match: HistoricalTeamMatch) -> dict[str, Any]:
    """Reconstruye estado del marcador minuto a minuto estrictamente desde eventos verificados.

    Soporta:
    - Goles en propia puerta (Own Goal, tanto convención API-Football como scored_in_own_net=True).
    - Penaltis anotados, penaltis fallados (Missed Penalty) y exclusión de tandas (Penalty Shootout).
    - Tiempo añadido en 1H (45+extra) y 2H (90+extra).
    - Detección de eventos incompletos cuando los goles en events no concuerdan con el marcador FT.
    - Prohibición de reconstruir minutos de córners cuando la fuente no informa eventos Corner.
    """
    if not match.events:
        return {
            "chronology_available": False,
            "corner_timeline_available": False,
            "reason": "NO_VERIFIED_EVENTS_TIMELINE",
            "minutes_leading": None,
            "minutes_drawing": None,
            "minutes_trailing": None,
            "total_tracked_minutes": None,
            "h1_added_minutes": 0,
            "h2_added_minutes": 0,
            "own_goals_count": 0,
            "penalties_scored_count": 0,
            "missed_penalties_count": 0,
            "goals_by_state": None,
            "cards_by_state": None,
            "corners_by_state": None,
        }

    team_id = match.team_id
    opp_id = match.opponent_id

    # Primera pasada: determinar tiempo añadido máximo en 1H y 2H
    h1_added = 0
    h2_added = 0
    for ev in match.events:
        t_obj = ev.get("time") or {}
        el = t_obj.get("elapsed")
        ex = t_obj.get("extra")
        comments = str(ev.get("comments") or "").strip().lower()
        if not isinstance(el, (int, float)) or el > 120 or "penalty shootout" in comments:
            continue
        el_i = int(el)
        ex_i = int(ex) if isinstance(ex, (int, float)) and ex > 0 else 0
        if el_i == 45 and ex_i > h1_added:
            h1_added = min(20, ex_i)
        elif el_i >= 90 and ex_i > h2_added:
            h2_added = min(25, ex_i)

    total_duration = 90 + h1_added + h2_added

    goal_events: list[tuple[int, int, str]] = []  # (eff_minute, credited_team_id, detail)
    card_events: list[tuple[int, int, str]] = []
    corner_events: list[tuple[int, int]] = []
    own_goals_count = 0
    penalties_scored_count = 0
    missed_penalties_count = 0

    for ev in match.events:
        etype = str(ev.get("type") or "").strip().lower()
        detail = str(ev.get("detail") or "").strip().lower()
        comments = str(ev.get("comments") or "").strip().lower()
        tid = (ev.get("team") or {}).get("id")
        t_obj = ev.get("time") or {}
        elapsed = t_obj.get("elapsed")
        extra = t_obj.get("extra")
        if not isinstance(tid, int) or not isinstance(elapsed, (int, float)):
            continue
        el_i = int(elapsed)
        if el_i > 120 or "penalty shootout" in comments:
            continue
        ex_i = int(extra) if isinstance(extra, (int, float)) and extra > 0 else 0
        eff_min = min(total_duration, _effective_minute(el_i, ex_i, h1_added))

        if etype == "goal":
            if "missed penalty" in detail:
                missed_penalties_count += 1
                continue
            if "own goal" in detail:
                own_goals_count += 1
                # Si el evento marca explícitamente scored_in_own_net=True, el gol sube al rival;
                # en API-Football estándar, team.id ya es el equipo beneficiario del gol.
                if ev.get("scored_in_own_net") is True:
                    credited_tid = opp_id if tid == team_id else team_id
                else:
                    credited_tid = tid
            else:
                if "penalty" in detail:
                    penalties_scored_count += 1
                credited_tid = tid
            goal_events.append((eff_min, credited_tid, detail))
        elif etype == "card":
            card_events.append((eff_min, tid, detail))
        elif etype == "corner":
            corner_events.append((eff_min, tid))

    goal_events.sort(key=lambda x: x[0])

    # Verificación de integridad: los goles en events deben coincidir con el marcador FT observado
    exp_own = match.own_metrics.get("goals")
    exp_con = match.conceded_metrics.get("goals")
    ev_own = sum(1 for _, ctid, _ in goal_events if ctid == team_id)
    ev_con = sum(1 for _, ctid, _ in goal_events if ctid != team_id)
    if exp_own is not None and exp_con is not None and match.status == "FT":
        if ev_own != int(round(exp_own)) or ev_con != int(round(exp_con)):
            return {
                "chronology_available": False,
                "corner_timeline_available": False,
                "reason": f"CHRONOLOGY_INCOMPLETE_SCORE_MISMATCH(events={ev_own}-{ev_con},ft={int(round(exp_own))}-{int(round(exp_con))})",
                "minutes_leading": None,
                "minutes_drawing": None,
                "minutes_trailing": None,
                "total_tracked_minutes": total_duration,
                "h1_added_minutes": h1_added,
                "h2_added_minutes": h2_added,
                "own_goals_count": own_goals_count,
                "penalties_scored_count": penalties_scored_count,
                "missed_penalties_count": missed_penalties_count,
                "goals_by_state": None,
                "cards_by_state": None,
                "corners_by_state": None,
            }

    own_score = 0
    opp_score = 0
    last_min = 0
    mins = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}
    goals_state = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}

    def current_state(o: int, a: int) -> str:
        return "LEADING" if o > a else "TRAILING" if o < a else "DRAWING"

    for gm, ctid, _ in goal_events:
        st = current_state(own_score, opp_score)
        if gm > last_min:
            mins[st] += gm - last_min
            last_min = gm
        if ctid == team_id:
            goals_state[st] += 1
            own_score += 1
        else:
            opp_score += 1
    if last_min < total_duration:
        mins[current_state(own_score, opp_score)] += total_duration - last_min

    def state_at_minute(target_min: int) -> str:
        o, a = 0, 0
        for gm, ctid, _ in goal_events:
            if gm < target_min:
                if ctid == team_id:
                    o += 1
                else:
                    a += 1
        return current_state(o, a)

    cards_state = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}
    for cm, ctid, _ in card_events:
        if ctid == team_id:
            cards_state[state_at_minute(cm)] += 1

    corners_state = None
    corner_tl = len(corner_events) > 0
    if corner_tl:
        corners_state = {"LEADING": 0, "DRAWING": 0, "TRAILING": 0}
        for km, ktid in corner_events:
            if ktid == team_id:
                corners_state[state_at_minute(km)] += 1

    return {
        "chronology_available": True,
        "corner_timeline_available": corner_tl,
        "reason": "VERIFIED_EVENTS_TIMELINE" if corner_tl else "CORNER_TIMELINE_ABSENT_NO_RECONSTRUCTION",
        "minutes_leading": mins["LEADING"],
        "minutes_drawing": mins["DRAWING"],
        "minutes_trailing": mins["TRAILING"],
        "total_tracked_minutes": total_duration,
        "h1_added_minutes": h1_added,
        "h2_added_minutes": h2_added,
        "own_goals_count": own_goals_count,
        "penalties_scored_count": penalties_scored_count,
        "missed_penalties_count": missed_penalties_count,
        "goals_by_state": goals_state,
        "cards_by_state": cards_state,
        "corners_by_state": corners_state,
    }


def build_feature_rows(
    matches: tuple[HistoricalTeamMatch, ...] | list[HistoricalTeamMatch],
    *,
    side: str,
) -> list[dict[str, Any]]:
    """Convierte partidos históricos en filas planas con prefijos estrictos:
    - lag.* y context.*: PREMATCH_OBSERVABLE estrictamente con available_at_utc <= m.kickoff_utc.
    - own.*, conceded.*, score_state.*: POSTMATCH_ONLY (resultado interno del partido histórico).
    """
    chronological = sorted(matches, key=lambda m: parse_utc_timestamp(m.kickoff_utc))
    rows: list[dict[str, Any]] = []

    for idx, m in enumerate(chronological):
        m_ko = parse_utc_timestamp(m.kickoff_utc)
        # Solo partidos cuya disponibilidad final sea <= kickoff del partido histórico m
        prior = [
            p
            for p in chronological[:idx]
            if parse_utc_timestamp(p.available_at_utc) <= m_ko
        ]
        gap_days = None
        max_prior_avail_utc = None
        if prior:
            prev_ko = parse_utc_timestamp(prior[-1].kickoff_utc)
            gap_days = round((m_ko - prev_ko).total_seconds() / 86400.0, 2)
            max_prior_avail_utc = max(parse_utc_timestamp(p.available_at_utc) for p in prior).isoformat()

        lag_own: dict[str, float | None] = {}
        lag_conceded: dict[str, float | None] = {}
        recent_prior = prior[-5:]
        for metric in CORE_METRICS:
            vals_o = [p.own_metrics[metric] for p in recent_prior if p.own_metrics.get(metric) is not None]
            vals_c = [p.conceded_metrics[metric] for p in recent_prior if p.conceded_metrics.get(metric) is not None]
            lag_own[f"{metric}_l5"] = round(sum(vals_o) / len(vals_o), 4) if len(vals_o) >= 2 else None
            lag_conceded[f"{metric}_l5"] = round(sum(vals_c) / len(vals_c), 4) if len(vals_c) >= 2 else None

        state_info = reconstruct_score_state_timeline(m)
        row: dict[str, Any] = {
            "fixture_id": m.fixture_id,
            "side": side,
            "team_id": m.team_id,
            "kickoff_utc": m.kickoff_utc,
            "available_at_utc": m.available_at_utc,
            "league_id": m.league_id,
            "season": m.season,
            "competition_stratum": m.competition_stratum,
            "context": {
                "venue": m.venue,
                "league_id": m.league_id,
                "season": m.season,
                "competition_stratum": m.competition_stratum,
                "coach_id": m.coach_id,
                "observed_gap_days": gap_days,
                "opponent_id": m.opponent_id,
            },
            "lag": {
                "own": lag_own,
                "conceded": lag_conceded,
                "prior_count": len(prior),
                "max_prior_available_at_utc": max_prior_avail_utc,
            },
            "own": dict(m.own_metrics),
            "conceded": dict(m.conceded_metrics),
            "score_state": {
                "chronology_available": state_info["chronology_available"],
                "corner_timeline_available": state_info["corner_timeline_available"],
                "reason": state_info["reason"],
                "minutes_trailing": state_info["minutes_trailing"],
                "minutes_drawing": state_info["minutes_drawing"],
                "minutes_leading": state_info["minutes_leading"],
                "total_tracked_minutes": state_info["total_tracked_minutes"],
                "own_goals_count": state_info["own_goals_count"],
                "penalties_scored_count": state_info["penalties_scored_count"],
                "missed_penalties_count": state_info["missed_penalties_count"],
                "red_card_distortion": m.red_card_distortion,
                "red_card_side": (m.red_card_detail or {}).get(
                    "distortion_side",
                    "UNKNOWN_EARLY" if m.red_card_distortion else "NONE",
                ),
                "early_own_red": bool((m.red_card_detail or {}).get("early_own_red", False)),
                "early_opp_red": bool((m.red_card_detail or {}).get("early_opp_red", False)),
                "own_first_red_minute": (m.red_card_detail or {}).get("own_first_red_minute"),
                "opp_first_red_minute": (m.red_card_detail or {}).get("opp_first_red_minute"),
                "own_affected_minutes": (m.red_card_detail or {}).get("own_affected_minutes", 0.0),
                "opp_affected_minutes": (m.red_card_detail or {}).get("opp_affected_minutes", 0.0),
                "red_card_data_availability": (m.red_card_detail or {}).get(
                    "data_availability", "NO_EVENTS_TIMELINE"
                ),
            },
        }
        rows.append(row)
    return rows


@dataclass(frozen=True)
class PluginOutput:
    """Salida tipada de un plugin del PatternRegistry."""

    plugin_id: str
    domain_family: str
    patterns: tuple[StatisticalPattern, ...] = ()
    hypotheses: tuple[TacticalHypothesis, ...] = ()
    skipped_reasons: tuple[str, ...] = ()
    telemetry: dict[str, Any] = field(default_factory=dict)


class PatternPlugin(Protocol):
    """Interfaz extensible para añadir nuevas familias de patrones sin tocar el orquestador."""

    plugin_id: str
    domain_family: str

    def evaluate(self, bundle: MatchObservationBundle) -> PluginOutput: ...


@dataclass(frozen=True)
class AnalyticsEngineResult:
    """Salida consolidada del núcleo de patrones (Etapa 3) manteniendo separación epistémica."""

    event_id: str
    fixture_id: int
    cutoff_utc: str
    prematch_patterns: tuple[StatisticalPattern, ...]
    internal_mechanisms: tuple[StatisticalPattern, ...]
    descriptive_splits: tuple[StatisticalPattern, ...]
    tactical_hypotheses: tuple[TacticalHypothesis, ...]
    skipped_plugins: dict[str, tuple[str, ...]]
    plugin_telemetry: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "fixture_id": self.fixture_id,
            "cutoff_utc": self.cutoff_utc,
            "counts": {
                "prematch_patterns": len(self.prematch_patterns),
                "internal_mechanisms": len(self.internal_mechanisms),
                "descriptive_splits": len(self.descriptive_splits),
                "tactical_hypotheses": len(self.tactical_hypotheses),
            },
            "prematch_patterns": [p.to_dict() for p in self.prematch_patterns],
            "internal_mechanisms": [p.to_dict() for p in self.internal_mechanisms],
            "descriptive_splits": [p.to_dict() for p in self.descriptive_splits],
            "tactical_hypotheses": [h.to_dict() for h in self.tactical_hypotheses],
            "skipped_plugins": {k: list(v) for k, v in self.skipped_plugins.items()},
            "plugin_telemetry": self.plugin_telemetry,
        }


class PatternRegistry:
    """Registro abierto/cerrado de detectores tácticos y de patrones."""

    def __init__(self) -> None:
        self._plugins: dict[str, PatternPlugin] = {}

    def register(self, plugin: PatternPlugin) -> None:
        if plugin.plugin_id in self._plugins:
            raise EpistemicContractError(f"DUPLICATE_PLUGIN_ID: {plugin.plugin_id}")
        self._plugins[plugin.plugin_id] = plugin

    def list_plugins(self) -> tuple[str, ...]:
        return tuple(self._plugins.keys())

    def run_all(self, bundle: MatchObservationBundle) -> AnalyticsEngineResult:
        prematch: list[StatisticalPattern] = []
        internal: list[StatisticalPattern] = []
        descriptive: list[StatisticalPattern] = []
        hypotheses: list[TacticalHypothesis] = []
        skipped: dict[str, tuple[str, ...]] = {}
        telemetry: dict[str, dict[str, Any]] = {}

        for pid, plugin in self._plugins.items():
            out = plugin.evaluate(bundle)
            for pat in out.patterns:
                if pat.pattern_scope == PatternScope.PREMATCH_TRIGGER:
                    prematch.append(pat)
                elif pat.pattern_scope == PatternScope.INTRA_MATCH_MECHANISM:
                    internal.append(pat)
                else:
                    descriptive.append(pat)
            hypotheses.extend(out.hypotheses)
            if out.skipped_reasons:
                skipped[pid] = out.skipped_reasons
            telemetry[pid] = out.telemetry

        return AnalyticsEngineResult(
            event_id=bundle.event_id,
            fixture_id=bundle.fixture_id,
            cutoff_utc=bundle.cutoff_utc,
            prematch_patterns=tuple(prematch),
            internal_mechanisms=tuple(internal),
            descriptive_splits=tuple(descriptive),
            tactical_hypotheses=tuple(hypotheses),
            skipped_plugins=skipped,
            plugin_telemetry=telemetry,
        )
