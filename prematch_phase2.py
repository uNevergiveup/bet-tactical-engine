#!/usr/bin/env python3
"""Orquestador offline de Fase 2 (patrones y señales tácticas descriptivas).

Consume el ``preselection.json`` producido por Fase 1/descubrimiento, reutiliza
los motores existentes y no realiza llamadas de red, scraping Bwin ni cálculos
de probabilidad/EV.  La ausencia de Bwin sólo bloquea la parte comercial; no
impide investigar patrones API-Football.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo
from statistics import median, pstdev

import prematch_pattern_discovery as discovery


SCHEMA = "2.0-prematch-phase2-mvp"
PLAYER_SCHEMA = "2.1.1-player-patterns-integrity"
MADRID = ZoneInfo("Europe/Madrid")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _status(pattern: dict, min_sample: int, min_coverage: float) -> str:
    """Mapea los estados internos V2 a los estados públicos de Fase 2."""
    internal = pattern.get("validation_status")
    n = min(pattern.get("n_own") or 0, pattern.get("n_opponent_conceded") or 0)
    cov = min((v or 0) for v in (pattern.get("coverage_effective_pct") or {}).values())
    if internal == "INSUFFICIENT_DATA":
        return "INSUFFICIENT_SAMPLE" if n < min_sample else "INSUFFICIENT_COVERAGE"
    if internal == "CONTRADICTORY":
        return "CONTRADICTORY_EVIDENCE"
    if internal == "DESCRIPTIVE_SUPPORTED" and n >= min_sample and cov >= min_coverage:
        return "SUPPORTED_PATTERN"
    return "WEAK_PATTERN"


PLAYER_METRICS = {
    "minutes": ("games", "minutes"), "shots": ("shots", "total"),
    "shots_on_target": ("shots", "on"), "goals": ("goals", "total"),
    "assists": ("goals", "assists"), "fouls_committed": ("fouls", "committed"),
    "fouls_drawn": ("fouls", "drawn"), "yellow": ("cards", "yellow"),
    "red": ("cards", "red"), "tackles": ("tackles", "total"),
    "interceptions": ("tackles", "interceptions"), "duels_won": ("duels", "won"),
    "passes": ("passes", "total"), "dribbles": ("dribbles", "attempts"),
}


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else None


class PlayerCache:
    """Lector local del contrato de Fase 1. Indexa una vez, nunca renueva caché.

    El TTL online no descarta un crudo histórico válido en una reconstrucción.
    SHA del payload y respuesta duplicada se comprueban con el contrato Fase 1.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.index = {}
        self.rejected = []

    def candidates(self, endpoint, fid):
        if endpoint not in self.index:
            index = defaultdict(list)
            for path in sorted((self.root / endpoint.replace("/", "_")).glob("*.json")):
                try:
                    blob = path.read_bytes(); doc = json.loads(blob)
                    if doc.get("endpoint", endpoint) != endpoint:
                        raise ValueError("CACHE_ENDPOINT_MISMATCH")
                    raw = doc.get("raw_payload")
                    response = doc.get("response")
                    if raw is not None:
                        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                        if hashlib.sha256(canonical).hexdigest() != doc.get("payload_sha256"):
                            raise ValueError("CACHE_PAYLOAD_SHA_MISMATCH")
                        if response != raw.get("response"):
                            raise ValueError("CACHE_RESPONSE_CONFLICT")
                    if not isinstance(response, list):
                        raise ValueError("CACHE_RESPONSE_NOT_LIST")
                    ref = {"path": str(path.resolve()), "sha256": hashlib.sha256(blob).hexdigest(),
                           "endpoint": endpoint, "query": doc.get("query"),
                           "fetched_at_utc": doc.get("fetched_at_utc"),
                           "payload_sha256": doc.get("payload_sha256"),
                           "integrity_status": "PAYLOAD_SHA_VERIFIED" if raw is not None else "FILE_SHA_RECORDED_LEGACY"}
                    params = dict(parse_qsl(doc.get("query", "")))
                    for declared in (doc.get("parameters"), (raw or {}).get("parameters")):
                        if declared is not None and {str(k): str(v) for k, v in declared.items()} != params:
                            raise ValueError("CACHE_PARAMETERS_CONFLICT")
                    if endpoint == "fixtures":
                        staged = []
                        for row in response:
                            key = str((row.get("fixture") or {}).get("id"))
                            # A query for id=X containing Y cannot establish Y.
                            ids = params.get("id", params.get("ids", "")).split("-")
                            if (params.get("id") or params.get("ids")) and key not in ids:
                                raise ValueError("CACHE_FIXTURE_QUERY_MISMATCH")
                            staged.append((key, row))
                        for key, row in staged:
                            index[key].append((row, ref))
                    elif set(params) == {"fixture"}:
                        index[params["fixture"]].append((response, ref))
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    self.rejected.append({"path": str(path.resolve()), "reason": str(exc)})
            self.index[endpoint] = index
        return self.index[endpoint].get(str(fid), [])

    def get(self, endpoint, fid, cutoff):
        candidates = self.candidates(endpoint, fid)
        if not candidates:
            return None, None
        # Prefer a version acquired by cutoff; otherwise the first reconstructible
        # version afterwards. Never combine different snapshots silently.
        def order(item):
            when = _dt(item[1].get("fetched_at_utc"))
            return (0 if when and when <= cutoff else 1, -when.timestamp() if when and when <= cutoff else when.timestamp() if when else float("inf"), item[1]["sha256"])
        return min(candidates, key=order)


