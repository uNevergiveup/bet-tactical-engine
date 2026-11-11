#!/usr/bin/env python3
"""PREMATCH Phase 1: lightweight discovery, shared Bwin catalog and raw API data.

This command never runs prediction, opportunity, qualitative or betting stages.
Network access is opt-in with ``--online`` and always requires a positive API
budget.  Without ``--online`` every API read is cache-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import discover_fixtures as discovery
from filter_major_leagues import filter_document


SCHEMA = "prematch-phase1-foundation-v1"
UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")
CACHE_NAMESPACE = "v3"
FINAL_STATES = {"FT", "AET", "PEN"}
LIVE_STATES = {"1H", "2H", "HT", "ET", "BT", "P", "INT", "LIVE"}

ENDPOINTS = {
    "fixtures", "fixtures/headtohead", "fixtures/statistics", "fixtures/events",
    "fixtures/lineups", "fixtures/players", "fixtures/rounds", "teams",
    "teams/statistics", "players", "players/squads", "players/topscorers",
    "players/topassists", "players/topyellowcards", "players/topredcards",
    "standings", "injuries", "sidelined", "coachs", "transfers", "leagues",
}

TTL_SECONDS = {
    "fixtures": 3600,
    "fixtures/headtohead": 86400,
    "fixtures/statistics": 30 * 86400,
    "fixtures/events": 30 * 86400,
    "fixtures/lineups": 30 * 86400,
    "fixtures/players": 30 * 86400,
    "fixtures/rounds": 86400,
    "teams": 7 * 86400,
    "teams/statistics": 12 * 3600,
    "players": 24 * 3600,
    "players/squads": 7 * 86400,
    "players/topscorers": 24 * 3600,
    "players/topassists": 24 * 3600,
    "players/topyellowcards": 24 * 3600,
    "players/topredcards": 24 * 3600,
    "standings": 3600,
    "injuries": 4 * 3600,
    "sidelined": 24 * 3600,
    "coachs": 24 * 3600,
    "transfers": 7 * 86400,
    "leagues": 24 * 3600,
}
EMPTY_TTL_SECONDS = 5 * 60
PARTIAL_TTL_SECONDS = 60 * 60


class Phase1Error(RuntimeError):
    pass


class CacheMissOffline(Phase1Error):
    pass


class BudgetExhausted(Phase1Error):
    pass


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_dt(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def sha_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def response_completeness(endpoint: str, response: list[Any], raw_payload: dict[str, Any] | None = None) -> str:
    if not response:
        return "EMPTY_PROVISIONAL"
    paging = (raw_payload or {}).get("paging") or {}
    if int(paging.get("current") or 1) < int(paging.get("total") or 1):
        return "PARTIAL_PAGINATION"
    # These fixture endpoints normally return one row per team. A single row is
    # useful evidence, but cannot be frozen as a complete historical response.
    if endpoint in {"fixtures/statistics", "fixtures/lineups", "fixtures/players"} and len(response) < 2:
        return "PARTIAL_RESPONSE"
    return "COMPLETE_RESPONSE"


def atomic_json(path: Path, value: Any) -> str:
    blob = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(blob)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return sha_bytes(blob)


def parse_clock(value: str) -> clock_time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("Hora requerida HH:MM")
    return clock_time.fromisoformat(value)


def normalized_params(params: dict[str, Any]) -> tuple[dict[str, str], str]:
    cleaned = {str(k): str(v).lower() if isinstance(v, bool) else str(v)
               for k, v in params.items() if v is not None}
    ordered = dict(sorted(cleaned.items()))
    return ordered, urlencode(list(ordered.items()))


def cache_token(endpoint: str, params: dict[str, Any]) -> tuple[str, str]:
    _, query = normalized_params(params)
    token = hashlib.sha256(f"{CACHE_NAMESPACE}:{endpoint}?{query}".encode()).hexdigest()[:24]
    return token, query


@dataclass
class ApiResult:
    endpoint: str
    params: dict[str, str]
    response: list[Any]
    source: str
    cache_path: Path | None
    fetched_at_utc: str | None
    completeness: str
    raw_payload: dict[str, Any] | None


Transport = Callable[[str, dict[str, str], dict[str, str], int], tuple[int, dict[str, str], dict[str, Any]]]


class Phase1ApiClient:
    """Cache-compatible API-Football client with raw envelopes and hard budget."""

    def __init__(self, cache_dir: Path, *, offline: bool = True, max_new_calls: int = 0,
                 key: str | None = None, base_url: str = "https://v3.football.api-sports.io",
                 timeout: int = 20, transport: Transport | None = None,
                 clock: Callable[[], datetime] = now_utc,
                 sleeper: Callable[[float], None] = time.sleep):
        if max_new_calls < 0:
            raise ValueError("max_new_calls debe ser >=0")
        if not offline and max_new_calls < 1:
            raise ValueError("--online exige --max-api-calls > 0")
        self.cache_dir = cache_dir
        self.offline = offline
        self.max_new_calls = max_new_calls
        self.key = key or os.environ.get("FOOTBALL_API_KEY") or os.environ.get("API_FOOTBALL_KEY")
        if not offline and not self.key:
            raise ValueError("FOOTBALL_API_KEY no configurada")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or self._http_transport
        self.clock = clock
        self.sleeper = sleeper
        self.new_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.retries = 0
        self.errors = 0
        self.http_calls_by_endpoint: Counter[str] = Counter()
        self.ledger: list[dict[str, Any]] = []
        self.used_cache_files: dict[str, dict[str, Any]] = {}

    def path_for(self, endpoint: str, params: dict[str, Any]) -> Path:
        token, _ = cache_token(endpoint, params)
        return self.cache_dir / endpoint.replace("/", "_") / f"{token}.json"

    def _http_transport(self, endpoint: str, params: dict[str, str], headers: dict[str, str],
                        timeout: int) -> tuple[int, dict[str, str], dict[str, Any]]:
        url = f"{self.base_url}/{endpoint}?{urlencode(list(params.items()))}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return int(response.status), dict(response.headers.items()), payload
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"errors": {"http": str(exc)}}
            return int(exc.code), dict(exc.headers.items()) if exc.headers else {}, payload

    def _read_cache(self, endpoint: str, params: dict[str, str]) -> ApiResult | None:
        path = self.path_for(endpoint, params)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            _, query = normalized_params(params)
            if document.get("endpoint") != endpoint or document.get("query") != query:
                raise ValueError("CACHE_IDENTITY_COLLISION")
            fetched = parse_dt(document.get("fetched_at_utc"))
            if fetched is None or fetched > self.clock() + timedelta(seconds=10):
                raise ValueError("CACHE_TIMESTAMP_INVALID")
            raw = document.get("raw_payload")
            response = document.get("response")
            if raw is not None:
                if document.get("payload_sha256") != sha_bytes(canonical_json(raw)):
                    raise ValueError("CACHE_PAYLOAD_SHA_MISMATCH")
                response = raw.get("response")
            if not isinstance(response, list):
                raise ValueError("CACHE_RESPONSE_NOT_LIST")
            kind = document.get("cache_kind") or ("negative" if not response else "positive")
            completeness = document.get("completeness") or response_completeness(endpoint, response, raw)
            default_ttl = (EMPTY_TTL_SECONDS if kind == "negative" else
                           PARTIAL_TTL_SECONDS if completeness.startswith("PARTIAL_") else
                           TTL_SECONDS.get(endpoint, 86400))
            ttl = int(document.get("ttl_seconds") or default_ttl)
            age = (self.clock() - fetched).total_seconds()
            if age < 0 or age > ttl:
                return None
            self.cache_hits += 1
            meta = {"path": str(path.resolve()), "sha256": sha_file(path), "endpoint": endpoint,
                    "params": params, "fetched_at_utc": fetched.isoformat(),
                    "completeness": completeness, "source": "cache"}
            self.used_cache_files[str(path.resolve())] = meta
            self.ledger.append({"endpoint": endpoint, "parameters": params, "status": "CACHE_HIT",
                                "cache_path": str(path.resolve()), "cache_sha256": meta["sha256"],
                                "fetched_at_utc": fetched.isoformat(), "completeness": completeness})
            return ApiResult(endpoint, params, response, "cache", path, fetched.isoformat(), completeness, raw)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.ledger.append({"endpoint": endpoint, "parameters": params,
                                "status": "CACHE_CORRUPT", "cache_path": str(path.resolve()),
                                "error": str(exc)})
            return None

    def _read_legacy_cache(self, endpoint: str, params: dict[str, str]) -> ApiResult | None:
        """Read, never rewrite, compatible generate_context/preselection caches."""
        data_cache = self.cache_dir.parent / "data" / "cache"
        _, query = normalized_params(params)
        context_key = hashlib.sha256(json.dumps(
            {"endpoint": endpoint, "params": params}, ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()
        preselect_key = hashlib.sha256(f"{endpoint}?{query}".encode()).hexdigest()
        candidates = ((data_cache / f"{context_key}.json", "legacy_generate_context"),
                      (data_cache / "preselect_api" / f"{preselect_key}.json", "legacy_preselection"))
        for path, source in candidates:
            if not path.is_file():
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(document, dict) or not isinstance(document.get("response"), list):
                    raise ValueError("LEGACY_RESPONSE_NOT_LIST")
                if source == "legacy_generate_context":
                    if document.get("get") != endpoint or normalized_params(document.get("parameters") or {})[0] != params:
                        raise ValueError("LEGACY_CACHE_IDENTITY_MISMATCH")
                    if document.get("errors"):
                        raise ValueError("LEGACY_API_ERRORS")
                    raw = document
                    fetched = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                else:
                    raw = {"response": document["response"], "paging": {"current": 1, "total": 1}}
                    fetched = parse_dt(document.get("retrieved_at_utc"))
                    if fetched is None:
                        raise ValueError("LEGACY_TIMESTAMP_INVALID")
                if fetched > self.clock() + timedelta(seconds=10):
                    raise ValueError("LEGACY_TIMESTAMP_FUTURE")
                response = document["response"]
                completeness = response_completeness(endpoint, response, raw)
                ttl = (EMPTY_TTL_SECONDS if not response else
                       PARTIAL_TTL_SECONDS if completeness.startswith("PARTIAL_") else
                       TTL_SECONDS.get(endpoint, 86400))
                if (self.clock() - fetched).total_seconds() > ttl:
                    continue
                self.cache_hits += 1
                meta = {"path": str(path.resolve()), "sha256": sha_file(path),
                        "endpoint": endpoint, "params": params,
                        "fetched_at_utc": fetched.isoformat(), "completeness": completeness,
                        "source": source}
                self.used_cache_files[str(path.resolve())] = meta
                self.ledger.append({"endpoint": endpoint, "parameters": params,
                                    "status": "LEGACY_CACHE_HIT", "legacy_source": source,
                                    "cache_path": str(path.resolve()), "cache_sha256": meta["sha256"],
                                    "fetched_at_utc": fetched.isoformat(), "completeness": completeness})
                return ApiResult(endpoint, params, response, source, path, fetched.isoformat(), completeness, raw)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self.ledger.append({"endpoint": endpoint, "parameters": params,
                                    "status": "LEGACY_CACHE_REJECTED", "legacy_source": source,
                                    "cache_path": str(path.resolve()), "error": str(exc)})
        return None

    def get(self, endpoint: str, **params: Any) -> ApiResult:
        if endpoint not in ENDPOINTS:
            raise ValueError(f"ENDPOINT_NOT_ALLOWED: {endpoint}")
        ordered, query = normalized_params(params)
        cached = self._read_cache(endpoint, ordered)
        if cached is not None:
            return cached
        legacy = self._read_legacy_cache(endpoint, ordered)
        if legacy is not None:
            return legacy
        self.cache_misses += 1
        if self.offline:
            self.ledger.append({"endpoint": endpoint, "parameters": ordered,
                                "status": "CACHE_MISS_OFFLINE"})
            raise CacheMissOffline(f"CACHE_MISS_OFFLINE {endpoint}?{query}")
        headers = {"x-apisports-key": str(self.key), "Accept": "application/json",
                   "User-Agent": f"prematch-phase1/{SCHEMA}"}
        last_error = ""
        for attempt in range(1, 4):
            if self.new_calls >= self.max_new_calls:
                self.ledger.append({"endpoint": endpoint, "parameters": ordered,
                                    "status": "BUDGET_EXHAUSTED", "attempt": attempt})
                raise BudgetExhausted(f"BUDGET_MAX_CALLS_{self.max_new_calls}")
            self.new_calls += 1
            self.http_calls_by_endpoint[endpoint] += 1
            try:
                status, response_headers, payload = self.transport(endpoint, ordered, headers, self.timeout)
                if status == 429 or status >= 500:
                    last_error = f"HTTP_{status}"
                    if attempt == 3:
                        break
                    self.retries += 1
                    retry = response_headers.get("Retry-After") or response_headers.get("retry-after") or attempt
                    try:
                        pause = min(30.0, max(0.0, float(retry)))
                    except (TypeError, ValueError):
                        pause = float(attempt)
                    self.sleeper(pause)
                    continue
                if status >= 400:
                    raise Phase1Error(f"API_HTTP_{status}_{endpoint}")
                if not isinstance(payload, dict) or payload.get("errors"):
                    raise Phase1Error(f"API_ERRORS_{endpoint}: {payload.get('errors') if isinstance(payload, dict) else 'INVALID_JSON'}")
                response = payload.get("response")
                if not isinstance(response, list):
                    raise Phase1Error(f"API_RESPONSE_NOT_LIST_{endpoint}")
                paging = payload.get("paging") or {}
                completeness = response_completeness(endpoint, response, payload)
                acquired = self.clock().astimezone(UTC)
                ttl = (EMPTY_TTL_SECONDS if not response else
                       PARTIAL_TTL_SECONDS if completeness.startswith("PARTIAL_") else
                       TTL_SECONDS.get(endpoint, 86400))
                document = {"schema": "phase1-shared-cache-v1", "endpoint": endpoint,
                            "parameters": ordered, "query": query,
                            "fetched_at_utc": acquired.isoformat(),
                            "source": "API-Football v3", "http_status": status,
                            "api_results": payload.get("results"), "api_paging": paging,
                            "cache_kind": "negative" if not response else "positive",
                            "completeness": completeness, "ttl_seconds": ttl,
                            "validity_policy": {"kind": ("SHORT_NEGATIVE" if not response else
                                                         "SHORT_PARTIAL" if completeness.startswith("PARTIAL_") else
                                                         "ENDPOINT_TTL"),
                                                "ttl_seconds": ttl,
                                                "empty_is_definitive": False if not response else None},
                            "payload_sha256": sha_bytes(canonical_json(payload)),
                            "raw_payload": payload, "response": response}
                path = self.path_for(endpoint, ordered)
                atomic_json(path, document)
                meta = {"path": str(path.resolve()), "sha256": sha_file(path), "endpoint": endpoint,
                        "params": ordered, "fetched_at_utc": acquired.isoformat(),
                        "completeness": completeness, "source": "api"}
                self.used_cache_files[str(path.resolve())] = meta
                self.ledger.append({"endpoint": endpoint, "parameters": ordered,
                                    "status": "API_FETCH", "attempt": attempt,
                                    "http_status": status, "cache_path": str(path.resolve()),
                                    "cache_sha256": meta["sha256"], "completeness": completeness})
                return ApiResult(endpoint, ordered, response, "api", path,
                                 acquired.isoformat(), completeness, payload)
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, Phase1Error) as exc:
                last_error = str(exc)
                if isinstance(exc, Phase1Error) or attempt == 3:
                    break
                self.retries += 1
                self.sleeper(float(attempt))
        self.errors += 1
        self.ledger.append({"endpoint": endpoint, "parameters": ordered,
                            "status": "ERROR", "error": last_error})
        raise Phase1Error(f"API_REQUEST_FAILED {endpoint}: {last_error}")

    def get_all_pages(self, endpoint: str, **params: Any) -> list[ApiResult]:
        first = self.get(endpoint, **params)
        results = [first]
        raw = first.raw_payload or {}
        paging = raw.get("paging") or {}
        total = int(paging.get("total") or 1)
        current = int(paging.get("current") or 1)
        while current < total:
            current += 1
            results.append(self.get(endpoint, **params, page=current))
        return results

    def statistics(self) -> dict[str, Any]:
        return {"new_http_calls": self.new_calls, "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses, "retries": self.retries,
                "errors": self.errors, "max_new_calls": self.max_new_calls,
                "offline": self.offline,
                "http_calls_by_endpoint": dict(sorted(self.http_calls_by_endpoint.items()))}


def window_bounds(day: date, start: clock_time, end: clock_time) -> tuple[datetime, datetime]:
    begin = datetime.combine(day, start, tzinfo=MADRID)
    finish_day = day + timedelta(days=1) if end <= start else day
    finish = datetime.combine(finish_day, end, tzinfo=MADRID)
    return begin.astimezone(UTC).astimezone(MADRID), finish.astimezone(UTC).astimezone(MADRID)


def window_key(begin: datetime, finish: datetime) -> str:
    return f"{begin.date():%Y-%m-%d}_{begin:%H%M}_{finish:%H%M}"


def active_window_day(current: datetime, start: clock_time, end: clock_time) -> date:
    local = current.astimezone(MADRID)
    if end <= start and local.timetz().replace(tzinfo=None) < end:
        return local.date() - timedelta(days=1)
    return local.date()


def locate_fixture_source(root: Path, key: str) -> Path | None:
    candidates = [root / "outputs" / "discovery" / key / "fixtures.json",
                  root / "outputs" / "phase1" / key / "fixtures_discovered.json"]
    return next((path for path in candidates if path.is_file()), None)


def discovery_report_from_api(client: Phase1ApiClient, begin: datetime, finish: datetime,
                              generated: datetime) -> dict[str, Any]:
    raw_rows: list[dict[str, Any]] = []
    sources = []
    for request_day in sorted({begin.date(), finish.date()}):
        result = client.get("fixtures", date=request_day.isoformat(), timezone="Europe/Madrid")
        raw_rows.extend(result.response)
        sources.append({"date": request_day.isoformat(), "source": result.source,
                        "returned": len(result.response), "cache_path": str(result.cache_path) if result.cache_path else None})
    audit: list[dict[str, Any]] = []
    selected, excluded, warnings = discovery.filter_fixtures(
        raw_rows, begin.replace(tzinfo=None), finish.replace(tzinfo=None), generated.astimezone(UTC), audit)
    return {"schema_version": discovery.VERSION, "generated_at_utc": generated.astimezone(UTC).isoformat(),
            "source": "API-Football v3 /fixtures?date=YYYY-MM-DD&timezone=Europe/Madrid",
            "scope": "ALL_AVAILABLE_COMPETITIONS_UNFILTERED",
            "window": {"start_madrid": begin.isoformat(), "end_madrid": finish.isoformat(),
                       "timezone": "Europe/Madrid", "inclusive_end": True},
            "request_days": sources,
            "counts": {"api_records_received": len(raw_rows), "prematch_matches": len(selected),
                       "competitions": len({(x.get('league_id'), x.get('season')) for x in selected}),
                       "excluded": excluded},
            "fixtures": selected, "fixture_audit": audit, "warnings": warnings}


def canonical_fixtures(source: dict[str, Any], source_path: Path | None) -> dict[str, Any]:
    if not isinstance(source.get("fixtures"), list):
        raise Phase1Error("El origen no contiene fixtures[]")
    raw = (source_path.read_bytes() if source_path else
           (json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n").encode())
    # A resumed run may receive its own canonical output.  Reuse that immutable
    # classification instead of trying to feed it back through the discovery
    # V1.0 parser (which deliberately accepts only the upstream contract).
    if source.get("schema") == SCHEMA and source.get("stage") == "LIGHTWEIGHT_DISCOVERY":
        rows = source["fixtures"]
        ids = [row.get("fixture_id") for row in rows if isinstance(row, dict)]
        if (len(ids) != len(rows) or any(not isinstance(fid, int) or isinstance(fid, bool) or fid <= 0
                                        for fid in ids) or len(set(ids)) != len(ids)):
            raise Phase1Error("Salida Phase 1 contiene Fixture ID inválido o duplicado")
        allowed = {"ELIGIBLE", "REVIEW", "EXCLUDED"}
        if any(row.get("category_status") not in allowed for row in rows):
            raise Phase1Error("Salida Phase 1 contiene categoría no reconocida")
        ordered = sorted((dict(row) for row in rows),
                         key=lambda x: (x.get("kickoff_utc") or "", int(x["fixture_id"])))
        groups = {status: [row for row in ordered if row.get("category_status") == status]
                  for status in allowed}
        return {**source, "generated_at_utc": now_utc().isoformat(),
                "source_path": str(source_path.resolve()) if source_path else source.get("source_path"),
                "source_sha256": sha_bytes(raw), "fixtures": ordered,
                "eligible": groups["ELIGIBLE"], "review": groups["REVIEW"],
                "excluded": groups["EXCLUDED"],
                "counts": {**source.get("counts", {}), "input": len(ordered),
                           "eligible": len(groups["ELIGIBLE"]), "review": len(groups["REVIEW"]),
                           "excluded": len(groups["EXCLUDED"]), "all_rows": len(ordered)}}
    filtered = filter_document(source, raw, policy="adult-open")
    rows = []
    for status_key, rows_source in (("ELIGIBLE", filtered["fixtures"]),
                                    ("REVIEW", filtered["review"]),
                                    ("EXCLUDED", filtered["excluded"])):
        for row in rows_source:
            rows.append({**row, "category_status": status_key,
                         "category_reason": row.get("filter_reason")})
    rows.sort(key=lambda x: (x.get("kickoff_utc") or "", int(x.get("fixture_id") or 0)))
    grouped = {status: [row for row in rows if row.get("category_status") == status]
               for status in ("ELIGIBLE", "REVIEW", "EXCLUDED")}
    return {"schema": SCHEMA, "stage": "LIGHTWEIGHT_DISCOVERY",
            "generated_at_utc": now_utc().isoformat(),
            "source_path": str(source_path.resolve()) if source_path else None,
            "source_sha256": sha_bytes(raw), "source_schema": source.get("schema_version"),
            "window": source.get("window"), "counts": {**filtered["counts"], "all_rows": len(rows)},
            "fixtures": rows, "eligible": grouped["ELIGIBLE"], "review": grouped["REVIEW"],
            "excluded": grouped["EXCLUDED"],
            "policy": filtered["policy"], "warnings": source.get("warnings", [])}


def merge_bwin_snapshots(paths: list[Path]) -> dict[str, Any]:
    catalogs: dict[str, dict[str, Any]] = {}
    matches: dict[str, dict[str, Any]] = {}
    pages: dict[str, dict[str, Any]] = {}
    sources = []
    group_events: defaultdict[str, set[str]] = defaultdict(set)
    group_pages: defaultdict[str, set[str]] = defaultdict(set)
    group_files: Counter[str] = Counter()
    group_times: defaultdict[str, list[str]] = defaultdict(list)
    status_rank = {"IDENTITY_VERIFIED_AUTOMATICALLY": 8, "EVENT_FOUND_TIME_CORROBORATED": 7,
                   "EVENT_FOUND_NEEDS_TIME_CONFIRMATION": 6, "EVENT_REQUIRES_REVIEW": 5,
                   "AMBIGUOUS_MULTIPLE_EVENTS": 4, "LOW_CONFIDENCE_REVIEW": 3,
                   "NOT_FOUND_IN_SCANNED_PAGES": 2}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        path_text = str(path.resolve())
        origin = ("phase1_bwin_scan" if "/bwin_scan/" in path_text else
                  "prospective_trial_batches" if "/batches/" in path_text else
                  "prospective_trial_catalog")
        generated = payload.get("generated_at_utc")
        group_files[origin] += 1
        if generated:
            group_times[origin].append(str(generated))
        source_events = {str(event.get("bwin_event_id")) for event in payload.get("catalog_observed", [])
                         if str(event.get("bwin_event_id") or "").isdigit()}
        source_pages = {str(page.get("catalog_page")) for page in payload.get("pages", [])
                        if page.get("catalog_page")}
        group_events[origin].update(source_events)
        group_pages[origin].update(source_pages)
        sources.append({"path": path_text, "sha256": sha_file(path),
                        "generated_at_utc": generated, "origin": origin,
                        "catalog_events_observed": len(source_events),
                        "pages_observed": len(source_pages),
                        "matches_in_snapshot": len(payload.get("matches", []))})
        for event in payload.get("catalog_observed", []):
            event_id = str(event.get("bwin_event_id") or "")
            if not event_id.isdigit():
                continue
            current = catalogs.get(event_id)
            if current is None:
                catalogs[event_id] = dict(event)
            else:
                current["source_pages"] = list(dict.fromkeys((current.get("source_pages") or []) +
                                                              (event.get("source_pages") or [])))
                if (str(event.get("text_observed_at_utc") or ""),len(str(event.get("text") or ""))) > (str(current.get("text_observed_at_utc") or ""),len(str(current.get("text") or ""))):
                    current.update({k: event.get(k) for k in ("text", "text_observed_at_utc", "bwin_url", "text_source")})
        for page in payload.get("pages", []):
            url = str(page.get("catalog_page") or "")
            if not url:
                continue
            existing = pages.get(url)
            if existing is None or (existing.get("status") != "OK" and page.get("status") == "OK"):
                pages[url] = {**page, "source_snapshot": str(path.resolve())}
        for row in payload.get("matches", []):
            fid = str(row.get("fixture_id") or "")
            if not fid.isdigit():
                continue
            existing = matches.get(fid)
            if existing is None or status_rank.get(row.get("status"), 0) > status_rank.get(existing.get("status"), 0):
                matches[fid] = {**row, "source_snapshot": str(path.resolve())}
    return {"schema": SCHEMA, "stage": "SHARED_BWIN_CATALOG",
            "generated_at_utc": now_utc().isoformat(), "source_snapshots": sources,
            "events": sorted(catalogs.values(), key=lambda x: str(x.get("bwin_event_id"))),
            "pages": sorted(pages.values(), key=lambda x: str(x.get("catalog_page"))),
            "matches": sorted(matches.values(), key=lambda x: int(x.get("fixture_id") or 0)),
            "merge_diagnostics": {
                "sources_by_origin": {
                    origin: {"files": group_files[origin], "unique_events": len(group_events[origin]),
                             "unique_pages": len(group_pages[origin]),
                             "first_generated_at_utc": min(group_times[origin]) if group_times[origin] else None,
                             "last_generated_at_utc": max(group_times[origin]) if group_times[origin] else None}
                    for origin in sorted(group_files)},
                "union_unique_events": len(catalogs), "union_unique_pages": len(pages),
                "union_matches": len(matches),
                "interpretation": "Counts are unions of supplied snapshots; a single scan and a historical batch union have different scope and acquisition times."
            }}


def api_fixture_verification(fixture: dict[str, Any], *, now: datetime | None = None) -> tuple[str, str]:
    """Validate the API fixture independently of any bookmaker observation."""
    if fixture.get("category_status") != "ELIGIBLE":
        return "API_FIXTURE_PENDING_CATEGORY", "CATEGORY_NOT_ELIGIBLE_FOR_AUTOMATIC_DEEP_ACQUISITION"
    if not all(isinstance(fixture.get(key), int) and not isinstance(fixture.get(key), bool) and fixture.get(key) > 0
               for key in ("fixture_id", "home_id", "away_id", "league_id", "season")):
        return "API_FIXTURE_UNVERIFIED", "MISSING_API_FIXTURE_IDS_COMPETITION_OR_SEASON"
    if fixture["home_id"] == fixture["away_id"] or not str(fixture.get("home") or "").strip() or not str(fixture.get("away") or "").strip():
        return "API_FIXTURE_UNVERIFIED", "HOME_AWAY_IDENTITY_OR_LOCALITY_INVALID"
    gender = str(fixture.get("gender") or "UNKNOWN").upper()
    if gender not in {"M", "F"}:
        return "API_FIXTURE_PENDING_CATEGORY", "GENDER_NOT_VERIFIED_BY_API_DISCOVERY"
    kickoff = parse_dt(fixture.get("kickoff_utc") or fixture.get("kickoff_madrid"))
    if kickoff is None:
        return "API_FIXTURE_UNVERIFIED", "KICKOFF_NOT_PARSEABLE"
    if str(fixture.get("status") or "") != "NS":
        return "API_FIXTURE_UNVERIFIED", "API_FIXTURE_NOT_PREMATCH_NS"
    reference = (now or now_utc()).astimezone(UTC)
    if kickoff <= reference:
        return "API_FIXTURE_UNVERIFIED", "API_FIXTURE_KICKOFF_NOT_FUTURE"
    return "API_FIXTURE_VERIFIED", "API_IDS_LOCALITY_COMPETITION_SEASON_KICKOFF_VERIFIED"


def identity_map(fixtures_doc: dict[str, Any], bwin_doc: dict[str, Any]) -> dict[str, Any]:
    bwin_by_fixture = {str(x.get("fixture_id")): x for x in bwin_doc.get("matches", [])}
    event_usage: defaultdict[str, list[str]] = defaultdict(list)
    for fid, row in bwin_by_fixture.items():
        event = str(row.get("bwin_event_id") or "")
        if event.isdigit():
            event_usage[event].append(fid)
    page_failures = [p for p in bwin_doc.get("pages", []) if p.get("status") not in {"OK"}]
    verified, api_verified, api_pending, pending, not_found, excluded = [], [], [], [], [], []
    for fixture in fixtures_doc.get("fixtures", []):
        fid = str(fixture["fixture_id"])
        observed = bwin_by_fixture.get(fid)
        base = {"api_fixture_id": fixture["fixture_id"], "league_id": fixture.get("league_id"),
                "season": fixture.get("season"), "home_team_id": fixture.get("home_id"),
                "away_team_id": fixture.get("away_id"), "home": fixture.get("home"),
                "away": fixture.get("away"), "gender": fixture.get("gender"),
                "category_status": fixture.get("category_status"),
                "kickoff_utc": fixture.get("kickoff_utc"),
                "kickoff_madrid": fixture.get("kickoff_madrid"),
                "bwin_event_id": observed.get("bwin_event_id") if observed else None,
                "bwin_url": observed.get("bwin_url") if observed else None,
                "bwin_binding_status": "UNVERIFIED",
                "source_timestamps": {"api_discovery": fixtures_doc.get("generated_at_utc"),
                                      "bwin_catalog": bwin_doc.get("generated_at_utc")},
                "evidence": {"bwin_status": observed.get("status") if observed else None,
                             "bwin_identity_status": observed.get("identity_status") if observed else None,
                             "inspection": observed.get("inspection") if observed else None,
                             "alternatives": observed.get("alternatives", []) if observed else [],
                             "source_snapshot": observed.get("source_snapshot") if observed else None}}
        fixture_kickoff = parse_dt(fixture.get("kickoff_utc") or fixture.get("kickoff_madrid"))
        base["temporal_status_at_map_build"] = ("KICKOFF_UNKNOWN" if fixture_kickoff is None else
                                                "FUTURE" if fixture_kickoff > now_utc() else
                                                "HISTORICAL_OR_STARTED")
        api_status, api_reason = api_fixture_verification(fixture)
        base["api_fixture_status"] = api_status
        base["api_fixture_reason"] = api_reason
        if api_status == "API_FIXTURE_VERIFIED":
            api_verified.append({**base, "bwin_binding_status": "UNVERIFIED"})
        elif api_status == "API_FIXTURE_PENDING_CATEGORY":
            api_pending.append(base)
        if fixture.get("category_status") == "EXCLUDED":
            base.update(identity_status="EXCLUDED_CATEGORY", reason=fixture.get("category_reason"))
            excluded.append(base); continue
        if not observed:
            base.update(identity_status="CATALOG_INCOMPLETE",
                        reason="FIXTURE_NOT_EVALUATED_IN_BWIN_SOURCE")
            pending.append(base); continue
        event = str(observed.get("bwin_event_id") or "")
        if event and len(event_usage[event]) > 1:
            base.update(identity_status="AMBIGUOUS", reason="BWIN_EVENT_REUSED_BY_MULTIPLE_API_FIXTURES")
            pending.append(base); continue
        if fixture.get("category_status") == "REVIEW":
            base.update(identity_status="AMBIGUOUS", reason="CATEGORY_REQUIRES_REVIEW")
            pending.append(base); continue
        status = observed.get("status")
        identity = observed.get("identity_status")
        if status == "IDENTITY_VERIFIED_AUTOMATICALLY" and identity == "NAME_AND_ABSOLUTE_TIME_MATCH":
            inspection = observed.get("inspection") or {}
            bwin_start = parse_dt(inspection.get("event_start_machine_readable"))
            api_start = parse_dt(fixture.get("kickoff_utc") or fixture.get("kickoff_madrid"))
            event_same = str(inspection.get("event_id_from_url") or "") == event
            time_diff = (abs((bwin_start - api_start).total_seconds()) / 60
                         if bwin_start and api_start else None)
            title_ok = inspection.get("title_teams_match") is True
            order_ok = observed.get("team_order_status") not in {
                "AWAY_HOME_CONFLICT", "AWAY_HOME_TOKEN_CONFLICT"}
            category_evidence = observed.get("category_compatibility")
            expected_gender = str(fixture.get("gender") or "UNKNOWN").upper()
            category_ok = ((expected_gender == "F" and category_evidence == "FEMALE_CORROBORATED") or
                           (expected_gender == "M" and category_evidence == "COMPATIBLE_OR_UNKNOWN"))
            competition_scores = [value for value in
                                  (observed.get("competition_similarity"),
                                   inspection.get("competition_match_score"))
                                  if isinstance(value, (int, float)) and not isinstance(value, bool)]
            competition_score = max(competition_scores, default=None)
            competition_ok = competition_score is not None and competition_score >= 0.82
            base["evidence"]["revalidated"] = {"event_id_matches": event_same,
                                                "title_teams_match": title_ok,
                                                "absolute_time_difference_minutes": time_diff,
                                                "team_order_compatible": order_ok,
                                                "category_compatible": category_ok,
                                                "competition_match_score": competition_score,
                                                "competition_compatible": competition_ok}
            if (event_same and title_ok and time_diff is not None and time_diff <= 15 and
                    order_ok and category_ok and competition_ok):
                base.update(identity_status="IDENTITY_VERIFIED", reason="BWIN_EVENT_ID_NAMES_AND_ABSOLUTE_TIME",
                            bwin_binding_status="VERIFIED")
                verified.append(base)
                for api_row in api_verified:
                    if api_row["api_fixture_id"] == fixture["fixture_id"]:
                        api_row["bwin_binding_status"] = "VERIFIED"
                        break
            else:
                base.update(identity_status="AMBIGUOUS", reason="DECLARED_VERIFICATION_NOT_REPRODUCIBLE")
                pending.append(base)
        elif status == "EVENT_FOUND_TIME_CORROBORATED" and identity == "NAME_AND_RELATIVE_TIME_CORROBORATED":
            base.update(identity_status="TIME_CORROBORATED_NOT_VERIFIED", reason="RELATIVE_COUNTDOWN_ONLY")
            pending.append(base)
        elif status == "EVENT_FOUND_NEEDS_TIME_CONFIRMATION":
            base.update(identity_status="NEEDS_TIME_CONFIRMATION", reason="BWIN_KICKOFF_NOT_MACHINE_VERIFIED")
            pending.append(base)
        elif status in {"AMBIGUOUS_MULTIPLE_EVENTS", "LOW_CONFIDENCE_REVIEW", "EVENT_REQUIRES_REVIEW"}:
            base.update(identity_status="AMBIGUOUS", reason=status)
            pending.append(base)
        elif status == "NOT_FOUND_IN_SCANNED_PAGES":
            base.update(identity_status="NOT_FOUND_IN_SCANNED_PAGES",
                        reason="ABSENCE_IN_SCANNED_PAGES_IS_NOT_PROOF_OF_NO_EVENT",
                        catalog_has_page_failures=bool(page_failures))
            not_found.append(base)
        else:
            base.update(identity_status="CATALOG_INCOMPLETE", reason=status or "NO_BWIN_STATUS")
            pending.append(base)
    return {"schema": SCHEMA, "stage": "FIXTURE_IDENTITY_MAP",
            "generated_at_utc": now_utc().isoformat(),
            "counts": {"verified": len(verified), "api_fixture_verified": len(api_verified),
                       "api_fixture_pending": len(api_pending), "pending": len(pending),
                       "not_found": len(not_found), "excluded": len(excluded)},
            "verified": verified, "api_verified": api_verified, "api_pending": api_pending,
            "pending": pending, "not_found": not_found,
            "excluded": excluded,
            "catalog_health": {"pages_visited": len(bwin_doc.get("pages", [])),
                               "page_failures": len(page_failures),
                               "events_observed": len(bwin_doc.get("events", [])),
                               "absence_is_not_proof": True}}


def reconcile_fixture_schedules(canonical, responses, begin, end, observed_at):
    """Derive a current schedule without rewriting sporting histories or source snapshots."""
    import copy
    from generate_context import detect_gender
    from bwin_discovery import NON_ADULT_RE
    result=copy.deepcopy(canonical);audit=[];byid=defaultdict(list)
    for raw in responses:
        byid[(raw.get('fixture') or {}).get('id')].append(raw)
    for f in result['fixtures']:
        rows=byid.get(f['fixture_id'],[]);state='SCHEDULE_UNVERIFIED';reason='CURRENT_FIXTURE_RESPONSE_NOT_UNIQUE'
        original=f.get('kickoff_utc') or f.get('kickoff_madrid');current=None
        if len(rows)==1:
            raw=rows[0];pair=raw.get('teams') or {};lg=raw.get('league') or {};fx=raw.get('fixture') or {}
            identity=(pair.get('home',{}).get('id')==f['home_id'] and pair.get('away',{}).get('id')==f['away_id'] and
                      lg.get('id')==f['league_id'] and lg.get('season')==f['season'] and
                      not any(NON_ADULT_RE.search(pair.get(side,{}).get('name','')) for side in ('home','away')) and
                      (f.get('gender') not in ('F','M') or all(detect_gender(pair.get(side,{}).get('name',''))=={'F':'female','M':'male'}[f['gender']] for side in ('home','away'))))
            current=parse_dt(fx.get('date'))
            if not identity:state='IDENTITY_CONFLICT';reason='CURRENT_API_IDENTITY_CONFLICT'
            elif current is not None:
                state='SCHEDULE_VERIFIED';reason='UNCHANGED' if parse_dt(original)==current else 'RESCHEDULED'
                f.update(kickoff_utc=current.astimezone(UTC).isoformat(),kickoff_madrid=current.astimezone(MADRID).isoformat(),status=(fx.get('status') or {}).get('short'))
                if not begin<=current<end:reason='RESCHEDULED_OUT_OF_WINDOW' if parse_dt(original)!=current else 'OUTSIDE_OPERATIONAL_WINDOW'
        evidence={'fixture_id':f['fixture_id'],'original_kickoff':original,'current_kickoff':current.isoformat() if current else None,
                  'observed_at_utc':observed_at.isoformat(),'source':'API_FOOTBALL_FIXTURES_CURRENT_RESPONSE','status':state,'reason':reason}
        f['schedule_reconciliation']=evidence;audit.append(evidence)
    return result,audit


def endpoint_key(endpoint: str, params: dict[str, Any]) -> str:
    _, query = normalized_params(params)
    return f"{endpoint}?{query}"


def final_historical_rows(rows: list[Any], target_kickoff: datetime, target_id: int,
                          team_id: int, limit: int) -> list[dict[str, Any]]:
    selected: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = row.get("fixture") or {}
        fid = fixture.get("id")
        started = parse_dt(fixture.get("date"))
        teams = row.get("teams") or {}
        ids = {(teams.get("home") or {}).get("id"), (teams.get("away") or {}).get("id")}
        status = (fixture.get("status") or {}).get("short")
        if (not isinstance(fid, int) or fid == target_id or team_id not in ids or
                started is None or started >= target_kickoff or status not in FINAL_STATES):
            continue
        selected[fid] = row
    return sorted(selected.values(), key=lambda x: (str((x.get("fixture") or {}).get("date")),
                                                     int((x.get("fixture") or {}).get("id") or 0)), reverse=True)[:limit]


def stat_metric_coverage(statistics_rows: list[Any]) -> dict[str, int]:
    families = {"goals": 0, "corners": 0, "shots": 0, "shots_on_target": 0,
                "fouls": 0, "cards": 0, "offsides": 0, "possession": 0,
                "passes": 0, "xg": 0}
    aliases = {"goals": ("goals",), "corners": ("corner kicks", "corners"),
               "shots": ("total shots",), "shots_on_target": ("shots on goal",),
               "fouls": ("fouls",), "cards": ("yellow cards", "red cards"),
               "offsides": ("offsides",), "possession": ("ball possession",),
               "passes": ("total passes", "passes accurate", "passes %"),
               "xg": ("expected goals", "xg")}
    for team_row in statistics_rows:
        for stat in (team_row or {}).get("statistics", []) if isinstance(team_row, dict) else []:
            label = str((stat or {}).get("type") or "").casefold()
            value = (stat or {}).get("value")
            if value is None:
                continue
            for family, names in aliases.items():
                if label in names:
                    families[family] += 1
    return families


def player_metric_coverage(player_rows: list[Any]) -> dict[str, int]:
    fields = {"minutes": 0, "starter": 0, "substitute": 0, "shots": 0,
              "shots_on_target": 0, "tackles": 0, "interceptions": 0,
              "duels": 0, "dribbles": 0, "fouls": 0, "cards": 0,
              "passes": 0, "assists": 0}
    for team in player_rows:
        for player in (team or {}).get("players", []) if isinstance(team, dict) else []:
            for stat in player.get("statistics", []) if isinstance(player, dict) else []:
                games = stat.get("games") or {}; shots = stat.get("shots") or {}
                tackles = stat.get("tackles") or {}; duels = stat.get("duels") or {}
                dribbles = stat.get("dribbles") or {}; fouls = stat.get("fouls") or {}
                cards = stat.get("cards") or {}; passes = stat.get("passes") or {}
                goals = stat.get("goals") or {}; substitutes = stat.get("substitutes") or {}
                values = {"minutes": games.get("minutes"), "starter": games.get("position"),
                          "substitute": substitutes.get("in"), "shots": shots.get("total"),
                          "shots_on_target": shots.get("on"), "tackles": tackles.get("total"),
                          "interceptions": tackles.get("interceptions"), "duels": duels.get("total"),
                          "dribbles": dribbles.get("attempts"), "fouls": fouls.get("committed"),
                          "cards": cards.get("yellow") if cards.get("yellow") is not None else cards.get("red"),
                          "passes": passes.get("total"), "assists": goals.get("assists")}
                for field, value in values.items():
                    if value is not None:
                        fields[field] += 1
    return fields


class DeepCollector:
    def __init__(self, client: Phase1ApiClient, *, history: int, cutoff: datetime):
        self.client = client
        self.history = history
        self.cutoff = cutoff
        self.records: dict[str, dict[str, Any]] = {}
        self.results: dict[str, ApiResult] = {}
        self._active_keys: list[str] = []
        self.fixture_payloads: dict[int, dict[str, ApiResult]] = defaultdict(dict)

    def request(self, endpoint: str, params: dict[str, Any], *, scope: dict[str, Any]) -> ApiResult | None:
        key = endpoint_key(endpoint, params)
        self._active_keys.append(key)
        if key in self.records and self.records[key].get("status") in {"AVAILABLE", "EMPTY"}:
            consumers = self.records[key].setdefault("consumers", [])
            if scope not in consumers:
                consumers.append(dict(scope))
            self.records[key]["reuse_count"] = int(self.records[key].get("reuse_count") or 0) + 1
            return self.results.get(key)
        try:
            result = self.client.get(endpoint, **params)
            target_kickoff = parse_dt(scope.get("target_kickoff_utc"))
            acquired = parse_dt(result.fetched_at_utc)
            temporal = ("AVAILABLE_BEFORE_TARGET_KICKOFF" if target_kickoff and acquired and acquired < target_kickoff
                        else "RETROSPECTIVE_ONLY" if target_kickoff and acquired else "UNKNOWN")
            self.records[key] = {"endpoint": endpoint, "parameters": result.params,
                                 "status": "AVAILABLE" if result.response else "EMPTY",
                                 "count": len(result.response), "source": result.source,
                                 "cache_path": str(result.cache_path) if result.cache_path else None,
                                 "fetched_at_utc": result.fetched_at_utc,
                                 "completeness": result.completeness,
                                 "temporal_eligibility": temporal, "consumers": [dict(scope)],
                                 "reuse_count": 0, **scope}
            self.results[key] = result
            return result
        except CacheMissOffline as exc:
            self.records[key] = {"endpoint": endpoint, "parameters": normalized_params(params)[0],
                                 "status": "CACHE_MISS_OFFLINE", "reason": str(exc),
                                 "consumers": [dict(scope)], **scope}
        except BudgetExhausted as exc:
            self.records[key] = {"endpoint": endpoint, "parameters": normalized_params(params)[0],
                                 "status": "BUDGET_EXHAUSTED", "reason": str(exc),
                                 "consumers": [dict(scope)], **scope}
        except (Phase1Error, ValueError) as exc:
            self.records[key] = {"endpoint": endpoint, "parameters": normalized_params(params)[0],
                                 "status": "ERROR", "reason": str(exc),
                                 "consumers": [dict(scope)], **scope}
        return None

    def request_pages(self, endpoint: str, params: dict[str, Any], *,
                      scope: dict[str, Any]) -> list[ApiResult]:
        """Acquire every advertised page while retaining per-page cache provenance."""
        first = self.request(endpoint, params, scope=scope)
        if first is None:
            return []
        results = [first]
        paging = (first.raw_payload or {}).get("paging") or {}
        total = int(paging.get("total") or 1)
        current = int(paging.get("current") or 1)
        while current < total:
            current += 1
            page = self.request(endpoint, {**params, "page": current}, scope=scope)
            if page is None:
                break
            results.append(page)
        return results

    def collect_fixture(self, identity: dict[str, Any]) -> dict[str, Any]:
        fid = int(identity["api_fixture_id"])
        target_kickoff = parse_dt(identity.get("kickoff_utc"))
        if target_kickoff is None:
            return {"api_fixture_id": fid, "status": "BLOCKED_NO_KICKOFF", "requests": []}
        if target_kickoff <= self.cutoff:
            return {"api_fixture_id": fid, "status": "BLOCKED_TARGET_NOT_FUTURE", "requests": []}
        bwin_bound = identity.get("bwin_binding_status") == "VERIFIED" and bool(identity.get("bwin_event_id"))
        scope = {"api_fixture_id": fid, "bwin_event_id": identity.get("bwin_event_id"),
                 "bwin_binding_status": "VERIFIED" if bwin_bound else "UNVERIFIED",
                 "target_kickoff_utc": target_kickoff.isoformat()}
        self._active_keys = []
        league = identity.get("league_id"); season = identity.get("season")

        # Prioritize historical detail so a small pilot spends its budget on
        # statistics, players, lineups and events before optional context.
        self.request("fixtures", {"id": fid}, scope={**scope, "role": "target_fixture"})
        historical_ids: set[int] = set()
        for side in ("home", "away"):
            team_id = int(identity[f"{side}_team_id"])
            team_scope = {**scope, "team_side": side, "team_id": team_id}
            history_result = self.request("fixtures", {"team": team_id, "last": max(self.history, 1)},
                                          scope={**team_scope, "role": "team_history"})
            if history_result:
                for row in final_historical_rows(history_result.response, target_kickoff, fid, team_id, self.history):
                    historical_ids.add(int(row["fixture"]["id"]))
        ordered_historical_ids = sorted(historical_ids)
        for start in range(0, len(ordered_historical_ids), 20):
            batch = ordered_historical_ids[start:start + 20]
            self.request("fixtures", {"ids": "-".join(map(str, batch))},
                         scope={**scope, "role": "historical_fixtures_batch",
                                "historical_fixture_ids": batch})
        for historical_id in ordered_historical_ids:
            hist_scope = {**scope, "historical_fixture_id": historical_id, "role": "historical_detail"}
            for endpoint in ("fixtures/statistics", "fixtures/events", "fixtures/lineups", "fixtures/players"):
                params = {"fixture": historical_id}
                result = self.request(endpoint, params, scope=hist_scope)
                if result:
                    self.fixture_payloads[historical_id][endpoint] = result

        # Optional target/competition context follows the historical detail.
        self.request("fixtures/headtohead", {"h2h": f"{identity['home_team_id']}-{identity['away_team_id']}",
                                             "last": min(20, self.history)}, scope={**scope, "role": "head_to_head"})
        self.request("injuries", {"fixture": fid}, scope={**scope, "role": "target_availability"})
        if league and season:
            for endpoint, params, role in (
                ("standings", {"league": league, "season": season}, "competition_context"),
                ("leagues", {"id": league, "season": season}, "coverage_contract"),
                ("fixtures/rounds", {"league": league, "season": season, "dates": "true",
                                     "timezone": "Europe/Madrid"}, "rounds"),
                ("players/topscorers", {"league": league, "season": season}, "league_players"),
                ("players/topassists", {"league": league, "season": season}, "league_players"),
                ("players/topyellowcards", {"league": league, "season": season}, "league_players"),
                ("players/topredcards", {"league": league, "season": season}, "league_players")):
                if endpoint.startswith("players/top"):
                    self.request_pages(endpoint, params, scope={**scope, "role": role})
                else:
                    self.request(endpoint, params, scope={**scope, "role": role})
        squad_player_ids: set[int] = set()
        coach_ids: set[int] = set()
        for side in ("home", "away"):
            team_id = int(identity[f"{side}_team_id"])
            team_scope = {**scope, "team_side": side, "team_id": team_id}
            self.request("teams", {"id": team_id}, scope={**team_scope, "role": "team_identity"})
            if league and season:
                self.request("teams/statistics", {"team": team_id, "league": league,
                                                  "season": season, "date": target_kickoff.date().isoformat()},
                             scope={**team_scope, "role": "team_season_asof"})
                self.request_pages("players", {"team": team_id, "season": season},
                                   scope={**team_scope, "role": "player_season"})
            squad = self.request("players/squads", {"team": team_id},
                                 scope={**team_scope, "role": "squad"})
            if squad:
                for team in squad.response:
                    for player in (team or {}).get("players", []) if isinstance(team, dict) else []:
                        if isinstance((player or {}).get("id"), int):
                            squad_player_ids.add(player["id"])
            coaches = self.request("coachs", {"team": team_id},
                                   scope={**team_scope, "role": "coaches"})
            if coaches:
                for coach in coaches.response:
                    if isinstance((coach or {}).get("id"), int):
                        coach_ids.add(coach["id"])
            self.request("transfers", {"team": team_id}, scope={**team_scope, "role": "transfers"})
        for start in range(0, len(squad_player_ids), 20):
            batch = sorted(squad_player_ids)[start:start + 20]
            self.request("sidelined", {"players": "-".join(map(str, batch))},
                         scope={**scope, "role": "player_sidelined"})
        if coach_ids:
            self.request("sidelined", {"coachs": "-".join(map(str, sorted(coach_ids)[:20]))},
                         scope={**scope, "role": "coach_sidelined"})
        keys = sorted(set(self._active_keys))
        statuses = Counter(self.records[key]["status"] for key in keys)
        required = {}
        pending_detail = []
        for historical_id in ordered_historical_ids:
            required[str(historical_id)] = {}
            for endpoint in ("fixtures/statistics", "fixtures/players", "fixtures/lineups", "fixtures/events"):
                key = endpoint_key(endpoint, {"fixture": historical_id})
                record = self.records.get(key)
                state = {"status": record.get("status") if record else "NOT_REQUESTED",
                         "completeness": record.get("completeness") if record else None}
                required[str(historical_id)][endpoint] = state
                if state["status"] != "AVAILABLE" or state["completeness"] != "COMPLETE_RESPONSE":
                    pending_detail.append({"historical_fixture_id": historical_id,
                                           "endpoint": endpoint, **state})
        if not ordered_historical_ids:
            result_status = "INCOMPLETE_NO_HISTORICAL_FIXTURES"
        elif pending_detail:
            result_status = "INCOMPLETE_HISTORICAL_DETAIL"
        elif any(k in statuses for k in ("CACHE_MISS_OFFLINE", "BUDGET_EXHAUSTED", "ERROR")):
            result_status = "INCOMPLETE_CONTEXT"
        else:
            result_status = "COMPLETE"
        return {"api_fixture_id": fid, "bwin_event_id": identity.get("bwin_event_id"),
                "bwin_binding_status": "VERIFIED" if bwin_bound else "UNVERIFIED",
                "market_identity_ready": bwin_bound,
                "status": result_status, "historical_fixture_ids": ordered_historical_ids,
                "historical_detail_status": required, "pending_historical_detail": pending_detail,
                "request_statuses": dict(statuses), "requests": keys}

    def coverage(self, fixture_results: list[dict[str, Any]]) -> dict[str, Any]:
        endpoint_status: defaultdict[str, Counter[str]] = defaultdict(Counter)
        fixture_endpoint_status: defaultdict[str, defaultdict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
        team_endpoint_status: defaultdict[str, defaultdict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
        for row in self.records.values():
            endpoint_status[row["endpoint"]][row["status"]] += 1
            for consumer in row.get("consumers", []):
                fid = consumer.get("api_fixture_id")
                if fid is not None:
                    fixture_endpoint_status[str(fid)][row["endpoint"]][row["status"]] += 1
                team_id = consumer.get("team_id")
                if team_id is not None:
                    team_endpoint_status[str(team_id)][row["endpoint"]][row["status"]] += 1
        stat_coverage = Counter(); player_coverage = Counter(); historical = 0
        historical_metric_coverage = {}
        for historical_id, payloads in self.fixture_payloads.items():
            historical += 1
            stats = payloads.get("fixtures/statistics")
            players = payloads.get("fixtures/players")
            fixture_stats = stat_metric_coverage(stats.response) if stats else stat_metric_coverage([])
            fixture_players = player_metric_coverage(players.response) if players else player_metric_coverage([])
            if stats:
                stat_coverage.update(fixture_stats)
            if players:
                player_coverage.update(fixture_players)
            historical_metric_coverage[str(historical_id)] = {
                "team_metric_non_null_observations": fixture_stats,
                "player_metric_non_null_observations": fixture_players}
        nested = lambda value: {outer: {endpoint: dict(states) for endpoint, states in sorted(endpoints.items())}
                                for outer, endpoints in sorted(value.items())}
        never_available = sorted(endpoint for endpoint, states in endpoint_status.items()
                                 if not states.get("AVAILABLE"))
        reuse_by_endpoint = Counter()
        reuse_records = []
        for key, record in self.records.items():
            count = int(record.get("reuse_count") or 0)
            if count:
                reuse_by_endpoint[record["endpoint"]] += count
                reuse_records.append({"request_key": key, "endpoint": record["endpoint"],
                                      "reuse_count": count, "consumers": record.get("consumers", [])})
        return {"schema": SCHEMA, "generated_at_utc": now_utc().isoformat(),
                "fixtures": fixture_results,
                "endpoint_coverage": {endpoint: dict(counts) for endpoint, counts in sorted(endpoint_status.items())},
                "fixture_endpoint_coverage": nested(fixture_endpoint_status),
                "team_endpoint_coverage": nested(team_endpoint_status),
                "endpoints_not_requested": sorted(ENDPOINTS - set(endpoint_status)),
                "endpoints_without_available_data": never_available,
                "reuse": {"total_reuses": sum(reuse_by_endpoint.values()),
                           "by_endpoint": dict(sorted(reuse_by_endpoint.items())),
                           "records": reuse_records},
                "historical_fixtures_inspected": historical,
                "historical_fixture_metric_coverage": historical_metric_coverage,
                "team_metric_non_null_observations": dict(stat_coverage),
                "player_metric_non_null_observations": dict(player_coverage),
                "notes": ["Conteos de campos no nulos; cero observado se conserva como observado.",
                          "EMPTY no equivale a cobertura definitiva; conserva TTL corto.",
                          "RETROSPECTIVE_ONLY no puede alimentar una reconstrucción PREMATCH histórica."]}


def auto_bwin_sources(root: Path, key: str) -> list[Path]:
    base = root / "outputs" / "discovery" / key / "prospective_trial" / "bwin"
    direct = base / "bwin_discovery.json"
    paths = [direct] if direct.is_file() else []
    paths.extend(sorted((base / "batches").glob("*/bwin_discovery.json")) if (base / "batches").is_dir() else [])
    phase1_scan = root / "outputs" / "phase1" / key / "bwin_scan" / "bwin_discovery.json"
    if phase1_scan.is_file():
        paths.append(phase1_scan)
    return list(dict.fromkeys(path.resolve() for path in paths))


def run_bwin_once(root: Path, fixtures_path: Path, output_dir: Path, args: argparse.Namespace) -> Path:
    command = [sys.executable, str(root / "bwin_discovery.py"), "--input", str(fixtures_path),
               "--top", "0", "--output-dir", str(output_dir), "--max-country-pages",
               str(args.bwin_max_country_pages), "--timeout", str(args.bwin_timeout)]
    if args.bwin_headless:
        command.append("--headless")
    log = output_dir.parent / "logs" / "bwin_discovery.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.run(command, cwd=root, stdout=handle, stderr=subprocess.STDOUT, check=False)
    if process.returncode not in (0, 2, 4):
        raise Phase1Error(f"bwin_discovery.py terminó {process.returncode}; {log}")
    result = output_dir / "bwin_discovery.json"
    if not result.is_file():
        raise Phase1Error("bwin_discovery no generó JSON")
    return result


def write_summary(run_dir: Path, fixtures: dict[str, Any], bwin: dict[str, Any], identities: dict[str, Any],
                  coverage: dict[str, Any], client: Phase1ApiClient, deep: list[dict[str, Any]],
                  progress: dict[str, Any]) -> dict[str, Any]:
    completed = sum(row.get("status") == "COMPLETE" for row in deep)
    incomplete = sum(row.get("status") != "COMPLETE" for row in deep)
    if client.offline:
        run_status = ("OFFLINE_AUDIT_COMPLETE" if identities["counts"]["verified"]
                      else "OFFLINE_AUDIT_COMPLETE_NO_BWIN_BINDING")
    else:
        run_status = ("ONLINE_ACQUISITION_COMPLETE" if identities["counts"]["verified"] and not incomplete
                      else "ONLINE_PARTIAL")
    limitations = []
    if not identities["counts"]["verified"]:
        limitations.append("BWIN_MARKET_BINDING_NOT_READY")
    if identities["counts"]["pending"] or identities["counts"]["not_found"]:
        limitations.append("IDENTITY_CATALOG_HAS_PENDING_ROWS")
    if incomplete:
        limitations.append("DEEP_ACQUISITION_INCOMPLETE")
    summary = {"schema": SCHEMA, "generated_at_utc": now_utc().isoformat(),
               "status": run_status, "critical_limitations": limitations,
               "counts": {"fixtures_discovered": len(fixtures.get("fixtures", [])),
                          "fixtures_eligible_adult": fixtures.get("counts", {}).get("eligible", 0),
                          "fixtures_category_review": fixtures.get("counts", {}).get("review", 0),
                          "fixtures_category_excluded": fixtures.get("counts", {}).get("excluded", 0),
                          "bwin_events_observed": len(bwin.get("events", [])),
                          "bwin_pages_visited_unique": len(bwin.get("pages", [])),
                          "identity_verified": identities["counts"]["verified"],
                          "api_fixture_verified": identities["counts"].get("api_fixture_verified", 0),
                          "api_fixture_pending": identities["counts"].get("api_fixture_pending", 0),
                          "identity_pending": identities["counts"]["pending"],
                          "not_found_in_scanned_pages": identities["counts"]["not_found"],
                          "deep_without_bwin_binding": sum(row.get("bwin_binding_status") != "VERIFIED" for row in deep),
                          "deep_complete": completed, "deep_incomplete": incomplete},
               "api_usage": client.statistics(),
               "coverage": {"historical_fixtures_inspected": coverage.get("historical_fixtures_inspected"),
                            "endpoint_coverage": coverage.get("endpoint_coverage"),
                            "endpoints_not_requested": coverage.get("endpoints_not_requested", []),
                            "endpoints_without_available_data": coverage.get("endpoints_without_available_data", []),
                            "reuse": coverage.get("reuse", {})},
               "bwin_catalog_scope": bwin.get("merge_diagnostics", {}),
               "pending_reasons": dict(Counter(row.get("reason") for group in
                                                (identities["pending"], identities["not_found"])
                                                for row in group)),
               "files": {name: str((run_dir / name).resolve()) for name in
                         ("fixtures_discovered.json", "bwin_catalog.json", "fixture_identity_map.json",
                          "api_coverage.json", "raw_manifest.json", "phase1_progress.json",
                          "phase1_summary.json", "phase1_summary.md", "phase1_execution.log")},
               "prohibitions": {"prediction_engines_executed": False,
                                "qualitative_ai_executed": False, "picks_generated": False,
                                "bwin_odds_scraper_executed": False},
               "progress": progress}
    atomic_json(run_dir / "phase1_summary.json", summary)
    lines = ["# PREMATCH Fase 1 — Data Foundation", "",
             f"- Estado: **{summary['status']}**", f"- Generado UTC: `{summary['generated_at_utc']}`",
             f"- Fixtures descubiertos: **{summary['counts']['fixtures_discovered']}**",
             f"- Adultos elegibles: **{summary['counts']['fixtures_eligible_adult']}**; review: **{summary['counts']['fixtures_category_review']}**; excluidos: **{summary['counts']['fixtures_category_excluded']}**",
             f"- Eventos Bwin observados: **{summary['counts']['bwin_events_observed']}** en **{summary['counts']['bwin_pages_visited_unique']}** páginas únicas",
             "", "## Alcance del catálogo Bwin", "",
             f"- Unión consolidada: **{summary['bwin_catalog_scope'].get('union_unique_events', 0)}** eventos / **{summary['bwin_catalog_scope'].get('union_unique_pages', 0)}** páginas.",
             "- El detalle por origen, número de snapshots y rango temporal queda en `bwin_catalog.json` y `phase1_summary.json`.",
             f"- Identidades verificadas: **{summary['counts']['identity_verified']}**; pendientes: **{summary['counts']['identity_pending']}**; no localizados: **{summary['counts']['not_found_in_scanned_pages']}**",
             f"- Fixtures API verificables para adquisición: **{summary['counts'].get('api_fixture_verified', 0)}**; adquisición sin unión Bwin: **{summary['counts'].get('deep_without_bwin_binding', 0)}**",
             f"- Adquisición profunda completa: **{completed}**; incompleta: **{incomplete}**",
             f"- API nuevas: **{client.new_calls}**; caché: **{client.cache_hits}**; reintentos: **{client.retries}**; errores: **{client.errors}**", "",
             "## Cobertura por endpoint", "", "| Endpoint | Estados |", "|---|---|"]
    for endpoint, states in coverage.get("endpoint_coverage", {}).items():
        lines.append(f"| `{endpoint}` | {', '.join(f'{k}={v}' for k, v in sorted(states.items()))} |")
    lines += ["", "## Endpoints sin cobertura", "",
              "- No solicitados: " + (", ".join(f"`{x}`" for x in coverage.get("endpoints_not_requested", [])) or "ninguno"),
              "- Solicitados sin respuesta disponible: " + (", ".join(f"`{x}`" for x in coverage.get("endpoints_without_available_data", [])) or "ninguno")]
    lines += ["", "## Pendientes", ""]
    for reason, count in sorted(summary["pending_reasons"].items(), key=lambda x: (-x[1], str(x[0]))):
        lines.append(f"- `{reason}`: {count}")
    lines += ["", "No se ejecutaron motores predictivos/tácticos, IA cualitativa, picks ni scraper profundo de cuotas.", ""]
    (run_dir / "phase1_summary.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def collect_longitudinal(client, preselection, fixture_id, cutoff, output_dir, history=5):
    """Piloto selectivo: metadatos faltantes, players y lineups; sin Bwin."""
    from prematch_phase2 import PlayerCache, _completion, _dt
    source = json.loads(Path(preselection).read_text(encoding="utf-8"))
    matches = [m for m in source.get("matches", []) if m.get("fixture_id") == fixture_id]
    if len(matches) != 1 or not cutoff:
        raise Phase1Error("LONGITUDINAL_IDENTITY_OR_CUTOFF_INVALID")
    match = matches[0]
    cutoff = min(cutoff, _dt(match["analysis_cutoff_utc"]), _dt(match["kickoff_madrid"]))
    planned = {}
    for side in ("home", "away"):
        rows = [r for r in match["statistical_observations"][side] if r.get("team_id") == match[side + "_id"] and
                r.get("fixture_id") != fixture_id and r.get("status") in FINAL_STATES and
                _dt(r.get("kickoff_utc")) and _dt(r["kickoff_utc"]) + timedelta(hours=3) < cutoff]
        unique = {r["fixture_id"]: r for r in rows}
        planned[side] = sorted(unique.values(), key=lambda r: _dt(r["kickoff_utc"]), reverse=True)[:history]
    cache = PlayerCache(client.cache_dir)
    ids = sorted({r["fixture_id"] for rows in planned.values() for r in rows})
    missing = [fid for fid in ids if cache.get("fixtures", fid, cutoff)[0] is None]
    report = {"fixture_id": fixture_id, "cutoff_utc": cutoff.isoformat(), "planned": planned,
              "initial_metadata_missing": missing, "requests": [], "status": "PARTIAL"}
    def save():
        report["api_calls"] = client.new_calls
        report["ledger"] = client.ledger
        atomic_json(Path(output_dir) / "longitudinal_acquisition.json", report)
    try:
        if missing:
            result = client.get("fixtures", ids="-".join(map(str, missing)))
            report["requests"].append({"endpoint": "fixtures", "ids": missing, "source": result.source})
        cache = PlayerCache(client.cache_dir)
        valid = []
        for fid in ids:
            raw, ref = cache.get("fixtures", fid, cutoff)
            rows = [r for group in planned.values() for r in group if r["fixture_id"] == fid]
            if raw and _completion(raw.get("fixture", {}), ref, cutoff) and all(
                raw.get("league", {}).get("id") == r["league_id"] and raw.get("league", {}).get("season") == r["season"] and
                _dt(raw["fixture"]["date"]) == _dt(r["kickoff_utc"]) and
                raw.get("teams", {}).get(r["venue"].lower(), {}).get("id") == r["team_id"] for r in rows):
                valid.append(fid)
        report["verified_historical_ids"] = valid
        for endpoint in ("fixtures/players", "fixtures/lineups"):
            for fid in valid:
                existing, ref = cache.get(endpoint, fid, cutoff)
                if existing:
                    report["requests"].append({"endpoint": endpoint, "fixture_id": fid, "source": "existing_cache", "sha256": ref["sha256"]})
                else:
                    result = client.get(endpoint, fixture=fid)
                    report["requests"].append({"endpoint": endpoint, "fixture_id": fid, "source": result.source,
                                               "rows": len(result.response), "status": "AVAILABLE" if result.response else "EMPTY"})
                save()
        report["status"] = "ACQUISITION_FINISHED_REQUIRES_OFFLINE_VALIDATION"
    except (Phase1Error, ValueError) as exc:
        report["error"] = str(exc)
    save()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="Día inicial Madrid YYYY-MM-DD")
    parser.add_argument("--start", type=parse_clock, default=clock_time(15, 0))
    parser.add_argument("--end", type=parse_clock, default=clock_time(3, 0))
    parser.add_argument("--fixtures-input", type=Path, help="fixtures.json ligero ya adquirido")
    parser.add_argument("--bwin-input", type=Path, action="append", default=[], help="bwin_discovery.json; repetible")
    parser.add_argument("--output-dir", type=Path)
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--online", action="store_true", help="Autoriza API y catálogo Bwin público")
    network.add_argument("--offline", action="store_true", help="Fuerza caché/snapshots solamente (predeterminado)")
    parser.add_argument("--discover-bwin", action="store_true", help="Con --online: ejecutar una sola exploración Bwin compartida")
    parser.add_argument("--max-api-calls", type=int, default=0, help="Límite duro de solicitudes HTTP API nuevas")
    parser.add_argument("--max-matches", type=int, default=0, help="Máximo de fixtures API verificables a profundizar; 0=todas")
    parser.add_argument("--fixture-id", type=int, action="append", default=[],
                        help="Fixture API-Football exacto para el piloto; repetible (máximo 3 recomendado)")
    parser.add_argument("--history", type=int, default=5, help="Históricos por equipo (1..20)")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--longitudinal-preselection", type=Path)
    parser.add_argument("--asof", type=parse_dt)
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--bwin-headless", action="store_true")
    parser.add_argument("--bwin-max-country-pages", type=int, default=30)
    parser.add_argument("--bwin-timeout", type=int, default=25000)
    args = parser.parse_args(argv)
    if (args.max_api_calls < 0 or args.max_matches < 0 or not 1 <= args.history <= 20 or
            any(fid <= 0 for fid in args.fixture_id)):
        parser.error("max-api-calls/max-matches >=0 e history 1..20")
    if args.online and args.max_api_calls < 1:
        parser.error("--online exige --max-api-calls >0")
    if args.discover_bwin and not args.online:
        parser.error("--discover-bwin exige --online")
    root = args.root.expanduser().resolve()
    day = args.date or active_window_day(datetime.now(MADRID), args.start, args.end)
    begin, finish = window_bounds(day, args.start, args.end)
    key = window_key(begin, finish)
    run_dir = (args.output_dir.expanduser().resolve() if args.output_dir else
               root / "outputs" / "phase1" / key)
    run_dir.mkdir(parents=True, exist_ok=True)
    progress_path = run_dir / "phase1_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if args.resume and progress_path.is_file() else {
        "schema": SCHEMA, "window": {"start_madrid": begin.isoformat(), "end_madrid": finish.isoformat()},
        "started_at_utc": now_utc().isoformat(), "stages": {}}
    cache_dir = root / "outputs" / ".api_football_history_cache"
    try:
        from config.constants import FOOTBALL_BASE_URL, REQUEST_TIMEOUT
        client = Phase1ApiClient(cache_dir, offline=not args.online, max_new_calls=args.max_api_calls,
                                 base_url=FOOTBALL_BASE_URL, timeout=REQUEST_TIMEOUT)
        if args.longitudinal_preselection:
            if len(args.fixture_id) != 1 or not args.asof or args.max_api_calls > 25:
                raise Phase1Error("LONGITUDINAL_REQUIRES_ONE_FIXTURE_ASOF_AND_MAX_25_CALLS")
            result = collect_longitudinal(client, args.longitudinal_preselection, args.fixture_id[0], args.asof, run_dir, args.history)
            print(json.dumps({"status": result["status"], "api_calls": result["api_calls"], "error": result.get("error")}))
            return 0 if "error" not in result else 2
        fixtures_source = args.fixtures_input.expanduser().resolve() if args.fixtures_input else locate_fixture_source(root, key)
        if fixtures_source and fixtures_source.is_file():
            source_doc = json.loads(fixtures_source.read_text(encoding="utf-8-sig"))
        else:
            source_doc = discovery_report_from_api(client, begin, finish, now_utc())
            fixtures_source = None
        fixtures_doc = canonical_fixtures(source_doc, fixtures_source)
        fixtures_path = run_dir / "fixtures_discovered.json"
        atomic_json(fixtures_path, fixtures_doc)
        progress["stages"]["fixtures"] = {"status": "COMPLETE", "path": str(fixtures_path.resolve()),
                                            "count": len(fixtures_doc["fixtures"])}
        atomic_json(progress_path, progress)

        bwin_paths = [p.expanduser().resolve() for p in args.bwin_input]
        if args.discover_bwin:
            bwin_paths = [run_bwin_once(root, fixtures_path, run_dir / "bwin_scan", args)]
        elif not bwin_paths:
            bwin_paths = auto_bwin_sources(root, key)
        bwin_doc = merge_bwin_snapshots([p for p in bwin_paths if p.is_file()])
        atomic_json(run_dir / "bwin_catalog.json", bwin_doc)
        progress["stages"]["bwin_catalog"] = {"status": "COMPLETE" if bwin_paths else "INCOMPLETE_NO_SOURCE",
                                                "sources": len(bwin_paths),
                                                "events": len(bwin_doc["events"]),
                                                "unique_pages": len(bwin_doc["pages"])}
        identities = identity_map(fixtures_doc, bwin_doc)
        atomic_json(run_dir / "fixture_identity_map.json", identities)
        progress["stages"]["identity"] = {"status": "COMPLETE", **identities["counts"]}
        atomic_json(progress_path, progress)

        collector = DeepCollector(client, history=args.history, cutoff=now_utc())
        # API fixture verification is sufficient for historical acquisition;
        # Bwin verification remains mandatory for any later market/odds stage.
        candidates = identities.get("api_verified", [])
        if args.fixture_id:
            by_fixture = {int(row["api_fixture_id"]): row for row in candidates}
            missing = [fid for fid in dict.fromkeys(args.fixture_id) if fid not in by_fixture]
            if missing:
                raise Phase1Error(f"FIXTURE_ID_NOT_API_VERIFIED: {missing}")
            candidates = [by_fixture[fid] for fid in dict.fromkeys(args.fixture_id)]
        if args.max_matches:
            candidates = candidates[:args.max_matches]
        deep = [] if args.discovery_only else [collector.collect_fixture(row) for row in candidates]
        coverage = collector.coverage(deep)
        atomic_json(run_dir / "api_coverage.json", coverage)
        manifest = {"schema": SCHEMA, "generated_at_utc": now_utc().isoformat(),
                    "cache_root": str(cache_dir.resolve()),
                    "entries": sorted(client.used_cache_files.values(), key=lambda x: x["path"]),
                    "request_ledger": client.ledger,
                    "api_statistics": client.statistics()}
        atomic_json(run_dir / "raw_manifest.json", manifest)
        deep_stage_status = ("SKIPPED_DISCOVERY_ONLY" if args.discovery_only else
                             "SKIPPED_NO_VERIFIED_IDENTITIES" if not candidates else
                             "COMPLETE" if deep and all(row.get("status") == "COMPLETE" for row in deep) else
                             "COMPLETE_WITH_PENDING")
        progress["stages"]["deep_acquisition"] = {"status": deep_stage_status,
                                                    "fixtures_attempted": len(deep),
                                                    "requested_fixture_ids": args.fixture_id,
                                                    "api_statistics": client.statistics()}
        progress["finished_at_utc"] = now_utc().isoformat()
        atomic_json(progress_path, progress)
        summary = write_summary(run_dir, fixtures_doc, bwin_doc, identities, coverage, client, deep, progress)
        log_lines = [json.dumps({"time_utc": now_utc().isoformat(), "kind": "SUMMARY",
                                  "status": summary["status"], "counts": summary["counts"],
                                  "api_usage": summary["api_usage"]}, ensure_ascii=False)]
        log_lines.extend(json.dumps({"kind": "API_OPERATION", **entry}, ensure_ascii=False, sort_keys=True)
                         for entry in client.ledger)
        (run_dir / "phase1_execution.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        print(f"FASE 1: {summary['status']}")
        print(f"Salida: {run_dir}")
        print(f"Fixtures={summary['counts']['fixtures_discovered']} Bwin={summary['counts']['bwin_events_observed']} "
              f"verificados={summary['counts']['identity_verified']} API_nuevas={client.new_calls} cache={client.cache_hits}")
        print(f"Resumen: {run_dir / 'phase1_summary.md'}")
        return 0
    except (Phase1Error, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        progress.setdefault("stages", {})["error"] = {"status": "ERROR", "detail": str(exc),
                                                        "time_utc": now_utc().isoformat()}
        atomic_json(progress_path, progress)
        (run_dir / "phase1_execution.log").write_text(
            json.dumps({"time_utc": now_utc().isoformat(), "kind": "FATAL_ERROR",
                        "error_type": type(exc).__name__, "detail": str(exc)},
                       ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        print(f"FASE_1_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
