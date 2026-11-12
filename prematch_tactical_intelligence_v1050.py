#!/usr/bin/env python3
"""PREMATCH Fase 2.5: inteligencia táctica y contextual, sin predicción.

Consume el análisis integral 2.4.1 y fuentes cualitativas suministradas por el
usuario. Nunca ejecuta investigación web ni modifica snapshots, SOS o cuotas.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LABELS = {"CONFIRMADO", "PROBABLE", "INFERIDO", "NO_VERIFICADO"}


def utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm(text: Any) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(text or "")).casefold()
                    if not unicodedata.combining(c)).split())


def verify_integral(doc: dict[str, Any]) -> None:
    if not str(doc.get("schema", "")).startswith("prematch-integral-analysis-v1046"):
        raise ValueError("UNSUPPORTED_INTEGRAL_SCHEMA")
    if not doc.get("event_id_bwin") or not isinstance(doc.get("target_fixture_id"), int):
        raise ValueError("INTEGRAL_IDENTITY_MISSING")
    if not utc(doc.get("analysis_cutoff_utc")) or not utc(doc.get("target_kickoff_utc")):
        raise ValueError("INTEGRAL_TEMPORAL_METADATA_MISSING")
    cutoff, kickoff = utc(doc["analysis_cutoff_utc"]), utc(doc["target_kickoff_utc"])
    if cutoff >= kickoff:
        raise ValueError("INTEGRAL_CUTOFF_AFTER_KICKOFF")


def source_integrity(doc: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    sources = doc.get("source_files") or {}
    for key, expected in sources.items():
        if not key.endswith("_sha256") or not isinstance(expected, str):
            continue
        path_key = key[:-7]
        path = Path(sources.get(path_key, ""))
        if not path.is_file():
            checks.append({"source": path_key, "path": str(path), "status": "MISSING"})
            continue
        actual = sha256(path)
        checks.append({"source": path_key, "path": str(path), "expected_sha256": expected,
                       "actual_sha256": actual, "status": "OK" if actual == expected else "MISMATCH"})
    mismatches = [x for x in checks if x["status"] == "MISMATCH"]
    if mismatches:
        raise ValueError("INTEGRAL_SOURCE_SHA256_MISMATCH")
    return checks


def _header(text: str, name: str) -> str | None:
    match = re.search(rf"(?im)^(?:{re.escape(name)}|{re.escape(name.replace('_', ' '))})\s*:\s*(\S+)\s*$", text)
    return match.group(1) if match else None


def load_qualitative_reports(paths: list[Path], *, event_id: str, fixture_id: int,
                             cutoff: datetime) -> dict[str, Any]:
    """Read only explicitly supplied reports; no network or provider execution."""
    accepted, excluded, facts, hypotheses = [], [], [], []
    seen_content: set[tuple[str, str]] = set()
    for path in paths:
        if not path.exists() or not path.is_file():
            excluded.append({"path": str(path), "status": "MISSING"})
            continue
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        source = path.stem.split("_")[0].lower()
        payload: dict[str, Any] | None = None
        try:
            if path.suffix.lower() == ".json":
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("JSON_REPORT_MUST_BE_OBJECT")
                source = str(payload.get("source") or source).lower()
                event_value = str(payload.get("event_id_bwin") or payload.get("event_id") or "")
                fixture_value = str(payload.get("fixture_id") or "")
                cutoff_value = payload.get("cutoff_utc")
                entries = list(payload.get("facts") or []) + list(payload.get("hypotheses") or [])
            else:
                event_value = _header(raw, "Event ID") or _header(raw, "event_id_bwin") or ""
                fixture_value = _header(raw, "Fixture ID") or _header(raw, "fixture_id") or ""
                cutoff_value = _header(raw, "cutoff_utc")
                entries = []
                for line in raw.splitlines():
                    m = re.match(r"(?i)^\s*(FACT|RUMOR|DECLARATION|HYPOTHESIS)\s*:\s*(.+)$", line)
                    if m:
                        entries.append({"kind": m.group(1).upper(), "text": m.group(2).strip()})
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            excluded.append({"path": str(path), "status": "INVALID_FORMAT", "reason": str(exc)})
            continue
        if str(event_value) != str(event_id) or str(fixture_value) != str(fixture_id):
            excluded.append({"path": str(path), "status": "IDENTITY_MISMATCH"})
            continue
        report_cutoff = utc(cutoff_value)
        if report_cutoff is None or report_cutoff > cutoff:
            excluded.append({"path": str(path), "status": "POST_CUTOFF_EXCLUDED", "cutoff_utc": cutoff_value})
            continue
        digest = sha256(path)
        accepted.append({"source": source, "path": str(path), "sha256": digest,
                         "cutoff_utc": report_cutoff.isoformat(),
                         "publication": (payload or {}).get("publication") if payload else None,
                         "url": (payload or {}).get("url") if payload else None})
        for item in entries:
            if isinstance(item, str):
                item = {"kind": "HYPOTHESIS", "text": item}
            if not isinstance(item, dict) or not item.get("text"):
                continue
            text = str(item["text"]).strip()
            # Misma evidencia copiada con otro nombre no cuenta como una
            # segunda confirmación. Fuentes distintas con URL distinta sí se
            # conservan para poder contrastarlas.
            source_identity = norm((payload or {}).get("url")) or digest
            key = (norm(text), source_identity)
            if key in seen_content:
                continue
            seen_content.add(key)
            kind = str(item.get("kind") or "HYPOTHESIS").upper()
            label = str(item.get("label") or ("CONFIRMADO" if kind == "FACT" else "NO_VERIFICADO")).upper()
            if label not in LABELS:
                label = "NO_VERIFICADO"
            record = {"source": source, "path": str(path), "sha256": digest,
                      "kind": kind, "label": label, "text": text,
                      "verification": item.get("verification") or "USER_SUPPLIED_NOT_PROVIDER_VERIFIED"}
            (facts if kind in {"FACT", "DECLARATION"} else hypotheses).append(record)
    return {"accepted": accepted, "excluded": excluded, "facts": facts, "hypotheses": hypotheses,
            "status": "PARTIAL_NO_EXTERNAL_REPORTS" if not accepted else "EXTERNAL_REPORTS_ATTACHED"}


def _metric(team: dict[str, Any], window: str, name: str) -> dict[str, Any]:
    metrics = team.get("windows", {}).get(window, {}).get("metrics", {})
    # team_form_engine names goalkeeper saves as ``saves``; Fase 2.4.1
    # exposes the directional alias ``saves_for`` in its defensive section.
    return (metrics.get(name) or metrics.get("saves" if name == "saves_for" else name) or {})


def period_interpretation(periods: dict[str, Any]) -> dict[str, Any]:
    first = periods.get("periods", {}).get("first_half", {}).get("metrics", {})
    second = periods.get("periods", {}).get("second_half", {}).get("metrics", {})
    comparison = {}
    for metric in ("xg", "shots", "on_target", "corners"):
        a, b = first.get(metric, {}), second.get(metric, {})
        av, bv = a.get("mean"), b.get("mean")
        comparison[metric] = {"first_half": a, "second_half": b,
                              "difference_second_minus_first": round(bv - av, 3)
                              if isinstance(av, (int, float)) and isinstance(bv, (int, float)) else None,
                              "status": "COMPARABLE" if isinstance(av, (int, float)) and isinstance(bv, (int, float)) else "INCOMPLETE"}
    usable = [x for x in comparison.values() if x["status"] == "COMPARABLE"]
    return {"status": "INFERIDO_DESCRIPTIVO" if usable else "NO_VERIFICADO",
            "metrics": comparison,
            "warning": "Una diferencia H1/H2 no demuestra ritmo, desgaste ni una causa táctica."}


PLAYER_METRIC_UNITS = {
    "shots_total": "remates", "shots_on": "tiros_a_puerta", "passes_total": "pases",
    "passes_key": "pases_clave", "tackles_total": "entradas", "tackles_interceptions": "intercepciones",
    "duels_total": "duelos", "fouls_committed": "faltas_cometidas", "fouls_drawn": "faltas_recibidas",
}


def player_denominators(player: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose every denominator; suppress per90 if eligible minutes are absent."""
    result = []
    for metric_name, record in (player.get("metrics") or {}).items():
        record = record or {}
        eligible = record.get("eligible_minutes")
        per90 = record.get("per90") if isinstance(eligible, (int, float)) and eligible > 0 else None
        result.append({
            "metric_name": metric_name,
            "unit": PLAYER_METRIC_UNITS.get(metric_name, "unidad_no_documentada"),
            "observed_matches": record.get("observed_n"),
            "played_matches": record.get("played_n", player.get("played_n")),
            "eligible_minutes": eligible,
            "total": record.get("total"),
            "per90": per90,
            "per90_status": "VERIFIED" if per90 is not None else "NOT_REPORTED_BASE_UNVERIFIABLE",
            "formula": "total / eligible_minutes * 90",
            "hist_fixture_ids": record.get("hist_fixture_ids", []),
        })
    return result