def _completion(fixture, ref, cutoff):
    ko = _dt(fixture.get("date")); status = (fixture.get("status") or {}).get("short")
    if not ko or ko >= cutoff or status not in {"FT", "AET", "PEN"}:
        return None
    ended = _dt(fixture.get("ended_at_utc"))
    acquired = _dt((ref or {}).get("fetched_at_utc"))
    if ended:
        return "EXPLICIT_END_BEFORE_CUTOFF" if ko < ended < cutoff else None
    if acquired and ko < acquired <= cutoff:
        return "FINAL_STATUS_OBSERVED_BY_CUTOFF"
    # Conservative eligibility buffer, NOT a fabricated final-whistle timestamp.
    # Near-cutoff retrospective fixtures without an end time remain excluded.
    if ko + timedelta(hours=3 if status == "FT" else 4) < cutoff:
        return "RETROSPECTIVE_FINAL_STATUS_WITH_COMPLETION_BUFFER"
    return None


def _availability(refs, cutoff):
    times = [_dt(r.get("fetched_at_utc")) for r in refs]
    if any(x and x > cutoff for x in times):
        return "RETROSPECTIVE_RECONSTRUCTION"
    return "RECORDED_BY_CUTOFF" if times and all(times) else "AVAILABILITY_UNPROVEN"


def _lineup(response, tid):
    blocks = [x for x in response or [] if str((x.get("team") or {}).get("id")) == str(tid)]
    if not blocks:
        return {}, "NOT_AVAILABLE"
    if len(blocks) != 1:
        return {}, "LINEUP_TEAM_DUPLICATE"
    roles = {}
    for key, role in (("startXI", "STARTER"), ("substitutes", "BENCH")):
        for item in blocks[0].get(key, []) or []:
            player = item.get("player") or {}; pid = player.get("id")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid in roles:
                return {}, "LINEUP_ID_CONFLICT"
            roles[pid] = (role, player)
    if sum(role == "STARTER" for role, _ in roles.values()) != 11:
        return {}, "LINEUP_STARTERS_NOT_11"
    return roles, "STRUCTURED_LINEUP_VALID"


