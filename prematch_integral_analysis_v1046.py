#!/usr/bin/env python3
"""PREMATCH Fase 2.4: inteligencia descriptiva de rendimiento y cruce táctico.

Este módulo es deliberadamente independiente de scraper, cuotas, SOS y validadores.
Consume artefactos ya generados y no produce picks, probabilidades ni stakes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

METRICS = {
    "goals_for": ("goals_for",), "goals_against": ("goals_against",),
    "xg_for": ("own", "xg"), "xg_against": ("conceded", "xg"),
    "shots_for": ("own", "shots"), "shots_against": ("conceded", "shots"),
    "on_target_for": ("own", "on_target"), "on_target_against": ("conceded", "on_target"),
    "corners_for": ("own", "corners"), "corners_against": ("conceded", "corners"),
    "fouls_for": ("own", "fouls"), "fouls_against": ("conceded", "fouls"),
    "yellow_for": ("own", "yellow"), "yellow_against": ("conceded", "yellow"),
    "red_for": ("own", "red"), "red_against": ("conceded", "red"),
    "saves_for": ("own", "saves"), "saves_against": ("conceded", "saves"),
}


def utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value(row: dict[str, Any], metric: str) -> float | None:
    current: Any = row
    for key in METRICS[metric]:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    return float(current) if math.isfinite(float(current)) else None


def metric_stats(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    vals = [value(r, metric) for r in rows]
    observed = [x for x in vals if x is not None]
    return {
        "requested_n": len(rows), "observed_n": len(observed),
        "missing_n": len(rows) - len(observed),
        "mean": round(mean(observed), 3) if observed else None,
        "min": min(observed) if observed else None,
        "max": max(observed) if observed else None,
    }


def windows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n in (5, 10, 20):
        selected = rows[:n]
        out[f"L{n}"] = {
            "requested_n": n, "observed_n": len(selected),
            "complete": len(selected) >= n,
            "metrics": {m: metric_stats(selected, m) for m in METRICS},
            "finishing_efficacy": finishing(selected),
        }
    if len(rows) >= 10:
        recent, previous = rows[:5], rows[5:10]
        out["period_comparison"] = {
            "status": "DESCRIPTIVO_N5_VS_N5",
            "recent_n": len(recent), "previous_n": len(previous),
            "metrics": {m: period_metric(recent, previous, m) for m in METRICS},
            "warning": "Diferencia descriptiva; no prueba persistencia, causalidad ni predicción.",
        }
    else:
        out["period_comparison"] = {
            "status": "MUESTRA_INSUFICIENTE", "recent_n": min(5, len(rows)),
            "previous_n": max(0, len(rows) - 5), "metrics": {},
        }
    return out


def period_metric(recent: list[dict[str, Any]], previous: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    a, b = metric_stats(recent, metric), metric_stats(previous, metric)
    return {"recent": a, "previous": b,
            "difference": round(a["mean"] - b["mean"], 3)
            if a["mean"] is not None and b["mean"] is not None else None,
            "status": "COMPARABLE" if a["mean"] is not None and b["mean"] is not None else "DATOS_INCOMPLETOS"}


def finishing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [(value(r, "goals_for"), value(r, "xg_for")) for r in rows]
    pairs = [(g, x) for g, x in pairs if g is not None and x is not None and x > 0]
    total_g = sum(g for g, _ in pairs) if pairs else None
    total_x = sum(x for _, x in pairs) if pairs else None
    return {"observed_n": len(pairs), "goals_numerator": total_g,
            "xg_denominator": round(total_x, 3) if total_x is not None else None,
            "goals_per_xg": round(total_g / total_x, 3) if total_x else None,
            "note": "Sólo partidos con goles y xG observados; null no se convierte en cero."}


def venue_splits(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {venue: {"observed_n": len(group), "metrics": {m: metric_stats(group, m) for m in METRICS}}
            for venue in ("HOME", "AWAY")
            for group in ([r for r in rows if r.get("venue") == venue],)}


def competition_key(row: dict[str, Any]) -> str:
    return f"{row.get('league_id')}:{row.get('season')}"


def is_official(row: dict[str, Any]) -> bool:
    # API-Football identifica Friendlies Clubs con liga 667 en estos snapshots.
    return row.get("league_id") != 667 and "friendly" not in str(row.get("league_name", "")).casefold()


def scope_rows(rows: list[dict[str, Any]], team_side: str) -> dict[str, list[dict[str, Any]]]:
    """Separate samples without discarding any historical fixture."""
    current_laliga = [r for r in rows if r.get("league_id") == 140 and r.get("season") == 2026]
    scopes = {
        "global": rows,
        "official": [r for r in rows if is_official(r)],
        "laliga_current": current_laliga,
        "friendlies": [r for r in rows if r.get("league_id") == 667 or "friendly" in str(r.get("league_name", "")).casefold()],
        "copa": [r for r in rows if r.get("league_id") == 143],
        "home_only": [r for r in rows if r.get("venue") == "HOME"],
        "away_only": [r for r in rows if r.get("venue") == "AWAY"],
    }
    return scopes


def change_classification(recent: dict[str, Any], previous: dict[str, Any], *, red_affected: bool = False) -> dict[str, Any]:
    """Conservative interpretation preserving incomplete 4/5 signals."""
    a, b = recent.get("mean"), previous.get("mean")
    n = min(recent.get("observed_n", recent.get("n", 0)), previous.get("observed_n", previous.get("n", 0)))
    if a is None or b is None or n < 3:
        return {"status": "SIN_EVIDENCIA_SUFICIENTE", "confidence": "BAJA", "difference": None, "reason": "media o denominador insuficiente"}
    diff = round(a - b, 3)
    if diff == 0:
        status = "ESTABLE_DESCRIPTIVO"
    elif n < 5:
        status = "SENAL_PRELIMINAR_DATOS_INCOMPLETOS"
    elif red_affected:
        status = "CAMBIO_OBSERVADO_CON_POSIBLE_DISTORSION"
    elif abs(diff) < max(0.15 * abs(b), 0.1):
        status = "ESTABLE_DESCRIPTIVO"
    else:
        status = "CAMBIO_ROBUSTO_DESCRIPTIVO" if n >= 5 else "CAMBIO_OBSERVADO"
    return {"status": status, "confidence": "LIMITADA" if n < 5 else "MODERADA",
            "difference": diff, "recent_n": recent.get("observed_n", recent.get("n", 0)),
            "previous_n": previous.get("observed_n", previous.get("n", 0)),
            "red_card_distortion": red_affected,
            "reason": "comparación no solapada L5 vs L5; no implica causalidad"}


def scope_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = windows(rows)
    period = base.get("period_comparison", {})
    red_ids = {r.get("fixture_id") for r in rows if (r.get("events") or {}).get("red_cards")}
    changes = {}
    for metric, pair in (period.get("metrics") or {}).items():
        changes[metric] = change_classification(pair.get("recent", {}), pair.get("previous", {}),
                                                red_affected=bool(red_ids))
    base["change_interpretation"] = changes
    base["red_card_fixture_ids"] = sorted(x for x in red_ids if x is not None)
    base["sample_note"] = "Muestra separada por competición/localía; no se mezclan para afirmar mejoras competitivas."
    return base


def red_card_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    own, opponent = [], []
    for row in rows:
        for event in (row.get("events") or {}).get("red_cards", []) or []:
            item = {"fixture_id": row.get("fixture_id"), "kickoff_utc": row.get("kickoff_utc"),
                    "minute": event.get("minute"), "detail": event.get("detail"), "side": event.get("side")}
            (own if event.get("side") == "own" else opponent).append(item)
    return {"own": own, "opponent": opponent, "own_n": len(own), "opponent_n": len(opponent)}


def verified_coach_assessment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    observed = []
    for row in rows:
        lineup = row.get("lineup") or {}
        if lineup.get("coach_id") or lineup.get("coach_name"):
            observed.append({"fixture_id": row.get("fixture_id"), "kickoff_utc": row.get("kickoff_utc"),
                             "coach_id": lineup.get("coach_id"), "coach_name": lineup.get("coach_name")})
    transitions = []
    for old, new in zip(observed, observed[1:]):
        if (old.get("coach_id"), old.get("coach_name")) == (new.get("coach_id"), new.get("coach_name")):
            continue
        # Name-only variations or shared surname are not enough to confirm a change.
        old_tokens = set(str(old.get("coach_name") or "").casefold().replace("á", "a").split())
        new_tokens = set(str(new.get("coach_name") or "").casefold().replace("á", "a").split())
        alias_overlap = bool(old_tokens & new_tokens)
        transitions.append({"before": old, "after": new,
                            "status": "NO_VERIFICADO_ALIAS_O_PROVEEDOR" if alias_overlap else "REQUIERE_VERIFICACION_EXTERNA"})
    return {"observations": observed, "transitions": transitions,
            "confirmed_changes": [],
            "warning": "IDs/nombres discrepantes no se declaran sustitución confirmada sin fuente independiente."}


def _half_metric(stats: list[dict[str, Any]], wanted: str) -> float | None:
    aliases = {"xg": {"expected_goals"}, "shots": {"Total Shots"},
               "on_target": {"Shots on Goal"}, "corners": {"Corner Kicks"},
               "fouls": {"Fouls"}, "yellow": {"Yellow Cards"}, "red": {"Red Cards"}}
    for item in stats or []:
        if item.get("type") in aliases.get(wanted, {wanted}):
            value = item.get("value")
            if isinstance(value, str):
                value = value.rstrip("%").strip()
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def halves_summary(context: dict[str, Any] | None, side: str, team_id: int,
                   cutoff: datetime, target_fixture_id: int) -> dict[str, Any]:
    detailed = ((context or {}).get("context") or {}).get(f"{side}_history_deep", {}).get("detailed_fixtures", [])
    rows: dict[str, list[dict[str, Any]]] = {"first_half": [], "second_half": []}
    excluded = []
    for fixture in detailed or []:
        fid = (fixture.get("fixture") or {}).get("id")
        dt = utc((fixture.get("fixture") or {}).get("date"))
        if fid == target_fixture_id or dt is None or dt >= cutoff:
            excluded.append(fid)
            continue
        record = next((x for x in fixture.get("statistics_halves", []) if (x.get("team") or {}).get("id") == team_id), None)
        if not record:
            continue
        for key, source_key in (("first_half", "statistics_1h"), ("second_half", "statistics_2h")):
            stats = record.get(source_key) or []
            if not stats:
                continue
            rows[key].append({"fixture_id": fid, "kickoff_utc": (fixture.get("fixture") or {}).get("date"),
                              "xg": _half_metric(stats, "xg"), "shots": _half_metric(stats, "shots"),
                              "on_target": _half_metric(stats, "on_target"), "corners": _half_metric(stats, "corners"),
                              "fouls": _half_metric(stats, "fouls"), "yellow": _half_metric(stats, "yellow"),
                              "red": _half_metric(stats, "red")})
    output: dict[str, Any] = {"source": "contexto_ampliado.statistics_halves", "excluded_n": len(excluded), "periods": {}}
    for key, period_rows in rows.items():
        output["periods"][key] = {"fixtures_n": len(period_rows), "metrics": {
            metric: {"observed_n": sum(r.get(metric) is not None for r in period_rows),
                     "requested_n": len(period_rows),
                     "mean": round(mean([r[metric] for r in period_rows if r.get(metric) is not None]), 3)
                     if any(r.get(metric) is not None for r in period_rows) else None}
            for metric in ("xg", "shots", "on_target", "corners", "fouls", "yellow", "red")},
            "fixture_ids": [r["fixture_id"] for r in period_rows]}
    output["status"] = "UTILIZABLE_PARTIAL" if any(v["fixtures_n"] for v in output["periods"].values()) else "NO_UTILIZABLE_DATA"
    output["note"] = "Las métricas H1/H2 provienen de statistics_halves; no se imputan ausencias."
    return output


def target_availability(context: dict[str, Any] | None, team_ids: dict[str, int]) -> dict[str, Any]:
    raw = ((context or {}).get("context") or {}).get("injuries", {}).get("response") or []
    seen = set()
    out = {"home": [], "away": []}
    by_id = {v: k for k, v in team_ids.items()}
    for item in raw:
        team = item.get("team") or {}; player = item.get("player") or {}
        side = by_id.get(team.get("id"))
        key = (team.get("id"), player.get("id"), player.get("reason"), player.get("type"))
        if not side or key in seen:
            continue
        seen.add(key)
        out[side].append({"team_id": team.get("id"), "team_name": team.get("name"),
                          "player_id": player.get("id"), "player_name": player.get("name"),
                          "reason": player.get("reason"), "status": player.get("type"),
                          "fixture_id": (item.get("fixture") or {}).get("id"),
                          "source": "contexto_ampliado.injuries", "verification": "API_SNAPSHOT"})
    return out


def temporal_filter(rows: list[dict[str, Any]], cutoff: datetime, target_fixture_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kept, excluded = [], []
    for row in rows:
        dt = utc(row.get("kickoff_utc"))
        if row.get("fixture_id") == target_fixture_id:
            excluded.append({"fixture_id": row.get("fixture_id"), "reason": "TARGET_FIXTURE"})
        elif dt is None:
            excluded.append({"fixture_id": row.get("fixture_id"), "reason": "KICKOFF_UNPARSEABLE"})
        elif dt >= cutoff:
            excluded.append({"fixture_id": row.get("fixture_id"), "kickoff_utc": row.get("kickoff_utc"), "reason": "POST_CUTOFF"})
        else:
            kept.append(row)
    kept.sort(key=lambda r: (utc(r.get("kickoff_utc")) or datetime.min.replace(tzinfo=timezone.utc), r.get("fixture_id", 0)), reverse=True)
    return kept, {"excluded_n": len(excluded), "excluded": excluded}


def player_summary(players: dict[str, Any] | None) -> dict[str, Any]:
    players = players or {}
    return {
        "historical_fixture_count": players.get("historical_fixture_count", 0),
        "players_with_minutes": players.get("players_with_minutes", 0),
        "players": [{"player_id": p.get("player_id"), "name": p.get("name"),
                      "played_n": p.get("played_n"), "minutes_observed": p.get("minutes_observed"),
                      "positions_observed": p.get("positions_observed", []), "metrics": p.get("metrics", {})}
                     for p in (players.get("players") or [])],
        "limitations": ["Las estadísticas agregadas no demuestran roles tácticos, motivos de cambios ni causalidad."],
    }


def hypothesis(statement: str, evidence_for: list[Any], evidence_against: list[Any], missing: list[str], label: str = "INFERIDO") -> dict[str, Any]:
    return {"statement": statement, "label": label, "evidence_for": evidence_for,
            "evidence_against": evidence_against, "missing": missing}


def readable_evidence(stats: dict[str, Any], label: str) -> str:
    return f"{label}: media={stats.get('mean')}, observados={stats.get('observed_n', stats.get('n', 0))}/{stats.get('requested_n', '?')}"


def build(performance: dict[str, Any], *, context: dict[str, Any] | None = None,
          trends: dict[str, Any] | None = None, depth: dict[str, Any] | None = None,
          players: dict[str, Any] | None = None, sos: dict[str, Any] | None = None,
          source_files: dict[str, str] | None = None,
          qualitative_inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cutoff = utc(performance.get("analysis_cutoff_utc"))
    target = performance.get("target_fixture_id")
    if cutoff is None or not isinstance(target, int):
        raise ValueError("TEMPORAL_METADATA_REQUIRED")
    result: dict[str, Any] = {
        "schema": "prematch-integral-analysis-v1046.1", "event_id_bwin": performance.get("event_id_bwin"),
        "target_fixture_id": target, "target_kickoff_utc": performance.get("target_kickoff_utc"),
        "analysis_cutoff_utc": cutoff.isoformat(), "scope": "DESCRIPTIVE_PREMATCH_NO_PICKS",
        "source_files": source_files or {}, "temporal_guard": {"cutoff_utc": cutoff.isoformat(), "teams": {}},
        "data_limitations": ["API-Football no garantiza xG, alineaciones, eventos ni estadísticas para todos los partidos.",
                             "Una muestra L5/L10/L20 es descriptiva y no prueba causalidad ni persistencia.",
                             "No se infieren sistemas, roles, ausencias o motivos de sustitución desde agregados."],
        "teams": {}, "matchup": {"hypotheses": [], "scenarios": []},
        "qualitative_inputs": [], "warnings": [], "odds_enabled": False,
    }
    for item in qualitative_inputs or []:
        if not isinstance(item, dict):
            continue
        label = item.get("label", "NO_VERIFICADO")
        result["qualitative_inputs"].append({"source": item.get("source"), "label": label if label in {"CONFIRMADO", "PROBABLE", "INFERIDO", "NO_VERIFICADO"} else "NO_VERIFICADO", "text": item.get("text"), "evidence": item.get("evidence", [])})
    if context:
        result["context_inventory"] = {
            "fixture": context.get("fixture"),
            "available_sections": sorted((context.get("context") or {}).keys()),
            "historical_detail_counts": {
                side: len(((context.get("context") or {}).get(f"{side}_history_deep") or {}).get("detailed_fixtures") or [])
                for side in ("home", "away")
            },
            "api_limitations": "Las secciones ausentes o vacías se conservan como no observadas; no se imputan ceros.",
        }
    for side in ("home", "away"):
        src = (performance.get("teams") or {}).get(side) or {}
        rows, guard = temporal_filter(src.get("historical_fixtures") or [], cutoff, target)
        result["temporal_guard"]["teams"][side] = guard
        # Las ventanas y comparaciones de forma ya fueron calculadas por
        # team_form_engine/prematch_pattern_discovery. Se reutilizan como
        # fuente canónica; el cálculo local sólo actúa como fallback offline.
        source_windows = ((trends or {}).get("teams", {}).get(side) or {}).get("window_summaries")
        window_data = copy.deepcopy(source_windows) if isinstance(source_windows, dict) else windows(rows)
        for n in (5, 10, 20):
            key = f"L{n}"
            if key not in window_data:
                window_data[key] = windows(rows)[key]
            window_data[key].setdefault("complete", window_data[key].get("complete_window", window_data[key].get("observed_n", 0) >= n))
            for stat in (window_data[key].get("metrics") or {}).values():
                if isinstance(stat, dict):
                    stat.setdefault("observed_n", stat.get("n", 0))
                    stat.setdefault("requested_n", stat.get("observed_n", 0) + stat.get("missing_n", 0))
            window_data[key]["finishing_efficacy"] = finishing(rows[:n])
        source_comparison = ((trends or {}).get("teams", {}).get(side) or {}).get("trend_comparison")
        if isinstance(source_comparison, dict):
            window_data["period_comparison"] = copy.deepcopy(source_comparison)
        else:
            window_data["period_comparison"] = windows(rows)["period_comparison"]
        scopes = {name: scope_analysis(group) for name, group in scope_rows(rows, side).items()}
        team_ids = {s: ((performance.get("teams") or {}).get(s) or {}).get("team_id") for s in ("home", "away")}
        team = {"team_id": src.get("team_id"), "name": src.get("team_name"),
                "observed_n": len(rows), "coverage": {f"L{n}": len(rows) >= n for n in (5, 10, 20)},
                "windows": window_data, "venue_splits": venue_splits(rows),
                "competition_scopes": scopes,
                "defensive_and_goalkeeper": {f"L{n}": {m: window_data[f"L{n}"]["metrics"].get(m, metric_stats(rows[:n], m)) for m in ("goals_against", "xg_against", "shots_against", "on_target_against", "saves_for")} for n in (5, 10, 20)},
                "period_analysis": halves_summary(context, side, src.get("team_id"), cutoff, target),
                "historical_rows": [{"fixture_id": r.get("fixture_id"), "kickoff_utc": r.get("kickoff_utc"),
                                     "league_id": r.get("league_id"), "season": r.get("season"),
                                     "venue": r.get("venue"), "opponent": r.get("opponent")} for r in rows],
                "trend_source": (trends or {}).get("teams", {}).get(side),
                "depth_source": (depth or {}).get("teams", {}).get(side),
                "players": player_summary(((players or {}).get("teams") or {}).get(side)),
                "coach_evidence": verified_coach_assessment(rows),
                "historical_coach_source": ((trends or {}).get("teams", {}).get(side) or {}).get("regime_evidence", {}),
                "red_card_breakdown": red_card_breakdown(rows),
                "distortions": [{"fixture_id": r.get("fixture_id"), "red_cards": (r.get("events") or {}).get("red_cards", [])}
                                for r in rows if (r.get("events") or {}).get("red_cards")],
                "review_required": len(rows) < 20 or any((window_data[f"L{n}"]["metrics"].get("xg_for") or {}).get("missing_n", 0) for n in (5, 10, 20)),
        }
        result["teams"][side] = team
    ids = {side: result["teams"][side]["team_id"] for side in ("home", "away")}
    result["target_availability"] = target_availability(context, ids)
    home, away = result["teams"]["home"], result["teams"]["away"]
    dimensions = []
    for side, offense, defense in (("home", home, away), ("away", away, home)):
        venue_key = "home_only" if side == "home" else "away_only"
        for metric in ("goals_for", "xg_for", "shots_for", "on_target_for", "corners_for"):
            conceded_metric = metric.replace("_for", "_against")
            own = offense["windows"]["L5"]["metrics"][metric]
            opp = defense["windows"]["L5"]["metrics"][conceded_metric]
            venue_stats = offense["competition_scopes"][venue_key]["L5"]["metrics"][metric]
            missing = []
            if own.get("observed_n", own.get("n", 0)) < own.get("requested_n", 5):
                missing.append(f"{metric} del equipo ({own.get('observed_n', own.get('n', 0))}/{own.get('requested_n', 5)})")
            if opp.get("observed_n", opp.get("n", 0)) < opp.get("requested_n", 5):
                missing.append(f"{conceded_metric} rival ({opp.get('observed_n', opp.get('n', 0))}/{opp.get('requested_n', 5)})")
            sos_window = (((sos or {}).get("teams", {}).get(side) or {}).get("windows", {}).get(metric, {}).get("L5")
                          if isinstance((sos or {}).get("teams", {}).get(side), dict) else None)
            evidence_for = [readable_evidence(own, f"Producción {offense['name']}"),
                            readable_evidence(opp, f"Concesión {defense['name']} (contexto, no evidencia contraria)")]
            evidence_against = []
            prior = offense["competition_scopes"]["global"].get("period_comparison", {}).get("metrics", {}).get(metric, {})
            if prior.get("difference") is not None and prior.get("difference") < 0:
                evidence_against.append(f"la producción reciente de {offense['name']} es inferior a su L5 anterior ({prior['difference']})")
            if not evidence_against:
                evidence_against.append("no hay evidencia contraria directa en esta muestra; el registro defensivo del rival se mantiene como contexto neutral")
            dimensions.append({"team": side, "team_name": offense["name"], "dimension": metric,
                               "global_L5": {"production": own, "opponent_concession": opp},
                               "venue_L5": venue_stats, "sos_L5": sos_window,
                               "evidence": {"favorable": evidence_for, "contrary": evidence_against,
                                            "neutral": [f"localía analizada como {venue_key}"], "missing": missing},
                               "confidence": "LIMITADA" if missing else "MODERADA",
                               "conditional_conclusion": f"Si las alineaciones mantienen disponibilidad comparable, {offense['name']} puede sostener su producción observada; la concesión rival no se interpreta por sí sola como evidencia contraria.",
                               "tactical_question": f"¿Puede {offense['name']} reproducir {metric} contra la estructura y localía del encuentro objetivo?"})
    result["matchup"]["dimensions"] = dimensions
    result["matchup"]["hypotheses"] = [hypothesis(
        f"{d['team_name']} producción {d['dimension']} frente a concesión rival en L5",
        d["evidence"]["favorable"], d["evidence"]["contrary"], d["evidence"]["missing"],
        "PROBABLE" if d["confidence"] == "MODERADA" else "INFERIDO") for d in dimensions]
    result["matchup"]["scenarios"] = [
        {"scenario": "alineaciones no confirmadas", "label": "NO_VERIFICADO", "evidence": [], "missing": ["alineaciones oficiales", "disponibilidad"]},
        {"scenario": "ritmo y desgaste", "label": "NO_VERIFICADO", "evidence": [], "missing": ["datos temporales por periodo y carga de minutos"]},
    ]
    if sos:
        result["sos"] = {"source_schema": sos.get("schema"), "source_sha256": (source_files or {}).get("sos_sha256"),
                         "quality_status": sos.get("quality_status"), "warnings": sos.get("warnings", []),
                         "teams": {side: {"observed_n": (sos.get("teams", {}).get(side) or {}).get("observed_n"),
                                          "windows": (sos.get("teams", {}).get(side) or {}).get("windows", {}),
                                          "reference_gaps": (sos.get("teams", {}).get(side) or {}).get("reference_gaps_shots_for", [])}
                                  for side in ("home", "away")}}
    result["warnings"].extend(["REVIEW_REQUIRED: no hay autorización para picks, cuotas, stakes ni probabilidades.",
                               "REVIEW_REQUIRED: cualquier hipótesis táctica requiere evidencia cualitativa independiente."])
    return result


def markdown(result: dict[str, Any]) -> str:
    lines = [f"# PREMATCH Fase 2.4 — {result['event_id_bwin']}", "",
             f"Fixture API: **{result['target_fixture_id']}**  ", f"Corte UTC: `{result['analysis_cutoff_utc']}`  ",
             "Alcance: análisis descriptivo PREMATCH. No contiene picks, cuotas, stakes ni probabilidades.", ""]
    for side in ("home", "away"):
        t = result["teams"][side]
        lines += [f"## {t['name']} ({'local' if side == 'home' else 'visitante'})", "",
                  f"Históricos válidos as-of: **{t['observed_n']}**; cobertura L5/L10/L20: "
                  f"{t['coverage']['L5']}/{t['coverage']['L10']}/{t['coverage']['L20']}.", ""]
        for n in (5, 10, 20):
            w = t["windows"][f"L{n}"]
            lines.append(f"### L{n} (n={w['observed_n']}/{w['requested_n']})")
            lines.append("; ".join(f"{m}={v['mean']} (n={v['observed_n']}/{v['requested_n']})" for m, v in w["metrics"].items() if m in ("goals_for", "goals_against", "xg_for", "xg_against", "shots_for", "on_target_for", "corners_for")))
            lines.append("")
        lines.append("### Competiciones y cambios")
        for scope_name in ("official", "laliga_current", "friendlies", "copa", "home_only", "away_only"):
            scope = t["competition_scopes"].get(scope_name, {})
            l5 = scope.get("L5", {})
            lines.append(f"- {scope_name}: L5 n={l5.get('observed_n', 0)}/{l5.get('requested_n', 5)}; "
                         f"L10 n={scope.get('L10', {}).get('observed_n', 0)}/{scope.get('L10', {}).get('requested_n', 10)}; "
                         f"L20 n={scope.get('L20', {}).get('observed_n', 0)}/{scope.get('L20', {}).get('requested_n', 20)}.")
        changes = t["competition_scopes"]["global"].get("change_interpretation", {})
        highlighted = [f"{m}: {x['status']} (Δ={x['difference']})" for m, x in changes.items() if x.get("status") not in ("ESTABLE_DESCRIPTIVO", "SIN_EVIDENCIA_SUFICIENTE")]
        lines.append("- Interpretación L5 reciente vs anterior: " + ("; ".join(highlighted) if highlighted else "sin cambio concluyente") + ".")
        lines.append("### Localía")
        for venue, split in t["venue_splits"].items():
            lines.append(f"- {venue}: n={split['observed_n']}; xG={split['metrics']['xg_for']['mean']} (n={split['metrics']['xg_for']['observed_n']}); tiros={split['metrics']['shots_for']['mean']}.")
        half = t["period_analysis"]
        lines += ["", f"Datos por mitades: {half.get('status')}; "
                  f"H1 fixtures={half.get('periods', {}).get('first_half', {}).get('fixtures_n', 0)}, "
                  f"H2 fixtures={half.get('periods', {}).get('second_half', {}).get('fixtures_n', 0)}.",
                  f"Jugadores con minutos observados: {t['players']['players_with_minutes']}; "
                  "los roles funcionales no se infieren automáticamente.",
                  f"Entrenadores: {len(t['coach_evidence'].get('observations', []))} observaciones; cambios confirmados={len(t['coach_evidence'].get('confirmed_changes', []))}.",
                  f"Tarjetas rojas propias={t['red_card_breakdown']['own_n']}; del contrario={t['red_card_breakdown']['opponent_n']}.", "",
                  f"REVIEW_REQUIRED: {'sí' if t['review_required'] else 'no'}.", ""]
    lines += ["## Cruce táctico-estadístico", "", "Cada hipótesis conserva evidencia favorable, contraria y datos faltantes; `INFERIDO` no equivale a confirmado.", ""]
    for d in result["matchup"].get("dimensions", [])[:10]:
        lines += [f"- **{('PROBABLE' if d['confidence'] == 'MODERADA' else 'INFERIDO')}** {d['team_name']} — {d['dimension']} (confianza {d['confidence']})",
                  f"  - Favorable: {'; '.join(d['evidence']['favorable'])}",
                  f"  - Contraria: {'; '.join(d['evidence']['contrary'])}",
                  f"  - Neutral: {'; '.join(d['evidence']['neutral'])}",
                  f"  - Faltante: {', '.join(d['evidence']['missing']) or 'ninguna crítica'}",
                  f"  - Condicional: {d['conditional_conclusion']}",
                  f"  - Pregunta táctica: {d['tactical_question']}" ]
    if result.get("sos"):
        lines += ["", "## Strength of Schedule (SOS) reutilizado", "", "No se recalcula el pool: se reutiliza V1045 y se conservan sus denominadores y advertencias."]
        for side, item in result["sos"]["teams"].items():
            lines.append(f"- {result['teams'][side]['name']}: n observado={item.get('observed_n')}; "
                         f"métricas/window disponibles={len(item.get('windows') or {})}; gaps={len(item.get('reference_gaps') or [])}.")
    lines += ["", "## Disponibilidad del partido objetivo", ""]
    for side, items in result.get("target_availability", {}).items():
        team_name = result["teams"][side]["name"]
        if not items:
            lines.append(f"- {team_name}: no hay ausencias en el snapshot objetivo.")
        else:
            lines.append(f"- {team_name}: " + "; ".join(f"{x['player_name']} ({x['reason']}, {x['status']}, fixture {x['fixture_id']})" for x in items))
    lines += ["", "## Prioridades para Fase 2.5", "", "1. Confirmar alineaciones y estado de las ausencias del snapshot.",
              "2. Verificar si las señales L5 sobreviven al filtro LaLiga y a la localía.",
              "3. Revisar los partidos afectados por expulsiones antes de atribuir deterioros.",
              "4. Contrastar las preguntas tácticas con evidencia cualitativa fechada; no convertirlas en pronósticos."]
    lines += ["", "## Limitaciones y control temporal", "", "- Se excluyen el fixture objetivo, kickoffs no parseables y cualquier histórico posterior al corte.", "- `null` se mantiene como ausencia de observación; un cero observado permanece cero.", "- No se ejecutó scraper ni motor de apuestas; la información cualitativa externa no fue suministrada.", ""]
    return "\n".join(lines)


def run(event_id: str, root: Path) -> tuple[Path, Path]:
    base = root / "outputs" / "matches" / event_id
    deep = base / "analysis" / "deep_v1042"
    perf_p = deep / f"rendimiento_profundo_{event_id}.json"
    context_p = deep / f"contexto_ampliado_{event_id}.json"
    trends_p = deep / f"tendencias_profundas_{event_id}.json"
    depth_p = deep / f"profundidad_historica_{event_id}.json"
    players_p = deep / f"jugadores_historicos_{event_id}.json"
    sos_p = deep / f"ajuste_rivales_v1045_{event_id}.json"
    performance = json.loads(perf_p.read_text())
    source = {"performance": str(perf_p), "performance_sha256": sha256(perf_p)}
    loaded = {}
    for key, path in (("context", context_p), ("trends", trends_p), ("depth", depth_p), ("players", players_p), ("sos", sos_p)):
        if path.exists():
            loaded[key] = json.loads(path.read_text()); source[key] = str(path); source[f"{key}_sha256"] = sha256(path)
    out = build(performance, **loaded, source_files=source)
    out_p = deep / f"analisis_integral_{event_id}.json"
    md_p = deep / f"analisis_integral_{event_id}.md"
    out_p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    md_p.write_text(markdown(out))
    return out_p, md_p


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", default="7902458")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    out, md = run(args.event_id, args.root)
    print(json.dumps({"json": str(out), "markdown": str(md), "odds_enabled": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