def window_trace(team: dict[str, Any]) -> dict[str, Any]:
    rows = team.get("historical_rows") or []
    traces = {}
    for label, selected in (("global_L5", rows[:5]),
                            ("home_L5", [r for r in rows if r.get("venue") == "HOME"][:5]),
                            ("away_L5", [r for r in rows if r.get("venue") == "AWAY"][:5])):
        dates = [r.get("kickoff_utc") for r in selected if r.get("kickoff_utc")]
        traces[label] = {"fixture_ids": [r.get("fixture_id") for r in selected],
                         "kickoff_utc": dates,
                         "period_start_utc": dates[-1] if dates else None,
                         "period_end_utc": dates[0] if dates else None,
                         "observed_n": len(selected)}
    return traces


def team_diagnosis(side: str, team: dict[str, Any], availability: list[dict[str, Any]],
                   qualitative: dict[str, Any]) -> dict[str, Any]:
    name = team.get("name")
    global_scope = (team.get("competition_scopes") or {}).get("global", {})
    changes = global_scope.get("change_interpretation", {})
    offensive = []
    defensive = []
    for metric in ("goals_for", "xg_for", "shots_for", "on_target_for", "corners_for"):
        status = (changes.get(metric) or {}).get("status", "SIN_EVIDENCIA_SUFICIENTE")
        comparison = (global_scope.get("period_comparison", {}).get("metrics", {}).get(metric) or {})
        offensive.append({"metric": metric, "status": status,
                          "L5": _metric(team, "L5", metric), "L10": _metric(team, "L10", metric),
                          "L20": _metric(team, "L20", metric),
                          "change": {"difference": comparison.get("difference"), "recent": comparison.get("recent"),
                                     "previous": comparison.get("previous"), "status": status},
                          "interpretation": "Cambio descriptivo; requiere contraste de localía, competición y rivales."})
    for metric in ("goals_against", "xg_against", "shots_against", "on_target_against", "saves_for"):
        status = (changes.get(metric) or {}).get("status", "SIN_EVIDENCIA_SUFICIENTE")
        comparison = (global_scope.get("period_comparison", {}).get("metrics", {}).get(metric) or {})
        defensive.append({"metric": metric, "status": status,
                          "L5": _metric(team, "L5", metric), "L10": _metric(team, "L10", metric),
                          "L20": _metric(team, "L20", metric),
                          "change": {"difference": comparison.get("difference"), "recent": comparison.get("recent"),
                                     "previous": comparison.get("previous"), "status": status},
                          "interpretation": "Vulnerabilidad o resistencia observada, no mecanismo causal demostrado."})
    periods = team.get("period_analysis") or {}
    period_view = period_interpretation(periods)
    mechanisms = [
        {"mechanism": "producción y concesión agregadas", "label": "CONFIRMADO" if offensive else "NO_VERIFICADO",
         "evidence": ["métricas históricas L5/L10/L20"],
         "contrary": ["muestra parcial y SOS no completo"],
         "missing": ["datos posicionales y alineación objetivo"]},
        {"mechanism": "diferencia descriptiva de métricas entre mitades", "label": period_view["status"],
         "evidence": [f"statistics_halves: {periods.get('periods', {}).get('first_half', {}).get('fixtures_n', 0)} H1 y {periods.get('periods', {}).get('second_half', {}).get('fixtures_n', 0)} H2",
                      f"xG Δ2H-1H={period_view['metrics']['xg']['difference_second_minus_first']}",
                      f"tiros Δ2H-1H={period_view['metrics']['shots']['difference_second_minus_first']}"],
         "contrary": ["cobertura incompleta y sin modelo causal"],
         "missing": ["muestra completa y eventos contextualizados"]},
    ]
    players = []
    for player in (team.get("players", {}).get("players") or [])[:10]:
        metrics = player.get("metrics") or {}
        observed_metric = next((k for k, v in metrics.items() if (v or {}).get("per90") is not None), None)
        denominators = player_denominators(player)
        sufficient = (player.get("played_n") or 0) >= 3 and (player.get("minutes_observed") or 0) >= 180
        players.append({"player_id": player.get("player_id"), "name": player.get("name"),
                        "label": "CONFIRMADO" if sufficient else "NO_VERIFICADO", "played_n": player.get("played_n"),
                        "minutes_observed": player.get("minutes_observed"),
                        "positions_observed": player.get("positions_observed", []),
                        "metric_denominator": denominators,
                        "metric_denominator_status": "PER90_SUPPRESSED_WHEN_BASE_MISSING",
                        "role": "NO_VERIFICADO", "note": "Participación observada no implica titularidad ni rol táctico en el objetivo."})
    return {"team_id": team.get("team_id"), "name": name, "side": side,
            "offensive_trends": offensive, "defensive_trends": defensive,
            "mechanisms": mechanisms, "players": players,
            "coach": {"observations": (team.get("coach_evidence") or {}).get("observations", []),
                      "confirmed_changes": (team.get("coach_evidence") or {}).get("confirmed_changes", []),
                      "unverified_transitions": (team.get("coach_evidence") or {}).get("transitions", [])},
            "availability": availability,
            "window_trace": window_trace(team),
            "red_cards": team.get("red_card_breakdown", {}),
            "formation_evidence": (team.get("historical_coach_source") or {}).get("formation_changes_in_observed_history", []),
            "periods": periods, "period_interpretation": period_view,
            "qualitative_relevant": [x for x in qualitative.get("facts", []) if norm(name) in norm(x.get("text"))],
            "limitations": ["No se infieren presión, bandas, juego aéreo, roles o decisiones del entrenador desde estadísticas agregadas."]}


