#!/usr/bin/env python3
"""Descubre TODOS los fixtures API-Football en una ventana de Europe/Madrid.

Fase 1 del selector PREMATCH. No consulta Bwin, no infiere probabilidades,
no hace ranking, no altera prematch_multi.py y no utiliza datos live.

Por defecto: hoy en Madrid, 16:00 -> 02:00 del día siguiente.

Ejemplos:
  python discover_fixtures.py
  python discover_fixtures.py --date 2026-11-01
  python discover_fixtures.py --date 2026-11-01 --refresh
  python discover_fixtures.py --choose 1234567 --date 2026-11-01
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

VERSION = "1.0-discovery"
MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
# Sólo NS (Not Started) corresponde a un encuentro con inicio programado
# que puede ser considerado para un análisis prematch.
PREMATCH_STATUSES = frozenset({"NS"})
CACHE_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 3
CSV_COLUMNS = [
    "fixture_id", "kickoff_madrid", "kickoff_utc", "country", "league",
    "league_id", "season", "league_type", "round", "home", "home_id",
    "away", "away_id", "status", "venue", "city", "coverage_status",
]


class DiscoveryError(RuntimeError):
    pass


def parse_hour(value: str) -> clock_time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("La hora debe tener formato HH:MM (00:00–23:59).")
    return clock_time.fromisoformat(value)


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Fecha inválida; utiliza YYYY-MM-DD.") from exc


def build_window(day: date, start: clock_time, end: clock_time) -> tuple[datetime, datetime]:
    """Ventana civil de Madrid, con cruce de medianoche y DST.

    Comparamos horas civiles para incluir las dos 02:00 de una noche de
    cambio de hora en otoño. Cada kickoff conserva su offset real.
    """
    start_naive = datetime.combine(day, start)
    end_day = day + timedelta(days=1) if end <= start else day
    end_naive = datetime.combine(end_day, end)
    return start_naive, end_naive


def parse_api_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("fixture.date ausente")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("fixture.date sin offset horario explícito")
    return parsed.astimezone(MADRID)


def safe_str(value: Any) -> str:
    return str(value) if value is not None else ""


def parse_fixture(raw: dict[str, Any]) -> dict[str, Any]:
    fixture = raw.get("fixture") or {}
    league = raw.get("league") or {}
    teams = raw.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    status = fixture.get("status") or {}
    stadium = fixture.get("venue") or {}
    fid = fixture.get("id")
    if not isinstance(fid, int) or isinstance(fid, bool) or fid <= 0:
        raise ValueError("fixture.id no es un entero positivo")
    local = parse_api_datetime(fixture.get("date"))
    if not all((isinstance(x, int) and not isinstance(x, bool) and x > 0)
               for x in (home.get("id"), away.get("id"))):
        raise ValueError("team IDs ausentes o inválidos")
    if home["id"] == away["id"]:
        raise ValueError("home_id y away_id idénticos")
    if not (home.get("name") and away.get("name") and league.get("name")):
        raise ValueError("nombres de equipos/competición ausentes")
    return {
        "fixture_id": fid,
        "kickoff_madrid": local.isoformat(timespec="seconds"),
        "kickoff_utc": local.astimezone(UTC).isoformat(timespec="seconds"),
        "country": league.get("country"),
        "league": league.get("name"),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "league_type": league.get("type"),  # /fixtures puede omitirlo
        "round": league.get("round"),
        "home": home.get("name"),
        "home_id": home.get("id"),
        "away": away.get("name"),
        "away_id": away.get("id"),
        "status": status.get("short"),
        "venue": stadium.get("name"),
        "city": stadium.get("city"),
        "coverage_status": "NOT_AUDITED",
    }


def filter_fixtures(
    all_raw: list[dict[str, Any]],
    start_local: datetime,
    end_local: datetime,
    now_utc: datetime,
    audit: list | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Filtra sólo PREMATCH real, no mercado live ni fixtures ya iniciados.

    La ventana usa comparación de tiempo civil Madrid (sin tzinfo) para
    manejar las horas repetidas por el cambio de horario.
    """
    counters: Counter[str] = Counter()
    by_id: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    start_naive = start_local.replace(tzinfo=None)
    end_naive = end_local.replace(tzinfo=None)
    assert now_utc.utcoffset() == timedelta(0)
    for raw in all_raw:
        try:
            row = parse_fixture(raw)
            kickoff = datetime.fromisoformat(row["kickoff_madrid"])
            kickoff_civil = kickoff.replace(tzinfo=None)
        except (ValueError, TypeError, AttributeError) as exc:
            counters["invalid_record"] += 1
            if len(warnings) < 20:
                warnings.append(f"Registro descartado por estructura inválida: {exc}")
            continue
        audit_row = dict(row)
        if audit is not None:
            audit.append(audit_row)
        if kickoff_civil < start_naive or kickoff_civil > end_naive:
            audit_row["discovery_exclusion"] = "OUTSIDE_OPERATIONAL_WINDOW"
            counters["outside_window"] += 1
            continue
        counters["inside_window"] += 1
        status = (row["status"] or "").upper()
        if status not in PREMATCH_STATUSES:
            audit_row["discovery_exclusion"] = "NOT_PREMATCH_STATUS"
            counters["not_prematch_status"] += 1
            continue
        if kickoff.astimezone(UTC) <= now_utc:
            audit_row["discovery_exclusion"] = "KICKOFF_ALREADY_PASSED"
            counters["kickoff_already_passed"] += 1
            continue
        identifier = row["fixture_id"]
        existing = by_id.get(identifier)
        if existing and existing != row:
            raise DiscoveryError(f"Fixture ID {identifier} contradictorio entre respuestas API")
        if existing:
            counters["duplicate"] += 1
            continue
        by_id[identifier] = row
    fixtures = sorted(by_id.values(), key=lambda row: (row["kickoff_utc"], row["fixture_id"]))
    counters["prematch_selected"] = len(fixtures)
    return fixtures, dict(counters), warnings


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def cache_path(root: Path, day: date) -> Path:
    return root / "outputs" / "data" / "cache" / "fixtures_discovery" / f"{day.isoformat()}_madrid.json"


