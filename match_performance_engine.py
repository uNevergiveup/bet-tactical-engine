#!/usr/bin/env python3
"""PREMATCH V1.0.4, fase 1: evidencia de rendimiento por partido histórico.

Solo lectura de un match_engine_*.json V5.7-MICRO, sin API, sin cuotas y sin
incorporar el resultado del partido objetivo. No asigna causas como suerte,
problemas de vestuario o incompetencia defensiva desde correlaciones.

Uso:
 python match_performance_engine.py --input outputs/matches/7896590/inputs/match_engine_7896590.json \
   --output outputs/matches/7896590/analysis/rendimiento_historico_7896590.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = "prematch-match-performance-1.0.4"
# Margen conservador para evitar incluir un encuentro que aún pudiera estar
# disputándose cuando se tomó el corte histórico; no es una hora final probada.
COMPLETION_BUFFER = timedelta(hours=3)

METRIC_TYPES: dict[str, str] = {
    "expected_goals": "xg",
    "Total Shots": "shots",
    "Shots on Goal": "on_target",
    "Shots off Goal": "off_target",
    "Shots insidebox": "shots_inside_box",
    "Shots outsidebox": "shots_outside_box",
    "Blocked Shots": "blocked_shots",
    "Corner Kicks": "corners",
    "Ball Possession": "possession_pct",
    "Goalkeeper Saves": "saves",
    "Fouls": "fouls",
    "Yellow Cards": "yellow",
    "Red Cards": "red",
    "Offsides": "offsides",
    "Total passes": "passes",
    "Passes accurate": "passes_accurate",
}
METRICS = tuple(METRIC_TYPES.values())
ADVANTAGE_METRICS = ("shots", "on_target", "xg", "corners", "possession_pct")


def aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field}: fecha ausente o no textual")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field}: ISO 8601 inválido: {value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field}: requiere zona horaria explícita")
    return result.astimezone(timezone.utc)


def as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        raw = value.strip().removesuffix("%").strip()
        if not raw or raw.lower() in {"n/a", "na", "null", "none", "-"}:
            return None
        try:
            number = float(raw)
            return number if math.isfinite(number) else None
        except ValueError:
            return None
    return None


def one_team_statistics(row: dict[str, Any], team_id: int) -> tuple[dict[str, float | None], list[str]]:
    values: dict[str, float | None] = {metric: None for metric in METRICS}
    warnings: list[str] = []
    matches = [r for r in row.get("statistics", []) if isinstance(r, dict)
               and (r.get("team") or {}).get("id") == team_id]
    if len(matches) != 1:
        warnings.append(f"TEAM_STATISTICS_ROWS_{len(matches)}")
        return values, warnings
    for entry in matches[0].get("statistics", []):
        if not isinstance(entry, dict):
            continue
        metric = METRIC_TYPES.get(entry.get("type"))
        if metric is None:
            continue
        number = as_number(entry.get("value"))
        if values[metric] is not None and number is not None and values[metric] != number:
            warnings.append(f"DUPLICATE_CONFLICT_{metric}")
            continue
        if number is not None:
            values[metric] = number
    return values, warnings


def audit_historical_statistics(row: dict[str, Any], own: dict[str, Any],
                                conceded: dict[str, Any]) -> dict[str, Any]:
    """Contraste interno de coherencia, NO verificación con fuente independiente."""
    issues: list[dict[str, str]] = []

    def issue(level: str, code: str) -> None:
        issues.append({"severity": level, "code": code})

    for prefix, block in (("OWN", own), ("OPPONENT", conceded)):
        shots = block.get("shots")
        if shots is not None:
            for metric in ("on_target", "blocked_shots", "shots_inside_box", "shots_outside_box"):
                value = block.get(metric)
                if value is not None and value > shots:
                    issue("ERROR", f"{prefix}_{metric.upper()}_EXCEEDS_TOTAL_SHOTS")
        for name, value in block.items():
            if value is not None and value < 0:
                issue("ERROR", f"{prefix}_{name.upper()}_NEGATIVE")
        poss = block.get("possession_pct")
        if poss is not None and not (0 <= poss <= 100):
            issue("ERROR", f"{prefix}_POSSESSION_OUTSIDE_0_100")
    a, b = own.get("possession_pct"), conceded.get("possession_pct")
    if a is not None and b is not None and abs(a + b - 100) > 3:
        issue("REVIEW", "POSSESSION_SUM_NOT_100")

    score_ft = (row.get("score") or {}).get("fulltime") or {}
    other_goals = row.get("goals") or {}
    for side in ("home", "away"):
        x, y = score_ft.get(side), other_goals.get(side)
        if x is not None and y is not None and x != y:
            issue("ERROR", f"FULLTIME_GOALS_DISAGREE_{side.upper()}")
    return {"status": "ERROR" if any(x["severity"] == "ERROR" for x in issues) else
                       "REVISAR" if issues else "OK",
            "issues": issues,
            "metric_present_n": {"own": sum(v is not None for v in own.values()),
                                 "opponent": sum(v is not None for v in conceded.values())},
            "scope": "CONSISTENCIA_INTERNA_API_NO_CORROBORACION_EXTERNA"}


def lineup_info(row: dict[str, Any], team_id: int) -> dict[str, Any]:
    matches = [l for l in row.get("lineups", []) if isinstance(l, dict)
               and (l.get("team") or {}).get("id") == team_id]
    if len(matches) != 1:
        return {"coach_id": None, "coach_name": None, "formation": None,
                "starter_ids": [], "xi_complete": False, "lineup_records": len(matches)}
    lineup = matches[0]
    coach = lineup.get("coach") or {}
    ids = [(x.get("player") or {}).get("id") for x in lineup.get("startXI", []) if isinstance(x, dict)]
    ids = sorted({x for x in ids if isinstance(x, int) and x > 0})
    raw_id = coach.get("id")
    return {"coach_id": raw_id if isinstance(raw_id, int) and raw_id > 0 else None,
            "coach_name": coach.get("name") or None,
            "formation": lineup.get("formation") or None,
            "starter_ids": ids, "xi_complete": len(ids) == 11,
            "lineup_records": len(matches)}


def key_events(row: dict[str, Any], team_id: int) -> dict[str, Any]:
    reds = []
    goals = []
    for event in row.get("events", []):
        if not isinstance(event, dict):
            continue
        team = event.get("team") or {}
        elapsed = as_number((event.get("time") or {}).get("elapsed"))
        event_type = (event.get("type") or "").lower()
        detail = str(event.get("detail") or "")
        side = "own" if team.get("id") == team_id else "opponent"
        item = {"side": side, "minute": int(elapsed) if elapsed is not None else None,
                "detail": detail}
        if event_type == "card" and ("red" in detail.lower() or "second yellow" in detail.lower()):
            reds.append(item)
        if event_type == "goal":
            goals.append(item)
    return {"red_cards": reds, "goal_events": goals}


def advantage(own: dict[str, Any], other: dict[str, Any], result: str) -> dict[str, Any]:
    compared: dict[str, str] = {}
    for metric in ADVANTAGE_METRICS:
        a, b = own.get(metric), other.get(metric)
        if a is None or b is None:
            continue
        compared[metric] = "FAVORABLE" if a > b else "ADVERSE" if a < b else "TIED"
    good = sum(v == "FAVORABLE" for v in compared.values())
    bad = sum(v == "ADVERSE" for v in compared.values())
    # Regla explicable; un marcador de dominio NO es probabilidad, causalidad ni xG.
    attacking_support = compared.get("xg") == "FAVORABLE" or compared.get("on_target") == "FAVORABLE"
    if len(compared) < 3:
        label = "EVIDENCIA_INSUFICIENTE"
    elif good >= 3 and bad <= 1 and attacking_support:
        label = "SUPERIORIDAD_ESTADISTICA_MULTIDIMENSIONAL"
    elif bad >= 3 and good <= 1 and (
            compared.get("xg") == "ADVERSE" or compared.get("on_target") == "ADVERSE"):
        label = "INFERIORIDAD_ESTADISTICA_MULTIDIMENSIONAL"
    else:
        label = "MIXTO_NO_CONCLUYENTE"
    discrepancy = None
    if label == "SUPERIORIDAD_ESTADISTICA_MULTIDIMENSIONAL" and result in {"D", "L"}:
        discrepancy = "RESULTADO_INFERIOR_A_PRODUCCION_OBSERVADA"
    elif label == "INFERIORIDAD_ESTADISTICA_MULTIDIMENSIONAL" and result == "W":
        discrepancy = "VICTORIA_CON_PRODUCCION_INFERIOR"
    return {"label": label, "comparable_metrics": compared, "favorable": good,
            "adverse": bad, "equal": sum(v == "TIED" for v in compared.values()),
            "result_performance_gap": discrepancy,
            "caveat": "Indicador descriptivo; no mide posiciones de tiros, calidad de rival ni causalidad"}


def _goal_count(row: dict[str, Any], side: str) -> int | None:
    # El marcador fulltime es preferible cuando existe; nunca utilizar PEN/AET.
    values = (row.get("score") or {}).get("fulltime") or {}
    raw = values.get(side)
    if raw is None:
        raw = (row.get("goals") or {}).get(side)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def parse_historical_match(row: dict[str, Any], team_id: int) -> dict[str, Any]:
    fixture = row["fixture"]
    teams = row["teams"]
    actual_side = "home" if (teams.get("home") or {}).get("id") == team_id else "away"
    opponent_side = "away" if actual_side == "home" else "home"
    if (teams.get(actual_side) or {}).get("id") != team_id:
        raise ValueError("HISTORICAL_TEAM_ID_MISMATCH")
    rival_id = (teams.get(opponent_side) or {}).get("id")
    if not isinstance(rival_id, int):
        raise ValueError("HISTORICAL_OPPONENT_ID_MISSING")
    own_goals, conceded_goals = _goal_count(row, actual_side), _goal_count(row, opponent_side)
    if own_goals is None or conceded_goals is None:
        raise ValueError("HISTORICAL_GOALS_MISSING")
    own, warnings_a = one_team_statistics(row, team_id)
    conceded, warnings_b = one_team_statistics(row, rival_id)
    result = "W" if own_goals > conceded_goals else "L" if own_goals < conceded_goals else "D"
    own_xg = own.get("xg")
    opponent_xg = conceded.get("xg")
    delta = round(own_goals - own_xg, 3) if own_xg is not None else None
    ga_delta = round(conceded_goals - opponent_xg, 3) if opponent_xg is not None else None
    xg_advantage = (
        "SUPERIOR" if own_xg > opponent_xg else
        "INFERIOR" if own_xg < opponent_xg else "IGUAL"
    ) if own_xg is not None and opponent_xg is not None else "NO_DISPONIBLE"
    xg_scoreline_gap = (
        "NO_GANO_PESE_A_XG_SUPERIOR" if xg_advantage == "SUPERIOR" and result != "W" else
        "GANO_PESE_A_XG_INFERIOR" if xg_advantage == "INFERIOR" and result == "W" else None
    )
    return {
        "fixture_id": fixture["id"],
        "kickoff_utc": aware(fixture["date"], "historical.kickoff").isoformat(),
        "status": (fixture.get("status") or {}).get("short"),
        "league_id": (row.get("league") or {}).get("id"),
        "league_name": (row.get("league") or {}).get("name"),
        "season": (row.get("league") or {}).get("season"),
        "round": (row.get("league") or {}).get("round"),
        "venue": actual_side.upper(),
        "opponent": {"id": rival_id, "name": (teams.get(opponent_side) or {}).get("name")},
        "goals_for": own_goals,
        "goals_against": conceded_goals,
        "result": result,
        "own": own,
        "conceded": conceded,
        "lineup": lineup_info(row, team_id),
        "events": key_events(row, team_id),
        "statistical_advantage": advantage(own, conceded, result),
        "xg_vs_opponent": xg_advantage,
        "xg_scoreline_gap": xg_scoreline_gap,
        "goal_minus_xg": delta,
        "goals_conceded_minus_xga": ga_delta,
        "finishing_flag": ("GOLES_POR_DEBAJO_DE_XG" if delta is not None and delta <= -0.75
                           else "GOLES_POR_ENCIMA_DE_XG" if delta is not None and delta >= 0.75
                           else None),
        "data_quality": audit_historical_statistics(row, own, conceded),
        "warnings": warnings_a + warnings_b,
    }


def build(data: dict[str, Any], *, input_sha256: str, as_of: str | None = None) -> dict[str, Any]:
    fixture = data.get("fixture") or (data.get("context") or {}).get("fixture") or {}
    match = data.get("match") or {}
    context = data.get("context") or {}
    event_id = str(match.get("event_id") or "").strip()
    target_id = fixture.get("fixture_id")
    if not isinstance(target_id, int) or not event_id:
        raise ValueError("TARGET_IDENTITY_MISSING")
    kickoff = aware(fixture.get("date"), "target.kickoff")
    snapshot = aware(data.get("generated_at"), "snapshot.generated_at")
    if snapshot >= kickoff:
        raise ValueError("SNAPSHOT_AT_OR_AFTER_TARGET_KICKOFF: prohibido usar datos posteriores")
    cutoff = min(kickoff, snapshot)
    if as_of is not None:
        requested = aware(as_of, "as_of")
        if requested > snapshot:
            raise ValueError("AS_OF_AFTER_SNAPSHOT: no se pueden solicitar evidencias futuras")
        cutoff = min(cutoff, requested)

    sides: dict[str, Any] = {}
    for side in ("home", "away"):
        team_id = fixture.get(f"{side}_id")
        if not isinstance(team_id, int):
            raise ValueError(f"TARGET_{side.upper()}_ID_MISSING")
        deep = context.get(f"{side}_history_deep") or {}
        records: dict[int, dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []
        for row in deep.get("detailed_fixtures", []):
            if not isinstance(row, dict):
                skipped.append({"fixture_id": None, "reason": "INVALID_ROW"})
                continue
            item = row.get("fixture") or {}
            rid = item.get("id")
            if rid == target_id:
                skipped.append({"fixture_id": rid, "reason": "TARGET_FIXTURE_EXCLUDED"})
                continue
            if not isinstance(rid, int):
                skipped.append({"fixture_id": None, "reason": "INVALID_FIXTURE_ID"})
                continue
            try:
                hist_kickoff = aware(item.get("date"), "historical.kickoff")
            except ValueError:
                skipped.append({"fixture_id": rid, "reason": "INVALID_HISTORY_KICKOFF"})
                continue
            if hist_kickoff + COMPLETION_BUFFER >= cutoff:
                skipped.append({"fixture_id": rid, "reason": "HISTORY_NOT_SAFE_BEFORE_CUTOFF"})
                continue
            status = (item.get("status") or {}).get("short")
            if status != "FT":
                skipped.append({"fixture_id": rid, "reason": "NOT_REGULAR_TIME_FT"})
                continue
            try:
                parsed = parse_historical_match(row, team_id)
            except (KeyError, TypeError, ValueError) as exc:
                skipped.append({"fixture_id": rid, "reason": str(exc)})
                continue
            previous = records.get(rid)
            # Duplicados: conservar el de mayor cobertura, nunca sumar resultados.
            if previous is None or sum(v is not None for v in parsed["own"].values()) > sum(
                    v is not None for v in previous["own"].values()):
                records[rid] = parsed
        history = sorted(records.values(), key=lambda r: (r["kickoff_utc"], r["fixture_id"]), reverse=True)
        sides[side] = {
            "team_id": team_id,
            "team_name": fixture.get(f"{side}_name"),
            "historical_fixtures": history,
            "valid_count": len(history),
            "requested_history_limit": deep.get("requested_limit"),
            "skipped": skipped,
        }

    availability: dict[str, list[dict[str, Any]]] = {"home": [], "away": []}
    # La lesión no es prueba de la causa de un cambio de forma: se registra
    # solo como evidencia de disponibilidad en el snapshot del partido objetivo.
    raw_injuries = (context.get("injuries") or {}).get("response") or []
    for side in ("home", "away"):
        seen: set[tuple[Any, ...]] = set()
        tid = sides[side]["team_id"]
        for injury in raw_injuries:
            if not isinstance(injury, dict) or (injury.get("team") or {}).get("id") != tid:
                continue
            player = injury.get("player") or {}
            key = (player.get("id"), player.get("type"), player.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            availability[side].append({"player_id": player.get("id"), "name": player.get("name"),
                                       "type": player.get("type"), "reason": player.get("reason")})
        availability[side].sort(key=lambda i: (str(i["player_id"]), str(i["reason"])))

    return {
        "schema": SCHEMA,
        "source_sha256": input_sha256,
        "event_id_bwin": event_id,
        "target_fixture_id": target_id,
        "target_kickoff_utc": kickoff.isoformat(),
        "snapshot_generated_at_utc": snapshot.isoformat(),
        "analysis_cutoff_utc": cutoff.isoformat(),
        "method": "OFFLINE_PREMATCH_HISTORICAL_FACTS",
        "notes": [
            "La producción estadística es distinta del resultado; no supone causalidad ni suerte.",
            "Sólo se incluyen fixtures FT previos al cutoff con margen conservador de 3h.",
            "Un null de proveedor jamás se transforma en 0.",
            "Lesiones del fixture objetivo son instantáneas de disponibilidad, no causas históricas.",
        ],
        "teams": sides,
        "availability_at_snapshot": availability,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--as-of", help="ISO 8601 con offset, nunca posterior a la captura")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    output = build(data, input_sha256=hashlib.sha256(raw).hexdigest(), as_of=args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"OK {output['event_id_bwin']}: histórico local={output['teams']['home']['valid_count']} "
          f"visitante={output['teams']['away']['valid_count']} -> {args.output}")


if __name__ == "__main__":
    main()
