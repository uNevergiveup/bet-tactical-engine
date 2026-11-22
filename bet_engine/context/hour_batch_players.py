"""Join catalogued player props to timestamped API-Football observations."""
from collections import Counter, defaultdict
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from bet_engine.context.evidence_aggregator import (
    MIN_PLAYER_SAMPLE_MATCHES, MIN_PLAYER_SAMPLE_MINUTES, _norm_name,
)
from prematch_cache_record import acquisition_before, historical_available, temporal_stamp


METRICS = {
    "PLAYER_SHOTS": ("total_shots", (("shots", "total"),)),
    "PLAYER_ON_TARGET": ("shots_on_target", (("shots", "on"),)),
    "PLAYER_FOULS_COMMITTED": ("fouls_committed", (("fouls", "committed"),)),
    "PLAYER_ASSISTS": ("assists", (("goals", "assists"),)),
    "PLAYER_CARDS": ("yellow_cards_proxy", (("cards", "yellow"),)),
    "PLAYER_GOAL_OR_ASSIST": ("goals_plus_assists", (("goals", "total"), ("goals", "assists"))),
}


def load_player_history(preselection, ranked_sources, fixture_ids, cutoff):
    """Read only the cache versions cited by this run and index historical fixtures once."""
    refs = list(ranked_sources.get("raw_cache_sources") or [])
    for request in preselection.get("api_request_ledger") or []:
        if request.get("endpoint") == "fixtures":
            refs.extend(request.get("cache_sources") or [])
            refs.append({"path": request.get("cache_path"), "sha256": request.get("cache_sha256")})
    records = defaultdict(list)
    keys = {(ref.get("path"), ref.get("sha256")) for ref in refs if ref.get("path") and ref.get("sha256")}
    for path, digest in sorted(keys):
        original = Path(path)
        for candidate in (original, original.parent / "versions" / f"{digest}.json"):
            try:
                data = candidate.read_bytes()
                if sha256(data).hexdigest() != digest:
                    continue
                payload = json.loads(data)
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("endpoint") != "fixtures":
                break
            acquired = temporal_stamp(payload.get("retrieved_at_utc"))
            if acquired is None or acquired > cutoff:
                break
            source = {"path": str(candidate), "sha256": digest, "retrieved_at_utc": acquired.isoformat()}
            for raw in payload.get("response") or []:
                if not isinstance(raw, dict) or not raw.get("players"):
                    continue
                fixture = raw.get("fixture") or {}
                if (fixture.get("id") in fixture_ids and (fixture.get("status") or {}).get("short") == "FT"
                        and historical_available(raw, acquired)):
                    records[fixture["id"]].append((acquired, raw, source))
            break
    return {fid: sorted(items, key=lambda record: record[0], reverse=True) for fid, items in records.items()}


def _appearances(records, match, cutoff):
    players = defaultdict(dict)
    sources = {}
    for side, observations in (match.get("statistical_observations") or {}).items():
        if side not in ("home", "away"):
            continue
        team_id = match[f"{side}_id"]
        for row in observations:
            fid = row.get("fixture_id")
            kickoff = temporal_stamp(row.get("kickoff_utc"))
            acquired = row.get("acquired_at_utc")
            if (row.get("status") != "FT" or fid == match["fixture_id"] or kickoff is None or kickoff >= cutoff
                    or (acquired and not acquisition_before(acquired, cutoff))
                    or row.get("team_id") != team_id or not row.get("opponent_id")
                    or (row.get("league_id"), row.get("season")) != (match["league_id"], match["season"])):
                continue
            for stamp, raw, source in records.get(fid, []):
                teams = raw.get("teams") or {}
                league = raw.get("league") or {}
                if (stamp > cutoff or temporal_stamp(raw["fixture"].get("date")) != kickoff
                        or (league.get("id"), league.get("season")) != (row["league_id"], row["season"])
                        or {(teams.get(s) or {}).get("id") for s in ("home", "away")} != {team_id, row["opponent_id"]}):
                    continue
                blocks = [block for block in raw["players"] if (block.get("team") or {}).get("id") == team_id]
                if len(blocks) != 1 or not blocks[0].get("players"):
                    continue
                home = (teams.get("home") or {}).get("id") == team_id
                if row.get("venue") in ("home", "away") and (row["venue"] == "home") != home:
                    continue
                reported = blocks[0]["players"]
                ids = Counter((player.get("player") or {}).get("id") for player in reported)
                for player in reported:
                    info = player.get("player") or {}
                    pid = info.get("id")
                    stats = player.get("statistics") or []
                    if type(pid) is not int or pid <= 0 or ids[pid] != 1 or not info.get("name") or len(stats) != 1:
                        continue
                    games = stats[0].get("games") or {}
                    minutes = _count(games.get("minutes"))
                    if not minutes:
                        continue
                    substitute = games.get("substitute")
                    role = "SUBSTITUTE" if substitute is True else "STARTER" if substitute is False else "UNKNOWN"
                    players[side, pid][fid] = {
                        "name": info["name"], "fixture_id": fid, "kickoff_utc": kickoff.isoformat(),
                        "venue": "home" if home else "away", "minutes": minutes, "position": games.get("position"),
                        "role": role, "stats": stats[0],
                    }
                    sources[source["sha256"]] = source
                break
    return players, sources