def duel_matrix(home: dict[str, Any], away: dict[str, Any], dimensions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = []
    for attacker, defender, dimension in ((home, away, "ataque_local_vs_defensa_visitante"),
                                           (away, home, "ataque_visitante_vs_defensa_local")):
        side = "home" if attacker.get("side") == "home" else "away"
        measured = [x for x in (dimensions or []) if x.get("team") == side]
        rows.append({"duel": dimension, "label": "INFERIDO",
                     "mechanism": "producción ofensiva frente a concesión defensiva agregada",
                     "players_involved": [], "evidence": [f"{attacker['name']} L5 ofensivo y {defender['name']} L5 defensivo"],
                     "statistical_cross": [{"dimension": x.get("dimension"), "global_L5": x.get("global_L5"),
                                            "venue_L5": x.get("venue_L5"), "sos_L5": x.get("sos_L5"),
                                            "confidence": x.get("confidence"), "evidence": x.get("evidence")} for x in measured],
                     "strengths": ["métricas de producción y concesión con denominadores"],
                     "possible_responses": ["ajustar estructura sólo si la alineación y el plan documentado lo confirman"],
                     "contrary": ["SOS parcial, cobertura xG/tiros incompleta y ausencia de datos posicionales"],
                     "missing": ["laterales/extremos, centrales/delanteros, presión, salida, juego aéreo y balón parado"]})
    for duel in ("laterales_extremos", "centrales_delanteros", "mediocampo_presion_salida", "juego_aereo_balón_parado", "faltas_tarjetas_corners"):
        rows.append({"duel": duel, "label": "NO_VERIFICADO", "mechanism": "no establecido en los artefactos disponibles",
                     "players_involved": [], "evidence": [], "strengths": [], "possible_responses": [],
                     "contrary": [], "missing": ["datos posicionales, alineaciones objetivo y fuente cualitativa fechada"]})
    return rows


def scenarios(home: dict[str, Any], away: dict[str, Any], reports: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"scenario": "A — ambos mantienen patrones históricos", "label": "NO_VERIFICADO",
         "conditions": ["alineaciones próximas a las observadas", "sin bajas adicionales"],
         "evidence": ["continuidad descriptiva de métricas L5/L10/L20"], "contradictions": ["cambios de entrenador no verificados y expulsiones"],
         "missing": ["XI oficial, formación objetivo y plan de partido"]},
        {"scenario": "B — ajuste de presión o bloque defensivo", "label": "NO_VERIFICADO",
         "conditions": ["declaración o alineación que documente el ajuste"], "evidence": [],
         "contradictions": ["no hay evidencia cualitativa adjunta"], "missing": ["fuente fechada, roles y estructura"]},
        {"scenario": "C — redistribución por bajas", "label": "NO_VERIFICADO",
         "conditions": ["bajas del snapshot afectan al XI"], "evidence": ["lista de disponibilidad del snapshot"],
         "contradictions": ["no se conoce reemplazo ni función redistribuida"], "missing": ["XI oficial y rol del sustituto"]},
    ]


