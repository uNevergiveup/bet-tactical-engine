#!/usr/bin/env python3
"""Fase 1 offline: ejecuta análisis histórico + tendencias en un comando.

No altera inputs, manifests, probabilistic, ranking, cuotas ni el motor vigente.
Uso: python prematch_intelligence.py --input outputs/matches/7896590/inputs/match_engine_7896590.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import match_performance_engine as performance
import team_form_engine as form


def write_json(path: Path, value: dict) -> str:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def make_report(p: dict, f: dict) -> str:
    title = (f"# Inteligencia histórica PREMATCH — Bwin {p['event_id_bwin']} "
             f"/ API {p['target_fixture_id']}")
    lines = [title, "", f"- **Kickoff UTC:** {p['target_kickoff_utc']}",
             f"- **Cutoff UTC:** {p['analysis_cutoff_utc']}",
             "- **Fuente:** snapshot JSON local de API-Football; coherencia interna verificada, sin corroboración externa.",
             "- **Alcance:** observaciones descriptivas; NO produce picks, p_model, EV ni cuota justa.", ""]
    for side in ("home", "away"):
        team = f["teams"][side]
        original = p["teams"][side]
        trends = team["trend_comparison"]
        lines.extend([f"## {'Local' if side == 'home' else 'Visitante'}: {team['team_name']}", "",
                      f"- **Muestra detallada:** {team['observed_n']} partidos.",
                      f"- **Señal de estabilidad/cambio:** {trends.get('direction', trends['status'])} "
                      f"({trends['status']}).",
                      f"- **Cambios de entrenador observados:** "
                      f"{len(team['regime_evidence']['confirmed_coach_changes_in_observed_history'])} "
                      "(no equivale a verificar toda la temporada).",
                      f"- **Continuidad XI adyacente:** "
                      f"{team['regime_evidence']['average_shared_starters']} titulares comunes de 11, "
                      "cuando se dispone de ambos onces.", ""])
        lines.extend(["| Métrica | L5 (n observado) | L10 (n observado) | L20 (n observado) |",
                      "|---|---:|---:|---:|"])
        for key in ("goals_for", "goals_against", "shots_for", "on_target_for", "xg_for",
                    "xg_against", "corners_for", "possession_pct"):
            columns = []
            for n in (5, 10, 20):
                item = team["window_summaries"][f"L{n}"]
                value = item["metrics"][key]
                avg = "N/D" if value["mean"] is None else str(value["mean"])
                partial = " parcial" if not item["complete_window"] else ""
                columns.append(f"{avg} (n={value['n']}/{item['observed_n']}{partial})")
            lines.append(f"| {key} | " + " | ".join(columns) + " |")
        lines.extend(["", "**Casos de resultado vs rendimiento a revisar:**", ""])
        interesting = [r for r in original["historical_fixtures"] if
                       (r["statistical_advantage"]["result_performance_gap"] or r.get("xg_scoreline_gap"))]
        if interesting:
            for item in interesting:
                labels = [v for v in (item["statistical_advantage"]["result_performance_gap"],
                                      item.get("xg_scoreline_gap")) if v]
                lines.append(f"- Fixture {item['fixture_id']} contra {item['opponent']['name']}: "
                             f"{item['goals_for']}-{item['goals_against']}; "
                             + ", ".join(labels) + ".")
        else:
            lines.append("- Sin discrepancias tipificadas en esta muestra; no equivale a ausencia real.")
        issues = [r for r in original["historical_fixtures"] if r["data_quality"]["status"] != "OK"]
        lines.extend(["", f"**Coherencia de la fuente:** {len(issues)} fixtures requieren revisión interna.",
                      "", "**Advertencia:** un cambio de forma puede estar condicionado por rivales, "
                      "entrenador, lesiones, estructura o azar; el snapshot NO permite atribuir una causa "
                      "a vestuario, conflicto interno o mala suerte.", ""])
    lines.extend(["## Próxima fase", "",
                  "- Ampliar históricos por equipo a L10/L20 con cobertura de competiciones/temporadas.",
                  "- Ponderar fuerza de rival y comparar estilos sin mezclar muestras indiscriminadamente.",
                  "- Corroborar entrenador, sanciones, bajas y sucesos mediante fuentes fechadas independientes.",
                  "- Conectar señales a modelos de mercado con backtesting prospectivo; no validar con el resultado objetivo.", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, help="Por defecto, carpeta analysis del Event ID")
    parser.add_argument("--as-of", help="Corte opcional ISO 8601 con UTC/offset, <= snapshot")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    p = performance.build(data, input_sha256=hashlib.sha256(raw).hexdigest(), as_of=args.as_of)
    eid = p["event_id_bwin"]
    if not eid.isdigit():
        raise ValueError("EVENT_ID_BWIN_INVALID")
    folder = args.output_dir or Path("outputs") / "matches" / eid / "analysis"
    folder.mkdir(parents=True, exist_ok=True)
    p_path = folder / f"rendimiento_historico_{eid}.json"
    p_hash = write_json(p_path, p)
    f = form.build(p, source_sha256=p_hash)
    f_path = folder / f"tendencias_equipos_{eid}.json"
    write_json(f_path, f)
    report_path = folder / f"resumen_inteligencia_{eid}.md"
    report_path.write_text(make_report(p, f), encoding="utf-8")
    print(f"OK PREMATCH {eid} / fixture {p['target_fixture_id']}")
    for side in ("home", "away"):
        summary = f["teams"][side]
        trend = summary["trend_comparison"]
        print(f"{summary['team_name']}: {summary['observed_n']} históricos; "
              f"{trend.get('direction', trend['status'])}; {trend['status']}")
    print("Salidas:\n" + "\n".join(map(str, (p_path, f_path, report_path))))


if __name__ == "__main__":
    main()