def _count(value):
    return value if type(value) in (int, float) and isfinite(value) and value >= 0 else None


def _threshold(entry):
    family = entry["market_family"]
    if family in ("PLAYER_CARDS", "PLAYER_GOAL_OR_ASSIST"):
        return 1
    line = str(entry.get("line") or "")
    return int(line) if line.isdecimal() and int(line) > 0 and entry.get("direction") == f"{line}+" else None


def link_player_props(entries, history, match, cutoff):
    props = [entry for entry in entries if entry["target_scope"] == "PLAYER"]
    if not props:
        return {}, {"priced_player_options": 0, "by_family": {}}, []
    players, sources = _appearances(history, match, cutoff)
    names = defaultdict(set)
    for key, games in players.items():
        for game in games.values():
            names[_norm_name(game["name"])].add(key)

    linked, coverage = {}, defaultdict(Counter)
    for entry in props:
        family = entry["market_family"]
        status = "DESCRIPTIVE_XI_PENDING"
        threshold = _threshold(entry)
        matches = names[_norm_name(entry.get("target_entity"))]
        if entry["temporal_scope"] != "FT":
            status = "PERIOD_NOT_SUPPORTED"
        elif family not in METRICS or threshold is None:
            status = "STATISTIC_OR_THRESHOLD_NOT_IMPLEMENTED"
        elif not players:
            status = "NO_LINKED_PLAYER_HISTORY"
        elif len(matches) != 1:
            status = "AMBIGUOUS_PLAYER_NAME" if matches else "PLAYER_ID_NOT_RESOLVED_FROM_HISTORY"
        else:
            side, pid = next(iter(matches))
            games = players[side, pid]
            metric, fields = METRICS[family]
            sample = []
            for game in sorted(games.values(), key=lambda item: item["kickoff_utc"], reverse=True):
                values = [_count((game["stats"].get(block) or {}).get(key)) for block, key in fields]
                if all(value is not None for value in values):
                    sample.append({**{k: v for k, v in game.items() if k not in {"name", "stats"}}, "value": sum(values)})
            minutes = sum(game["minutes"] for game in sample)
            if len(sample) < MIN_PLAYER_SAMPLE_MATCHES or minutes < MIN_PLAYER_SAMPLE_MINUTES:
                status = "INSUFFICIENT_OBSERVED_SAMPLE"
            else:
                linked[entry["selection_id"]] = {
                    "status": status, "player_id": pid, "side": side,
                    "identity_basis": "UNIQUE_NORMALIZED_NAME_IN_BOTH_CLUBS_HISTORICAL_SAMPLE",
                    "sample_competition_id": match["league_id"], "sample_season": match["season"],
                    "metric": metric, "threshold_at_least": threshold, "n_valid": len(sample),
                    "n_missing_metric_in_observed_appearances": len(games) - len(sample), "valid_minutes": minutes,
                    "hits": sum(game["value"] >= threshold for game in sample), "sample": sample,
                }
        coverage[family][status] += 1
    return linked, {"priced_player_options": len(props), "by_family": coverage}, list(sources.values())
