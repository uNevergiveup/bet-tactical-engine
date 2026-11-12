#!/usr/bin/env python3
"""API-Football v3 historical depth collector, incremental, with explicit online opt-in.

Additive: no edits to original match_engine or project manifests.  A post-kickoff
live historical replay is intentionally disallowed; for past fixtures run offline.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from match_performance_engine import aware, COMPLETION_BUFFER

HOST = "https://v3.football.api-sports.io"
FINAL = {"FT"}  # strict regular-time scope; AET/PEN not mixed silently
NEGATIVE_STATISTICS_CACHE_TTL = timedelta(minutes=5)
POSITIVE_CACHE_TTL = timedelta(days=30)


def dump_json(path: Path, data: Any) -> str:
    blob = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Fetch:
    calls: int = 0
    hits: int = 0
    max_calls: int = 110


class ApiClient:
    def __init__(self, cache_dir: Path, *, max_calls: int = 110, key: str | None = None):
        self.cache_dir = cache_dir
        self.key = (
                key
                or os.environ.get("FOOTBALL_API_KEY")
                or os.environ.get("API_FOOTBALL_KEY")
                or os.environ.get("APISPORTS_KEY")
        )
        if not self.key:
            raise ValueError("MISSING_KEY: export API_FOOTBALL_KEY=... (nunca ponerla en scripts)")
        self.stat = Fetch(max_calls=max_calls)

    def get(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        if endpoint not in {"fixtures", "fixtures/statistics", "fixtures/events", "fixtures/lineups", "fixtures/players"}:
            raise ValueError("ENDPOINT_NOT_ALLOWED")
        params = {k: v for k, v in params.items() if v is not None}
        normalized = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
        token = hashlib.sha256(f"v3:{endpoint}?{normalized}".encode()).hexdigest()[:24]
        cache = self.cache_dir / endpoint.replace("/", "_") / (token + ".json")
        if cache.exists():
            document = json.loads(cache.read_text(encoding="utf-8"))
            if document.get("query") != normalized or document.get("endpoint") != endpoint:
                raise ValueError("CACHE_IDENTITY_COLLISION")
            # Ignore cached material recorded in the future (bad clock or replay).
            received_at = aware(document["fetched_at_utc"], "cache.fetched_at_utc")
            if received_at > datetime.now(timezone.utc) + timedelta(seconds=10):
                raise ValueError("FUTURE_CACHE_REJECTED")
            # Las respuestas vacías de estadísticas son negativas, no evidencia
            # permanente: una captura transitoriamente incompleta debe poder
            # recuperarse en una ejecución posterior.
            cache_kind = document.get("cache_kind")
            if cache_kind not in {"positive", "negative"}:
                cache_kind = "negative" if not document.get("response") else "positive"
            ttl = (NEGATIVE_STATISTICS_CACHE_TTL
                   if endpoint == "fixtures/statistics" and cache_kind == "negative"
                   else timedelta(hours=1)
                   if endpoint == "fixtures" and "last=" in normalized
                   else POSITIVE_CACHE_TTL)
            stale = datetime.now(timezone.utc) - received_at > ttl
            if not stale:
                self.stat.hits += 1
                return document["response"]
        if self.stat.calls >= self.stat.max_calls:
            raise BudgetExhausted(f"BUDGET_MAX_CALLS_{self.stat.max_calls}")
        url = f"{HOST}/{endpoint}?{normalized}"
        req = Request(url, headers={"x-apisports-key": self.key, "Accept": "application/json",
                                    "User-Agent": "prematch-depth-v1042"})
        for attempt in range(3):
            self.stat.calls += 1
            try:
                with urlopen(req, timeout=18) as res:
                    data = json.loads(res.read().decode("utf-8"))
                    remaining = res.headers.get("x-ratelimit-requests-remaining")
                    if remaining is not None and remaining.isdigit() and int(remaining) < 3:
                        raise BudgetExhausted("API_RATE_LIMIT_REMAINING_LOW")
                if data.get("errors"):
                    raise RuntimeError(f"API_ERRORS {endpoint}: {data['errors']!r}")
                response = data.get("response")
                if not isinstance(response, list):
                    raise RuntimeError(f"INVALID_API_RESPONSE {endpoint}")
                dump_json(cache, {"endpoint": endpoint, "query": normalized,
                                  "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                                  "cache_kind": "negative" if endpoint == "fixtures/statistics" and not response else "positive",
                                  "response": response})
                return response
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f"API_HTTP_{exc.code}_{endpoint}") from exc
                if self.stat.calls >= self.stat.max_calls:
                    raise BudgetExhausted("BUDGET_DURING_RETRY") from exc
                time.sleep(1.5 * (attempt + 1))
            except URLError as exc:
                if attempt == 2:
                    raise RuntimeError(f"API_NETWORK_ERROR_{endpoint}") from exc
                if self.stat.calls >= self.stat.max_calls:
                    raise BudgetExhausted("BUDGET_DURING_RETRY") from exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("API_RETRIES_EXHAUSTED")


def safe_fixture(raw: Any, team_id: int, target_id: int, cutoff: datetime) -> bool:
    if not isinstance(raw, dict):
        return False
    fixture = raw.get("fixture") or {}
    fid = fixture.get("id")
    ids = [(raw.get("teams") or {}).get(side, {}).get("id") for side in ("home", "away")]
    if not isinstance(fid, int) or fid == target_id or team_id not in ids:
        return False
    try:
        started = aware(fixture.get("date"), "historical.date")
    except ValueError:
        return False
    return started + COMPLETION_BUFFER < cutoff and (fixture.get("status") or {}).get("short") in FINAL


def select_history(client: ApiClient, tid: int, target_id: int, cutoff: datetime, n: int) -> list[dict]:
    # last=99 is the documented per-query limit. No league restriction: all competitions.
    response = client.get("fixtures", team=tid, last=99)
    dedup = {r["fixture"]["id"]: r for r in response if safe_fixture(r, tid, target_id, cutoff)}
    rows = list(dedup.values())
    return sorted(rows, key=lambda r: (r["fixture"]["date"], r["fixture"]["id"]), reverse=True)[:n]


def enrich(base: dict, client: ApiClient, *, history: int, include_players: bool = False,
           include_halves: bool = False) -> tuple[dict, dict]:
    fixture = base["fixture"]
    target_id = fixture["fixture_id"]
    kickoff = aware(fixture["date"], "target.kickoff")
    snapshot = aware(base["generated_at"], "snapshot.generated_at")
    now = datetime.now(timezone.utc)
    if not snapshot < now < kickoff:
        raise ValueError("ONLINE_PREMATCH_ONLY: replay histórico debe ser --offline")
    # Ensure at least 3h completed for all historical games; no fixture target allowed.
    result = copy.deepcopy(base)
    cutoff = now
    report: dict[str, Any] = {"requested_history": history, "teams": {}, "issues": [],
                               "base_snapshot_generated_at_utc": snapshot.isoformat(),
                               "enrichment_collected_at_utc": None,
                               "source_kind": "API_FOOTBALL_LIVE_COLLECTED_PRE_KICKOFF",
                               "fixture_id": target_id}
    for side in ("home", "away"):
        tid = fixture[f"{side}_id"]
        history_obj = result["context"].setdefault(f"{side}_history_deep", {})
        existing = {r["fixture"]["id"]: r for r in history_obj.get("detailed_fixtures", [])
                    if safe_fixture(r, tid, target_id, cutoff)}
        try:
            available = select_history(client, tid, target_id, cutoff, history)
        except BudgetExhausted as exc:
            report["issues"].append({"team": side, "error": str(exc)})
            available = []
        needed = [r for r in available if r["fixture"]["id"] not in existing]
        # ids can batch at most 20 fixtures; if response embeds stats etc, reuse it.
        for start in range(0, len(needed), 20):
            chunk = needed[start:start + 20]
            ids = "-".join(str(r["fixture"]["id"]) for r in chunk)
            try:
                batched = client.get("fixtures", ids=ids)
            except (BudgetExhausted, RuntimeError) as exc:
                report["issues"].append({"team": side, "ids": ids, "error": str(exc)})
                batched = []
            for record in batched:
                if safe_fixture(record, tid, target_id, cutoff):
                    existing[record["fixture"]["id"]] = record
        # Fallback for rows not provided by batch and for nonembedded endpoints.
        for original in available:
            fid = original["fixture"]["id"]
            record = existing.get(fid, copy.deepcopy(original))
            for field, endpoint in (("statistics", "fixtures/statistics"),
                                    ("lineups", "fixtures/lineups"),
                                    ("events", "fixtures/events")):
                if len(record.get(field) or []) >= (2 if field in ("statistics", "lineups") else 1):
                    continue
                try:
                    record[field] = client.get(endpoint, fixture=fid)
                except (BudgetExhausted, RuntimeError) as exc:
                    report["issues"].append({"team": side, "fixture_id": fid,
                                             "endpoint": endpoint, "error": str(exc)})
                    record.setdefault(field, [])
            # Raw player statistics and halftime split: optional, not consumed by phase1 parser.
            if include_players and not record.get("players"):
                try:
                    record["players"] = client.get("fixtures/players", fixture=fid)
                except (BudgetExhausted, RuntimeError) as exc:
                    report["issues"].append({"team": side, "fixture_id": fid,
                                             "endpoint": "fixtures/players", "error": str(exc)})
            if include_halves and not record.get("statistics_halves"):
                try:
                    record["statistics_halves"] = client.get("fixtures/statistics", fixture=fid, half="true")
                except (BudgetExhausted, RuntimeError) as exc:
                    report["issues"].append({"team": side, "fixture_id": fid,
                                             "endpoint": "fixtures/statistics?half=true", "error": str(exc)})
            existing[fid] = record
        valid = [r for r in existing.values() if safe_fixture(r, tid, target_id, cutoff)]
        valid.sort(key=lambda r: (r["fixture"]["date"], r["fixture"]["id"]), reverse=True)
        chosen = valid[:history]
        history_obj["detailed_fixtures"] = chosen
        history_obj["requested_limit"] = history
        report["teams"][side] = {"team_id": tid, "history_found": len(available),
                                  "historical_detail_count": len(chosen),
                                  "with_statistics": sum(bool(r.get("statistics")) for r in chosen),
                                  "with_players": sum(bool(r.get("players")) for r in chosen),
                                  "with_halftime": sum(bool(r.get("statistics_halves")) for r in chosen)}
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    if aware(result["generated_at"], "enriched_generated_at") >= kickoff:
        raise ValueError("ONLINE_COLLECTION_CROSSED_KICKOFF: output rejected")
    report["enrichment_collected_at_utc"] = result["generated_at"]
    report["requests"] = {"api_calls": client.stat.calls, "cache_hits": client.stat.hits,
                           "max_calls": client.stat.max_calls}
    return result, report
