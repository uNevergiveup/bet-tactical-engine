#!/usr/bin/env python3
"""Auditoría OFFLINE de cobertura API-Football para un único Event ID.

No consulta API/Bwin, no calcula picks, no altera el manifest ni los inputs.
Ejemplos:
  python audit_context_coverage.py --event-id 7899092
  python audit_context_coverage.py --context-json /ruta/match_engine_7899092.json \
      --identity /ruta/match_identity.json --manifest /ruta/manifest.json \
      --output-dir /ruta/coverage
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

SCHEMA_VERSION = "1.0-coverage-offline"
COMPLETED = {"FT", "AET", "PEN"}
STAT_KEYS = (
    "Corner Kicks", "Total Shots", "Shots on Goal", "Shots off Goal",
    "Blocked Shots", "Shots insidebox", "Shots outsidebox", "Offsides",
    "Fouls", "Yellow Cards", "Red Cards", "Ball Possession",
    "expected_goals", "Total passes", "Passes accurate", "Passes %",
)
PLAYER_FIELDS = {
    "shots_total": ("shots", "total"),
    "shots_on_target": ("shots", "on"),
    "key_passes": ("passes", "key"),
    "tackles": ("tackles", "total"),
    "fouls_committed": ("fouls", "committed"),
    "offsides": ("offsides",),
}


def read_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError(f"JSON raíz no es un objeto: {path.name}")
    return obj


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def date_utc(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Fecha/hora ausente")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Fecha sin zona horaria: {raw}")
    return dt.astimezone(timezone.utc)


def number(raw: Any) -> float | int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        s = raw.strip().replace(",", ".")
        if re.fullmatch(r"-?\d+(?:\.\d+)?%?", s):
            return float(s.rstrip("%"))
    return None


def safe_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def response_count(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {"estado": "AUSENTE", "registros": 0}
    if obj.get("error") or obj.get("errors"):
        return {"estado": "ERROR", "registros": 0, "detalle": str(obj.get("error") or obj.get("errors"))[:180]}
    val = obj.get("response")
    if isinstance(val, list):
        return {"estado": "CON_DATOS" if val else "VACIO", "registros": len(val)}
    return {"estado": "FORMATO_DESCONOCIDO", "registros": 0}


def identity_check(payload: dict[str, Any], identity: dict[str, Any], event: str) -> None:
    match = payload.get("match") or {}
    fx = payload.get("fixture") or {}
    if str(identity.get("bwin_event_id")) != event:
        raise ValueError("Identidad canónica: Event ID distinto")
    if identity.get("identity_status") != "FIXTURE_VERIFIED_BY_GENERATOR":
        raise ValueError("Identidad canónica: estado no validado por el generador")
    fields = {
        "api_fixture_id": fx.get("fixture_id"),
        "league_id": fx.get("league_id"),
        "season": fx.get("season"),
        "bwin_url": match.get("url"),
    }
    for key, expected in fields.items():
        if identity.get(key) != expected:
            raise ValueError(f"Identidad canónica: contradicción en {key}")
    if date_utc(identity.get("kickoff_utc")) != date_utc(fx.get("date")):
        raise ValueError("Identidad canónica: kickoff contradictorio")
    for side in ("home", "away"):
        data = identity.get(side) or {}
        if data.get("api_team_id") != fx.get(f"{side}_id") or data.get("api_name") != fx.get(f"{side}_name"):
            raise ValueError(f"Identidad canónica: {side} no coincide con el fixture")
        expected_bwin = match.get(f"{side}_name_raw") or match.get(f"{side}_name")
        if data.get("bwin_name") != expected_bwin or data.get("category") != match.get(f"{side}_gender", "male"):
            raise ValueError(f"Identidad canónica: {side} no coincide con Bwin/categoría")


def team_stat_block(fixture: dict[str, Any], team_id: int) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    for row in fixture.get("statistics") or []:
        if safe_int((row.get("team") or {}).get("id")) != team_id:
            continue
        for stat in row.get("statistics") or []:
            key = stat.get("type")
            value = number(stat.get("value"))
            if isinstance(key, str) and value is not None:
                result[key] = value
    return result


def lineup_for(fixture: dict[str, Any], team_id: int) -> dict[str, Any]:
    return next((x for x in fixture.get("lineups") or []
                 if safe_int((x.get("team") or {}).get("id")) == team_id), {})


def players_for(fixture: dict[str, Any], team_id: int) -> list[dict[str, Any]]:
    rows = []
    for team in fixture.get("players") or []:
        if safe_int((team.get("team") or {}).get("id")) == team_id:
            rows.extend(team.get("players") or [])
    return rows


def nested(row: dict[str, Any], path: tuple[str, ...]) -> Any:
    result: Any = row
    for key in path:
        if not isinstance(result, dict):
            return None
        result = result.get(key)
    return result


def analyze_history(side: str, obj: Any, team_id: int, cutoff: datetime) -> dict[str, Any]:
    warnings: list[str] = []
    if not isinstance(obj, dict) or obj.get("error"):
        return {"equipo_id": team_id, "partidos_recibidos": 0, "partidos_elegibles": 0,
                "advertencias": [f"Historial {side} no disponible: {(obj or {}).get('error') if isinstance(obj, dict) else 'ausente'}"]}
    raw_fixtures = obj.get("detailed_fixtures") or []
    if not isinstance(raw_fixtures, list):
        raise ValueError(f"Historial {side}: detailed_fixtures no es lista")
    valid: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    ids = set()
    for row in raw_fixtures:
        f = row.get("fixture") or {}
        fid = safe_int(f.get("id"))
        reason = None
        if not fid:
            reason = "SIN_FIXTURE_ID"
        elif fid in ids:
            reason = "FIXTURE_DUPLICADO"
        else:
            ids.add(fid)
        try:
            dt = date_utc(f.get("date"))
        except (ValueError, TypeError):
            dt = None
            reason = reason or "FECHA_INVALIDA"
        if dt and dt >= cutoff:
            reason = reason or "FUERA_DE_CUTOFF"
        if (f.get("status") or {}).get("short") not in COMPLETED:
            reason = reason or "PARTIDO_NO_FINALIZADO"
        home_id = safe_int(((row.get("teams") or {}).get("home") or {}).get("id"))
        away_id = safe_int(((row.get("teams") or {}).get("away") or {}).get("id"))
        if home_id == away_id or team_id not in (home_id, away_id):
            reason = reason or "IDENTIDAD_INCOMPATIBLE"
        if reason:
            rejects.append({"fixture_id": fid, "motivo": reason})
        else:
            valid.append(row)

    # Cobertura por métrica: no confundir campo ausente con un cero explícito.
    stats = {k: {"propia": 0, "rival": 0, "ambas": 0, "ceros_propios": 0} for k in STAT_KEYS}
    other_keys: Counter[str] = Counter()
    venues: Counter[str] = Counter()
    coaches: Counter[str] = Counter()
    formations: Counter[str] = Counter()
    quality: Counter[str] = Counter()
    player_fields: Counter[str] = Counter()
    appearances: dict[int, dict[str, Any]] = {}
    match_rows = []
    for item in valid:
        f = item["fixture"]
        home_id = safe_int(item["teams"]["home"]["id"])
        away_id = safe_int(item["teams"]["away"]["id"])
        is_home = home_id == team_id
        opp = away_id if is_home else home_id
        venue = "home" if is_home else "away"
        venues[venue] += 1
        ours = team_stat_block(item, team_id)
        theirs = team_stat_block(item, opp)
        other_keys.update(x for x in ours if x not in STAT_KEYS)
        for metric in STAT_KEYS:
            a = metric in ours
            b = metric in theirs
            stats[metric]["propia"] += int(a)
            stats[metric]["rival"] += int(b)
            stats[metric]["ambas"] += int(a and b)
            stats[metric]["ceros_propios"] += int(a and ours[metric] == 0)
        if ours and theirs:
            quality["estadisticas_ambos"] += 1
        own_xi, opp_xi = lineup_for(item, team_id), lineup_for(item, opp)
        own_start = own_xi.get("startXI") or []
        opp_start = opp_xi.get("startXI") or []
        if len(own_start) == len(opp_start) == 11:
            quality["xi_ambos_11"] += 1
        coach = own_xi.get("coach") or {}
        coach_id = safe_int(coach.get("id"))
        coach_key = str(coach_id) if coach_id else "NO_DISPONIBLE"
        coaches[coach_key] += 1
        formation = own_xi.get("formation") or "NO_DISPONIBLE"
        formations[formation] += 1
        player_rows = players_for(item, team_id)
        if player_rows:
            quality["jugadores_listados"] += 1
        starters = {safe_int((x.get("player") or {}).get("id")) for x in own_start}
        for pr in player_rows:
            player = pr.get("player") or {}
            pid = safe_int(player.get("id"))
            if not pid:
                continue
            game_stat = (pr.get("statistics") or [{}])[0]
            minutes = number(nested(game_stat, ("games", "minutes")))
            if minutes is None or minutes <= 0:
                continue
            entry = appearances.setdefault(pid, {"id": pid, "nombre": player.get("name"),
                                                 "partidos": 0, "minutos": 0, "titularidades": 0,
                                                 "cobertura": {k: 0 for k in PLAYER_FIELDS}})
            entry["partidos"] += 1
            entry["minutos"] += minutes
            entry["titularidades"] += int(pid in starters)
            for name, path in PLAYER_FIELDS.items():
                if number(nested(game_stat, path)) is not None:
                    entry["cobertura"][name] += 1
                    player_fields[name] += 1
        if item.get("events"):
            quality["eventos_cronologicos"] += 1
        score = item.get("score") or {}
        halftime = score.get("halftime") or {}
        fulltime = score.get("fulltime") or {}
        if halftime.get("home") is not None and halftime.get("away") is not None:
            quality["marcador_descanso"] += 1
        if fulltime.get("home") is not None and fulltime.get("away") is not None:
            quality["marcador_final"] += 1
        match_rows.append({"fixture_id": f.get("id"), "fecha_utc": f.get("date"),
                           "localia": venue, "rival_id": opp, "entrenador_id": coach_id,
                           "entrenador_nombre_observado": coach.get("name"), "formacion": formation,
                           "estadisticas_propias": len(ours), "estadisticas_rival": len(theirs),
                           "jugadores_registrados": len(player_rows),
                           "alineacion_propia": len(own_start), "alineacion_rival": len(opp_start)})
    match_rows.sort(key=lambda x: x["fecha_utc"], reverse=True)
    eligible = len(valid)
    if eligible < 10:
        warnings.append(f"MUESTRA_CORTA: {eligible} partidos; no inferir estabilidad por frecuencia sin incertidumbre.")
    if eligible and venues.get("home", 0) < 3:
        warnings.append("LOCALIA_HOME_ESCASA: menos de 3 encuentros de local en el historial.")
    if eligible and venues.get("away", 0) < 3:
        warnings.append("LOCALIA_AWAY_ESCASA: menos de 3 encuentros de visitante en el historial.")
    if len([k for k in coaches if k != "NO_DISPONIBLE"]) > 1:
        warnings.append("MULTIPLES_ENTRENADORES: separar períodos por ID antes de calcular tendencias.")
    if rejects:
        warnings.append(f"REGISTROS_EXCLUIDOS: {len(rejects)} por integridad/temporalidad.")
    return {
        "equipo_id": team_id, "limite_solicitado": obj.get("requested_limit"),
        "partidos_basicos": obj.get("basic_fixtures_count"),
        "partidos_recibidos": len(raw_fixtures), "partidos_elegibles": eligible,
        "partidos_excluidos": rejects, "localia": dict(venues),
        "entrenadores_por_id": dict(coaches), "formaciones": dict(formations),
        "cobertura_partidos": dict(quality), "metricas_colectivas": stats,
        "otras_metricas_observadas": dict(other_keys),
        "jugadores_con_minutos": len(appearances), "cobertura_campos_jugador": dict(player_fields),
        "jugadores_mayor_exposicion": sorted(appearances.values(), key=lambda x: (-x["minutos"], -x["partidos"]))[:5],
        "partidos": match_rows, "advertencias": warnings,
    }


def make_report(context: Path, identity_path: Path | None, manifest_path: Path | None,
                explicit_event: str | None) -> dict[str, Any]:
    payload = read_json(context)
    match = payload.get("match") or {}
    fixture = payload.get("fixture") or {}
    event = str(match.get("event_id") or "")
    if not re.fullmatch(r"\d{6,}", event) or (explicit_event and event != explicit_event):
        raise ValueError(f"Event ID del JSON inválido o contradice el parámetro: {event}")
    if not isinstance(fixture.get("fixture_id"), int) or fixture["fixture_id"] <= 0:
        raise ValueError("Fixture ID inválido")
    if not fixture.get("home_id") or not fixture.get("away_id") or fixture["home_id"] == fixture["away_id"]:
        raise ValueError("Fixture sin IDs de equipos válidos")
    cutoff = date_utc(payload.get("generated_at"))
    kickoff = date_utc(fixture.get("date"))
    ctx = payload.get("context") or {}
    if (ctx.get("fixture") or {}).get("fixture_id") != fixture["fixture_id"]:
        raise ValueError("Contexto.fixture difiere del fixture raíz")
    meta_at = date_utc((ctx.get("meta") or {}).get("generated_at"))
    if abs((cutoff - meta_at).total_seconds()) > 30:
        raise ValueError("Timestamps de generación raíz/contexto separados más de 30 segundos")
    integrity: dict[str, Any] = {"context_json_sha256": checksum(context),
                                  "identidad": "NO_SUMINISTRADA", "manifiesto": "NO_SUMINISTRADO"}
    if identity_path:
        identity = read_json(identity_path)
        identity_check(payload, identity, event)
        integrity["identidad"] = "COINCIDE"
        integrity["identity_sha256"] = checksum(identity_path)
    if manifest_path:
        manifest = read_json(manifest_path)
        if str(manifest.get("event_id")) != event or manifest.get("url") != match.get("url"):
            raise ValueError("Manifiesto de otro Event ID o URL")
        entries = manifest.get("inputs") or {}
        csum = (entries.get("context_json") or {}).get("sha256")
        if csum != integrity["context_json_sha256"]:
            raise ValueError("SHA-256 del JSON no coincide con el manifest")
        if identity_path:
            isum = (entries.get("identity") or {}).get("sha256")
            if isum != integrity["identity_sha256"]:
                raise ValueError("SHA-256 de identidad no coincide con el manifest")
        integrity["manifiesto"] = "COINCIDE"
        integrity["estado_en_manifest"] = manifest.get("status")
        integrity["historico_en_manifest"] = bool(manifest.get("historical"))
    if not identity_path:
        raise ValueError("Se requiere --identity o identidad canónica en inputs/ para auditar este evento")

    # Si generated_at >= kickoff, no se puede certificar que sea una captura PREMATCH.
    safe_snapshot = cutoff < kickoff
    warnings: list[str] = []
    if not safe_snapshot:
        warnings.append("SNAPSHOT_POST_KICKOFF: datos potencialmente posteriores al inicio; solo auditoría, no prematch.")
    if (ctx.get("meta") or {}).get("history_limit") != (ctx.get("home_history_deep") or {}).get("requested_limit"):
        warnings.append("HISTORY_LIMIT_INCONSISTENTE: meta y home_history_deep no coinciden")
    sides = {}
    for side in ("home", "away"):
        key = f"{side}_history_deep"
        sides[side] = analyze_history(side, ctx.get(key), int(fixture[f"{side}_id"]), cutoff)
    h2h = response_count(ctx.get("h2h"))
    if h2h["estado"] == "CON_DATOS":
        dated = ctx["h2h"].get("response") or []
        eligible = 0
        for row in dated:
            try:
                if date_utc((row.get("fixture") or {}).get("date")) < cutoff and (row.get("fixture", {}).get("status") or {}).get("short") in COMPLETED:
                    eligible += 1
            except (ValueError, TypeError):
                pass
        h2h["elegibles_antes_de_captura"] = eligible
        if eligible != len(dated):
            warnings.append("H2H_NO_ELEGIBLES: algunos enfrentamientos requieren exclusión temporal.")
    ref_obj = ctx.get("referee_history") or {}
    referee = {"disponible": bool(ref_obj.get("available")),
               "partidos_reportados": len(ref_obj.get("matches") or []),
               "cobertura_reportada": ref_obj.get("coverage") or {}}
    target = {}
    for key in ("target_fixture_full", "lineups", "statistics", "standings", "predictions", "injuries", "api_odds"):
        target[key] = response_count(ctx.get(key))
    target["h2h"] = h2h
    target["referee_history"] = referee
    for side in ("home", "away"):
        target[f"{side}_squads"] = response_count((ctx.get("squads") or {}).get(side))
        target[f"{side}_coachs"] = response_count((ctx.get("coachs") or {}).get(side))
    target_rows = (ctx.get("target_fixture_full") or {}).get("response") or []
    if target_rows:
        full = target_rows[0]
        full_fx = full.get("fixture") or {}
        full_teams = full.get("teams") or {}
        if (full_fx.get("id") != fixture["fixture_id"] or
            safe_int(((full_teams.get("home") or {}).get("id"))) != fixture["home_id"] or
            safe_int(((full_teams.get("away") or {}).get("id"))) != fixture["away_id"]):
            raise ValueError("Fixture objetivo en respuesta completa contradice identidad principal")
        status = (full_fx.get("status") or {}).get("short")
        target["target_fixture_full"]["estado_fixture_en_captura"] = status
        if safe_snapshot and status not in {"NS", "TBD", "PST", "SUSP"}:
            warnings.append("ESTADO_OBJETIVO_NO_PREMATCH: el estado retornado por la API no es previo al kickoff.")
    if target["lineups"]["registros"] == 0:
        warnings.append("XI_OBJETIVO_NO_DISPONIBLE_EN_API: cero registros en fixtures/lineups del snapshot.")
    # El árbitro se audita con el mismo corte temporal: el contador reportado
    # por la fuente puede contener registros que no se deben utilizar.
    referee["partidos_elegibles"] = 0
    for ref_match in ref_obj.get("matches") or []:
        try:
            if date_utc(ref_match.get("date")) < cutoff:
                referee["partidos_elegibles"] += 1
        except (ValueError, TypeError):
            pass
    if referee["partidos_elegibles"] != referee["partidos_reportados"]:
        warnings.append("ARBITRO_FUERA_DE_CUTOFF: parte del historial arbitral no es temporalmente elegible.")
    # La cobertura del proveedor se audita, no implica recomendaciones automáticas.
    return {
        "schema_version": SCHEMA_VERSION, "event_id": event,
        "fixture_id": fixture["fixture_id"],
        "partido": f"{fixture.get('home_name')} vs {fixture.get('away_name')}",
        "snapshot": {"generated_at_utc": cutoff.isoformat(), "kickoff_utc": kickoff.isoformat(),
                     "antes_del_kickoff": safe_snapshot,
                     "minutos_antes_kickoff": round((kickoff-cutoff).total_seconds()/60, 2)},
        "integridad": integrity, "cobertura_fuentes": target,
        "equipos": sides, "advertencias": warnings,
        "interpretacion": "Auditoría descriptiva de disponibilidad, no probabilidades calibradas, picks ni valoración de apuestas.",
    }


def render_md(r: dict[str, Any]) -> str:
    snap = r["snapshot"]
    lines = [f"# Auditoría de cobertura · Evento {r['event_id']}", "",
             f"**Partido:** {r['partido']} · **Fixture:** {r['fixture_id']}",
             f"**Generación UTC:** {snap['generated_at_utc']}",
             f"**Kickoff UTC:** {snap['kickoff_utc']}",
             f"**Snapshot anterior al kickoff:** {'SÍ' if snap['antes_del_kickoff'] else 'NO'}",
             f"**Antelación registrada:** {snap['minutos_antes_kickoff']} min", "",
             "## Verificaciones de integridad", "",
             f"- Identidad: {r['integridad']['identidad']}",
             f"- Manifiesto: {r['integridad']['manifiesto']}",
             f"- Estado del manifiesto: {r['integridad'].get('estado_en_manifest','N/D')}", "",
             "## Históricos disponibles", "",
             "| Equipo | Recibidos | Elegibles | Local/Visitante | Estadísticas de ambos | XI completos | Jugadores |",
             "|---|---:|---:|---|---:|---:|---:|",]
    for side, label in (("home", "LOCAL"), ("away", "VISITANTE")):
        t = r["equipos"][side]
        v = t.get("localia") or {}
        q = t.get("cobertura_partidos") or {}
        lines.append(f"| {label} (ID {t['equipo_id']}) | {t['partidos_recibidos']} | {t['partidos_elegibles']} | "
                     f"{v.get('home',0)}/{v.get('away',0)} | {q.get('estadisticas_ambos',0)} | "
                     f"{q.get('xi_ambos_11',0)} | {t.get('jugadores_con_minutos',0)} |")
    lines.extend(["", "## Cobertura de métricas colectivas", "",
                  "*Celdas: partidos con dato propio / partidos con dato rival; un cero explícito cuenta como observado.*", "",
                  "| Métrica | Local (propio/rival) | Visitante (propio/rival) |",
                  "|---|---:|---:|"])
    for metric in STAT_KEYS:
        values = []
        for side in ("home", "away"):
            m = (r["equipos"][side].get("metricas_colectivas") or {}).get(metric, {})
            values.append(f"{m.get('propia',0)}/{m.get('rival',0)}")
        lines.append(f"| {metric} | {values[0]} | {values[1]} |")
    lines.extend(["", "## Fuentes complementarias", "",
                  "| Recurso | Estado | Registros |", "|---|---|---:|"])
    for key, obj in r["cobertura_fuentes"].items():
        if "estado" in obj:
            lines.append(f"| {key} | {obj['estado']} | {obj['registros']} |")
        elif key == "referee_history":
            lines.append(f"| {key} | {'CON_DATOS' if obj['disponible'] else 'NO_DISPONIBLE'} | {obj['partidos_reportados']} |")
    lines.extend(["", "## Entrenadores observados por ID", ""])
    for side, label in (("home", "LOCAL"), ("away", "VISITANTE")):
        t = r["equipos"][side]
        lines.append(f"- {label}: {t.get('entrenadores_por_id') or {}}; formaciones: {t.get('formaciones') or {}}.")
    lines.extend(["", "## Advertencias", ""])
    notes = list(r["advertencias"])
    for side, label in (("home", "LOCAL"), ("away", "VISITANTE")):
        notes.extend(f"{label}: {warning}" for warning in r["equipos"][side].get("advertencias", []))
    if not notes:
        notes.append("Sin incidencias detectadas por las comprobaciones implementadas.")
    lines.extend(f"- {x}" for x in notes)
    lines.extend(["", "## Alcance", "",
                  "El detector solo examina el snapshot suministrado. No comprueba fuentes externas, "
                  "no certifica que un dato del proveedor sea correcto y no convierte frecuencias en probabilidades.",
                  "La fecha de generación del JSON no prueba por sí sola la cronología de cada dato externo.",
                  "No reconstruye córners o tiros por intervalos a partir de eventos cuando el proveedor no los incluye.", ""])
    return "\n".join(lines)


def cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--event-id", help="Bwin Event ID (solo un partido)")
    p.add_argument("--context-json", type=Path)
    p.add_argument("--identity", type=Path)
    p.add_argument("--manifest", type=Path)
    p.add_argument("--output-dir", type=Path)
    args = p.parse_args()
    if not args.event_id and not args.context_json:
        p.error("Se necesita --event-id o --context-json")
    if args.event_id and not re.fullmatch(r"\d{6,}", args.event_id):
        p.error("--event-id debe contener 6 o más dígitos")
    root = args.root.expanduser().resolve()
    folder = root / "outputs" / "matches" / args.event_id if args.event_id else None
    context = args.context_json or (folder / "inputs" / f"match_engine_{args.event_id}.json")
    context = context.expanduser().resolve()
    if not context.is_file():
        p.error(f"No existe JSON: {context}")
    # Si se pasa solo un JSON, buscar identidad junto a él y luego manifest
    # bajo el directorio del evento; las rutas del manifest no se ejecutan.
    if folder is None:
        folder = context.parent.parent if context.parent.name == "inputs" else None
    identity = args.identity or (context.parent / "match_identity.json")
    if not identity.is_file():
        p.error(f"Identidad canónica requerida: {identity}")
    manifest = args.manifest or (folder / "manifest.json" if folder else None)
    if manifest is not None and not manifest.is_file():
        if args.manifest:
            p.error(f"No existe manifiesto: {manifest}")
        manifest = None
    try:
        r = make_report(context, identity, manifest, args.event_id)
        output = args.output_dir or ((folder / "coverage") if folder else context.parent / "coverage")
        output.mkdir(parents=True, exist_ok=True)
        event = r["event_id"]
        json_file = output / f"cobertura_{event}.json"
        md_file = output / f"cobertura_{event}.md"
        json_file.write_text(json.dumps(r, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_file.write_text(render_md(r), encoding="utf-8")
        print(f"[OK] Event ID {event} · fixture {r['fixture_id']}")
        print(f"[OK] Snapshot prematch: {r['snapshot']['antes_del_kickoff']}")
        for side in ("home", "away"):
            t = r["equipos"][side]
            print(f"[OK] {side}: {t['partidos_elegibles']}/{t['partidos_recibidos']} partidos históricos elegibles")
        print(f"[OK] JSON: {json_file}")
        print(f"[OK] MD: {md_file}")
        return 0
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Auditoría abortada: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
