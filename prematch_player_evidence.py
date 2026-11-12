#!/usr/bin/env python3
"""Evidence for match-level historical player props, no projections or EV."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from typing import Any

from match_performance_engine import aware, COMPLETION_BUFFER

FIELDS = {
    "shots_total": ("shots", "total"),
    "shots_on": ("shots", "on"),
    "passes_total": ("passes", "total"),
    "passes_key": ("passes", "key"),
    "tackles_total": ("tackles", "total"),
    "tackles_interceptions": ("tackles", "interceptions"),
    "duels_total": ("duels", "total"),
    "fouls_committed": ("fouls", "committed"),
    "fouls_drawn": ("fouls", "drawn"),
}


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def build(enriched: dict, performance: dict, *, max_players: int | None = None) -> dict:
    """Compute observed shots+minutes and per-90 with explicit missing-data n.

    Does not convert null to 0; minutes=0 means nonappearance and is excluded.
    """
    event = performance["event_id_bwin"]
    observed = performance["teams"]
    source = enriched["context"]
    out = {"schema": "prematch-player-evidence-1.0.4.2", "event_id_bwin": event,
           "target_fixture_id": performance["target_fixture_id"],
           "cutoff_utc": performance["analysis_cutoff_utc"], "teams": {},
           "warning": "Fixture-player stats depend on league/fixture coverage; lack of data is not zero"}
    for side in ("home", "away"):
        team = observed[side]
        tid = team["team_id"]
        allowed = {r["fixture_id"] for r in team["historical_fixtures"]}
        raw = (source.get(f"{side}_history_deep") or {}).get("detailed_fixtures") or []
        per_player: dict[int, dict] = {}
        for fixture in raw:
            fid = (fixture.get("fixture") or {}).get("id")
            if fid not in allowed:
                continue
            matches = [x for x in fixture.get("players", []) if (x.get("team") or {}).get("id") == tid]
            if len(matches) != 1:
                continue
            for record in matches[0].get("players", []):
                player = record.get("player") or {}
                pid = player.get("id")
                if not isinstance(pid, int) or pid <= 0 or not isinstance(record.get("statistics"), list):
                    continue
                stat = next((s for s in record["statistics"] if isinstance(s, dict)), None)
                if stat is None:
                    continue
                games = stat.get("games") or {}
                minutes = number(games.get("minutes"))
                if minutes is None or minutes <= 0:
                    continue
                p = per_player.setdefault(pid, {"player_id": pid, "name": player.get("name"),
                                                "observations": [], "team_id": tid})
                row = {"fixture_id": fid, "minutes": minutes,
                       "position": games.get("position"), "metrics": {}}
                for key, path in FIELDS.items():
                    row["metrics"][key] = number((stat.get(path[0]) or {}).get(path[1]))
                p["observations"].append(row)
        summary = []
        for pid, player in sorted(per_player.items()):
            rows = player.pop("observations")
            metrics = {}
            for key in FIELDS:
                valid = [r for r in rows if r["metrics"][key] is not None]
                total = sum(r["metrics"][key] for r in valid)
                mins = sum(r["minutes"] for r in valid)
                metrics[key] = {"observed_n": len(valid), "played_n": len(rows),
                                "total": round(total, 2) if valid else None,
                                "per90": round(total / mins * 90, 3) if mins > 0 else None,
                                "hit_1plus_n": sum(r["metrics"][key] >= 1 for r in valid),
                                "hist_fixture_ids": [r["fixture_id"] for r in valid]}
            player.update({"played_n": len(rows), "minutes_observed": sum(r["minutes"] for r in rows),
                           "positions_observed": sorted({r["position"] for r in rows if r["position"]}),
                           "metrics": metrics})
            summary.append(player)
        summary.sort(key=lambda p: (-p["played_n"], -p["minutes_observed"], p["player_id"]))
        out["teams"][side] = {"team_id": tid, "name": team["team_name"],
                              "historical_fixture_count": len(allowed),
                              "players_with_minutes": len(summary),
                              "players": summary[:max_players] if max_players is not None else summary}
    return out