def _read_cache(path: Path, now: datetime) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(cached["retrieved_at_utc"])
        if stamp.tzinfo is None or not (0 <= (now - stamp).total_seconds() <= CACHE_TTL_SECONDS):
            return None
        data = cached["payload"]
        if isinstance(data.get("response"), list):
            return data
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def request_fixtures(
    session: requests.Session, root: Path, day: date, api_key: str, base_url: str,
    request_timeout: int, now_utc: datetime, refresh: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    path = cache_path(root, day)
    if not refresh:
        cached = _read_cache(path, now_utc)
        if cached is not None:
            return cached["response"], "cache"
    url = base_url.rstrip("/") + "/fixtures"
    params = {"date": day.isoformat(), "timezone": "Europe/Madrid"}
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, params=params, timeout=request_timeout)
            if response.status_code in {401, 403}:
                raise DiscoveryError(f"API-Football: credencial inválida o acceso denegado (HTTP {response.status_code})")
            if response.status_code == 429:
                last_error = "HTTP 429, límite temporal de peticiones"
                if attempt == MAX_ATTEMPTS:
                    break
                retry = response.headers.get("Retry-After", "5")
                try:
                    pause = min(60.0, max(1.0, float(retry)))
                except (TypeError, ValueError):
                    pause = 5.0
                time.sleep(pause)
                continue
            if 400 <= response.status_code < 500:
                raise DiscoveryError(f"API-Football HTTP {response.status_code}: {response.text[:250]}")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise DiscoveryError("API-Football devolvió un objeto JSON inesperado")
            errors = payload.get("errors")
            if errors:
                raise DiscoveryError(f"API-Football devolvió errors: {errors}")
            data = payload.get("response")
            if not isinstance(data, list):
                raise DiscoveryError("API-Football sin lista 'response'; no se admite listado incompleto")
            paging = payload.get("paging") or {}
            if int(paging.get("total") or 1) > 1:
                raise DiscoveryError("/fixtures indicó más de una página; se detiene para no omitir partidos")
            if isinstance(payload.get("results"), int) and payload["results"] != len(data):
                raise DiscoveryError("/fixtures results no coincide con registros recibidos")
            atomic_json(path, {"retrieved_at_utc": now_utc.isoformat(), "payload": payload})
            return data, "api"
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(3 * attempt, 9))
    raise DiscoveryError(f"Error consultando /fixtures?date={day}: {last_error}")