def collect_player_observations(preselection_match: dict, cache_root: Path, asof, audit=None, cache=None):
    """Normaliza jugadores con identidad del fixture y roles de /lineups.

    ``side`` se conserva como alias de ``target_side``; ``venue`` siempre es la
    localía histórica. Cada fila conserva las estadísticas originales y fuentes.
    """
    audit = audit if audit is not None else {}
    audit.update({"rejected_history": [], "fixture_checks": [], "cache_rejections": []})
    cache = cache or PlayerCache(cache_root)
    cutoff_values = [asof, _dt(preselection_match.get("analysis_cutoff_utc")), _dt(preselection_match.get("kickoff_madrid"))]
    if not all(cutoff_values):
        raise ValueError("PLAYER_CUTOFF_OR_TARGET_KICKOFF_INVALID")
    cutoff = min(cutoff_values).astimezone(timezone.utc)
    historical = {}
    for side in ("home", "away"):
        for row in (preselection_match.get("statistical_observations") or {}).get(side, []) or []:
            ko = _dt(row.get("kickoff_utc")); fid = row.get("fixture_id"); tid = row.get("team_id")
            reason = None
            if tid != preselection_match.get(side + "_id"): reason = "HISTORY_TEAM_MISMATCH"
            elif fid == preselection_match.get("fixture_id"): reason = "TARGET_FIXTURE_EXCLUDED"
            elif not isinstance(fid, int) or isinstance(fid, bool) or not ko or ko >= cutoff: reason = "POST_CUTOFF_OR_INVALID_HISTORY"
            elif row.get("status") not in {"FT", "AET", "PEN"}: reason = "HISTORY_NOT_FINAL"
            if reason:
                audit["rejected_history"].append({"fixture_id": fid, "team_id": tid, "reason": reason}); continue
            key = (fid, tid)
            if key in historical and historical[key] != {**row, "target_side": side.upper()}:
                audit["rejected_history"].append({"fixture_id": fid, "team_id": tid, "reason": "HISTORY_DUPLICATE_CONFLICT"})
                historical[key] = None
            elif key not in historical:
                historical[key] = {**row, "target_side": side.upper()}
    observations = []; coverage = {}
    for (fid, tid), row in sorted(historical.items()):
        if row is None: continue
        fixture_row, fixture_ref = cache.get("fixtures", fid, cutoff)
        if fixture_row is None:
            pending, _ = cache.get("fixtures/players", fid, cutoff)
            coverage[str(fid)] = "NOT_REQUESTED" if pending is None else "INSUFFICIENT_COVERAGE"
            audit["rejected_history"].append({"fixture_id": fid, "team_id": tid, "reason": "FIXTURE_IDENTITY_UNAVAILABLE"}); continue
        fixture = fixture_row.get("fixture") or {}; league = fixture_row.get("league") or {}; teams = fixture_row.get("teams") or {}
        home, away = (teams.get("home") or {}).get("id"), (teams.get("away") or {}).get("id")
        venue = "HOME" if tid == home else "AWAY" if tid == away else None
        opponent = away if venue == "HOME" else home
        reason = None
        identities = {(str((f.get("teams") or {}).get("home", {}).get("id")), str((f.get("teams") or {}).get("away", {}).get("id")),
                       str((f.get("league") or {}).get("id")), str((f.get("league") or {}).get("season")),
                       str(_dt((f.get("fixture") or {}).get("date")))) for f, _ in cache.candidates("fixtures", fid)}
        if len(identities) > 1: reason = "FIXTURE_IDENTITY_NOT_UNIQUE"
        if (home is None or away is None or home == away or not venue or fixture.get("id") != fid or
                opponent != row.get("opponent_id") or venue != row.get("venue") or
                league.get("id") != row.get("league_id") or league.get("season") != row.get("season") or
                _dt(fixture.get("date")) != _dt(row.get("kickoff_utc"))): reason = reason or "FIXTURE_IDENTITY_CONFLICT"
        completion = _completion(fixture, fixture_ref, cutoff)
        if not completion and not reason: reason = "COMPLETION_BEFORE_CUTOFF_UNPROVEN"
        if reason:
            audit["rejected_history"].append({"fixture_id": fid, "team_id": tid, "reason": reason}); continue
        response, player_ref = cache.get("fixtures/players", fid, cutoff)
        if response is None:
            coverage[str(fid)] = "NOT_REQUESTED"; continue
        lineup_response, lineup_ref = cache.get("fixtures/lineups", fid, cutoff)
        roles, lineup_status = _lineup(lineup_response, tid)
        events, events_ref = cache.get("fixtures/events", fid, cutoff)
        blocks = [b for b in response if str((b.get("team") or {}).get("id")) == str(tid)]
        check = {"fixture_id": fid, "team_id": tid, "venue": venue, "target_side": row["target_side"],
                 "lineup_status": lineup_status, "completion_evidence": completion,
                 "starter_ids": sorted(pid for pid, (role, _) in roles.items() if role == "STARTER")}
        audit["fixture_checks"].append(check)
        if not blocks:
            coverage[str(fid)] = "EMPTY" if not response else "INSUFFICIENT_COVERAGE"; continue
        entries = defaultdict(list)
        for block in blocks:
            for entry in block.get("players", []) or []:
                pid = (entry.get("player") or {}).get("id")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    entries[pid].append(entry)
        for pid, (_, lp) in roles.items():
            if pid not in entries: entries[pid] = [{"player": lp, "statistics": []}]
        emitted = []
        for pid, duplicates in sorted(entries.items()):
            entry = duplicates[0]; stats_list = entry.get("statistics") or []
            flags = []
            if len(blocks) > 1: flags.append("PLAYER_TEAM_BLOCK_DUPLICATE")
            if any(x != entry for x in duplicates): flags.append("DUPLICATE_PLAYER_CONFLICT")
            if len(stats_list) > 1: flags.append("MULTIPLE_STATISTICS_AMBIGUOUS")
            stats = stats_list[0] if len(stats_list) == 1 else {}; games = stats.get("games") or {}
            minutes = _num(games.get("minutes"))
            maximum = 90 if (fixture.get("status") or {}).get("short") == "FT" else 120
            if minutes is not None and not 0 <= minutes <= maximum:
                flags.append("INVALID_MINUTES"); minutes = None
            role, lp = roles.get(pid, ("UNKNOWN", {}))
            if role == "STARTER" and minutes == 0: flags.append("STARTER_ZERO_MINUTES_CONFLICT")
            if roles and pid not in roles: flags.append("PLAYER_NOT_IN_LINEUP")
            participation = "STARTER" if role == "STARTER" else "SUBSTITUTE_APPEARED" if role == "BENCH" and minutes and minutes > 0 else "BENCH_UNUSED" if role == "BENCH" and minutes == 0 else "UNKNOWN"
            appeared = True if minutes and minutes > 0 else False if participation == "BENCH_UNUSED" else None
            if "STARTER_ZERO_MINUTES_CONFLICT" in flags: participation = "UNKNOWN"
            refs = [r for r in (fixture_ref, player_ref, lineup_ref, events_ref) if r]
            metrics = {name: _num((stats.get(group) or {}).get(key)) for name, (group, key) in PLAYER_METRICS.items()}
            metrics["minutes"] = minutes
            metric_issues = {}
            for name, value in metrics.items():
                if value is not None and value < 0: metric_issues[name] = "NEGATIVE_VALUE"
            for small, large in (("shots_on_target", "shots"),):
                if metrics[small] is not None and metrics[large] is not None and metrics[small] > metrics[large]:
                    metric_issues[small] = metric_issues[large] = "SUBSET_EXCEEDS_TOTAL"
            won = metrics["duels_won"]; duels_total = _num((stats.get("duels") or {}).get("total"))
            if won is not None and duels_total is not None and won > duels_total:
                metric_issues["duels_won"] = "SUBSET_EXCEEDS_TOTAL"
            for name in ("yellow", "red"):
                if metrics[name] is not None and metrics[name] > (2 if name == "yellow" else 1):
                    metric_issues[name] = "CARD_COUNT_OUT_OF_RANGE"
            if minutes == 0 and any(v and v > 0 for name, v in metrics.items() if name not in {"minutes", "yellow", "red"}):
                flags.append("ZERO_MINUTES_WITH_ACTIONS")
            rec = {"player_id": pid, "player_name": (entry.get("player") or {}).get("name"), "team_id": tid,
                   "team": (blocks[0].get("team") or {}).get("name"), "fixture_id": fid, "opponent_id": opponent,
                   "kickoff_utc": _dt(fixture["date"]).isoformat(), "venue": venue,
                   "side": row["target_side"], "target_side": row["target_side"], "league_id": league["id"], "season": league["season"],
                   "minutes": minutes, "raw_minutes": games.get("minutes"), "starter": True if role == "STARTER" and participation != "UNKNOWN" else False if role == "BENCH" else None,
                   "substitute": True if role == "BENCH" else False if role == "STARTER" else None,
                   "lineup_role": role, "lineup_status": lineup_status, "appeared": appeared,
                   "participation_status": participation, "position": games.get("position"),
                   "source": "fixtures/players", "source_cache_query": player_ref["query"], "provenance": refs,
                   "availability_status": _availability(refs, cutoff), "completion_evidence": completion,
                   "metrics": metrics, "raw_statistics": stats_list, "raw_substitute": games.get("substitute"),
                   "data_quality_flags": flags, "metric_issues": metric_issues, "duplicate_records_n": len(duplicates),
                   "event_reconciliation": {},
                   "performance_eligible": not flags and appeared is True and minutes is not None and minutes > 0}
            emitted.append(rec)
        # Events are a cross-check, not a replacement for player totals. Cards
        # can include bench players; retain source contradictions per metric.
        if events is not None:
            for rec in emitted:
                for metric, details in (("yellow", {"Yellow Card", "Second Yellow card"}), ("red", {"Red Card", "Second Yellow card"})):
                    ev = [e for e in events if (e.get("team") or {}).get("id") == tid and (e.get("player") or {}).get("id") == rec["player_id"] and e.get("type") == "Card" and e.get("detail") in details]
                    rec["event_reconciliation"][metric] = {"player_statistics_value": rec["metrics"].get(metric),
                        "events_observed": len(ev), "event_times": [e.get("time") for e in ev],
                        "scope": "PROVIDER_CROSS_CHECK_NOT_AN_OVERRIDE"}
                    if rec["metrics"].get(metric) is not None and len(ev) != rec["metrics"][metric]:
                        rec["metric_issues"][metric] = "EVENTS_PLAYER_STATISTICS_CONFLICT"
        observations.extend(emitted)
        valid = sum(r["performance_eligible"] for r in emitted)
        coverage[str(fid)] = "AVAILABLE" if valid else "INSUFFICIENT_COVERAGE"
        check.update({"records": len(emitted), "performance_eligible": valid,
                      "statistics_records": sum(bool(r["raw_statistics"]) for r in emitted),
                      "coverage_meaning": "AVAILABLE means usable rows; metric completeness is reported independently",
                      "participation_counts": dict(Counter(r["participation_status"] for r in emitted))})
    by_player_fixture = defaultdict(list)
    for rec in observations:
        by_player_fixture[(rec["fixture_id"], rec["player_id"])].append(rec)
    for duplicates in by_player_fixture.values():
        if len({r["team_id"] for r in duplicates}) > 1:
            for rec in duplicates:
                rec["data_quality_flags"].append("PLAYER_ON_BOTH_TEAMS")
                rec["performance_eligible"] = False
    for check in audit["fixture_checks"]:
        check["performance_eligible"] = sum(r["performance_eligible"] for r in observations if
            r["fixture_id"] == check["fixture_id"] and r["team_id"] == check["team_id"])
    for fid, state in coverage.items():
        if state == "AVAILABLE" and not any(r["performance_eligible"] for r in observations if str(r["fixture_id"]) == fid):
            coverage[fid] = "INSUFFICIENT_COVERAGE"
    audit["cache_rejections"] = cache.rejected
    audit["cutoff_utc"] = cutoff.isoformat()
    # Detect a concurrent writer before persisting conclusions from stale bytes.
    for ref in {r["path"]: r for obs in observations for r in obs["provenance"]}.values():
        if _sha(Path(ref["path"])) != ref["sha256"]:
            raise ValueError("PLAYER_CACHE_CHANGED_DURING_RUN")
    return sorted(observations, key=lambda r: (r["kickoff_utc"], r["team_id"], r["player_id"]), reverse=True), coverage