def research_questions(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Event-specific questions; each has explicit confirmation/refutation tests."""
    questions = []
    for side, team in report["teams"].items():
        for trend in team["offensive_trends"] + team["defensive_trends"]:
            change = trend.get("change") or {}
            if change.get("difference") is None or trend.get("status") == "ESTABLE_DESCRIPTIVO":
                continue
            trace = team.get("window_trace", {}).get("global_L5", {})
            venue = team.get("window_trace", {}).get("home_L5" if side == "home" else "away_L5", {})
            questions.append({"team": team["name"], "category": "L5_CAMBIO",
                              "question": f"¿El cambio de {trend['metric']} de {team['name']} ({change['difference']}) se mantiene al filtrar por la localía objetivo?",
                              "hypothesis": f"La señal de {trend['metric']} puede reflejar un cambio reciente.",
                              "fixture_ids_global_L5": trace.get("fixture_ids", []), "fixture_ids_venue_L5": venue.get("fixture_ids", []),
                              "period_global_L5": [trace.get("period_start_utc"), trace.get("period_end_utc")],
                              "confirming_information": ["métrica equivalente en L5 de local/visitante", "alineaciones comparables", "SOS compatible"],
                              "refuting_information": ["reversión al separar LaLiga de amistosos", "expulsión que explique el cambio"],
                              "priority_basis": [trend.get("status"), "magnitud L5 vs L5 anterior"]})
        period = team.get("period_interpretation", {})
        questions.append({"team": team["name"], "category": "H1_H2",
                          "question": f"¿La diferencia H1/H2 observada en {team['name']} representa un patrón repetible o sólo composición de rivales/partidos?",
                          "hypothesis": "La segunda mitad presenta una media distinta en métricas observadas.",
                          "fixture_ids_global_L5": [], "fixture_ids_venue_L5": [],
                          "period_global_L5": None,
                          "confirming_information": ["mismo signo H1/H2 en submuestras de competición y localía", "eventos y expulsiones fechados"],
                          "refuting_information": ["el signo desaparece al excluir partidos extremos", "cobertura H1/H2 insuficiente"],
                          "period_metrics": period.get("metrics", {}), "priority_basis": ["cobertura H1/H2 real", "sin atribución causal"]})
        for formation in (team.get("formation_evidence") or [])[:3]:
            questions.append({"team": team["name"], "category": "FORMACION_HISTORICA",
                              "question": f"¿La formación registrada {formation.get('before')}→{formation.get('after')} se repite con la misma alineación y produce cambios medibles en el partido objetivo?",
                              "hypothesis": "La formación registrada puede ser un contexto, no una prueba de comportamiento táctico.",
                              "fixture_ids_global_L5": [formation.get("before_fixture"), formation.get("after_fixture")],
                              "fixture_ids_venue_L5": [], "period_global_L5": None,
                              "confirming_information": ["XI oficial, posiciones y repetición del patrón"],
                              "refuting_information": ["cambio de etiqueta del proveedor sin cambio de jugadores", "ausencia de efecto en métricas"],
                              "priority_basis": ["formación histórica verificada", "comportamiento aún no demostrado"]})
        for absence in team.get("availability", []):
            questions.append({"team": team["name"], "category": "AUSENCIA",
                              "question": f"¿La ausencia de {absence.get('player_name')} redistribuye una función relevante para el partido objetivo?",
                              "hypothesis": "La baja puede alterar disponibilidad, pero no permite inferir automáticamente el reemplazo.",
                              "fixture_ids_global_L5": [], "fixture_ids_venue_L5": [], "period_global_L5": None,
                              "confirming_information": ["XI oficial", "posición y minutos del sustituto", "fuente médica/sancionadora fechada"],
                              "refuting_information": ["jugador disponible o reemplazo con la misma función documentada"],
                              "priority_basis": ["snapshot objetivo", "alta sensibilidad a alineación"]})
    for duel in report.get("duel_matrix", [])[:2]:
        questions.append({"team": None, "category": "CRUCE_ATAQUE_DEFENSA",
                          "question": f"¿El cruce {duel['duel']} se sostiene con las alineaciones objetivo y el SOS disponible?",
                          "hypothesis": duel["mechanism"], "fixture_ids_global_L5": [], "fixture_ids_venue_L5": [], "period_global_L5": None,
                          "confirming_information": ["XI oficial", "métricas de localía y referencias SOS suficientes", "datos posicionales"],
                          "refuting_information": ["cambio de estructura o ausencia crítica", "SOS parcial no representativo"],
                          "priority_basis": ["relevancia directa del enfrentamiento", "evidencia agregada limitada"]})
    return questions


def priorities(report: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    qualitative_n = len((report.get("qualitative") or {}).get("facts", [])) + len((report.get("qualitative") or {}).get("hypotheses", []))
    for side, team in report["teams"].items():
        for trend in team["offensive_trends"] + team["defensive_trends"]:
            l5 = trend["L5"]
            coverage = (l5.get("observed_n", l5.get("n", 0)) / max(1, l5.get("requested_n", 5)))
            change = trend.get("change") or {}
            diff = change.get("difference")
            previous = ((change.get("previous") or {}).get("mean"))
            magnitude = min(1.0, abs(diff) / max(abs(previous or 0), 1.0)) if isinstance(diff, (int, float)) else 0.0
            dispersion = l5.get("cv") if isinstance(l5.get("cv"), (int, float)) else None
            relevance = 1.0 if trend["metric"] in {"goals_for", "goals_against", "xg_for", "xg_against"} else 0.65
            contradictions = []
            if "DISTORSION" in trend["status"]:
                contradictions.append("expulsiones en la muestra comparada")
            score = round(25 * coverage + 30 * magnitude + 20 * relevance
                          - 10 * min(dispersion or 0, 2.0) / 2
                          - 10 * len(contradictions) + (5 if qualitative_n else 0), 2)
            items.append({"team": side, "metric": trend["metric"], "priority_score": score,
                          "priority_type": "INVESTIGACION_NO_PROBABILIDAD", "evidence_quality": trend["status"],
                          "qualitative_convergence_n": qualitative_n, "alignment_sensitive": True,
                          "magnitude": magnitude, "dispersion_cv": dispersion, "matchup_relevance": relevance,
                          "contradictions": contradictions, "reasons": [trend["status"], f"L5 coverage {coverage:.2f}",
                                                                          f"magnitud relativa {magnitude:.2f}",
                                                                          f"dispersión CV {dispersion if dispersion is not None else 'N/D'}"]})
    return sorted(items, key=lambda x: (-x["priority_score"], x["team"], x["metric"]))


def build(integral: dict[str, Any], *, qualitative_reports: list[Path] | None = None,
          source_files: dict[str, str] | None = None) -> dict[str, Any]:
    verify_integral(integral)
    integrity = source_integrity(integral)
    cutoff = utc(integral["analysis_cutoff_utc"])
    reports = load_qualitative_reports(qualitative_reports or [], event_id=str(integral["event_id_bwin"]),
                                       fixture_id=integral["target_fixture_id"], cutoff=cutoff)
    teams = integral["teams"]
    diagnoses = {side: team_diagnosis(side, teams[side], integral.get("target_availability", {}).get(side, []), reports)
                 for side in ("home", "away")}
    out = {"schema": "prematch-tactical-intelligence-v1050", "event_id_bwin": integral["event_id_bwin"],
           "target_fixture_id": integral["target_fixture_id"], "target_kickoff_utc": integral["target_kickoff_utc"],
           "analysis_cutoff_utc": integral["analysis_cutoff_utc"], "scope": "TACTICAL_CONTEXT_NO_PICKS",
           "source_files": source_files or {}, "source_integral_sha256": (source_files or {}).get("integral_sha256"),
           "input_source_integrity": integrity,
           "temporal_guard": {"cutoff_utc": integral["analysis_cutoff_utc"], "post_cutoff_reports_excluded": reports["excluded"]},
           "teams": diagnoses, "duel_matrix": duel_matrix(diagnoses["home"], diagnoses["away"],
                                                            (integral.get("matchup") or {}).get("dimensions", [])),
           "scenarios": scenarios(diagnoses["home"], diagnoses["away"], reports),
           "qualitative": reports, "research_questions": [], "priorities": [], "odds_enabled": False,
           "warnings": ["REVIEW_REQUIRED: investigación táctica, no predicción ni apuesta.",
                        "Las estadísticas agregadas no confirman mecanismos futbolísticos."]}
    out["research_questions"] = research_questions(out)
    out["priorities"] = priorities(out)
    return out


def markdown(doc: dict[str, Any]) -> str:
    lines = [f"# PREMATCH Fase 2.5 — {doc['event_id_bwin']}", "",
             f"Fixture API: **{doc['target_fixture_id']}**", f"Corte UTC: `{doc['analysis_cutoff_utc']}`",
             "Alcance: inteligencia táctica contextual. No contiene picks, probabilidades, cuotas ni stakes.", ""]
    for side in ("home", "away"):
        team = doc["teams"][side]
        lines += [f"## Diagnóstico — {team['name']} ({'local' if side == 'home' else 'visitante'})", "",
                  "### Tendencias ofensivas"]
        for item in team["offensive_trends"]:
            l5 = item["L5"]; lines.append(f"- **{item['status']}** {item['metric']}: L5 {l5.get('mean')} (n={l5.get('observed_n', l5.get('n', 0))}/{l5.get('requested_n', '?')}), L10 {item['L10'].get('mean')}, L20 {item['L20'].get('mean')}.")
        lines.append("### Tendencias defensivas")
        for item in team["defensive_trends"]:
            l5 = item["L5"]; lines.append(f"- **{item['status']}** {item['metric']}: L5 {l5.get('mean')} (n={l5.get('observed_n', l5.get('n', 0))}/{l5.get('requested_n', '?')}), L10 {item['L10'].get('mean')}, L20 {item['L20'].get('mean')}.")
        lines += ["### Mecanismos", *[f"- **{m['label']}** {m['mechanism']}. A favor: {', '.join(m['evidence'])}. En contra: {', '.join(m['contrary'])}. Falta: {', '.join(m['missing'])}." for m in team["mechanisms"]],
                  "### Trazabilidad L5",
                  f"- Global: fixtures {team['window_trace']['global_L5']['fixture_ids']} ({team['window_trace']['global_L5']['period_start_utc']} → {team['window_trace']['global_L5']['period_end_utc']}).",
                  f"- Localía objetivo: fixtures {team['window_trace']['home_L5' if side == 'home' else 'away_L5']['fixture_ids']} ({team['window_trace']['home_L5' if side == 'home' else 'away_L5']['period_start_utc']} → {team['window_trace']['home_L5' if side == 'home' else 'away_L5']['period_end_utc']}).",
                  f"### Periodos: {team['periods'].get('status')}", f"H1={team['periods'].get('periods', {}).get('first_half', {}).get('fixtures_n', 0)} partidos; H2={team['periods'].get('periods', {}).get('second_half', {}).get('fixtures_n', 0)} partidos.",
                  f"### Entrenador y disponibilidad", f"Cambios confirmados: {len(team['coach']['confirmed_changes'])}; transiciones no verificadas: {len(team['coach']['unverified_transitions'])}.",
                  "Ausencias del snapshot: " + ("; ".join(f"{x['player_name']} ({x['reason']}, {x['status']})" for x in team["availability"]) or "ninguna registrada"),
                  "Per90: sólo se muestran tasas si existen minutos elegibles verificables; de lo contrario se suprimen.", ""]
        for player in team["players"][:3]:
            den = player.get("metric_denominator") or []
            if den:
                text = "; ".join(f"{d['metric_name']} {d['unit']}: partidos {d['observed_matches']}, minutos elegibles {d['eligible_minutes']}, per90 {d['per90'] if d['per90'] is not None else 'SUPRIMIDO'}" for d in den[:3])
                lines.append(f"- {player['name']}: {text}.")
    lines += ["## Matriz de duelos y contramedidas", ""]
    for duel in doc["duel_matrix"]:
        lines += [f"### {duel['duel']} — {duel['label']}", f"Mecanismo: {duel['mechanism']}", f"Evidencia: {'; '.join(duel['evidence']) or 'no disponible'}"]
        if duel.get("statistical_cross"):
            for cross in duel["statistical_cross"]:
                prod = (cross.get("global_L5") or {}).get("production", {}).get("mean")
                concession = (cross.get("global_L5") or {}).get("opponent_concession", {}).get("mean")
                lines.append(f"- Cruce {cross.get('dimension')}: producción L5={prod}; concesión rival L5={concession}; confianza={cross.get('confidence')}.")
        lines += [f"Fortalezas: {'; '.join(duel['strengths']) or 'no verificadas'}", f"Respuestas posibles: {'; '.join(duel['possible_responses']) or 'no verificadas'}", f"Evidencia contraria: {'; '.join(duel['contrary']) or 'no disponible'}", f"Falta: {'; '.join(duel['missing'])}", ""]
    lines += ["## Escenarios contrapuestos", ""]
    for scenario in doc["scenarios"]:
        lines += [f"- **{scenario['label']}** {scenario['scenario']}. Condiciones: {'; '.join(scenario['conditions'])}. Evidencia: {'; '.join(scenario['evidence']) or 'ninguna'}. Contradicciones: {'; '.join(scenario['contradictions'])}. Falta: {'; '.join(scenario['missing'])}."]
    lines += ["", "## Informes cualitativos", "", f"Estado: **{doc['qualitative']['status']}**; aceptados={len(doc['qualitative']['accepted'])}; excluidos={len(doc['qualitative']['excluded'])}."]
    for item in doc["qualitative"]["facts"] + doc["qualitative"]["hypotheses"]:
        lines.append(f"- **{item['label']}** [{item['source']}] {item['text']}")
    lines += ["", "## Prioridades para investigación", ""]
    for item in doc["priorities"][:12]:
        lines.append(f"- {item['team']} / {item['metric']}: prioridad {item['priority_score']} ({'; '.join(item['reasons'])}; contradicciones: {', '.join(item['contradictions']) or 'ninguna'}).")
    lines += ["", "## Preguntas concretas para Gemini/Grok", ""]
    for item in doc["research_questions"]:
        lines.append(f"- [{item['category']}] {item['question']} Confirmaría: {'; '.join(item['confirming_information'])}. Refutaría: {'; '.join(item['refuting_information'])}.")
    lines += ["", "## Restricciones", "", "- Las fuentes externas sólo se incorporan si tienen Event ID, Fixture ID y corte válido.", "- Informes posteriores al corte o con identidad incorrecta se excluyen.", "- No se ejecutó investigación web ni API real.", "- No se generan picks, probabilidades, cuotas ni stakes.", ""]
    return "\n".join(lines)


def run(event_id: str, root: Path, reports: list[Path] | None = None) -> tuple[Path, Path]:
    deep = root / "outputs" / "matches" / event_id / "analysis" / "deep_v1042"
    integral_path = deep / f"analisis_integral_{event_id}.json"
    integral = json.loads(integral_path.read_text(encoding="utf-8"))
    source = {"integral": str(integral_path), "integral_sha256": sha256(integral_path)}
    sos_path = deep / f"ajuste_rivales_v1045_{event_id}.json"
    if sos_path.exists():
        source["sos"] = str(sos_path); source["sos_sha256"] = sha256(sos_path)
    out = build(integral, qualitative_reports=reports, source_files=source)
    out_path = deep / f"inteligencia_tactica_{event_id}.json"
    md_path = deep / f"inteligencia_tactica_{event_id}.md"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown(out), encoding="utf-8")
    return out_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", default="7902458")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--report", action="append", type=Path, default=[], help="Informe Gemini/Grok suministrado manualmente")
    args = parser.parse_args()
    out, md = run(args.event_id, args.root, args.report)
    print(json.dumps({"json": str(out), "markdown": str(md), "odds_enabled": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
