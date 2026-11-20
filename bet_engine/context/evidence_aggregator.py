"""Evidencia estadística reutilizable con distribuciones, exposición individual y contraste rival (Fase 2 - Tarea A).

Recupera observaciones por partido y por jugador desde los cachés congelados existentes
(`raw_cache_sources`, `tactical_signal_engine.json`, `preselection.json`) respetando
estrictamente `kickoff_utc` y distinguiendo `NO_DATA`, `NOT_RECORDED`, `ZERO_RECORDED`,
`INSUFFICIENT_SAMPLE` y `AVAILABLE`.
"""

from __future__ import annotations

import json
import math
import statistics
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.context.observation_store import STAT_LABEL_MAP, _to_numeric

PLAYER_STATUS_NO_DATA = "NO_DATA"
PLAYER_STATUS_NOT_RECORDED = "NOT_RECORDED"
PLAYER_STATUS_ZERO_RECORDED = "ZERO_RECORDED"
PLAYER_STATUS_INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
PLAYER_STATUS_AVAILABLE = "AVAILABLE"

MIN_PLAYER_SAMPLE_MATCHES = 3
MIN_PLAYER_SAMPLE_MINUTES = 90


def _norm_name(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _parse_dt(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def _compute_distribution(
    observations: list[dict[str, Any]],
    *,
    thresholds: tuple[float, ...] = (),
) -> dict[str, Any]:
    """Calcula distribución exacta a partir de observaciones individuales válidas (sin reconstruir desde medias)."""
    vals = [float(o["value"]) for o in observations if o.get("value") is not None]
    n = len(vals)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "stdev": None,
            "cv": None,
            "threshold_exceedance": {},
            "red_card_distorted_count": 0,
            "observations": [],
        }

    mean_v = sum(vals) / n
    median_v = statistics.median(vals)
    min_v = min(vals)
    max_v = max(vals)
    stdev_v = statistics.pstdev(vals) if n >= 2 else 0.0
    cv_v = (stdev_v / mean_v) if (mean_v > 0 and stdev_v is not None) else None

    exceedance: dict[str, dict[str, Any]] = {}
    for th in thresholds:
        hits = sum(1 for v in vals if v > th)
        exceedance[f"gt_{th:g}"] = {
            "hits": hits,
            "n": n,
            "rate": round(hits / n, 4),
        }

    red_count = sum(1 for o in observations if o.get("red_card_present"))
    return {
        "n": n,
        "mean": round(mean_v, 4),
        "median": round(float(median_v), 4),
        "min": round(min_v, 4),
        "max": round(max_v, 4),
        "stdev": round(stdev_v, 4),
        "cv": round(cv_v, 4) if cv_v is not None else None,
        "threshold_exceedance": exceedance,
        "red_card_distorted_count": red_count,
        "observations": observations,
    }


def _extract_team_stats_map(stats_list: list[dict[str, Any]], team_id: int) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for entry in stats_list or []:
        t = (entry.get("team") or {}).get("id")
        if t != team_id:
            continue
        for item in entry.get("statistics") or []:
            mapped = STAT_LABEL_MAP.get(str(item.get("type") or ""))
            if mapped:
                out[mapped] = _to_numeric(item.get("value"))
    return out


def _extract_red_card_flag(events: list[dict[str, Any]] | None) -> bool:
    for ev in events or []:
        if str(ev.get("type") or "").lower() == "card":
            det = str(ev.get("detail") or "").lower()
            if "red" in det or "second yellow" in det:
                return True
    return False


def _load_raw_fixtures_from_cache_sources(
    raw_cache_sources: list[dict[str, Any]],
    *,
    kickoff_utc: datetime | None,
) -> dict[int, dict[str, Any]]:
    """Carga fixtures completos desde raw_cache_sources filtrando estrictamente antes del kickoff."""
    fixtures_by_id: dict[int, dict[str, Any]] = {}
    for src in raw_cache_sources or []:
        p = Path(str(src.get("path") or ""))
        if not p.is_file():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in doc.get("response") or []:
            if not isinstance(item, dict):
                continue
            fx = item.get("fixture") or {}
            fid = fx.get("id")
            if fid is None:
                continue
            status_short = str((fx.get("status") or {}).get("short") or "").upper()
            if status_short not in {"FT", "AET", "PEN"}:
                continue
            fx_dt = _parse_dt(fx.get("date"))
            if kickoff_utc is not None and fx_dt is not None and fx_dt >= kickoff_utc:
                continue
            # Prefer richer fixture payload (with statistics/players)
            existing = fixtures_by_id.get(int(fid))
            if existing is None or (item.get("statistics") and not existing.get("statistics")) or (item.get("players") and not existing.get("players")):
                fixtures_by_id[int(fid)] = item
    return fixtures_by_id


def _build_team_distributions_from_fixtures(
    fixtures_by_id: dict[int, dict[str, Any]],
    *,
    team_id: int,
) -> dict[str, Any]:
    """Construye distribuciones por métrica y splits HOME/AWAY a partir de fixtures completos."""
    team_fixtures: list[dict[str, Any]] = []
    for item in fixtures_by_id.values():
        teams = item.get("teams") or {}
        hid = (teams.get("home") or {}).get("id")
        aid = (teams.get("away") or {}).get("id")
        if hid == team_id or aid == team_id:
            team_fixtures.append(item)

    team_fixtures.sort(key=lambda x: str((x.get("fixture") or {}).get("date") or ""), reverse=True)
    metrics_cfg: dict[str, tuple[float, ...]] = {
        "goals": (0.5, 1.5, 2.5),
        "corners": (3.5, 4.5, 5.5, 6.5),
        "shots": (9.5, 11.5, 13.5),
        "on_target": (2.5, 3.5, 4.5),
        "fouls": (10.5, 11.5, 13.5),
        "yellow": (1.5, 2.5, 3.5),
        "xg": (0.75, 1.25, 1.75),
        "offsides": (0.5, 1.5, 2.5),
    }

    own_obs: dict[str, list[dict[str, Any]]] = {m: [] for m in metrics_cfg}
    conc_obs: dict[str, list[dict[str, Any]]] = {m: [] for m in metrics_cfg}

    for item in team_fixtures:
        fx = item.get("fixture") or {}
        teams = item.get("teams") or {}
        hid = (teams.get("home") or {}).get("id")
        aid = (teams.get("away") or {}).get("id")
        is_home = hid == team_id
        opp_id = aid if is_home else hid
        opp_name = (teams.get("away") if is_home else teams.get("home") or {}).get("name")
        venue = "HOME" if is_home else "AWAY"
        red_flag = _extract_red_card_flag(item.get("events"))

        goals = item.get("goals") or {}
        own_goals = _to_numeric(goals.get("home") if is_home else goals.get("away"))
        opp_goals = _to_numeric(goals.get("away") if is_home else goals.get("home"))

        stats_list = item.get("statistics") or []
        own_map = _extract_team_stats_map(stats_list, team_id)
        opp_map = _extract_team_stats_map(stats_list, opp_id) if opp_id is not None else {}
        own_map["goals"] = own_goals
        opp_map["goals"] = opp_goals

        base_meta = {
            "fixture_id": fx.get("id"),
            "date_utc": fx.get("date"),
            "venue": venue,
            "opponent": opp_name,
            "red_card_present": red_flag,
        }
        for m in metrics_cfg:
            if own_map.get(m) is not None:
                own_obs[m].append({**base_meta, "value": own_map[m]})
            if opp_map.get(m) is not None:
                conc_obs[m].append({**base_meta, "value": opp_map[m]})

    result: dict[str, Any] = {}
    for m, ths in metrics_cfg.items():
        own_all = own_obs[m]
        conc_all = conc_obs[m]
        result[m] = {
            "own": {
                "all": _compute_distribution(own_all, thresholds=ths),
                "home": _compute_distribution([o for o in own_all if o["venue"] == "HOME"], thresholds=ths),
                "away": _compute_distribution([o for o in own_all if o["venue"] == "AWAY"], thresholds=ths),
            },
            "conceded": {
                "all": _compute_distribution(conc_all, thresholds=ths),
                "home": _compute_distribution([o for o in conc_all if o["venue"] == "HOME"], thresholds=ths),
                "away": _compute_distribution([o for o in conc_all if o["venue"] == "AWAY"], thresholds=ths),
            },
        }
    return {
        "fixtures_matched": len(team_fixtures),
        "metrics": result,
    }


def _extract_player_exposure_from_fixtures(
    fixtures_by_id: dict[int, dict[str, Any]],
    *,
    team_id: int,
) -> dict[str, dict[str, Any]]:
    """Recupera exposición y estadísticas individuales reales desde `players[]` y `lineups[]`."""
    players: dict[int, dict[str, Any]] = {}

    for item in fixtures_by_id.values():
        fx = item.get("fixture") or {}
        fid = fx.get("id")
        fdate = fx.get("date")
        teams = item.get("teams") or {}
        hid = (teams.get("home") or {}).get("id")
        aid = (teams.get("away") or {}).get("id")
        if hid != team_id and aid != team_id:
            continue

        starter_ids: set[int] = set()
        sub_ids: set[int] = set()
        for lu in item.get("lineups") or []:
            if (lu.get("team") or {}).get("id") != team_id:
                continue
            for p_entry in lu.get("startXI") or []:
                pid = (p_entry.get("player") or {}).get("id")
                if pid is not None:
                    starter_ids.add(int(pid))
            for p_entry in lu.get("substitutes") or []:
                pid = (p_entry.get("player") or {}).get("id")
                if pid is not None:
                    sub_ids.add(int(pid))

        for p_team in item.get("players") or []:
            if (p_team.get("team") or {}).get("id") != team_id:
                continue
            for p_rec in p_team.get("players") or []:
                p_info = p_rec.get("player") or {}
                pid = p_info.get("id")
                if pid is None:
                    continue
                pid = int(pid)
                pname = str(p_info.get("name") or f"player_{pid}")
                st_list = p_rec.get("statistics") or []
                st0 = st_list[0] if st_list else {}
                games = st0.get("games") or {}
                shots = st0.get("shots") or {}
                fouls = st0.get("fouls") or {}
                cards = st0.get("cards") or {}
                goals = st0.get("goals") or {}
                dribbles = st0.get("dribbles") or {}
                duels = st0.get("duels") or {}

                mins = _to_numeric(games.get("minutes"))
                pos = str(games.get("position") or "").strip().upper() or None
                is_sub = bool(games.get("substitute")) or (pid in sub_ids and pid not in starter_ids)
                is_start = (pid in starter_ids) or (not is_sub and mins is not None and mins > 0)

                acc = players.setdefault(
                    pid,
                    {
                        "player_id": pid,
                        "name": pname,
                        "team_id": team_id,
                        "matches_played": 0,
                        "starts": 0,
                        "subs": 0,
                        "positions": [],
                        "minutes_values": [],
                        "shots_values": [],
                        "on_target_values": [],
                        "fouls_committed_values": [],
                        "fouls_drawn_values": [],
                        "dribbles_attempts_values": [],
                        "dribbles_success_values": [],
                        "dribbles_past_values": [],
                        "duels_total_values": [],
                        "duels_won_values": [],
                        "yellow_values": [],
                        "red_values": [],
                        "goals_values": [],
                        "assists_values": [],
                        "fixture_ids": [],
                        "latest_date_utc": fdate,
                    },
                )
                if mins is not None and mins > 0:
                    acc["matches_played"] += 1
                    if pos:
                        acc["positions"].append(pos)
                if is_start:
                    acc["starts"] += 1
                elif is_sub and mins is not None and mins > 0:
                    acc["subs"] += 1
                if mins is not None:
                    acc["minutes_values"].append(mins)
                if shots.get("total") is not None:
                    acc["shots_values"].append(_to_numeric(shots.get("total")))
                if shots.get("on") is not None:
                    acc["on_target_values"].append(_to_numeric(shots.get("on")))
                if fouls.get("committed") is not None:
                    acc["fouls_committed_values"].append(_to_numeric(fouls.get("committed")))
                if fouls.get("drawn") is not None:
                    acc["fouls_drawn_values"].append(_to_numeric(fouls.get("drawn")))
                if dribbles.get("attempts") is not None:
                    acc["dribbles_attempts_values"].append(_to_numeric(dribbles.get("attempts")))
                if dribbles.get("success") is not None:
                    acc["dribbles_success_values"].append(_to_numeric(dribbles.get("success")))
                if dribbles.get("past") is not None:
                    acc["dribbles_past_values"].append(_to_numeric(dribbles.get("past")))
                if duels.get("total") is not None:
                    acc["duels_total_values"].append(_to_numeric(duels.get("total")))
                if duels.get("won") is not None:
                    acc["duels_won_values"].append(_to_numeric(duels.get("won")))
                if cards.get("yellow") is not None:
                    acc["yellow_values"].append(_to_numeric(cards.get("yellow")))
                if cards.get("red") is not None:
                    acc["red_values"].append(_to_numeric(cards.get("red")))
                if goals.get("total") is not None:
                    acc["goals_values"].append(_to_numeric(goals.get("total")))
                if goals.get("assists") is not None:
                    acc["assists_values"].append(_to_numeric(goals.get("assists")))
                acc["fixture_ids"].append(fid)

    def _summarize_metric(
        vals: list[float | None],
        *,
        thresholds: tuple[int, ...] = (),
        total_minutes: float | None = None,
        exposure_ok: bool = False,
    ) -> dict[str, Any]:
        clean = [float(v) for v in vals if v is not None]
        if not clean:
            return {"status": PLAYER_STATUS_NOT_RECORDED, "n": 0, "total": None, "mean": None, "min": None, "max": None, "stdev": None, "cv": None, "per_90": None, "threshold_rates": {}}
        tot = sum(clean)
        n = len(clean)
        mean_v = tot / n
        stdev_v = statistics.pstdev(clean) if n >= 2 else 0.0
        cv_v = (stdev_v / mean_v) if mean_v > 0 else None
        per_90 = round((tot / total_minutes) * 90.0, 4) if (exposure_ok and total_minutes and total_minutes > 0) else None
        th_rates = {f"{t}+": round(sum(1 for x in clean if x >= t) / n, 4) for t in thresholds}
        status = PLAYER_STATUS_ZERO_RECORDED if tot == 0 else PLAYER_STATUS_AVAILABLE
        return {
            "status": status,
            "n": n,
            "total": round(tot, 2),
            "mean": round(mean_v, 4),
            "min": round(min(clean), 2),
            "max": round(max(clean), 2),
            "stdev": round(stdev_v, 4),
            "cv": round(cv_v, 4) if cv_v is not None else None,
            "per_90": per_90,
            "threshold_rates": th_rates,
        }

    by_norm_name: dict[str, dict[str, Any]] = {}
    for acc in players.values():
        mins_clean = [float(v) for v in acc["minutes_values"] if v is not None]
        tot_mins = sum(mins_clean)
        mp = acc["matches_played"]
        if not mins_clean and mp == 0:
            exp_status = PLAYER_STATUS_NO_DATA
        elif tot_mins == 0:
            exp_status = PLAYER_STATUS_ZERO_RECORDED
        elif mp < MIN_PLAYER_SAMPLE_MATCHES or tot_mins < MIN_PLAYER_SAMPLE_MINUTES:
            exp_status = PLAYER_STATUS_INSUFFICIENT_SAMPLE
        else:
            exp_status = PLAYER_STATUS_AVAILABLE
        exposure_ok = exp_status == PLAYER_STATUS_AVAILABLE
        uniq_positions = sorted(set(acc["positions"]))

        summary = {
            "player_id": acc["player_id"],
            "name": acc["name"],
            "team_id": acc["team_id"],
            "exposure_status": exp_status,
            "current_squad_status": "HISTORICAL_CACHE_ONLY_REQUIRES_CURRENT_VERIFICATION",
            "positions_observed": uniq_positions,
            "role_changes_detected": len(uniq_positions) > 1,
            "matches_played": mp,
            "starts": acc["starts"],
            "subs": acc["subs"],
            "minutes_total": round(tot_mins, 1) if mins_clean else None,
            "minutes_mean": round(tot_mins / len(mins_clean), 1) if mins_clean else None,
            "shots": _summarize_metric(acc["shots_values"], thresholds=(1, 2, 3, 4), total_minutes=tot_mins, exposure_ok=exposure_ok),
            "on_target": _summarize_metric(acc["on_target_values"], thresholds=(1, 2), total_minutes=tot_mins, exposure_ok=exposure_ok),
            "fouls_committed": _summarize_metric(acc["fouls_committed_values"], thresholds=(1, 2, 3), total_minutes=tot_mins, exposure_ok=exposure_ok),
            "fouls_drawn": _summarize_metric(acc["fouls_drawn_values"], thresholds=(1, 2, 3), total_minutes=tot_mins, exposure_ok=exposure_ok),
            "dribbles_attempts": _summarize_metric(acc["dribbles_attempts_values"], total_minutes=tot_mins, exposure_ok=exposure_ok),
            "dribbles_success": _summarize_metric(acc["dribbles_success_values"], total_minutes=tot_mins, exposure_ok=exposure_ok),
            "dribbles_past": _summarize_metric(acc["dribbles_past_values"], total_minutes=tot_mins, exposure_ok=exposure_ok),
            "duels_total": _summarize_metric(acc["duels_total_values"], total_minutes=tot_mins, exposure_ok=exposure_ok),
            "duels_won": _summarize_metric(acc["duels_won_values"], total_minutes=tot_mins, exposure_ok=exposure_ok),
            "yellow": _summarize_metric(acc["yellow_values"], thresholds=(1,), total_minutes=tot_mins, exposure_ok=exposure_ok),
            "red": _summarize_metric(acc["red_values"]),
            "goals": _summarize_metric(acc["goals_values"]),
            "assists": _summarize_metric(acc["assists_values"]),
            "fixture_ids": acc["fixture_ids"],
        }
        by_norm_name[_norm_name(acc["name"])] = summary
    return by_norm_name


def build_statistical_evidence_bundle(
    *,
    home_id: int,
    away_id: int,
    kickoff_utc: str | datetime | None = None,
    raw_cache_sources: list[dict[str, Any]] | None = None,
    tactical_fx: dict[str, Any] | None = None,
    pre_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construye el entregable estadístico A con distribuciones reales, jugadores y contraste rival."""
    ko_dt = _parse_dt(kickoff_utc)
    fixtures_by_id = _load_raw_fixtures_from_cache_sources(raw_cache_sources or [], kickoff_utc=ko_dt)

    home_dist = _build_team_distributions_from_fixtures(fixtures_by_id, team_id=home_id)
    away_dist = _build_team_distributions_from_fixtures(fixtures_by_id, team_id=away_id)
    home_players = _extract_player_exposure_from_fixtures(fixtures_by_id, team_id=home_id)
    away_players = _extract_player_exposure_from_fixtures(fixtures_by_id, team_id=away_id)

    # Build explicit opponent contrast per metric (never averaging frequencies as predictive prob)
    opponent_contrast: dict[str, Any] = {}
    for m in ("corners", "shots", "on_target", "fouls", "yellow", "goals", "xg"):
        h_own_home = home_dist["metrics"][m]["own"]["home"]
        a_conc_away = away_dist["metrics"][m]["conceded"]["away"]
        a_own_away = away_dist["metrics"][m]["own"]["away"]
        h_conc_home = home_dist["metrics"][m]["conceded"]["home"]
        opponent_contrast[m] = {
            "home_production_at_home": h_own_home,
            "away_conceded_away": a_conc_away,
            "away_production_away": a_own_away,
            "home_conceded_at_home": h_conc_home,
            "predictive_probability_note": "Descriptivo por localía; no promediar frecuencias históricas como probabilidad predictiva.",
        }

    return {
        "schema_version": "1.0-statistical-evidence-bundle",
        "home_id": home_id,
        "away_id": away_id,
        "raw_fixtures_loaded": len(fixtures_by_id),
        "home_distributions": home_dist,
        "away_distributions": away_dist,
        "opponent_contrast": opponent_contrast,
        "players": {
            "home": home_players,
            "away": away_players,
        },
        "fallback_tactical_signals_available": bool(tactical_fx and tactical_fx.get("signals")),
        "fallback_preselection_available": pre_match is not None,
    }


def _find_player_record(players_map: dict[str, dict[str, Any]], player_name: str) -> dict[str, Any] | None:
    target = _norm_name(player_name)
    if target in players_map:
        return players_map[target]
    tokens = [t for t in target.split() if len(t) >= 3]
    for k, rec in players_map.items():
        if target in k or (tokens and all(t in k for t in tokens)):
            return rec
    return None


def build_player_fouls_duel_context(
    *,
    evidence_bundle: dict[str, Any],
    player_name: str,
    side: str,
    bwin_catalog: dict[str, Any] | None = None,
    lineup_status: str = "LINEUPS_PENDING",
    current_squad_verified: bool = False,
    referee_info: dict[str, Any] | None = None,
    tactical_duel_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construye el contexto de duelos defensivos para analizar faltas de un jugador (Sección 3)."""
    side_low = side.lower()
    if side_low not in ("home", "away"):
        raise ValueError(f"INVALID_SIDE:{side}")
    opp_side = "away" if side_low == "home" else "home"

    players_map = (evidence_bundle.get("players") or {}).get(side_low) or {}
    opp_players_map = (evidence_bundle.get("players") or {}).get(opp_side) or {}
    rec = _find_player_record(players_map, player_name)

    if rec is None:
        defensive_block: dict[str, Any] = {
            "player_name": player_name,
            "team_verified": False,
            "current_squad_status": "NO_VERIFICADO",
            "exposure_status": PLAYER_STATUS_NO_DATA,
            "positions_observed": [],
            "role_changes_detected": False,
            "matches_played": 0,
            "starts": 0,
            "subs": 0,
            "minutes_total": None,
            "fouls_committed": {"status": PLAYER_STATUS_NO_DATA, "n": 0, "mean": None, "per_90": None},
            "yellow": {"status": PLAYER_STATUS_NO_DATA, "n": 0, "mean": None},
        }
    else:
        fc = rec.get("fouls_committed") or {}
        defensive_block = {
            "player_id": rec.get("player_id"),
            "player_name": rec.get("name"),
            "team_id": rec.get("team_id"),
            "team_verified": bool(current_squad_verified),
            "current_squad_status": "VERIFIED" if current_squad_verified else rec.get("current_squad_status"),
            "exposure_status": rec.get("exposure_status"),
            "positions_observed": rec.get("positions_observed") or [],
            "role_changes_detected": bool(rec.get("role_changes_detected")),
            "matches_played": rec.get("matches_played", 0),
            "starts": rec.get("starts", 0),
            "subs": rec.get("subs", 0),
            "minutes_total": rec.get("minutes_total"),
            "minutes_mean": rec.get("minutes_mean"),
            "fouls_committed": fc,
            "fouls_per_90_status": "AVAILABLE" if fc.get("per_90") is not None else "INSUFFICIENT_EXPOSURE_FOR_PER90",
            "yellow": rec.get("yellow"),
            "red": rec.get("red"),
            "dribbles_past": rec.get("dribbles_past"),
        }

    # B. Opponent context (dribbles attempted/completed, fouls drawn, attacking volume)
    opp_dribble_leaders = []
    opp_fouls_drawn_leaders = []
    has_opp_dribbles = False
    has_opp_fouls_drawn = False
    for orec in opp_players_map.values():
        da = orec.get("dribbles_attempts") or {}
        ds = orec.get("dribbles_success") or {}
        fd = orec.get("fouls_drawn") or {}
        if (da.get("total") or 0) > 0:
            has_opp_dribbles = True
            opp_dribble_leaders.append({
                "name": orec.get("name"),
                "positions": orec.get("positions_observed"),
                "minutes_total": orec.get("minutes_total"),
                "dribbles_attempts_mean": da.get("mean"),
                "dribbles_success_mean": ds.get("mean"),
                "dribbles_attempts_per_90": da.get("per_90"),
            })
        if (fd.get("total") or 0) > 0:
            has_opp_fouls_drawn = True
            opp_fouls_drawn_leaders.append({
                "name": orec.get("name"),
                "positions": orec.get("positions_observed"),
                "minutes_total": orec.get("minutes_total"),
                "fouls_drawn_mean": fd.get("mean"),
                "fouls_drawn_per_90": fd.get("per_90"),
            })

    opp_dribble_leaders.sort(key=lambda x: x.get("dribbles_attempts_mean") or 0.0, reverse=True)
    opp_fouls_drawn_leaders.sort(key=lambda x: x.get("fouls_drawn_mean") or 0.0, reverse=True)

    opp_dist_key = "away_distributions" if opp_side == "away" else "home_distributions"
    opp_fouls_team = ((((evidence_bundle.get(opp_dist_key) or {}).get("metrics") or {}).get("fouls") or {}).get("conceded") or {}).get("all") or {}

    opponent_block = {
        "dribbles_status": "AVAILABLE" if has_opp_dribbles else "NO_VERIFICADO",
        "top_dribblers": opp_dribble_leaders[:6],
        "fouls_drawn_status": "AVAILABLE" if has_opp_fouls_drawn else "NO_VERIFICADO",
        "top_fouls_drawn": opp_fouls_drawn_leaders[:6],
        "opponent_team_fouls_conceded_by_rivals": {
            "n": opp_fouls_team.get("n", 0),
            "mean": opp_fouls_team.get("mean"),
        },
        "attacking_corridors_and_transitions": (tactical_duel_notes or {}).get("attacking_corridors") or "NO_VERIFICADO",
        "duels_and_pressing": (tactical_duel_notes or {}).get("pressing_and_duels") or "NO_VERIFICADO",
    }

    # C. Tactical matchup (never invent fixed 1v1 marking)
    matchup_block = {
        "probable_direct_opponent": (tactical_duel_notes or {}).get("direct_opponent") or "NO_VERIFICADO",
        "marking_scheme": (tactical_duel_notes or {}).get("marking_scheme") or "NO_VERIFICADO",
        "defensive_cover": (tactical_duel_notes or {}).get("defensive_cover") or "NO_VERIFICADO",
        "transition_exposure_risk": (tactical_duel_notes or {}).get("transition_exposure_risk") or "NO_VERIFICADO",
        "coach_tactical_adjustments": (tactical_duel_notes or {}).get("coach_adjustments") or "NO_VERIFICADO",
        "duel_reducing_scenarios": (tactical_duel_notes or {}).get("duel_reducing_scenarios") or "NO_VERIFICADO",
    }

    # D. Referee
    referee_block = referee_info or {
        "status": "NO_VERIFICADO",
        "fouls_called_per_match": None,
        "yellow_cards_per_match": None,
        "foul_vs_card_distinction": "Cometer faltas tacticas no equivale automaticamente a recibir tarjeta amarilla; depende de zona, reiteracion y criterio arbitral.",
    }

    # E. Commercial comparison (Bwin PLAYER_FOULS_COMMITTED lines 1+, 2+, 3+...)
    from bet_engine.markets.tactical_market_comparator import compute_implied_break_even

    bwin_lines: list[dict[str, Any]] = []
    if bwin_catalog:
        p_norm = _norm_name(player_name)
        p_toks = [t for t in p_norm.split() if len(t) >= 4]
        for e in bwin_catalog.get("entries") or []:
            if e.get("market_family") != "PLAYER_FOULS_COMMITTED":
                continue
            ent = _norm_name(e.get("target_entity"))
            if p_norm == ent or (p_toks and all(t in ent for t in p_toks)):
                imp = compute_implied_break_even(e.get("odd"))
                line_int = int(e["line"]) if str(e.get("line") or "").isdigit() else None
                th_key = f"{line_int}+" if line_int is not None else None
                hist_rate = ((defensive_block.get("fouls_committed") or {}).get("threshold_rates") or {}).get(th_key) if th_key else None
                bwin_lines.append({
                    "selection_id": e.get("selection_id"),
                    "line": e.get("direction"),
                    "odd": e.get("odd"),
                    "implied_break_even_prob": str(imp) if imp is not None else None,
                    "historical_descriptive_rate": hist_rate,
                    "settlement_status": e.get("settlement_status"),
                    "predictive_probability_status": "PREDICTIVE_PROBABILITY_NOT_ACCREDITED",
                    "lineup_dependency": "VERIFIED" if lineup_status in {"LINEUPS_API_CONFIRMED", "LINEUPS_EXTERNAL_CONFIRMED", "LINEUPS_MANUAL_VERIFIED"} else "LINEUP_DEPENDENCY_PENDING",
                })
    bwin_lines.sort(key=lambda x: str(x.get("line") or ""))

    return {
        "schema_version": "1.0-player-fouls-duel-context",
        "player_defensive": defensive_block,
        "opponent_profile": opponent_block,
        "tactical_matchup": matchup_block,
        "referee": referee_block,
        "commercial_bwin_lines": bwin_lines,
        "epistemic_guards": {
            "predictive_ev_allowed": False,
            "lineup_status": lineup_status,
            "note": "El promedio historico de faltas no prueba por si solo la actuacion futura; evaluar rol, rival y XI oficial.",
        },
    }


def build_player_shots_context(
    *,
    evidence_bundle: dict[str, Any],
    player_name: str,
    side: str,
    lineup_status: str = "LINEUPS_PENDING",
) -> dict[str, Any]:
    """Alcance limitado para remates de jugadores sin crear modelo probabilístico individual (Sección 4)."""
    side_low = side.lower()
    opp_side = "away" if side_low == "home" else "home"
    players_map = (evidence_bundle.get("players") or {}).get(side_low) or {}
    rec = _find_player_record(players_map, player_name)

    team_dist_key = "home_distributions" if side_low == "home" else "away_distributions"
    opp_dist_key = "away_distributions" if opp_side == "away" else "home_distributions"
    team_shots = ((((evidence_bundle.get(team_dist_key) or {}).get("metrics") or {}).get("shots") or {}).get("own") or {}).get("all") or {}
    opp_shots_conc = ((((evidence_bundle.get(opp_dist_key) or {}).get("metrics") or {}).get("shots") or {}).get("conceded") or {}).get("all") or {}
    opp_ot_conc = ((((evidence_bundle.get(opp_dist_key) or {}).get("metrics") or {}).get("on_target") or {}).get("conceded") or {}).get("all") or {}

    return {
        "schema_version": "1.0-player-shots-limited-context",
        "player_name": rec.get("name") if rec else player_name,
        "lineup_status": lineup_status,
        "exposure_status": rec.get("exposure_status") if rec else PLAYER_STATUS_NO_DATA,
        "positions_observed": rec.get("positions_observed") if rec else [],
        "matches_played": rec.get("matches_played", 0) if rec else 0,
        "starts": rec.get("starts", 0) if rec else 0,
        "minutes_total": rec.get("minutes_total") if rec else None,
        "shots": rec.get("shots") if rec else {"status": PLAYER_STATUS_NO_DATA, "n": 0},
        "on_target": rec.get("on_target") if rec else {"status": PLAYER_STATUS_NO_DATA, "n": 0},
        "team_offensive_shots": {"n": team_shots.get("n", 0), "mean": team_shots.get("mean")},
        "opponent_shots_conceded": {"n": opp_shots_conc.get("n", 0), "mean": opp_shots_conc.get("mean")},
        "opponent_on_target_conceded": {"n": opp_ot_conc.get("n", 0), "mean": opp_ot_conc.get("mean")},
        "future_phase_note": "PLAYER_SHOT_OPPOSITION_ANALYSIS",
        "note": (
            "Alcance limitado sin nuevo modelo probabilistico individual. Pendiente para fase futura "
            "(PLAYER_SHOT_OPPOSITION_ANALYSIS): estudiar suministro ofensivo, zonas de finalizacion y por que "
            "un equipo genera corners/remates colectivos sin tiros a puerta ni volumen individual (ej. Calleri vs Fluminense)."
        ),
    }