def build_player_patterns(match: dict, observations, coverage, min_matches=3, min_minutes=180,
                          min_per90_minutes=180, audit=None):
    if min_matches < 3 or min_minutes <= 0 or min_per90_minutes <= 0:
        raise ValueError("PLAYER_THRESHOLDS_REQUIRE_MATCHES_GE_3_AND_POSITIVE_MINUTES")
    grouped = defaultdict(list)
    seen = set()
    for row in observations:
        key = (str(row["player_id"]), str(row["team_id"]), str(row["fixture_id"]))
        if key in seen:
            raise ValueError("DUPLICATE_PLAYER_OBSERVATION")
        seen.add(key)
        grouped[(str(row["player_id"]), str(row["team_id"]))].append(row)
    patterns = []
    for (player_id, team_id), rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda x: str(x.get("kickoff_utc", "")), reverse=True)
        # Direct callers may supply older normalized rows; eligibility still
        # requires positive valid minutes, never an observed bench record.
        eligible = [r for r in rows if r.get("performance_eligible", True) and
                    _num(r.get("minutes")) is not None and 0 < r["minutes"] <= 120]
        n = len(rows); total_minutes = sum(x["minutes"] for x in eligible)
        appearances = sum(_num(x.get("minutes")) is not None and 0 < x["minutes"] <= 120 for x in rows)
        base = {"player_id": int(player_id) if player_id.isdigit() else player_id, "team_id": int(team_id) if team_id.isdigit() else team_id,
                "player_name": rows[0].get("player_name"), "team": rows[0].get("team"), "matches_observed": n,
                "records_observed": n, "appearances": appearances, "performance_appearances": len(eligible),
                "minutes_observed": total_minutes, "minutes_mean": round(total_minutes / len(eligible), 2) if eligible else None,
                "starts": sum(x.get("starter") is True for x in rows),
                "bench_presences": sum(x.get("lineup_role") == "BENCH" for x in rows),
                "squad_presences": sum(x.get("lineup_role") in {"STARTER", "BENCH"} for x in rows),
                "substitute_appearances": sum(x.get("participation_status") == "SUBSTITUTE_APPEARED" for x in rows),
                "bench_unused": sum(x.get("participation_status") == "BENCH_UNUSED" for x in rows),
                "participation_unknown": sum(x.get("appeared") is None for x in rows),
                "fixture_ids": [x["fixture_id"] for x in rows], "eligible_fixture_ids": [x["fixture_id"] for x in eligible],
                "kickoffs_utc": [x.get("kickoff_utc") for x in rows],
                "venues": sorted({x.get("venue") for x in rows if x.get("venue")}), "source": "fixtures/players", "metrics": {}}
        for metric in PLAYER_METRICS:
            valid = [r for r in eligible if _num(r["metrics"].get(metric)) is not None and
                     r["metrics"][metric] >= 0 and metric not in r.get("metric_issues", {})]
            vals = [r["metrics"][metric] for r in valid]
            exposure = sum(r["minutes"] for r in valid)
            per90 = round(sum(vals) / exposure * 90, 4) if metric != "minutes" and exposure > 0 and vals else None
            quality = ("NO_MINUTES" if total_minutes == 0 else "INSUFFICIENT_COVERAGE" if not vals else
                       "LOW_EXPOSURE" if exposure < min_per90_minutes else "USABLE")
            missing = sum(r["metrics"].get(metric) is None for r in eligible)
            invalid = len(eligible) - len(valid) - missing
            metric_status = ("INSUFFICIENT_COVERAGE" if len(eligible) >= min_matches and not vals else
                             "INSUFFICIENT_SAMPLE" if len(valid) < min_matches or exposure < min_minutes else
                             "INSUFFICIENT_COVERAGE" if missing or invalid else "WEAK_PATTERN")
            base["metrics"][metric] = {"n": len(vals), "total": round(sum(vals), 4) if vals else None,
                "median": median(vals) if vals else None, "stdev": round(pstdev(vals), 4) if vals else None,
                "recent_vs_previous": {"recent_fixture_ids": [r["fixture_id"] for r in valid[:3]],
                    "previous_fixture_ids": [r["fixture_id"] for r in valid[3:]],
                    "recent_mean": round(sum(vals[:3]) / len(vals[:3]), 4) if vals else None,
                    "previous_mean": round(sum(vals[3:]) / len(vals[3:]), 4) if len(vals) > 3 else None,
                    "status": "DESCRIPTIVE_ONLY_SMALL_NON_OVERLAPPING_SAMPLES"},
                "mean_per_match": round(sum(vals) / len(vals), 4) if vals else None,
                "per90": per90, "per90_quality_status": quality, "per90_usable": metric != "minutes" and quality == "USABLE",
                "eligible_minutes": exposure, "fixture_ids": [r["fixture_id"] for r in valid],
                "denominator": {"metric": metric, "unit": "minutes" if metric == "minutes" else "count",
                    "matches": len(vals), "minutes": exposure,
                    "formula": "sum(metric on valid participating fixtures) / sum(minutes on same fixtures) * 90"},
                "eligible_appearance_n": len(eligible), "null_count": missing, "invalid_count": invalid,
                "excluded_records_n": n - len(eligible), "raw_non_null_records_n": sum(r["metrics"].get(metric) is not None for r in rows),
                "raw_null_count": sum(r["metrics"].get(metric) is None for r in rows), "evidence_status": metric_status}
        if len(eligible) < min_matches:
            status = "INSUFFICIENT_SAMPLE"; reason = "MIN_MATCHES_NOT_MET"
        elif total_minutes < min_minutes:
            status = "INSUFFICIENT_SAMPLE"; reason = "MIN_MINUTES_NOT_MET"
        elif not any(v["n"] for k, v in base["metrics"].items() if k != "minutes"):
            status = "INSUFFICIENT_COVERAGE"; reason = "NO_USABLE_PLAYER_METRICS"
        else:
            status = "WEAK_PATTERN"; reason = "DESCRIPTIVE_ONLY_NO_FORWARD_VALIDATION"
        base.update({"evidence_status": status, "evidence_reason": reason, "actionable_bet": False,
                     "metric_denominator": "Cada métrica declara n y minutos de los mismos fixtures elegibles; per90 conserva tasa matemática y estado de exposición.",
                     "observation_count": n})
        patterns.append(base)
    cutoff = _dt((audit or {}).get("cutoff_utc")) or _dt(match.get("analysis_cutoff_utc"))
    return {"schema_version": PLAYER_SCHEMA, "fixture_id": match.get("fixture_id"),
            "asof_madrid": cutoff.astimezone(MADRID).isoformat() if cutoff else None,
            "cutoff_utc": cutoff.isoformat() if cutoff else None, "patterns": patterns,
            "observations": observations, "coverage_by_fixture": coverage,
            "coverage_status": "AVAILABLE" if coverage and all(v == "AVAILABLE" for v in coverage.values()) else "INSUFFICIENT_COVERAGE" if observations else "EMPTY" if coverage and all(v == "EMPTY" for v in coverage.values()) else "NOT_REQUESTED" if coverage and all(v == "NOT_REQUESTED" for v in coverage.values()) else "INSUFFICIENT_COVERAGE",
            "thresholds": {"min_matches": min_matches, "min_minutes": min_minutes, "min_per90_minutes": min_per90_minutes,
                           "rationale": "180 minutos: exposición operativa mínima configurable equivalente a dos partidos completos; no acredita estabilidad por sí sola. Se exigen además >=3 apariciones."},
            "audit": audit or {}, "summary": {
                "players_registered": len(patterns), "records_observed": len(observations),
                "appearances": sum(x["appearances"] for x in patterns), "performance_observations": sum(x["performance_appearances"] for x in patterns),
                "confirmed_starts": sum(x["starts"] for x in patterns), "confirmed_bench_presences": sum(x["bench_presences"] for x in patterns),
                "substitute_appearances": sum(x["substitute_appearances"] for x in patterns), "bench_unused": sum(x["bench_unused"] for x in patterns),
                "participation_unknown": sum(x["participation_unknown"] for x in patterns),
                "by_status": dict(Counter(x["evidence_status"] for x in patterns)),
                "metric_coverage": {metric: {"valid_performance_observations": sum(x["metrics"][metric]["n"] for x in patterns),
                    "missing_performance_observations": sum(x["metrics"][metric]["null_count"] for x in patterns),
                    "invalid_performance_observations": sum(x["metrics"][metric]["invalid_count"] for x in patterns)} for metric in PLAYER_METRICS}},
            "contract": {"matches_observed": "compatibility alias for records_observed, NOT appearances", "side": "compatibility alias for target_side; NOT historical venue",
                         "n": "participating, valid, metric-specific observations", "null_count": "missing metric within eligible_appearance_n",
                         "NOT_REQUESTED": "no matching cache response supplied; not proof that the provider was never queried",
                         "per90": "mathematical rate; use per90_quality_status and per90_usable before interpretation"},
            "limitations": ["Dos partidos no permiten SUPPORTED_PATTERN.", "null no se convierte en cero.", "No se combinan equipos distintos para el mismo player_id.",
                            "Las alineaciones estructuradas son evidencia del proveedor, no certificación oficial del club.",
                            "Un crudo adquirido después del corte es reconstrucción retrospectiva; no acredita disponibilidad al corte.",
                            "Sin final explícito ni estado final adquirido al corte, se exige un margen conservador de 3h (FT) / 4h (AET/PEN); no es hora de final observada."],
            "market_identity_ready": bool(match.get("market_identity_ready", False)),
            "actionable_bet": False}