def output_folder(root: Path, start_local: datetime, end_local: datetime) -> Path:
    marker = f"{start_local.date():%Y-%m-%d}_{start_local:%H%M}_{end_local:%H%M}"
    return root / "outputs" / "discovery" / marker


def write_outputs(folder: Path, report: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / "fixtures.json", report)
    with (folder / "fixtures.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["fixtures"])
    window = report["window"]
    lines = [
        "# Descubrimiento de partidos — API-Football",
        "",
        f"- Ventana: **{window['start_madrid']} → {window['end_madrid']} (Europe/Madrid)**",
        f"- Consulta ejecutada: `{report['generated_at_utc']}`",
        f"- Total PREMATCH: **{len(report['fixtures'])}**",
        f"- Competiciones distintas: **{report['counts']['competitions']}**",
        "- Cobertura estadística: **NO AUDITADA**",
        "- Ranking / cuotas Bwin: **NO GENERADOS**",
        "- Filtro: solo `NS` y kickoff posterior al momento de consulta.",
        "",
        "| Madrid | País | Liga | Local | Visitante | Fixture ID |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["fixtures"]:
        def txt(v: Any) -> str:
            return safe_str(v).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {txt(row['kickoff_madrid'])} | {txt(row['country'])} | {txt(row['league'])} "
                     f"| {txt(row['home'])} | {txt(row['away'])} | `{row['fixture_id']}` |")
    if not report["fixtures"]:
        lines.append("| — | — | — | Sin encuentros futuros elegibles | — | — |")
    if report["warnings"]:
        lines.extend(["", "## Advertencias", ""] + [f"- {w}" for w in report["warnings"]])
    (folder / "fixtures.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def choose_fixture(folder: Path, fixture_id: int, now_utc: datetime) -> Path:
    target = folder / "fixtures.json"
    if not target.is_file():
        raise DiscoveryError(f"No existe {target}; ejecuta primero el descubrimiento")
    report = json.loads(target.read_text(encoding="utf-8"))
    items = [x for x in report.get("fixtures", []) if x.get("fixture_id") == fixture_id]
    if len(items) != 1:
        raise DiscoveryError(f"Fixture {fixture_id} no figura entre los PREMATCH disponibles de este listado")
    row = items[0]
    if row.get("status") != "NS" or datetime.fromisoformat(row["kickoff_utc"]) <= now_utc:
        raise DiscoveryError("Ya no es elegible como PREMATCH por estado o fecha")
    source_bytes = target.read_bytes()
    selection = {
        "schema_version": "1.0-manual-selection", "selected_at_utc": now_utc.isoformat(),
        "selection_method": "MANUAL", "source_list": str(target),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "fixture": row, "bwin_event_id": None,
        "note": "No implica recomendación de apuesta ni correspondencia Bwin confirmada",
    }
    path = folder / f"selected_fixture_{fixture_id}.json"
    atomic_json(path, selection)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window",help="Perfil de config/prematch_windows.json")
    parser.add_argument("--date", type=parse_day, help="Día de inicio en Madrid, YYYY-MM-DD (predeterminado: hoy)")
    parser.add_argument("--start", type=parse_hour, default=None, help="Inicio HH:MM (configuración canónica)")
    parser.add_argument("--end", type=parse_hour, default=None, help="Fin HH:MM (configuración canónica)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="Raíz del proyecto scraper")
    parser.add_argument("--refresh", action="store_true", help="Ignorar caché (dos consultas API nuevas)")
    parser.add_argument("--show", type=int, default=40, help="Máximo de partidos en consola (0 = todos)")
    parser.add_argument("--choose", type=int, metavar="FIXTURE_ID", help="Selección manual desde fixtures.json; no consulta API")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    now = datetime.now(UTC)
    from prematch_prospective_trial import load_window_config,resolve_window,automatic_day
    cfg,_,_=load_window_config(root)
    _,args.start,args.end=resolve_window(cfg,args.window,args.start,args.end)
    day = args.date or automatic_day(now,args.start,args.end)
    begin, end = build_window(day, args.start, args.end)
    folder = output_folder(root, begin, end)

    if args.choose is not None:
        try:
            path = choose_fixture(folder, args.choose, now)
            print(f"Selección manual registrada: {path}")
            print("Pendiente: evaluación estadística y vinculación con Event ID Bwin.")
            return 0
        except (DiscoveryError, OSError, ValueError, KeyError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    try:
        # Reutiliza la carga de config/.env establecida en el proyecto.
        from config.constants import API_KEY, FOOTBALL_BASE_URL, REQUEST_TIMEOUT
        if not API_KEY:
            raise DiscoveryError("FOOTBALL_API_KEY no configurada (revisa config/.env)")
        days = sorted({begin.date(), end.date()})
        responses: list[dict[str, Any]] = []
        sources: list[dict[str, str]] = []
        with requests.Session() as session:
            session.headers.update({"x-apisports-key": API_KEY,
                                    "Accept": "application/json", "User-Agent": f"prematch-discovery/{VERSION}"})
            for d in days:
                raw, origin = request_fixtures(session, root, d, API_KEY, FOOTBALL_BASE_URL,
                                               REQUEST_TIMEOUT, now, args.refresh)
                responses.extend(raw)
                sources.append({"date": d.isoformat(), "source": origin, "returned": len(raw)})
        fixture_audit = []
        selected, excluded, warnings = filter_fixtures(responses, begin, end, now, fixture_audit)
        league_keys = {(x["league_id"], x["country"], x["season"]) for x in selected}
        report = {
            "schema_version": VERSION,
            "generated_at_utc": now.isoformat(),
            "source": "API-Football v3 /fixtures?date=YYYY-MM-DD&timezone=Europe/Madrid",
            "scope": "ALL_AVAILABLE_COMPETITIONS_UNFILTERED",
            "window": {"start_madrid": begin.isoformat() + " Europe/Madrid (hora civil)",
                       "end_madrid": end.isoformat() + " Europe/Madrid (hora civil)",
                       "timezone": "Europe/Madrid", "inclusive_end": True},
            "request_days": sources,
            "counts": {"api_records_received": len(responses), "prematch_matches": len(selected),
                       "competitions": len(league_keys), "excluded": excluded},
            "fixtures": selected,
            "fixture_audit": fixture_audit,
            "warnings": warnings,
            "note": "Descubrimiento solamente; no se ha valorado calidad de datos, rentabilidad ni cuotas Bwin.",
        }
        write_outputs(folder, report)
        print(f"Ventana Madrid: {begin:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}")
        print("Fuentes: " + ", ".join(f"{x['date']}={x['source']} ({x['returned']})" for x in sources))
        print(f"PREMATCH: {len(selected)} partidos | {len(league_keys)} competiciones")
        shown = selected if args.show == 0 else selected[:max(0, args.show)]
        for x in shown:
            print(f"{x['kickoff_madrid'][11:16]}  {x['country'] or '?'} / {x['league']}  "
                  f"{x['home']} - {x['away']}  [fixture {x['fixture_id']}]")
        if len(shown) < len(selected):
            print(f"... y {len(selected)-len(shown)} más en {folder / 'fixtures.md'}")
        print("Archivos: " + ", ".join(str(folder / f) for f in ("fixtures.json", "fixtures.csv", "fixtures.md")))
        return 0
    except (DiscoveryError, requests.RequestException, OSError, ValueError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
