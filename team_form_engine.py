#!/usr/bin/env python3
"""PREMATCH V1.0.4 fase 1: forma, estabilidad y cambios de régimen.

Consume el JSON de match_performance_engine.py. Compara ventanas L5/L10/L20
con cobertura explícita; compara últimos 3 vs 3 previos si sólo hay 6
históricos, o últimos 5 vs 5 previos si hay >=10. Señales NO son p_model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import unicodedata
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

SCHEMA = "prematch-team-form-1.0.4"
WINDOWS = (5, 10, 20)
METRICS: dict[str, tuple[str, ...]] = {
    "goals_for": ("goals_for",),
    "goals_against": ("goals_against",),
    "shots_for": ("own", "shots"),
    "shots_against": ("conceded", "shots"),
    "on_target_for": ("own", "on_target"),
    "on_target_against": ("conceded", "on_target"),
    "xg_for": ("own", "xg"),
    "xg_against": ("conceded", "xg"),
    "corners_for": ("own", "corners"),
    "corners_against": ("conceded", "corners"),
    "possession_pct": ("own", "possession_pct"),
    "shots_inside_box_for": ("own", "shots_inside_box"),
    "shots_outside_box_for": ("own", "shots_outside_box"),
    "blocked_shots_for": ("own", "blocked_shots"),
    "saves": ("own", "saves"),
}
# Cambio absoluto mínimo + variación relativa indicativa para no llamar
# 'caída' a una variación minúscula. Umbrales heurísticos, NO calibrados.
MIN_ABSOLUTE_DELTA = {
    "goals_for": 0.5, "goals_against": 0.5,
    "shots_for": 2.0, "shots_against": 2.0,
    "on_target_for": 1.0, "on_target_against": 1.0,
    "xg_for": 0.3, "xg_against": 0.3,
    "corners_for": 1.5, "corners_against": 1.5,
    "possession_pct": 5.0, "shots_inside_box_for": 1.5,
    "shots_outside_box_for": 1.5, "blocked_shots_for": 1.0,
    "saves": 1.0,
}


def metric_value(row: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = row
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def metric_stats(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [metric_value(row, METRICS[key]) for row in rows]
    values = [v for v in values if v is not None]
    avg = mean(values) if values else None
    sd = pstdev(values) if values else None
    return {
        "n": len(values), "missing_n": len(rows) - len(values),
        "mean": round(avg, 3) if avg is not None else None,
        "stdev": round(sd, 3) if sd is not None else None,
        "cv": round(sd / abs(avg), 3) if avg is not None and abs(avg) > 1e-9 else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def describe_window(rows: list[dict[str, Any]], window: int) -> dict[str, Any]:
    selected = rows[:window]
    successes = sum(r.get("result") == "W" for r in selected)
    draws = sum(r.get("result") == "D" for r in selected)
    losses = sum(r.get("result") == "L" for r in selected)
    favored = sum((r.get("statistical_advantage") or {}).get("label") ==
                  "SUPERIORIDAD_ESTADISTICA_MULTIDIMENSIONAL" for r in selected)
    mismatches = sum((r.get("statistical_advantage") or {}).get("result_performance_gap") ==
                     "RESULTADO_INFERIOR_A_PRODUCCION_OBSERVADA" for r in selected)
    return {
        "requested_n": window,
        "observed_n": len(selected),
        "complete_window": len(selected) >= window,
        "W_D_L": {"W": successes, "D": draws, "L": losses},
        "statistical_superiority_n": favored,
        "underperformed_scoreline_vs_production_n": mismatches,
        "metrics": {metric: metric_stats(selected, metric) for metric in METRICS},
    }


def compare_periods(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n >= 10:
        size = 5
    elif n >= 6:
        size = 3
    else:
        return {"status": "HISTORIAL_INSUFICIENTE_PARA_COMPARAR", "n": n,
                "required_at_least": 6, "comparisons": {}}
    recent, baseline = rows[:size], rows[size:2 * size]
    comparisons: dict[str, Any] = {}
    for metric in METRICS:
        current, previous = metric_stats(recent, metric), metric_stats(baseline, metric)
        newmean, oldmean = current["mean"], previous["mean"]
        if current["n"] < size or previous["n"] < size or newmean is None or oldmean is None:
            comparisons[metric] = {
                "status": "DATOS_INCOMPLETOS", "recent": current, "previous": previous,
                "missing_for_complete_comparison": True,
            }
            continue
        delta = round(newmean - oldmean, 3)
        abs_min = MIN_ABSOLUTE_DELTA[metric]
        threshold = max(abs_min, 0.15 * abs(oldmean))
        status = ("DESCENSO_DESCRIPTIVO" if delta < -threshold
                  else "AUMENTO_DESCRIPTIVO" if delta > threshold
                  else "SIN_CAMBIO_MATERIAL_DESCRIPTIVO")
        comparisons[metric] = {
            "status": status, "recent": current, "previous": previous,
            "difference": delta, "threshold_heuristic": round(threshold, 3),
            "relative_change_pct": round(100 * delta / oldmean, 2) if abs(oldmean) > 1e-9 else None,
            "missing_for_complete_comparison": False,
        }

    declines = [name for name in ("shots_for", "on_target_for", "xg_for")
                if comparisons[name]["status"] == "DESCENSO_DESCRIPTIVO"]
    rises = [name for name in ("shots_for", "on_target_for", "xg_for")
             if comparisons[name]["status"] == "AUMENTO_DESCRIPTIVO"]
    if len(declines) >= 2:
        direction = "POSIBLE_DESACELERACION_OFENSIVA"
    elif len(rises) >= 2:
        direction = "POSIBLE_ACELERACION_OFENSIVA"
    else:
        direction = "SIN_SENAL_CONVERGENTE_DE_CAMBIO_OFENSIVO"
    return {
        "status": "EXPLORATORIO_N3_VS_N3" if size == 3 else "DESCRIPTIVO_N5_VS_N5",
        "recent_n": size, "baseline_n": size,
        "direction": direction,
        "signals_down": declines, "signals_up": rises,
        "comparisons": comparisons,
        "warning": "El cambio descriptivo no prueba tendencia persistente, causalidad ni probabilidad futura",
    }


def lineup_and_coach_regimes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = list(reversed(rows))
    coach_changes: list[dict[str, Any]] = []
    formation_changes: list[dict[str, Any]] = []
    season_changes: list[dict[str, Any]] = []
    starters_overlap: list[dict[str, Any]] = []

    def coach_key(lineup: dict[str, Any]) -> tuple[Any, ...] | None:
        # Priorizar el nombre cuando hay registros con coach_id=0 en un partido
        # y el mismo nombre con ID positivo en otro; evitar falso relevo.
        if lineup.get("coach_name"):
            name = " ".join(str(lineup["coach_name"]).split()).casefold()
            name = "".join(c for c in unicodedata.normalize("NFKD", name)
                           if not unicodedata.combining(c))
            return ("name", name)
        if lineup.get("coach_id"):
            return ("id", lineup["coach_id"])
        return None

    for old, new in zip(ordered, ordered[1:]):
        prev, current = old.get("lineup") or {}, new.get("lineup") or {}
        a, b = coach_key(prev), coach_key(current)
        # Si cambia entre nombre e ID sin saber si son el mismo, no proclamar relevo.
        if (a and b and a[0] == b[0] and a != b and
                not (prev.get("coach_id") and prev.get("coach_id") == current.get("coach_id"))):
            coach_changes.append({"before_fixture": old["fixture_id"],
                                  "after_fixture": new["fixture_id"],
                                  "before": {"id": prev.get("coach_id"), "name": prev.get("coach_name")},
                                  "after": {"id": current.get("coach_id"), "name": current.get("coach_name")}})
        fa, fb = prev.get("formation"), current.get("formation")
        if fa and fb and fa != fb:
            formation_changes.append({"before_fixture": old["fixture_id"],
                                      "after_fixture": new["fixture_id"],
                                      "before": fa, "after": fb})
        if (old.get("league_id"), old.get("season")) != (new.get("league_id"), new.get("season")):
            # Un cambio de liga NO prueba un cambio de temporada o cuerpo técnico.
            if old.get("season") != new.get("season"):
                season_changes.append({"from": old.get("season"), "to": new.get("season"),
                                       "between": [old["fixture_id"], new["fixture_id"]]})
        if prev.get("xi_complete") and current.get("xi_complete"):
            a_set = set(prev.get("starter_ids", []))
            b_set = set(current.get("starter_ids", []))
            starters_overlap.append({"from_fixture": old["fixture_id"],
                                     "to_fixture": new["fixture_id"],
                                     "common_starters": len(a_set & b_set),
                                     "changed_starters": 11 - len(a_set & b_set)})

    return {
        "confirmed_coach_changes_in_observed_history": coach_changes,
        "formation_changes_in_observed_history": formation_changes,
        "season_boundaries_in_observed_history": season_changes,
        "adjacent_lineup_continuity": starters_overlap,
        "average_shared_starters": (round(mean(x["common_starters"] for x in starters_overlap), 2)
                                    if starters_overlap else None),
        "limits": ["El endpoint de entrenadores puede contener trayectorias antiguas y no determina el titular por orden.",
                   "El XI no prueba posiciones durante todo el partido, conflictos internos ni intensidad de juego.",
                   "La ausencia de cambios visibles no prueba estabilidad del vestuario o del cuerpo técnico."],
    }


def competition_splits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("league_id")), str(row.get("season")))
        by_key.setdefault(key, []).append(row)
    return [{"league_id": group[0].get("league_id"), "league_name": group[0].get("league_name"),
             "season": group[0].get("season"), "observed_n": len(group),
             "metrics": {k: metric_stats(group, k) for k in METRICS},
             "W_D_L": {"W": sum(r["result"] == "W" for r in group),
                       "D": sum(r["result"] == "D" for r in group),
                       "L": sum(r["result"] == "L" for r in group)}}
            for _, group in sorted(by_key.items())]


def build(data: dict[str, Any], *, source_sha256: str) -> dict[str, Any]:
    if data.get("schema") != "prematch-match-performance-1.0.4":
        raise ValueError("UNSUPPORTED_MATCH_PERFORMANCE_SCHEMA")
    teams: dict[str, Any] = {}
    for side in ("home", "away"):
        source = (data.get("teams") or {}).get(side) or {}
        rows = source.get("historical_fixtures") or []
        # No aceptar orden externo no determinista.
        rows = sorted(rows, key=lambda r: (r["kickoff_utc"], r["fixture_id"]), reverse=True)
        windows = {f"L{w}": describe_window(rows, w) for w in WINDOWS}
        comparable = compare_periods(rows)
        regimes = lineup_and_coach_regimes(rows)
        teams[side] = {
            "team_id": source.get("team_id"), "team_name": source.get("team_name"),
            "observed_n": len(rows),
            "window_summaries": windows,
            "trend_comparison": comparable,
            "regime_evidence": regimes,
            "competition_season_splits": competition_splits(rows),
            "coverage_warning": ("L10/L20 INCOMPLETAS: se necesitan históricos adicionales para contrastar "
                                 "regímenes y cambios sostenidos" if len(rows) < 20 else None),
            "contextual_flags": {
                "lineups_with_11_ids": sum(bool((r.get("lineup") or {}).get("xi_complete")) for r in rows),
                "games_with_red_card_events": sum(bool((r.get("events") or {}).get("red_cards")) for r in rows),
                "observed_coach_change": bool(regimes["confirmed_coach_changes_in_observed_history"]),
                "availability_in_target_snapshot": (data.get("availability_at_snapshot") or {}).get(side, []),
            },
        }
    return {
        "schema": SCHEMA,
        "source_performance_sha256": source_sha256,
        "event_id_bwin": data["event_id_bwin"],
        "target_fixture_id": data["target_fixture_id"],
        "target_kickoff_utc": data["target_kickoff_utc"],
        "analysis_cutoff_utc": data["analysis_cutoff_utc"],
        "scope": "TENDENCIAS_HISTORICAS_DESCRIPTIVAS_PREMATCH",
        "teams": teams,
        "global_warnings": [
            "L5/L10/L20 incluyen distintas competiciones; usar desgloses antes de transferir tendencias.",
            "Con sólo seis históricos las señales N3 vs N3 son exploratorias.",
            "Lesiones actuales no prueban causas de derrotas antiguas; rumores de vestuario no se infieren.",
            "No calcula probabilidades, EV, cuotas justas ni recomendaciones financieras.",
            "Los umbrales de cambios son heurísticos, no calibrados con backtesting.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    result = build(data, source_sha256=hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"OK {result['event_id_bwin']}: "
          f"local={result['teams']['home']['trend_comparison']['direction'] if 'direction' in result['teams']['home']['trend_comparison'] else 'SIN_DATOS'} "
          f"visitante={result['teams']['away']['trend_comparison']['direction'] if 'direction' in result['teams']['away']['trend_comparison'] else 'SIN_DATOS'} "
          f"-> {args.output}")


if __name__ == "__main__":
    main()