def asof_iso(value):
    parsed = _dt(value)
    return parsed.astimezone(MADRID).isoformat() if parsed else value


def validate_player_artifact(doc):
    """Comprueba el JSON producido, además de las pruebas de sus funciones."""
    errors = []; rows = doc["observations"]; cutoff = _dt(doc.get("cutoff_utc"))
    keys = [(r["fixture_id"], r["team_id"], r["player_id"]) for r in rows]
    if len(keys) != len(set(keys)): errors.append("DUPLICATE_OBSERVATION_KEY")
    if _dt(doc.get("asof_madrid")) != cutoff: errors.append("CUTOFF_PRESENTATION_MISMATCH")
    for r in rows:
        if r["fixture_id"] == doc["fixture_id"] or not cutoff or _dt(r["kickoff_utc"]) >= cutoff:
            errors.append("TEMPORAL_LEAKAGE")
        if r["starter"] is True and (r["lineup_role"] != "STARTER" or r["lineup_status"] != "STRUCTURED_LINEUP_VALID"):
            errors.append("UNVERIFIED_START")
        if r["performance_eligible"] and (r["minutes"] is None or r["minutes"] <= 0):
            errors.append("INVALID_PERFORMANCE_EXPOSURE")
    starts = Counter((r["fixture_id"], r["team_id"]) for r in rows if r["starter"] is True)
    if any(n > 11 for n in starts.values()): errors.append("IMPOSSIBLE_STARTER_COUNT")
    for pl in doc["patterns"]:
        own = [r for r in rows if r["team_id"] == pl["team_id"] and r["player_id"] == pl["player_id"]]
        if len(own) != pl["records_observed"] or pl["records_observed"] != pl["matches_observed"]:
            errors.append("RECORD_COUNT_MISMATCH")
        for metric, result in pl["metrics"].items():
            valid = [r for r in own if r["fixture_id"] in result["fixture_ids"]]
            exposure = sum(r["minutes"] for r in valid)
            if result["n"] != len(valid) or exposure != result["eligible_minutes"]:
                errors.append("METRIC_DENOMINATOR_MISMATCH")
            if result["n"] + result["null_count"] + result["invalid_count"] != result["eligible_appearance_n"]:
                errors.append("COVERAGE_COUNT_MISMATCH")
            if valid:
                total = sum(r["metrics"][metric] for r in valid)
                if result["mean_per_match"] != round(total / len(valid), 4): errors.append("MEAN_MISMATCH")
                rate = round(total / exposure * 90, 4) if metric != "minutes" and exposure else None
                if result["per90"] != rate: errors.append("PER90_MISMATCH")
            elif result["per90"] is not None: errors.append("PER90_WITHOUT_DATA")
            if result["per90_usable"] and exposure < doc["thresholds"]["min_per90_minutes"]:
                errors.append("LOW_EXPOSURE_RATE_MARKED_USABLE")
    return {"status": "PASS" if not errors else "FAIL", "errors": sorted(set(errors)),
            "checks": ["unique_player_team_fixture", "cutoff_utc_madrid", "no_target_or_future_fixture",
                       "lineup_start_evidence", "maximum_11_starters", "participating_performance_sample",
                       "metric_specific_exposure", "coverage_counts", "means_and_per90", "rate_quality"]}


