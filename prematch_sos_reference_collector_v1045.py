#!/usr/bin/env python3
"""PREMATCH Fase 2.3 — referencias históricas incrementales para SOS.

Este módulo es deliberadamente independiente del scraper, del generador de
contexto y del motor probabilístico. Lee el rendimiento congelado de Fase 2.1,
consulta únicamente históricos de los rivales que carecen de referencia y
escribe artefactos v1.0.4.5 nuevos. Los snapshots originales nunca se editan.

Las referencias se guardan con todas las competiciones/temporadas que devuelve
API-Football. ``prematch_sos_v1044`` las cruza de forma estricta por
``league_id`` + ``season`` para no mezclar competiciones silenciosamente.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import prematch_sos_v1044 as sos
try:
    from config.constants import API_KEY as CONFIGURED_FOOTBALL_API_KEY
except ImportError:  # pragma: no cover
    CONFIGURED_FOOTBALL_API_KEY = ""

try:
    from prematch_depth_collector import ApiClient, BudgetExhausted
except ImportError:  # pragma: no cover - permite importar el esquema aislado
    ApiClient = None  # type: ignore[assignment]

    class BudgetExhausted(RuntimeError):
        pass


SCHEMA = "prematch-sos-reference-pool-1.0.4.5"
POOL_SCHEMA = "prematch-sos-reference-pool-1.0"
VERSION = "1.0.4.5"
COMPLETION_BUFFER = timedelta(hours=3)
FINISHED = {"FT", "AET", "PEN"}
METRIC_NAMES = ("shots", "on_target", "corners", "xg")


def parse_time(value: Any) -> datetime | None:
    return sos.parse_time(value)


def dump_json(path: Path, data: Any) -> str:
    blob = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Escritura atómica: una interrupción no puede dejar un pool truncado.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
    return hashlib.sha256(blob).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError(f"JSON raíz no es objeto: {path}")
    return obj


def _positive_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _finite_number(value: Any) -> float | None:
    return sos.number(value)


def _stat_map(raw: Any) -> dict[str, float | None]:
    """Normaliza las etiquetas variables de API-Football sin convertir null a 0."""
    result: dict[str, float | None] = {key: None for key in METRIC_NAMES}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("type") or item.get("name") or "").strip().lower()
        value = item.get("value")
        if isinstance(value, str):
            value = value.rstrip("%").replace(",", ".")
            try:
                value = float(value)
            except ValueError:
                value = None
        if "total shots" in label or label in {"shots", "shots total"}:
            result["shots"] = _finite_number(value)
        elif "shots on goal" in label or "shots on target" in label or label in {"on target", "on_target"}:
            result["on_target"] = _finite_number(value)
        elif "corner" in label:
            result["corners"] = _finite_number(value)
        elif label in {"expected goals", "expected_goals", "xg"} or label.startswith("expected goals"):
            result["xg"] = _finite_number(value)
    return result


def _goals(record: dict[str, Any], side: str) -> int | float | None:
    value = ((record.get("goals") or {}).get(side))
    return _finite_number(value)


def _fixture_is_finished_before(record: dict[str, Any], *, target_fixture: int,
                                cutoff: datetime) -> bool:
    fixture = record.get("fixture") or {}
    fid = _positive_id(fixture.get("id"))
    dt = parse_time(fixture.get("date"))
    status = (fixture.get("status") or {}).get("short")
    # El endpoint no garantiza que las métricas de AET/PEN sean 90 minutos.
    # La Fase 2.3 incorpora solo FT; esos estados quedan auditados como no
    # utilizables y nunca se mezclan silenciosamente en el baseline.
    if not fid or fid == target_fixture or dt is None or status != "FT":
        return False
    return dt + COMPLETION_BUFFER < cutoff


def _extract_statistics(rows: list[dict[str, Any]], team_id: int) -> dict[int, dict[str, float | None]]:
    out: dict[int, dict[str, float | None]] = {}
    for row in rows:
        team = (row.get("team") or {}).get("id")
        if team != team_id:
            continue
        out[team_id] = _stat_map(row.get("statistics"))
    return out


def canonical_game(record: dict[str, Any], *, tracked_team_id: int,
                   source: str, source_query: str,
                   reference_scopes: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    fixture = record.get("fixture") or {}
    teams = record.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    fid = _positive_id(fixture.get("id"))
    kickoff = parse_time(fixture.get("date"))
    home_id, away_id = _positive_id(home.get("id")), _positive_id(away.get("id"))
    league = record.get("league") or {}
    league_id, season = _positive_id(league.get("id")), league.get("season")
    if not (fid and kickoff and home_id and away_id and home_id != away_id and
            league_id and isinstance(season, int) and tracked_team_id in (home_id, away_id)):
        return None
    if (fixture.get("status") or {}).get("short") != "FT":
        return None
    venue = "HOME" if tracked_team_id == home_id else "AWAY"
    opponent = away if venue == "HOME" else home
    own = (record.get("_stats_by_team") or {}).get(str(tracked_team_id)) or {}
    conceded = (record.get("_stats_by_team") or {}).get(str(_positive_id(opponent.get("id")) or 0)) or {}
    own_stats = {key: own.get(key) for key in METRIC_NAMES}
    conceded_stats = {key: conceded.get(key) for key in METRIC_NAMES}
    goals_home = _goals(record, "home")
    goals_away = _goals(record, "away")
    home_stats = {**(own_stats if venue == "HOME" else conceded_stats), "goals": goals_home}
    away_stats = {**(conceded_stats if venue == "HOME" else own_stats), "goals": goals_away}
    return {
        "fixture_id": fid,
        "kickoff_utc": kickoff.isoformat(),
        "league_id": league_id,
        "season": season,
        "status": fixture.get("status", {}).get("short"),
        "venue": venue,
        "opponent": {"id": _positive_id(opponent.get("id")), "name": opponent.get("name")},
        "home": {"id": home_id, "stats": home_stats},
        "away": {"id": away_id, "stats": away_stats},
        "own": own_stats,
        "conceded": conceded_stats,
        "goals_for": goals_home if venue == "HOME" else goals_away,
        "goals_against": goals_away if venue == "HOME" else goals_home,
        "provenance": {
            "source": source,
            "source_query": source_query,
            "tracked_team_id": tracked_team_id,
            "competition": {"id": league_id, "name": league.get("name"),
                            "country": league.get("country"), "season": season},
            "kickoff_utc": kickoff.isoformat(),
            "reference_scopes": reference_scopes or record.get("_matched_scopes") or [],
            "stats_observed": {key: own_stats[key] is not None or conceded_stats[key] is not None
                                for key in METRIC_NAMES},
        },
    }


class DryRunClient:
    """Cliente inerte para pilotos: no exige credenciales ni realiza red."""

    def __init__(self) -> None:
        self.stat = type("Stats", (), {"calls": 0, "hits": 0, "max_calls": 0})()

    def get(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        return []


def _load_target(root: Path, event_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    folder = root / "outputs" / "matches" / event_id / "analysis" / "deep_v1042"
    path = folder / f"rendimiento_profundo_{event_id}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    performance = read_json(path)
    cutoff = parse_time(performance.get("analysis_cutoff_utc"))
    kickoff = parse_time(performance.get("target_kickoff_utc"))
    target = _positive_id(performance.get("target_fixture_id"))
    if not (cutoff and kickoff and target and cutoff < kickoff):
        raise ValueError("TARGET_CUTOFF_INVALID")
    return folder, performance, {"cutoff": cutoff, "kickoff": kickoff, "target_fixture": target}


def _existing_pool(path: Path, *, target_fixture: int, cutoff: datetime) -> dict[int, dict[str, Any]]:
    if not path.is_file():
        return {}
    obj = read_json(path)
    games = obj.get("games") or []
    retained: dict[int, dict[str, Any]] = {}
    for game in games:
        fid = _positive_id(game.get("fixture_id"))
        dt = parse_time(game.get("kickoff_utc"))
        if not (fid and dt and fid != target_fixture and dt + COMPLETION_BUFFER < cutoff):
            raise ValueError(f"ENRICHED_POOL_INVALID_OR_FUTURE_FIXTURE_{fid}")
        # SOS valida el registro completo más tarde; conserva nulls métricos.
        if fid in retained:
            previous = retained[fid]
            identity = (game.get("home", {}).get("id"), game.get("away", {}).get("id"),
                        game.get("kickoff_utc"), game.get("league_id"), game.get("season"))
            previous_identity = (previous.get("home", {}).get("id"), previous.get("away", {}).get("id"),
                                 previous.get("kickoff_utc"), previous.get("league_id"), previous.get("season"))
            if identity != previous_identity:
                raise ValueError(f"ENRICHED_POOL_CONFLICT_{fid}")
            continue
        retained[fid] = game
    return retained


def _seed_target_history(performance: dict[str, Any], *, target_fixture: int,
                         cutoff: datetime) -> dict[int, dict[str, Any]]:
    retained: dict[int, dict[str, Any]] = {}
    for side in ("home", "away"):
        team = (performance.get("teams") or {}).get(side) or {}
        tid = _positive_id(team.get("team_id"))
        if not tid:
            continue
        for row in team.get("historical_fixtures") or []:
            try:
                canonical = sos.normalize_record({**row, "_tracked_team_id": tid})
            except (TypeError, ValueError):
                continue
            fid = canonical["fixture_id"]
            dt = parse_time(canonical["kickoff_utc"])
            if fid == target_fixture or dt is None or dt + COMPLETION_BUFFER >= cutoff:
                continue
            # normalize_record is already the SOS schema; add provenance.
            canonical["provenance"] = {"source": "TARGET_PERFORMANCE_SNAPSHOT",
                                        "tracked_team_id": tid}
            prior = retained.get(fid)
            if prior is not None and (prior["home"]["id"], prior["away"]["id"], prior["kickoff_utc"]) != (
                    canonical["home"]["id"], canonical["away"]["id"], canonical["kickoff_utc"]):
                raise ValueError(f"REFERENCE_FIXTURE_CONFLICT_{fid}")
            retained[fid] = canonical
    return retained


def _gap_rivals(adjustment: dict[str, Any], performance: dict[str, Any], *, pilot: bool) -> list[dict[str, Any]]:
    histories: dict[int, dict[str, list[tuple[str, int]]]] = {}
    for side in ("home", "away"):
        team = (performance.get("teams") or {}).get(side) or {}
        rows_by_venue = {"HOME": [], "AWAY": []}
        for row in team.get("historical_fixtures") or []:
            fid = _positive_id(row.get("fixture_id"))
            dt = parse_time(row.get("kickoff_utc"))
            oid = _positive_id((row.get("opponent") or {}).get("id"))
            if fid and dt and oid:
                venue = row.get("venue")
                if venue in rows_by_venue:
                    # Kickoff is the primary key. Fixture ID is only a stable
                    # tie-breaker and must never define recency.
                    rows_by_venue[venue].append((dt.isoformat(), fid))
        histories[_positive_id(team.get("team_id")) or 0] = {
            venue: sorted(rows, key=lambda x: (x[0], x[1]), reverse=True)
            for venue, rows in rows_by_venue.items()
        }
    # Una consulta /fixtures?team=RIVAL&last=99 sirve para todas las
    # competiciones y temporadas de ese rival. Los alcances se conservan como
    # metadatos, pero no generan llamadas duplicadas.
    candidates: dict[int, dict[str, Any]] = {}
    for side in ("home", "away"):
        for gap in ((adjustment.get("teams") or {}).get(side, {}).get("reference_gaps_shots_for") or []):
            oid = _positive_id(gap.get("opponent_id"))
            if not oid:
                continue
            affected = [_positive_id(x) for x in gap.get("affected_fixture_ids") or []]
            affected = [x for x in affected if x]
            team_id = _positive_id(((performance.get("teams") or {}).get(side) or {}).get("team_id")) or 0
            if oid not in candidates:
                candidates[oid] = {"team_id": team_id, "opponent_id": oid,
                                   "scopes": [], "affected_fixture_ids": [],
                                   "team_ids": [], "sides": []}
            item = candidates[oid]
            item["scopes"].append({"league_id": gap.get("league_id"),
                                   "season": gap.get("season"),
                                   "team_venue": gap.get("team_venue"),
                                   "affected_fixture_ids": affected})
            item["affected_fixture_ids"] = sorted(set(item["affected_fixture_ids"]) | set(affected))
            if team_id not in item["team_ids"]:
                item["team_ids"].append(team_id)
            if side not in item["sides"]:
                item["sides"].append(side)
    values = list(candidates.values())
    # Prioridad estimada por impacto en L5/L10/L20; el piloto solo restringe
    # el conjunto a rivales con impacto L5 reciente.
    for item in values:
        hits = {"L5": 0, "L10": 0, "L20": 0, "venues": []}
        affected = set(item["affected_fixture_ids"])
        for team_id in item["team_ids"]:
            by_venue = histories.get(team_id, {})
            # Pilot L5/L10/L20 uses the global chronological history. Venue
            # splits remain available only as diagnostic context for SOS.
            global_rows = sorted(
                [entry for rows in by_venue.values() for entry in rows],
                key=lambda x: (x[0], x[1]), reverse=True)
            global_ids = [fid for _, fid in global_rows]
            for n in (5, 10, 20):
                hits[f"L{n}"] += len(affected & set(global_ids[:n]))
            for venue, rows in by_venue.items():
                ids = [fid for _, fid in rows]
                if affected & set(ids[:5]):
                    hits["venues"].append({"team_id": team_id, "venue": venue})
        item["priority"] = hits
    # A rival with recent L5 impact is more valuable than one that only fills
    # an L20 gap. Cost is a later tie-breaker, not a reason to starve others.
    selected = [item for item in values if item["priority"]["L5"] > 0] if pilot else values
    return sorted(selected, key=lambda x: (
        -x["priority"]["L5"], -x["priority"]["L10"], -x["priority"]["L20"],
        x["opponent_id"]))


def _affected_history_index(performance: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    index: dict[int, list[dict[str, Any]]] = {}
    for side in ("home", "away"):
        for row in (((performance.get("teams") or {}).get(side) or {}).get("historical_fixtures") or []):
            fid = _positive_id(row.get("fixture_id"))
            dt = parse_time(row.get("kickoff_utc"))
            if fid and dt:
                index.setdefault(fid, []).append({
                    "kickoff": dt,
                    "league_id": row.get("league_id"),
                    "season": row.get("season"),
                    "venue": row.get("venue"),
                    "side": side,
                })
    return index


def _select_reference_rows(history: list[dict[str, Any]], rival: dict[str, Any],
                           affected_index: dict[int, list[dict[str, Any]]],
                           max_fixtures: int) -> list[dict[str, Any]]:
    """Selecciona solo referencias útiles para los históricos afectados.

    Cada candidato debe compartir liga, temporada y localía del rival con al
    menos un gap, y terminar tres horas antes del kickoff de ese histórico.
    Por ello no basta con tomar los 20 partidos más recientes del adversario.
    """
    rid = rival["opponent_id"]
    candidates: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for raw in history:
        fid = _positive_id((raw.get("fixture") or {}).get("id"))
        dt = parse_time((raw.get("fixture") or {}).get("date"))
        if not fid or not dt:
            continue
        teams = raw.get("teams") or {}
        home_id = _positive_id((teams.get("home") or {}).get("id"))
        away_id = _positive_id((teams.get("away") or {}).get("id"))
        candidate_venue = "HOME" if rid == home_id else "AWAY" if rid == away_id else None
        if candidate_venue is None:
            continue
        league = raw.get("league") or {}
        matched: list[dict[str, Any]] = []
        for scope in rival.get("scopes") or []:
            if _positive_id(league.get("id")) != _positive_id(scope.get("league_id")):
                continue
            if league.get("season") != scope.get("season"):
                continue
            # team_venue is the localía del equipo analizado. The rival is in
            # the opposite venue in the reference fixture, matching the
            # opponent_venue convention used by sos.prior_reference().
            expected_rival_venue = {
                "HOME": "AWAY", "AWAY": "HOME",
            }.get(scope.get("team_venue"))
            if expected_rival_venue and candidate_venue != expected_rival_venue:
                continue
            for affected_id in scope.get("affected_fixture_ids") or []:
                for affected in affected_index.get(affected_id, []):
                    if dt + COMPLETION_BUFFER < affected["kickoff"]:
                        matched.append({"affected_fixture_id": affected_id,
                                        "affected_kickoff_utc": affected["kickoff"].isoformat(),
                                        "league_id": scope.get("league_id"),
                                        "season": scope.get("season"),
                                        "rival_venue": candidate_venue})
        if matched:
            candidates[fid] = (raw, matched)
    ranked = sorted(candidates.values(), key=lambda pair: (
        len(pair[1]),
        (pair[0].get("fixture") or {}).get("date") or "",
        (pair[0].get("fixture") or {}).get("id") or 0), reverse=True)
    chosen: list[dict[str, Any]] = []
    for raw, matched in ranked[:max_fixtures]:
        copy = dict(raw)
        copy["_matched_scopes"] = matched
        chosen.append(copy)
    return chosen


def _usable_stats(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        values = _stat_map(row.get("statistics"))
        if any(value is not None for value in values.values()):
            return True
    return False


def _stats_for_fixture(client: Any, record: dict[str, Any], cache: dict[int, list[dict[str, Any]]],
                       *, dry_run: bool, query_log: list[dict[str, Any]],
                       max_empty_retries: int = 1) -> bool:
    fid = _positive_id((record.get("fixture") or {}).get("id"))
    if not fid or record.get("_stats_by_team"):
        return False
    if dry_run:
        record["_stats_by_team"] = {}
        query_log.append({"endpoint": "fixtures/statistics", "fixture_id": fid, "status": "DRY_RUN"})
        return False
    if fid in cache:
        response = cache[fid]
        query_log.append({"endpoint": "fixtures/statistics", "fixture_id": fid, "status": "CACHE_OR_RESUME"})
    else:
        response = client.get("fixtures/statistics", fixture=fid)
        # Una respuesta vacía no se convierte en una observación válida. Se
        # permite un único reintento de endpoint; API-Client conserva además
        # sus reintentos acotados para errores de transporte/HTTP.
        attempts = 0
        while not _usable_stats(response) and attempts < max_empty_retries:
            attempts += 1
            response = client.get("fixtures/statistics", fixture=fid)
            query_log.append({"endpoint": "fixtures/statistics", "fixture_id": fid,
                              "status": "RETRY_EMPTY", "attempt": attempts + 1})
        cache[fid] = response
        query_log.append({"endpoint": "fixtures/statistics", "fixture_id": fid, "status": "FETCHED"})
    by_team: dict[str, dict[str, float | None]] = {}
    for item in response:
        tid = _positive_id((item.get("team") or {}).get("id"))
        if tid:
            by_team[str(tid)] = _stat_map(item.get("statistics"))
    record["_stats_by_team"] = by_team
    record["_stats_usable"] = _usable_stats(response)
    return bool(record["_stats_usable"])


def collect(*, root: Path, event_id: str, client: Any | None = None,
            dry_run: bool = False, max_api_calls: int = 40,
            resume: bool = False, pilot: bool = False,
            max_fixtures_per_rival: int = 20,
            now: datetime | None = None) -> dict[str, Any]:
    folder, performance, target_meta = _load_target(root, event_id)
    adjustment_path = folder / f"ajuste_rivales_{event_id}.json"
    if not adjustment_path.is_file():
        try:
            # La interfaz Fase 2.2 genera el SOS inicial offline usando el
            # snapshot, identidad y auditoría ya existentes.
            sos.run(event_id, root)
        except Exception as exc:
            raise RuntimeError(
                f"SOS_INITIAL_REQUIRED: no existe {adjustment_path} y no pudo generarse mediante prematch_sos_v1044.run: {exc}"
            ) from exc
        if not adjustment_path.is_file():
            raise RuntimeError(f"SOS_INITIAL_REQUIRED: no se generó {adjustment_path}")
    adjustment = read_json(adjustment_path)
    original = folder / f"rendimiento_profundo_{event_id}.json"
    cutoff: datetime = target_meta["cutoff"]
    target_fixture: int = target_meta["target_fixture"]
    # A dry-run is always preview-only: never replace an enriched definitive
    # pool or any of its audit artefacts.
    dry = bool(dry_run)
    suffix = "_dryrun" if dry else ""
    out_pool = folder / f"pool_referencias_v1045_{event_id}{suffix}.json"
    # Un pool definitivo enriquecido se reutiliza siempre, aunque el operador
    # omita --resume. Así una ejecución normal nunca lo reduce accidentalmente.
    pool_exists = out_pool.is_file()
    reuse_pool = bool(resume or pool_exists)
    prior = _existing_pool(out_pool, target_fixture=target_fixture, cutoff=cutoff) if reuse_pool else {}
    prior.update(_seed_target_history(performance, target_fixture=target_fixture, cutoff=cutoff))
    query_log: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    period_exclusions: list[dict[str, Any]] = []
    if dry:
        client = DryRunClient()
    elif client is None:
        if ApiClient is None:
            raise RuntimeError("API_CLIENT_UNAVAILABLE")
        client = ApiClient(root / "outputs" / ".api_football_history_cache",
                           max_calls=max_api_calls,
                           key=CONFIGURED_FOOTBALL_API_KEY or None)
    rivals = _gap_rivals(adjustment, performance, pilot=pilot)
    pending_path = folder / f"pendientes_referencias_v1045_{event_id}{suffix}.json"
    previous_pending: list[dict[str, Any]] = []
    if resume and pending_path.is_file():
        try:
            previous_pending = read_json(pending_path).get("pending") or []
        except (OSError, ValueError, TypeError):
            previous_pending = []
    pending_rivals = {_positive_id(item.get("opponent_id")) for item in previous_pending}
    rivals.sort(key=lambda item: (0 if item["opponent_id"] in pending_rivals else 1,
                                  -item.get("priority", {}).get("L5", 0),
                                  -item.get("priority", {}).get("L10", 0),
                                  item["opponent_id"]))
    requested_rivals = len(rivals)
    fetched_rivals = 0
    stats_cache: dict[int, list[dict[str, Any]]] = {}
    calendar_rows: dict[int, list[dict[str, Any]]] = {}
    affected_index = _affected_history_index(performance)
    pending: list[dict[str, Any]] = []
    # Calendarios y estadísticas tienen el mismo coste unitario por endpoint.
    # Reservar aproximadamente la mitad del presupuesto a estadísticas evita
    # consumirlo entero en el primer rival cuando hay varios candidatos.
    calendar_limit = len(rivals) if dry else (
        len(rivals) if max_api_calls <= 0 else max(1, min(len(rivals), max_api_calls // 2)))
    for index, rival in enumerate(rivals):
        rid = rival["opponent_id"]
        if index >= calendar_limit:
            pending.append({"opponent_id": rid, "stage": "fixtures",
                            "reason": "BUDGET_RESERVED_FOR_DISTRIBUTION",
                            "priority": rival.get("priority")})
            continue
        try:
            if dry:
                history = []
                query_log.append({"endpoint": "fixtures", "team": rid, "status": "DRY_RUN"})
            else:
                history = client.get("fixtures", team=rid, last=99)
                query_log.append({"endpoint": "fixtures", "team": rid, "status": "FETCHED",
                                  "returned": len(history)})
            fetched_rivals += 1
        except BudgetExhausted as exc:
            errors.append({"opponent_id": rid, "stage": "fixtures", "reason": str(exc)})
            break
        except Exception as exc:
            errors.append({"opponent_id": rid, "stage": "fixtures", "reason": str(exc)})
            continue
        eligible_history = [r for r in history if _fixture_is_finished_before(
            r, target_fixture=target_fixture, cutoff=cutoff) and
            rid in (((r.get("teams") or {}).get("home") or {}).get("id"),
                    ((r.get("teams") or {}).get("away") or {}).get("id"))]
        for raw in history:
            status = ((raw.get("fixture") or {}).get("status") or {}).get("short")
            fid = _positive_id((raw.get("fixture") or {}).get("id"))
            if status in {"AET", "PEN"} and fid:
                period_exclusions.append({"fixture_id": fid, "opponent_id": rid,
                                          "status": status,
                                          "reason": "PERIOD_NOT_VERIFIED_AS_90_MINUTES"})
        calendar_rows[rid] = _select_reference_rows(
            eligible_history, rival, affected_index, max_fixtures_per_rival)

    # Round-robin por rival ya priorizado: primero una referencia de cada
    # candidato, luego la segunda, etc. Así el presupuesto no queda absorbido
    # por un único adversario.
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for round_index in range(max_fixtures_per_rival):
        for rival in rivals:
            rows = calendar_rows.get(rival["opponent_id"], [])
            if round_index < len(rows):
                jobs.append((rival, rows[round_index]))
    for rival, raw in jobs:
        rid = rival["opponent_id"]
        fid = _positive_id((raw.get("fixture") or {}).get("id"))
        if not fid or fid in prior:
            continue
        try:
            stats_ok = _stats_for_fixture(client, raw, stats_cache, dry_run=dry, query_log=query_log)
            if not stats_ok:
                pending.append({"opponent_id": rid, "fixture_id": fid,
                                "stage": "statistics",
                                "reason": "STATISTICS_EMPTY_OR_UNUSABLE",
                                "priority": rival.get("priority")})
                continue
            record = canonical_game(raw, tracked_team_id=rid,
                                    source="API_FOOTBALL_REFERENCE_COLLECTOR_V1045",
                                    source_query=f"fixtures?team={rid}&last=99")
            if record:
                prior[fid] = record
        except BudgetExhausted as exc:
            errors.append({"opponent_id": rid, "fixture_id": fid,
                           "stage": "statistics", "reason": str(exc)})
            pending.append({"opponent_id": rid, "fixture_id": fid,
                            "stage": "statistics", "reason": str(exc),
                            "priority": rival.get("priority")})
            break
        except Exception as exc:
            errors.append({"opponent_id": rid, "fixture_id": fid,
                           "stage": "statistics", "reason": str(exc)})
            pending.append({"opponent_id": rid, "fixture_id": fid,
                            "stage": "statistics", "reason": str(exc),
                            "priority": rival.get("priority")})
    for rival, raw in jobs:
        fid = _positive_id((raw.get("fixture") or {}).get("id"))
        if fid and fid not in prior and not any(x.get("fixture_id") == fid for x in pending):
            pending.append({"opponent_id": rival["opponent_id"], "fixture_id": fid,
                            "stage": "statistics", "reason": "NOT_PROCESSED_BUDGET",
                            "priority": rival.get("priority")})
    games = sorted(prior.values(), key=lambda x: (x.get("kickoff_utc", ""), x.get("fixture_id", 0)))
    # Remove internal acquisition-only fields before writing the SOS-compatible pool.
    for game in games:
        game.pop("_stats_by_team", None)
        game.pop("_stats_usable", None)
    pool = {
        "schema": POOL_SCHEMA,
        "collector_schema": SCHEMA,
        "version": VERSION,
        "target_event_id_bwin": event_id,
        "target_fixture_id": target_fixture,
        "target_cutoff_utc": cutoff.isoformat(),
        "source_scope": "TARGET_SNAPSHOT_PLUS_RIVAL_HISTORIES_ALL_COMPETITIONS_PRESERVED",
        "competition_treatment": "SOS_MATCHES_EXACT_LEAGUE_ID_AND_SEASON; NO_SILENT_MIXING",
        "reconstruction": True,
        "reconstruction_note": "Datos API obtenidos después del corte original; no se presentan como disponibles en ese momento.",
        "games": games,
    }
    pool_hash = dump_json(out_pool, pool)
    usage = {
        "api_calls": getattr(getattr(client, "stat", None), "calls", 0),
        "cache_hits": getattr(getattr(client, "stat", None), "hits", 0),
        "max_api_calls": max_api_calls,
        "dry_run": dry,
        "budget_exhausted": any("BUDGET" in str(x.get("reason")) for x in errors),
    }
    manifest = {
        "schema": SCHEMA + ".manifest",
        "version": VERSION,
        "target_event_id_bwin": event_id,
        "target_fixture_id": target_fixture,
        "original_cutoff_utc": cutoff.isoformat(),
        "original_snapshot_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "pool_sha256": pool_hash,
        "reconstruction": True,
        "pool_reused_without_resume": bool(reuse_pool and not resume and pool_exists),
        "rival_selection": {"pilot": pilot, "requested_rivals": requested_rivals,
                            "queried_rivals": fetched_rivals},
        "queries": query_log,
        "errors": errors,
        "period_exclusions": period_exclusions,
        "pending": pending,
        "usage": usage,
        "as_of_guarantee": "Cada referencia cumple kickoff+3h < corte objetivo; SOS vuelve a filtrar antes de cada partido histórico.",
    }
    manifest_path = folder / f"manifiesto_referencias_v1045_{event_id}{suffix}.json"
    dump_json(manifest_path, manifest)
    coverage = coverage_report(pool, performance)
    coverage_path = folder / f"cobertura_referencias_v1045_{event_id}{suffix}.json"
    dump_json(coverage_path, coverage)
    usage_path = folder / f"consultas_referencias_v1045_{event_id}{suffix}.json"
    dump_json(usage_path, {"schema": SCHEMA + ".queries", "event_id_bwin": event_id,
                           "usage": usage, "queries": query_log, "errors": errors,
                           "period_exclusions": period_exclusions})
    dump_json(pending_path, {"schema": SCHEMA + ".pending", "event_id_bwin": event_id,
                             "target_fixture_id": target_fixture, "pending": pending,
                             "generated_at_utc": datetime.now(timezone.utc).isoformat()})
    return {"pool": pool, "pool_path": str(out_pool), "manifest_path": str(manifest_path),
            "coverage_path": str(coverage_path), "usage_path": str(usage_path),
            "pending_path": str(pending_path), "manifest": manifest, "coverage": coverage}


def coverage_report(pool: dict[str, Any], performance: dict[str, Any]) -> dict[str, Any]:
    games = pool.get("games") or []
    by_key = Counter((g.get("league_id"), g.get("season")) for g in games)
    metrics = {metric: sum(
        1 for g in games if (g.get("own") or {}).get(metric) is not None or
        (g.get("conceded") or {}).get(metric) is not None) for metric in METRIC_NAMES}
    result: dict[str, Any] = {
        "schema": SCHEMA + ".coverage",
        "reference_games_n": len(games),
        "competition_season_games": {f"{k[0]}:{k[1]}": v for k, v in sorted(by_key.items())},
        "metrics_observed_n": metrics,
        "metrics": {},
        "warning": "Cobertura describe referencias reconstruidas; no equivale a disponibilidad en el corte original.",
    }
    # Reutilizamos exactamente el motor SOS para que los denominadores y la
    # regla as-of no diverjan entre el informe de cobertura y el recálculo.
    analysis = sos.analyze(performance, extra_pool=pool)
    for side in ("home", "away"):
        result["metrics"][side] = {}
        team = analysis["teams"][side]
        for metric in sos.METRICS:
            result["metrics"][side][metric] = {}
            for n in (5, 10, 20):
                window = team["windows"][metric][f"L{n}"]
                result["metrics"][side][metric][f"L{n}"] = {
                    "denominator_target_matches": window["matches_n"],
                    "observed_metric_n": window["raw_metric_n"],
                    "adjustable_n": window["adjusted_metric_n"],
                    "status": window["status"],
                    "reference_games_available": len(games),
                }
    return result


def recalculate_without_api(*, root: Path, event_id: str, pool_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Recalcula SOS usando exclusivamente el pool ya persistido."""
    folder = root / "outputs" / "matches" / event_id / "analysis" / "deep_v1042"
    performance = read_json(folder / f"rendimiento_profundo_{event_id}.json")
    identity = read_json(root / "outputs" / "matches" / event_id / "inputs" / "match_identity.json")
    quality = read_json(folder / f"auditoria_calidad_{event_id}.json")
    report = sos.analyze(performance, identity=identity, quality=quality,
                         extra_pool=read_json(pool_path),
                         source_hashes={"reference_pool": hashlib.sha256(pool_path.read_bytes()).hexdigest()})
    jpath = folder / f"ajuste_rivales_v1045_{event_id}.json"
    mpath = folder / f"ajuste_rivales_v1045_{event_id}.md"
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mpath.write_text(sos.markdown(report), encoding="utf-8")
    return jpath, mpath, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-api-calls", type=int, default=40)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--pilot", action="store_true", help="Solo rivales presentes en los últimos cinco partidos")
    parser.add_argument("--max-fixtures-per-rival", type=int, default=20)
    parser.add_argument("--recalculate", action="store_true", help="Recalcular SOS offline con el pool generado")
    args = parser.parse_args()
    if args.max_api_calls < 0 or args.max_api_calls > 300:
        parser.error("--max-api-calls entre 0 y 300")
    result = collect(root=args.root, event_id=args.event_id, dry_run=args.dry_run,
                     max_api_calls=args.max_api_calls, resume=args.resume, pilot=args.pilot,
                     max_fixtures_per_rival=args.max_fixtures_per_rival)
    print(f"POOL_V1045: {len(result['pool']['games'])} fixtures · {result['pool_path']}")
    print(f"COBERTURA: {result['coverage_path']}")
    print(f"MANIFIESTO: {result['manifest_path']}")
    print(f"CONSULTAS: {result['usage_path']}")
    print(f"PENDIENTES: {result['pending_path']}")
    if args.recalculate:
        jpath, mpath, report = recalculate_without_api(
            root=args.root, event_id=args.event_id, pool_path=Path(result["pool_path"]))
        print(f"SOS_V1045: {jpath} · {mpath}")
        for side in ("home", "away"):
            team = report["teams"][side]
            print(f"{team.get('team_name')}: " + " ".join(
                f"L{n}={team['windows']['shots_for'][f'L{n}']['adjusted_metric_n']}/{team['windows']['shots_for'][f'L{n}']['matches_n']}"
                for n in (5, 10, 20)))


if __name__ == "__main__":
    main()