def run(preselection: Path, output_dir: Path, fixture_ids=None, asof=None,
        min_sample: int = 4, min_coverage: float = 40.0, cache_root=None,
        player_min_matches: int = 3, player_min_minutes: int = 180, player_min_per90_minutes: int = 180):
    """Genera el paquete Fase 2 únicamente desde archivos locales."""
    preselection = Path(preselection)
    source = discovery.read(preselection)
    source_sha = _sha(preselection)
    if asof is None:
        asof = _dt(source.get("generated_at_utc"))
    if asof is None:
        raise ValueError("PRESELECTION_CUTOFF_MISSING_OR_INVALID")
    if asof.tzinfo is None:
        raise ValueError("ASOF_REQUIRES_EXPLICIT_OFFSET")
    asof = asof.astimezone(timezone.utc)
    if player_min_matches < 3 or player_min_minutes <= 0 or player_min_per90_minutes <= 0:
        raise ValueError("PLAYER_THRESHOLDS_REQUIRE_MATCHES_GE_3_AND_POSITIVE_MINUTES")
    selected = source.get("matches") or []
    wanted = {str(x) for x in (fixture_ids or [])}
    if wanted:
        selected = [m for m in selected if str(m.get("fixture_id")) in wanted]
        missing = sorted(wanted - {str(m.get("fixture_id")) for m in selected})
        if missing:
            raise ValueError(f"FIXTURE_NOT_IN_PRESELECTION:{','.join(missing)}")
    if not selected:
        raise ValueError("NO_FIXTURES_SELECTED")
    local_source = dict(source)
    local_source["matches"] = selected
    matrix, raw_discovery, queue = discovery.build_coverage(local_source, source_sha, asof), discovery.build_discovery(
        local_source, source_sha, asof, min_sample, min_coverage), None
    queue = discovery.build_queue(raw_discovery)
    for row in raw_discovery["patterns"]:
        row["phase2_status"] = _status(row, min_sample, min_coverage)
        row["actionable_bet"] = False
    for row in queue["items"]:
        row["phase2_status"] = _status(row, min_sample, min_coverage)
        row["actionable_bet"] = False
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    discovery.dump(out / "coverage_matrix.json", matrix)
    discovery.dump(out / "pattern_discovery.json", raw_discovery)
    discovery.dump(out / "research_queue_sport.json", queue)
    by_fixture = {}
    player_cache = PlayerCache(cache_root) if cache_root else None
    for m in selected:
        fid = str(m["fixture_id"])
        pats = [p for p in raw_discovery["patterns"] if str(p.get("fixture_id")) == fid]
        cov = [r for r in matrix["rows"] if r.get("league_id") == m.get("league_id") and r.get("season") == m.get("season")]
        identity = {
            "fixture_id": m.get("fixture_id"), "home": m.get("home"), "away": m.get("away"),
            "home_id": m.get("home_id"), "away_id": m.get("away_id"),
            "kickoff_madrid": m.get("kickoff_madrid"), "analysis_cutoff_utc": m.get("analysis_cutoff_utc"),
            "market_identity_ready": bool(m.get("market_identity_ready", False)),
        }
        doc = {
            "schema_version": SCHEMA, "fixture": identity,
            "source_preselection": str(preselection.resolve()), "source_preselection_sha256": source_sha,
            "asof_madrid": asof.astimezone(MADRID).isoformat(), "patterns": pats, "coverage": cov,
            "unsupported_dimensions": raw_discovery["unsupported_dimensions"],
            "limitations": [
                "Fase 2 es descriptiva: no probabilidades, EV, cuotas, picks ni stake.",
                "Periodos, entrenadores, árbitros y PPDA quedan no verificados si no hay campos literales y fechados.",
                "La vinculación Bwin no se infiere desde equipos; market_identity_ready=false bloquea mercados.",
            ],
            "market_identity_ready": bool(m.get("market_identity_ready", False)),
            "actionable_bet": False,
            "status_counts": {},
        }
        doc["status_counts"] = dict(Counter(p["phase2_status"] for p in pats))
        if cache_root:
            audit = {}
            obs, player_coverage = collect_player_observations(m, Path(cache_root), asof, audit, player_cache)
            player_doc = build_player_patterns(m, obs, player_coverage, player_min_matches, player_min_minutes, player_min_per90_minutes, audit)
            player_doc["integrity_validation"] = validate_player_artifact(player_doc)
            if player_doc["integrity_validation"]["status"] != "PASS":
                raise ValueError("PLAYER_ARTIFACT_INTEGRITY_FAILED:" + ",".join(player_doc["integrity_validation"]["errors"]))
        else:
            player_doc = {"schema_version": PLAYER_SCHEMA, "fixture_id": m.get("fixture_id"),
                          "patterns": [], "observations": [], "coverage_by_fixture": {},
                          "coverage_status": "NOT_REQUESTED", "thresholds": {"min_matches": player_min_matches, "min_minutes": player_min_minutes},
                          "limitations": ["No se proporcionó cache_root de Fase 1."], "actionable_bet": False}
        discovery.dump(out / f"player_patterns_{fid}.json", player_doc)
        doc["player_patterns_file"] = str((out / f"player_patterns_{fid}.json").resolve())
        doc["player_summary"] = {"patterns": len(player_doc["patterns"]), "observations": len(player_doc["observations"]), "coverage_status": player_doc["coverage_status"]}
        doc["unsupported_dimensions"] = {**doc["unsupported_dimensions"], "players": {
            "status": "INTEGRATED_SEPARATE_ARTIFACT", "path": doc["player_patterns_file"],
            "coverage_status": player_doc["coverage_status"]}}
        discovery.dump(out / f"patterns_{fid}.json", doc)
        by_fixture[fid] = {"patterns": len(pats), "status_counts": doc["status_counts"], "coverage_rows": len(cov), "player_patterns": len(player_doc["patterns"]), "player_coverage": player_doc["coverage_status"]}
    summary = {
        "schema_version": SCHEMA, "status": "FASE_2_READY_FOR_TEST",
        "mode": "OFFLINE_CACHE_ONLY", "asof_madrid": asof.astimezone(MADRID).isoformat(),
        "cutoff_utc": asof.isoformat(), "player_schema_version": PLAYER_SCHEMA,
        "player_phase_status": "FASE_2_1_READY_FOR_TEST",
        "source_preselection": str(preselection.resolve()), "source_preselection_sha256": source_sha,
        "fixtures": by_fixture, "counts": {"fixtures": len(by_fixture), "patterns": len(raw_discovery["patterns"])},
        "api_calls": 0, "bwin_scraper_calls": 0, "actionable_bet": False,
        "note": "Piloto descriptivo; dos observaciones no constituyen evidencia estadística sólida.",
    }
    discovery.dump(out / "phase2_summary.json", summary)
    (out / "phase2_summary.md").write_text(
        "# PREMATCH Fase 2 MVP\n\n" +
        f"Estado: **{summary['status']}** · corte: `{summary['asof_madrid']}` · API calls: **0**\n\n" +
        "La salida es descriptiva y no autoriza mercados, picks, probabilidades ni stake.\n\n" +
        "| Fixture | Patrones | Estados | Cobertura |\n|---:|---:|---|---:|\n" +
        "\n".join(f"| {fid} | {v['patterns']} | {v['status_counts']} | {v['coverage_rows']} |" for fid, v in by_fixture.items()) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--preselection", type=Path, required=True)
    p.add_argument("--fixture-id", action="append", default=[])
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--asof")
    p.add_argument("--min-sample", type=int, default=4)
    p.add_argument("--min-effective-coverage", type=float, default=40.0)
    p.add_argument("--cache-root", type=Path, help="caché Fase 1; sólo lectura")
    p.add_argument("--player-min-matches", type=int, default=3)
    p.add_argument("--player-min-minutes", type=int, default=180)
    p.add_argument("--player-min-per90-minutes", type=int, default=180)
    p.add_argument("--online", action="store_true", help="rechazado: Fase 2 MVP no hace red")
    a = p.parse_args(argv)
    if a.online:
        p.error("FASE_2_OFFLINE_ONLY_NO_NETWORK")
    when = _dt(a.asof) if a.asof else None
    if a.asof and when is None:
        p.error("ASOF_REQUIRES_EXPLICIT_OFFSET")
    try:
        result = run(a.preselection, a.output_dir, a.fixture_id, when, a.min_sample, a.min_effective_coverage,
                     a.cache_root, a.player_min_matches, a.player_min_minutes, a.player_min_per90_minutes)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        p.exit(2, f"ERROR: {exc}\n")
    print(f"{result['status']}: fixtures={result['counts']['fixtures']} patterns={result['counts']['patterns']} -> {a.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
