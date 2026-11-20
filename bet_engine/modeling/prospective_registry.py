"""Registro prospectivo PREMATCH inmutable y evaluador pareado (Iteración 2.2B -> 2.2C.1).

Totalmente independiente de las decisiones accionables de Shadow en producción.
Garantiza:
1. Conexión real as-of de AsOfEloTracker y team_ordered_by_id (SoS) para que M0, M0_plus_Elo,
   M4 y M4_plus_Elo representen cuatro variantes funcionalmente distintas.
2. Verificación estricta de identidad del fixture (IDs, equipos, competición, kickoff, status=NS)
   y hora real de ejecución (now_utc < kickoff_utc).
3. Creación exclusiva atómica (`xb`) para evitar sobrescritura por concurrencia, y liquidación
   en archivo separado (`settlements/*.settlement.json`) sin alterar el registro original.
4. Separación entre coincidencia de campos, procedencia verificada del proveedor y resultado
   acreditable (`VERIFIED_AUTHORITATIVE` vs `UNVERIFIED_SOURCE_PROVENANCE` vs `UNVERIFIED_RESULT`
   vs `SIMULATED_TEST_ONLY`).
5. Validación de `odds_captured_utc` (`NO_ODDS`, `ODDS_UNVERIFIED`, `ODDS_VERIFIED`) para separar
   evaluación deportiva de métricas económicas.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bet_engine.context.observation_store import HistoricalTeamMatch, MatchObservationBundle, ObservationStore
from bet_engine.modeling.calibration import brier_score, log_loss, parse_utc_timestamp
from bet_engine.modeling.distributions import _gamma_rate_bounds, dixon_coles_joint_grid
from bet_engine.modeling.experimental_v2_1b import (
    AsOfEloTracker,
    ExperimentalConfig,
    FINISHED_ELO_STATUSES,
    predict_experimental_match,
    resolve_elo_available_dt,
)

MADRID_TZ = ZoneInfo("Europe/Madrid")
CORPUS_CUTOFF_UTC = "2026-11-14T19:45:00+00:00"
PROSPECTIVE_MIN_SAMPLE = {"CLUB": 200, "NATIONAL_TEAM": 50}
FROZEN_MODEL_NAMES = ("M0", "M0_plus_Elo", "M4", "M4_plus_Elo")
LIVE_OR_FINISHED_STATUSES = frozenset({"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "FT", "AET", "PEN"})
ALLOWED_PREMATCH_STATUSES = frozenset({"NS"})
REGISTRY_SCHEMA_VERSION = "2.2C.1-prospective-record"
SETTLEMENT_SCHEMA_VERSION = "2.2C.1-prospective-settlement"


class ProspectiveRegistryError(ValueError):
    """Error de integridad temporal, identidad, retrospectiva, procedencia o inmutabilidad."""


def _canonical_sha256(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_manifest_code_hashes(manifest_path: Path, raw_manifest: dict[str, Any]) -> dict[str, str]:
    """Verifica hashes SHA-256 de código activo declarados en el manifiesto."""
    code_hashes = dict(raw_manifest.get("code_hashes") or {})
    if not code_hashes:
        return {}
    # Buscar raíz del proyecto (subiendo desde manifest_path o cwd)
    repo_root = Path.cwd()
    for cand in (manifest_path.resolve().parent.parent.parent, manifest_path.resolve().parent.parent, Path.cwd()):
        if (cand / "bet_engine").is_dir():
            repo_root = cand
            break
    verified: dict[str, str] = {}
    for rel, expected_sha in code_hashes.items():
        target = repo_root / rel
        if not target.is_file():
            raise ProspectiveRegistryError(f"MANIFEST_CODE_FILE_MISSING:{rel}")
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise ProspectiveRegistryError(f"MANIFEST_CODE_HASH_MISMATCH:{rel}:{actual_sha}!={expected_sha}")
        verified[rel] = actual_sha
    return verified


def load_frozen_configs(
    manifest_path: Path,
    *,
    verify_code_hashes: bool = True,
) -> tuple[dict[str, ExperimentalConfig], str]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if verify_code_hashes:
        verify_manifest_code_hashes(manifest_path, raw)
    version = str(raw.get("schema_version") or "2.2A-frozen-models")
    base_rates = {
        str(k): (float(v[0]), float(v[1]))
        for k, v in (raw.get("train_fit_base_rates") or {}).items()
    }
    cfgs: dict[str, ExperimentalConfig] = {}
    for key in FROZEN_MODEL_NAMES:
        spec = (raw.get("frozen_candidates") or {})[key]
        cfgs[key] = ExperimentalConfig(
            name=str(spec["name"]),
            multiplicative=bool(spec["multiplicative"]),
            use_sos_opp_rates=bool(spec["use_sos_opp_rates"]),
            sos_alpha=float(spec["sos_alpha"]),
            use_elo=bool(spec["use_elo"]),
            elo_weight=float(spec["elo_weight"]),
            half_life_days=spec["half_life_days"],
            venue_blend=float(spec["venue_blend"]),
            shrink_k=float(spec["shrink_k"]),
            rho=float(spec["rho"]),
            base_rates_by_stratum=base_rates or None,
        )
    return cfgs, version


def extract_bwin_capture_utc(bwin_txt_path: Path) -> str | None:
    """Extrae y valida el timestamp UTC con zona horaria desde la cabecera de Bwin TXT."""
    if not bwin_txt_path.is_file():
        return None
    txt_head = bwin_txt_path.read_text(encoding="utf-8-sig", errors="replace")[:1500]
    cap = re.search(r"(?:Fecha captura|Captura)[^:]*:\s*(\S+)", txt_head)
    if not cap:
        return None
    cap_raw = cap.group(1).strip()
    iso_str = cap_raw[:-1] + "+00:00" if cap_raw.endswith("Z") else cap_raw
    try:
        dt_parsed = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if dt_parsed.tzinfo is None:
        return None
    return dt_parsed.astimezone(timezone.utc).isoformat()


def _classify_odds_status(
    bwin_odds: dict[str, Any] | None,
    odds_captured_utc: str | None,
    odds_sha256: str | None,
    now_dt: datetime,
    ko_dt: datetime,
) -> tuple[str, str | None]:
    if not bwin_odds:
        return "NO_ODDS", None
    if not odds_captured_utc or not odds_sha256:
        return "ODDS_UNVERIFIED", None
    try:
        iso_str = odds_captured_utc[:-1] + "+00:00" if odds_captured_utc.endswith("Z") else odds_captured_utc
        dt_parsed = datetime.fromisoformat(iso_str)
        if dt_parsed.tzinfo is None:
            return "ODDS_UNVERIFIED", None
        cap_dt = dt_parsed.astimezone(timezone.utc)
    except ValueError:
        return "ODDS_UNVERIFIED", None
    if cap_dt > now_dt or cap_dt >= ko_dt:
        return "ODDS_UNVERIFIED", cap_dt.isoformat()
    return "ODDS_VERIFIED", cap_dt.isoformat()


def _hm_to_elo_fixture(m: HistoricalTeamMatch) -> dict[str, Any] | None:
    hg_raw = m.own_metrics.get("goals")
    ag_raw = m.conceded_metrics.get("goals")
    if hg_raw is None or ag_raw is None:
        return None
    if m.venue == "HOME":
        hid, aid, hg, ag = int(m.team_id), int(m.opponent_id), float(hg_raw), float(ag_raw)
    else:
        hid, aid, hg, ag = int(m.opponent_id), int(m.team_id), float(ag_raw), float(hg_raw)
    return {
        "fixture_id": int(m.fixture_id),
        "home_id": hid,
        "away_id": aid,
        "hg": hg,
        "ag": ag,
        "stratum": m.competition_stratum,
        "kickoff_utc": m.kickoff_utc,
        "avail_dt": m.available_at_utc,
        "status": str(m.status or "FT").upper(),
    }


def build_asof_elo_and_team_history(
    *,
    bundle: MatchObservationBundle,
    cutoff_dt: datetime,
    corpus_root: Path | None = None,
    extra_fixtures: list[dict[str, Any]] | None = None,
    extra_team_series: dict[int, list[HistoricalTeamMatch]] | None = None,
) -> tuple[AsOfEloTracker, dict[int, list[HistoricalTeamMatch]], dict[str, Any]]:
    """Construye AsOfEloTracker y team_ordered_by_id estrictamente as-of (<= cutoff_dt)."""
    chron_by_fid: dict[int, dict[str, Any]] = {}
    team_map: dict[int, dict[int, HistoricalTeamMatch]] = {}
    corpus_loaded = False
    corpus_fixtures_raw = 0
    excluded_post_cutoff = 0

    def _add_hm(m: HistoricalTeamMatch) -> None:
        nonlocal excluded_post_cutoff
        ko = parse_utc_timestamp(m.kickoff_utc)
        av = parse_utc_timestamp(m.available_at_utc)
        if ko >= cutoff_dt or av > cutoff_dt:
            excluded_post_cutoff += 1
            return
        if str(m.status or "").strip().upper() not in FINISHED_ELO_STATUSES:
            return
        team_map.setdefault(int(m.team_id), {})[int(m.fixture_id)] = m
        fx = _hm_to_elo_fixture(m)
        if fx is not None and int(m.fixture_id) not in chron_by_fid:
            chron_by_fid[int(m.fixture_id)] = fx

    if corpus_root is not None:
        try:
            from tools.reproduce_baseline_backtest import _load_corpus

            _, c_team_series, c_all_fixtures, _ = _load_corpus(corpus_root)
            corpus_loaded = True
            corpus_fixtures_raw = len(c_all_fixtures)
            for tid, fmap in c_team_series.items():
                for m in fmap.values():
                    _add_hm(m)
            for fid, info in c_all_fixtures.items():
                ko_str = info.get("kickoff_utc")
                st = str(info.get("status") or "FT").strip().upper()
                if not ko_str or st not in FINISHED_ELO_STATUSES:
                    continue
                ko_dt = parse_utc_timestamp(str(ko_str))
                av_dt, _ = resolve_elo_available_dt({"ko_dt": ko_dt, "status": st, "avail_dt": info.get("available_at_utc")})
                if av_dt is None or ko_dt >= cutoff_dt or av_dt > cutoff_dt:
                    excluded_post_cutoff += 1
                    continue
                if info.get("home_goals") is None or info.get("away_goals") is None:
                    continue
                chron_by_fid[int(fid)] = {
                    "fixture_id": int(fid),
                    "home_id": int(info["home_id"]),
                    "away_id": int(info["away_id"]),
                    "hg": float(info["home_goals"]),
                    "ag": float(info["away_goals"]),
                    "stratum": info.get("stratum") or "CLUB",
                    "ko_dt": ko_dt,
                    "avail_dt": av_dt,
                    "status": st,
                }
        except FileNotFoundError:
            corpus_loaded = False

    for m in tuple(bundle.home_matches) + tuple(bundle.away_matches):
        _add_hm(m)

    if extra_team_series:
        for ms in extra_team_series.values():
            for m in ms:
                _add_hm(m)

    if extra_fixtures:
        for fx in extra_fixtures:
            fid = int(fx.get("fixture_id") or 0)
            st = str(fx.get("status") or "FT").strip().upper()
            if st not in FINISHED_ELO_STATUSES:
                continue
            av_dt, _ = resolve_elo_available_dt(fx)
            ko_dt = fx.get("ko_dt") or (parse_utc_timestamp(str(fx["kickoff_utc"])) if fx.get("kickoff_utc") else None)
            if av_dt is None or ko_dt is None or ko_dt >= cutoff_dt or av_dt > cutoff_dt:
                excluded_post_cutoff += 1
                continue
            row = dict(fx)
            row["ko_dt"] = ko_dt
            row["avail_dt"] = av_dt
            row["status"] = st
            if fid:
                chron_by_fid[fid] = row

    ordered_chron = list(chron_by_fid.values())
    elo_tracker = AsOfEloTracker.build_from_fixtures(ordered_chron, k_factor=20.0, hfa=55.0)
    team_ordered_by_id = {
        tid: sorted(fmap.values(), key=lambda x: (parse_utc_timestamp(x.kickoff_utc), x.fixture_id))
        for tid, fmap in team_map.items()
    }
    meta = {
        "corpus_loaded": corpus_loaded,
        "corpus_fixtures_raw": corpus_fixtures_raw,
        "total_elo_fixtures_asof": len(ordered_chron),
        "teams_in_history_n": len(team_ordered_by_id),
        "excluded_post_cutoff_n": excluded_post_cutoff,
        "elo_provenance_counts": dict(elo_tracker.provenance_counts),
        "elo_excluded_non_finished": int(elo_tracker.excluded_non_finished),
    }
    return elo_tracker, team_ordered_by_id, meta


def _verify_target_fixture_identity(
    match_engine_payload: dict[str, Any],
    bundle: MatchObservationBundle,
    ko_dt: datetime,
) -> dict[str, Any]:
    """Verifica identidad completa del fixture entre match_engine y target_fixture_full."""
    fx = match_engine_payload.get("fixture") or {}
    ctx = match_engine_payload.get("context") or {}
    target_resp = ((ctx.get("target_fixture_full") or {}).get("response") or [])
    if not target_resp:
        raise ProspectiveRegistryError("FIXTURE_IDENTITY_UNVERIFIED:MISSING_TARGET_FIXTURE_FULL")

    t0 = target_resp[0]
    t_fx = t0.get("fixture") or {}
    t_lg = t0.get("league") or {}
    t_tm = t0.get("teams") or {}
    t_goals = t0.get("goals") or {}

    t_status = str((t_fx.get("status") or {}).get("short") or "").strip().upper()
    if t_status in LIVE_OR_FINISHED_STATUSES:
        raise ProspectiveRegistryError(f"RETROSPECTIVE_RECORD_FORBIDDEN:status={t_status}")
    if t_status not in ALLOWED_PREMATCH_STATUSES:
        raise ProspectiveRegistryError(f"UNKNOWN_OR_NON_PREMATCH_STATUS:{t_status or 'EMPTY'}")

    if t_goals.get("home") is not None or t_goals.get("away") is not None:
        raise ProspectiveRegistryError("RETROSPECTIVE_RECORD_FORBIDDEN:TARGET_GOALS_ALREADY_PRESENT")

    t_fid = t_fx.get("id")
    if t_fid is None or int(t_fid) != int(bundle.fixture_id):
        raise ProspectiveRegistryError(f"FIXTURE_IDENTITY_ID_MISMATCH:{t_fid}!={bundle.fixture_id}")

    t_hid = (t_tm.get("home") or {}).get("id")
    t_aid = (t_tm.get("away") or {}).get("id")
    if t_hid is None or int(t_hid) != int(bundle.home_id) or t_aid is None or int(t_aid) != int(bundle.away_id):
        raise ProspectiveRegistryError(
            f"FIXTURE_IDENTITY_TEAMS_MISMATCH:({t_hid},{t_aid})!=({bundle.home_id},{bundle.away_id})"
        )

    t_lid = t_lg.get("id")
    if t_lid is not None and bundle.league_id and int(t_lid) != int(bundle.league_id):
        raise ProspectiveRegistryError(f"FIXTURE_IDENTITY_LEAGUE_MISMATCH:{t_lid}!={bundle.league_id}")
    t_season = t_lg.get("season")
    if t_season is not None and bundle.season and int(t_season) != int(bundle.season):
        raise ProspectiveRegistryError(f"FIXTURE_IDENTITY_SEASON_MISMATCH:{t_season}!={bundle.season}")

    t_date = t_fx.get("date")
    if not t_date:
        raise ProspectiveRegistryError("FIXTURE_IDENTITY_MISSING_TARGET_DATE")
    t_ko_dt = parse_utc_timestamp(str(t_date))
    if abs((t_ko_dt - ko_dt).total_seconds()) > 60.0:
        raise ProspectiveRegistryError(f"FIXTURE_IDENTITY_KICKOFF_MISMATCH:{t_ko_dt.isoformat()}!={ko_dt.isoformat()}")

    return {
        "fixture_identity_verified": True,
        "verified_fixture_id": int(bundle.fixture_id),
        "verified_home_id": int(bundle.home_id),
        "verified_away_id": int(bundle.away_id),
        "verified_league_id": int(bundle.league_id),
        "verified_season": int(bundle.season),
        "verified_status": t_status,
        "kickoff_delta_seconds": round(abs((t_ko_dt - ko_dt).total_seconds()), 3),
    }


def _classify_lineup_state(bundle: MatchObservationBundle, payload: dict[str, Any]) -> dict[str, Any]:
    """Diferencia explícitamente alineaciones confirmadas vs probables vs no disponibles."""
    le = dict(getattr(bundle, "target_lineup_evaluation", None) or {})
    st = str(le.get("status") or "").upper()
    ctx = payload.get("context") or {}
    api_lineups = ((ctx.get("lineups") or {}).get("response") or [])
    confirmed_teams = sum(1 for item in api_lineups if isinstance(item, dict) and len(item.get("startXI") or []) == 11)
    if st == "LINEUPS_CONFIRMED_COMPLETE" or confirmed_teams == 2:
        state = "CONFIRMED_COMPLETE_22"
    elif confirmed_teams == 1 or le.get("partial_team_confirmed"):
        state = "CONFIRMED_PARTIAL_11"
    elif "PROBABLE" in st:
        state = "PROBABLE_ONLY"
    else:
        state = "UNAVAILABLE"
    return {
        "lineup_state": state,
        "confirmed_complete_22": state == "CONFIRMED_COMPLETE_22",
        "api_confirmed_sides": confirmed_teams,
        "raw_gate_status": st or "LINEUPS_PENDING",
    }


def _quantile_range_from_grid(grid: list[list[float]], q_lo: float = 0.10, q_hi: float = 0.90) -> list[int]:
    mg = len(grid) - 1
    tot_pmf = [0.0] * (2 * mg + 1)
    for i in range(mg + 1):
        for j in range(mg + 1):
            tot_pmf[i + j] += grid[i][j]
    cdf = 0.0
    lo_k, hi_k = 0, len(tot_pmf) - 1
    got_lo = False
    for k, p in enumerate(tot_pmf):
        cdf += p
        if not got_lo and cdf >= q_lo:
            lo_k = k
            got_lo = True
        if cdf >= q_hi:
            hi_k = k
            break
    return [lo_k, hi_k]


def _observations_fingerprint(
    bundle: MatchObservationBundle,
    cutoff_dt: datetime,
    elo_meta: dict[str, Any],
) -> str:
    return _canonical_sha256(
        {
            "cutoff_utc": cutoff_dt.isoformat(),
            "fixture_id": bundle.fixture_id,
            "home_id": bundle.home_id,
            "away_id": bundle.away_id,
            "home_matches": [
                (m.fixture_id, m.kickoff_utc, m.own_metrics.get("goals"), m.conceded_metrics.get("goals"), m.venue)
                for m in bundle.home_matches
            ],
            "away_matches": [
                (m.fixture_id, m.kickoff_utc, m.own_metrics.get("goals"), m.conceded_metrics.get("goals"), m.venue)
                for m in bundle.away_matches
            ],
            "elo_fixtures_asof": elo_meta.get("total_elo_fixtures_asof"),
            "teams_in_history_n": elo_meta.get("teams_in_history_n"),
        }
    )


def record_prospective_prediction(
    *,
    match_engine_payload: dict[str, Any],
    manifest_path: Path,
    records_dir: Path,
    elo_tracker: AsOfEloTracker | None = None,
    team_ordered_by_id: dict[int, list[Any]] | None = None,
    corpus_root: Path | None = None,
    extra_fixtures: list[dict[str, Any]] | None = None,
    extra_team_series: dict[int, list[HistoricalTeamMatch]] | None = None,
    bwin_odds: dict[str, Any] | None = None,
    odds_captured_utc: str | None = None,
    odds_sha256: str | None = None,
    shadow_decision: str | None = None,
    source_paths: dict[str, str] | None = None,
    now_utc: datetime | None = None,
    min_corpus_cutoff_utc: str = CORPUS_CUTOFF_UTC,
    allow_overwrite: bool = False,
    verify_code_hashes: bool = True,
) -> Path:
    """Genera y congela un registro prospectivo PREMATCH estrictamente antes del kickoff."""
    if allow_overwrite:
        raise ProspectiveRegistryError("OVERWRITE_PROSPECTIVE_RECORD_FORBIDDEN")

    now_dt = now_utc or datetime.now(timezone.utc)
    fx = match_engine_payload.get("fixture") or {}
    match_meta = match_engine_payload.get("match") or {}
    ctx = match_engine_payload.get("context") or {}

    target_resp = ((ctx.get("target_fixture_full") or {}).get("response") or [])
    target_fx_obj = (target_resp[0].get("fixture") or {}) if target_resp else {}

    ko_raw = fx.get("date") or target_fx_obj.get("date")
    if not ko_raw:
        raise ProspectiveRegistryError("MISSING_KICKOFF_UTC")
    ko_dt = parse_utc_timestamp(str(ko_raw))

    if now_dt >= ko_dt:
        raise ProspectiveRegistryError(f"RETROSPECTIVE_RECORD_FORBIDDEN:now({now_dt.isoformat()})>=kickoff({ko_dt.isoformat()})")

    corpus_cut_dt = parse_utc_timestamp(min_corpus_cutoff_utc)
    if ko_dt <= corpus_cut_dt:
        raise ProspectiveRegistryError(
            f"NON_PROSPECTIVE_KICKOFF:kickoff({ko_dt.isoformat()})<=corpus_cutoff({corpus_cut_dt.isoformat()})"
        )

    cutoff_raw = match_engine_payload.get("generated_at") or now_dt.isoformat()
    cutoff_dt = parse_utc_timestamp(str(cutoff_raw))
    if cutoff_dt > now_dt:
        raise ProspectiveRegistryError("FUTURE_CUTOFF_TIMESTAMP_INVALID")
    if cutoff_dt >= ko_dt:
        raise ProspectiveRegistryError("RETROSPECTIVE_RECORD_FORBIDDEN:cutoff>=kickoff")

    bundle = ObservationStore.from_match_engine_json(match_engine_payload, cutoff_utc=cutoff_dt)
    identity_check = _verify_target_fixture_identity(match_engine_payload, bundle, ko_dt)

    stratum = (
        bundle.home_matches[0].competition_stratum
        if bundle.home_matches
        else (bundle.away_matches[0].competition_stratum if bundle.away_matches else "CLUB")
    )
    stratum_norm = "NATIONAL_TEAM" if stratum == "NATIONAL_TEAM" else "CLUB"

    cfgs, manifest_version = load_frozen_configs(manifest_path, verify_code_hashes=verify_code_hashes)

    if elo_tracker is None or team_ordered_by_id is None:
        built_elo, built_teams, elo_builder_meta = build_asof_elo_and_team_history(
            bundle=bundle,
            cutoff_dt=cutoff_dt,
            corpus_root=corpus_root,
            extra_fixtures=extra_fixtures,
            extra_team_series=extra_team_series,
        )
        if elo_tracker is None:
            elo_tracker = built_elo
        if team_ordered_by_id is None:
            team_ordered_by_id = built_teams
    else:
        elo_builder_meta = {
            "corpus_loaded": False,
            "corpus_fixtures_raw": 0,
            "total_elo_fixtures_asof": sum(len(v) for v in elo_tracker.history.values()),
            "teams_in_history_n": len(team_ordered_by_id),
            "excluded_post_cutoff_n": 0,
            "elo_provenance_counts": dict(elo_tracker.provenance_counts),
            "elo_excluded_non_finished": int(elo_tracker.excluded_non_finished),
        }

    obs_sha = _observations_fingerprint(bundle, cutoff_dt, elo_builder_meta)
    params_sha = _canonical_sha256({m: dataclasses.asdict(cfgs[m]) for m in FROZEN_MODEL_NAMES})

    home_obs_frozen = tuple(bundle.home_matches)
    away_obs_frozen = tuple(bundle.away_matches)

    predictions: dict[str, Any] = {}
    for mname in FROZEN_MODEL_NAMES:
        cfg = cfgs[mname]
        res = predict_experimental_match(
            home_obs_frozen,
            away_obs_frozen,
            home_id=bundle.home_id,
            away_id=bundle.away_id,
            target_ko_dt=ko_dt,
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=stratum_norm,
            cfg=cfg,
            team_ordered_by_id=team_ordered_by_id,
            elo_tracker=elo_tracker,
            history_limit=20,
        )
        lh, la = float(res["lambda_home"]), float(res["lambda_away"])
        comp = res["components"]
        nh_post = (float(comp["home_n_eff"]) + cfg.shrink_k) * 0.85
        na_post = (float(comp["away_n_eff"]) + cfg.shrink_k) * 0.85
        lh_lo, lh_hi = _gamma_rate_bounds(lh, nh_post, 1.18)
        la_lo, la_hi = _gamma_rate_bounds(la, na_post, 1.18)
        corners = [
            sum(g[i][j] for i in range(13) for j in range(13) if i > j)
            for g in [dixon_coles_joint_grid(a, b, rho=cfg.rho, max_goals=12) for a in (lh_lo, lh_hi) for b in (la_lo, la_hi)]
        ]
        grid = dixon_coles_joint_grid(lh, la, rho=cfg.rho, max_goals=12)
        predictions[mname] = {
            "lambda_home": lh,
            "lambda_away": la,
            "outcomes_1x2": {
                "home_win": round(res["p_home"], 6),
                "draw": round(res["p_draw"], 6),
                "away_win": round(res["p_away"], 6),
            },
            "p_over_2_5": round(res["p_over_2_5"], 6),
            "p_btts_yes": round(res["p_btts_yes"], 6),
            "uncertainty": {
                "p_home_epistemic_bounds": [round(min(corners), 4), round(max(corners), 4)],
                "total_goals_q10_q90": _quantile_range_from_grid(grid, 0.10, 0.90),
            },
            "info_state_sha256": obs_sha,
            "components": comp,
        }

    degradation_reasons: list[str] = []
    if not predictions["M0_plus_Elo"]["components"].get("elo_coverage_ok"):
        degradation_reasons.append("M0_PLUS_ELO_DEGRADED_NO_ELO_COVERAGE")
    if not predictions["M4_plus_Elo"]["components"].get("elo_coverage_ok"):
        degradation_reasons.append("M4_PLUS_ELO_DEGRADED_NO_ELO_COVERAGE")
    if float(predictions["M4"]["components"].get("sos_coverage_rate") or 0.0) <= 0.0:
        degradation_reasons.append("M4_DEGRADED_NO_SOS_COVERAGE")
    if float(predictions["M4_plus_Elo"]["components"].get("sos_coverage_rate") or 0.0) <= 0.0:
        degradation_reasons.append("M4_PLUS_ELO_DEGRADED_NO_SOS_COVERAGE")

    comparison_integrity = {
        "full_four_model_comparison_valid": len(degradation_reasons) == 0,
        "degradation_reasons": degradation_reasons,
        "elo_effective": {
            "m0_plus_elo_ok": bool(predictions["M0_plus_Elo"]["components"].get("elo_coverage_ok")),
            "m4_plus_elo_ok": bool(predictions["M4_plus_Elo"]["components"].get("elo_coverage_ok")),
            "elo_home": predictions["M0_plus_Elo"]["components"].get("elo_home"),
            "elo_away": predictions["M0_plus_Elo"]["components"].get("elo_away"),
            "elo_home_n": predictions["M0_plus_Elo"]["components"].get("elo_home_n"),
            "elo_away_n": predictions["M0_plus_Elo"]["components"].get("elo_away_n"),
        },
        "sos_effective": {
            "m4_sos_coverage_rate": predictions["M4"]["components"].get("sos_coverage_rate"),
            "m4_plus_elo_sos_coverage_rate": predictions["M4_plus_Elo"]["components"].get("sos_coverage_rate"),
        },
    }

    odds_status, normalized_odds_cap = _classify_odds_status(bwin_odds, odds_captured_utc, odds_sha256, now_dt, ko_dt)

    exclusions: list[str] = []
    if len(bundle.home_matches) < 6 or len(bundle.away_matches) < 6:
        exclusions.append(f"LOW_HISTORY_LT_6(home={len(bundle.home_matches)},away={len(bundle.away_matches)})")
    if odds_status == "NO_ODDS":
        exclusions.append("NO_BWIN_ODDS_CAPTURED")
    elif odds_status == "ODDS_UNVERIFIED":
        exclusions.append("BWIN_ODDS_UNVERIFIED_TIMESTAMP")

    red_unk_h = sum(1 for m in bundle.home_matches if (m.red_card_detail or {}).get("red_card_status") == "RED_CARD_UNKNOWN")
    red_unk_a = sum(1 for m in bundle.away_matches if (m.red_card_detail or {}).get("red_card_status") == "RED_CARD_UNKNOWN")

    pre_kickoff_block = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "fixture_id": bundle.fixture_id,
        "event_id": str(match_meta.get("event_id") or bundle.event_id or ""),
        "competition": {
            "league_id": bundle.league_id,
            "league_name": str(fx.get("league_name") or ""),
            "season": bundle.season,
            "stratum": stratum_norm,
        },
        "teams": {
            "home_id": bundle.home_id,
            "home_name": bundle.home_name,
            "away_id": bundle.away_id,
            "away_name": bundle.away_name,
        },
        "identity_verification": identity_check,
        "timing": {
            "kickoff_utc": ko_dt.isoformat(),
            "kickoff_madrid": ko_dt.astimezone(MADRID_TZ).isoformat(),
            "cutoff_utc": cutoff_dt.isoformat(),
            "generated_utc": now_dt.isoformat(),
            "lead_minutes_before_kickoff": round((ko_dt - now_dt).total_seconds() / 60.0, 2),
            "temporal_integrity_status": "PREMATCH_VERIFIED",
        },
        "provenance": {
            "registry_schema_version": REGISTRY_SCHEMA_VERSION,
            "input_sha256": bundle.source_sha256,
            "match_engine_payload_sha256": _canonical_sha256(match_engine_payload),
            "observations_sha256": obs_sha,
            "odds_sha256": odds_sha256,
            "manifest_sha256": _canonical_sha256(json.loads(manifest_path.read_text(encoding="utf-8"))),
            "model_params_sha256": params_sha,
            "frozen_manifest_version": manifest_version,
            "elo_builder_meta": elo_builder_meta,
            "source_paths": dict(source_paths or {}),
        },
        "history_summary": {
            "home_raw_n": len(bundle.home_matches),
            "away_raw_n": len(bundle.away_matches),
            "home_n_eff_m0": predictions["M0"]["components"]["home_n_eff"],
            "away_n_eff_m0": predictions["M0"]["components"]["away_n_eff"],
            "red_card_unknown_home_n": red_unk_h,
            "red_card_unknown_away_n": red_unk_a,
        },
        "model_comparison_integrity": comparison_integrity,
        "lineup_status": _classify_lineup_state(bundle, match_engine_payload),
        "bwin_odds": {
            "odds_status": odds_status,
            "economic_eval_eligible": odds_status == "ODDS_VERIFIED",
            "captured_utc": normalized_odds_cap or odds_captured_utc,
            "quotes": dict(bwin_odds or {}),
        },
        "shadow_independence": {
            "shadow_decision_at_record": shadow_decision,
            "evaluation_coupled_to_shadow": False,
        },
        "exclusion_reasons": exclusions,
        "eligible_for_paired_eval": not any(e.startswith("LOW_HISTORY_LT_6") for e in exclusions),
        "predictions": predictions,
    }

    records_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{bundle.fixture_id}_{pre_kickoff_block['event_id'] or 'noevent'}.json"
    out_path = records_dir / slug

    full_doc = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "pre_kickoff": pre_kickoff_block,
        "pre_kickoff_sha256": _canonical_sha256(pre_kickoff_block),
        "settlement_mode": "SEPARATE_FILE_IMMUTABLE_PRE_KICKOFF",
    }
    encoded = json.dumps(full_doc, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        with out_path.open("xb") as fh:
            fh.write(encoded)
    except FileExistsError as exc:
        raise ProspectiveRegistryError(f"IMMUTABLE_RECORD_ALREADY_EXISTS:{out_path}") from exc
    return out_path


def _extract_authoritative_fields(source_payload: dict[str, Any]) -> dict[str, Any]:
    """Extrae campos y metadatos de procedencia de API-Football o snapshot estructurado."""
    if isinstance(source_payload.get("response"), list) and source_payload["response"]:
        item = source_payload["response"][0]
        fx = item.get("fixture") or {}
        lg = item.get("league") or {}
        tm = item.get("teams") or {}
        gl = item.get("goals") or {}
        has_api_envelope = bool(source_payload.get("get") == "fixtures" and isinstance(source_payload.get("parameters"), dict))
        return {
            "fixture_id": fx.get("id"),
            "home_id": (tm.get("home") or {}).get("id"),
            "away_id": (tm.get("away") or {}).get("id"),
            "league_id": lg.get("id"),
            "status": (fx.get("status") or {}).get("short"),
            "hg": gl.get("home"),
            "ag": gl.get("away"),
            "has_api_envelope": has_api_envelope,
            "provider_meta": source_payload.get("_provenance") or {},
        }
    ctx_resp = (((source_payload.get("context") or {}).get("target_fixture_full") or {}).get("response") or [])
    if ctx_resp:
        out = _extract_authoritative_fields({"response": ctx_resp})
        out["provider_meta"] = source_payload.get("_provenance") or {}
        return out
    return {
        "fixture_id": source_payload.get("fixture_id"),
        "home_id": source_payload.get("home_id"),
        "away_id": source_payload.get("away_id"),
        "league_id": source_payload.get("league_id"),
        "status": source_payload.get("status"),
        "hg": source_payload.get("hg"),
        "ag": source_payload.get("ag"),
        "has_api_envelope": False,
        "provider_meta": source_payload.get("_provenance") or {},
    }


def _has_verified_provider_provenance(
    source_payload: dict[str, Any],
    *,
    source_path: Path | None,
    provider_provenance_verified: bool,
) -> tuple[bool, str]:
    if provider_provenance_verified:
        return True, "EXPLICIT_VERIFIED_PROVIDER"
    extracted = _extract_authoritative_fields(source_payload)
    pmeta = extracted.get("provider_meta") or {}
    if extracted.get("has_api_envelope") and pmeta.get("provider") == "API-Football" and pmeta.get("captured_at_utc"):
        return True, "API_FOOTBALL_ENVELOPE_WITH_PROVENANCE"
    if source_path is not None and extracted.get("has_api_envelope"):
        norm = source_path.as_posix()
        if "/outputs/matches/" in norm or norm.startswith("outputs/matches/") or "/outputs/api_cache/" in norm or norm.startswith("outputs/api_cache/"):
            return True, "SANCTIONED_LOCAL_API_SNAPSHOT"
    return False, "UNVERIFIED_ARBITRARY_JSON_PROVENANCE"


def verify_authoritative_result(
    pre_kickoff: dict[str, Any],
    *,
    hg: int,
    ag: int,
    match_status: str,
    authoritative_fixture: dict[str, Any],
) -> dict[str, Any]:
    """Verifica coincidencia de fixture_id, equipos, competición, estado final y marcador."""
    src = _extract_authoritative_fields(authoritative_fixture)
    exp_fid = int(pre_kickoff["fixture_id"])
    exp_hid = int(pre_kickoff["teams"]["home_id"])
    exp_aid = int(pre_kickoff["teams"]["away_id"])
    exp_lid = int(pre_kickoff["competition"]["league_id"])
    exp_status = str(match_status).strip().upper()

    if src.get("fixture_id") is None or int(src["fixture_id"]) != exp_fid:
        raise ProspectiveRegistryError(f"SETTLEMENT_VERIFICATION_MISMATCH:fixture_id({src.get('fixture_id')}!={exp_fid})")
    if src.get("home_id") is None or int(src["home_id"]) != exp_hid or src.get("away_id") is None or int(src["away_id"]) != exp_aid:
        raise ProspectiveRegistryError(
            f"SETTLEMENT_VERIFICATION_MISMATCH:teams({src.get('home_id')}-{src.get('away_id')}!={exp_hid}-{exp_aid})"
        )
    if src.get("league_id") is not None and int(src["league_id"]) != exp_lid:
        raise ProspectiveRegistryError(f"SETTLEMENT_VERIFICATION_MISMATCH:league_id({src.get('league_id')}!={exp_lid})")

    src_st = str(src.get("status") or "").strip().upper()
    if src_st not in FINISHED_ELO_STATUSES or src_st != exp_status:
        raise ProspectiveRegistryError(f"SETTLEMENT_VERIFICATION_MISMATCH:status({src_st}!={exp_status})")

    if src.get("hg") is None or src.get("ag") is None:
        raise ProspectiveRegistryError("SETTLEMENT_VERIFICATION_MISMATCH:MISSING_GOALS")
    if int(src["hg"]) != int(hg) or int(src["ag"]) != int(ag):
        raise ProspectiveRegistryError(
            f"SETTLEMENT_VERIFICATION_MISMATCH:score({src.get('hg')}-{src.get('ag')}!={hg}-{ag})"
        )

    return {
        "fields_matched": True,
        "verified_fixture_id": exp_fid,
        "verified_teams": [exp_hid, exp_aid],
        "verified_league_id": exp_lid,
        "verified_status": src_st,
        "verified_score": [int(hg), int(ag)],
        "source_sha256": _canonical_sha256(authoritative_fixture),
    }


def default_settlement_path(record_path: Path, settlements_dir: Path | None = None) -> Path:
    sdir = settlements_dir or (record_path.parent / "settlements")
    return sdir / f"{record_path.stem}.settlement.json"


def settle_prospective_record(
    record_path: Path,
    *,
    hg: int,
    ag: int,
    match_status: str = "FT",
    settled_at_utc: datetime | None = None,
    authoritative_fixture: dict[str, Any] | None = None,
    source_path: Path | None = None,
    source_label: str | None = None,
    provider_provenance_verified: bool = False,
    simulation_mode: bool = False,
    settlements_dir: Path | None = None,
) -> dict[str, Any]:
    """Liquida resultados en archivo separado SIN modificar el registro original pre_kickoff."""
    raw_bytes = record_path.read_bytes()
    record_file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    doc = json.loads(raw_bytes.decode("utf-8"))
    pre = doc["pre_kickoff"]
    expected_sha = _canonical_sha256(pre)
    if expected_sha != doc.get("pre_kickoff_sha256"):
        raise ProspectiveRegistryError("PRE_KICKOFF_RECORD_TAMPERED")

    if (doc.get("settlement") or {}).get("status") == "SETTLED":
        raise ProspectiveRegistryError(f"RECORD_ALREADY_SETTLED:{record_path}")

    settlement_path = default_settlement_path(record_path, settlements_dir=settlements_dir)
    if settlement_path.exists():
        raise ProspectiveRegistryError(f"RECORD_ALREADY_SETTLED:{settlement_path}")

    st = str(match_status).strip().upper()
    if st not in FINISHED_ELO_STATUSES:
        raise ProspectiveRegistryError(f"NON_FINAL_SETTLEMENT_STATUS:{st}")

    ko_dt = parse_utc_timestamp(pre["timing"]["kickoff_utc"])
    now_dt = settled_at_utc or datetime.now(timezone.utc)
    min_avail_dt, _ = resolve_elo_available_dt({"ko_dt": ko_dt, "status": st})
    assert min_avail_dt is not None
    if now_dt < min_avail_dt:
        raise ProspectiveRegistryError(
            f"PREMATURE_SETTLEMENT:settled_at({now_dt.isoformat()})<min_finish({min_avail_dt.isoformat()})"
        )

    verification_detail: dict[str, Any] | None = None
    prov_ok = False
    prov_reason = "NONE"
    if simulation_mode:
        verification_status = "SIMULATED_TEST_ONLY"
        creditable = False
        prov_reason = "SIMULATION_MODE"
    elif authoritative_fixture is not None:
        verification_detail = verify_authoritative_result(
            pre,
            hg=int(hg),
            ag=int(ag),
            match_status=st,
            authoritative_fixture=authoritative_fixture,
        )
        prov_ok, prov_reason = _has_verified_provider_provenance(
            authoritative_fixture,
            source_path=source_path,
            provider_provenance_verified=provider_provenance_verified,
        )
        if prov_ok:
            verification_status = "VERIFIED_AUTHORITATIVE"
            creditable = True
        else:
            verification_status = "UNVERIFIED_SOURCE_PROVENANCE"
            creditable = False
    else:
        verification_status = "UNVERIFIED_RESULT"
        creditable = False
        prov_reason = "MANUAL_NO_SOURCE"

    y_1x2 = "HOME" if hg > ag else ("DRAW" if hg == ag else "AWAY")
    y_hw = 1 if hg > ag else 0
    y_dr = 1 if hg == ag else 0
    y_aw = 1 if hg < ag else 0
    y_o25 = 1 if (hg + ag) > 2.5 else 0
    y_btts = 1 if (hg > 0 and ag > 0) else 0

    scores: dict[str, Any] = {}
    eps = 1e-6
    for mname, pred in pre["predictions"].items():
        p1 = float(pred["outcomes_1x2"]["home_win"])
        px = float(pred["outcomes_1x2"]["draw"])
        p2 = float(pred["outcomes_1x2"]["away_win"])
        po25 = float(pred["p_over_2_5"])
        pbtts = float(pred["p_btts_yes"])
        rps = ((p1 - y_hw) ** 2 + ((p1 + px) - (y_hw + y_dr)) ** 2) / 2.0
        ll_1x2 = -math.log(max(eps, p1 if y_1x2 == "HOME" else (px if y_1x2 == "DRAW" else p2)))
        brier_1x2 = ((p1 - y_hw) ** 2 + (px - y_dr) ** 2 + (p2 - y_aw) ** 2) / 3.0
        scores[mname] = {
            "rps": round(rps, 6),
            "ll_1x2": round(ll_1x2, 6),
            "brier_1x2_per_out": round(brier_1x2, 6),
            "brier_o25": round((po25 - y_o25) ** 2, 6),
            "brier_btts": round((pbtts - y_btts) ** 2, 6),
            "err_home": round(float(pred["lambda_home"]) - hg, 4),
            "err_away": round(float(pred["lambda_away"]) - ag, 4),
        }

    settlement_obj = {
        "status": "SETTLED",
        "verification_status": verification_status,
        "provider_provenance_verified": prov_ok,
        "provider_provenance_reason": prov_reason,
        "creditable": creditable,
        "is_simulation": bool(simulation_mode),
        "source_label": source_label or (str(source_path) if source_path else ("SIMULATION" if simulation_mode else "MANUAL_UNVERIFIED")),
        "verification_detail": verification_detail,
        "match_status": st,
        "hg": int(hg),
        "ag": int(ag),
        "actual_1x2": y_1x2,
        "actual_o25": y_o25,
        "actual_btts": y_btts,
        "settled_at_utc": now_dt.isoformat(),
        "model_scores": scores,
    }
    settlement_doc = {
        "schema_version": SETTLEMENT_SCHEMA_VERSION,
        "record_file": record_path.name,
        "record_file_sha256": record_file_sha256,
        "pre_kickoff_sha256": doc["pre_kickoff_sha256"],
        "settlement": settlement_obj,
    }
    settlement_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with settlement_path.open("xb") as fh:
            fh.write(json.dumps(settlement_doc, indent=2, ensure_ascii=False).encode("utf-8"))
    except FileExistsError as exc:
        raise ProspectiveRegistryError(f"RECORD_ALREADY_SETTLED:{settlement_path}") from exc
    return {
        "pre_kickoff_sha256": doc["pre_kickoff_sha256"],
        "record_file_sha256": record_file_sha256,
        "settlement_path": str(settlement_path),
        "settlement": settlement_obj,
    }


def evaluate_prospective_records(
    records_dir: Path,
    *,
    settlements_dir: Path | None = None,
    require_full_four_model_comparison: bool = True,
) -> dict[str, Any]:
    """Evalúa registros acreditables (VERIFIED_AUTHORITATIVE), separando métricas deportivas y económicas."""
    settled_docs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending_n = 0
    excluded_n = 0
    degraded_comparison_rejected_n = 0
    unverified_result_n = 0
    simulated_test_n = 0
    tampered: list[str] = []

    for p in sorted(records_dir.glob("*.json")):
        raw_bytes = p.read_bytes()
        file_sha = hashlib.sha256(raw_bytes).hexdigest()
        doc = json.loads(raw_bytes.decode("utf-8"))
        pre = doc.get("pre_kickoff") or {}
        if _canonical_sha256(pre) != doc.get("pre_kickoff_sha256"):
            tampered.append(p.name)
            continue

        s_path = default_settlement_path(p, settlements_dir=settlements_dir)
        settlement_obj: dict[str, Any] | None = None
        if s_path.is_file():
            s_doc = json.loads(s_path.read_text(encoding="utf-8"))
            if s_doc.get("pre_kickoff_sha256") != doc.get("pre_kickoff_sha256") or s_doc.get("record_file_sha256") != file_sha:
                tampered.append(p.name)
                continue
            settlement_obj = s_doc.get("settlement") or {}
        elif (doc.get("settlement") or {}).get("status") == "SETTLED":
            settlement_obj = doc["settlement"]

        if not settlement_obj or settlement_obj.get("status") != "SETTLED":
            pending_n += 1
            continue

        v_status = str(settlement_obj.get("verification_status") or "UNVERIFIED_RESULT")
        if v_status == "SIMULATED_TEST_ONLY" or settlement_obj.get("is_simulation"):
            simulated_test_n += 1
            continue
        if v_status != "VERIFIED_AUTHORITATIVE" or not settlement_obj.get("creditable", False):
            unverified_result_n += 1
            continue

        if not pre.get("eligible_for_paired_eval", True):
            excluded_n += 1
            continue

        mci = pre.get("model_comparison_integrity") or {}
        if require_full_four_model_comparison and not mci.get("full_four_model_comparison_valid", False):
            degraded_comparison_rejected_n += 1
            continue

        if all(m in (pre.get("predictions") or {}) for m in FROZEN_MODEL_NAMES):
            settled_docs.append((doc, settlement_obj))

    club_docs = [(d, s) for d, s in settled_docs if d["pre_kickoff"]["competition"]["stratum"] != "NATIONAL_TEAM"]
    nat_docs = [(d, s) for d, s in settled_docs if d["pre_kickoff"]["competition"]["stratum"] == "NATIONAL_TEAM"]
    economic_docs = [
        (d, s) for d, s in settled_docs if (d["pre_kickoff"].get("bwin_odds") or {}).get("odds_status") == "ODDS_VERIFIED"
    ]

    def _agg(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        if not items:
            return {"n_fixtures": 0}
        out: dict[str, Any] = {"n_fixtures": len(items), "models": {}}
        for mname in FROZEN_MODEL_NAMES:
            sc_list = [s["model_scores"][mname] for _, s in items]
            o25_pairs = [(float(d["pre_kickoff"]["predictions"][mname]["p_over_2_5"]), int(s["actual_o25"])) for d, s in items]
            btts_pairs = [(float(d["pre_kickoff"]["predictions"][mname]["p_btts_yes"]), int(s["actual_btts"])) for d, s in items]
            fav_obs = []
            for d, s in items:
                pr = d["pre_kickoff"]["predictions"][mname]["outcomes_1x2"]
                fav_obs.append((float(pr["home_win"]), 1 if s["actual_1x2"] == "HOME" else 0))
                fav_obs.append((float(pr["away_win"]), 1 if s["actual_1x2"] == "AWAY" else 0))
            fav_ge = [(p, y) for p, y in fav_obs if p >= 0.55]
            out["models"][mname] = {
                "rps": round(statistics.fmean(sc["rps"] for sc in sc_list), 5),
                "log_loss_1x2": round(statistics.fmean(sc["ll_1x2"] for sc in sc_list), 5),
                "brier_1x2": round(statistics.fmean(sc["brier_1x2_per_out"] for sc in sc_list), 5),
                "brier_over_2_5": brier_score(o25_pairs),
                "log_loss_over_2_5": log_loss(o25_pairs),
                "brier_btts": brier_score(btts_pairs),
                "log_loss_btts": log_loss(btts_pairs),
                "bias_home_goals": round(statistics.fmean(sc["err_home"] for sc in sc_list), 4),
                "bias_away_goals": round(statistics.fmean(sc["err_away"] for sc in sc_list), 4),
                "mae_home": round(statistics.fmean(abs(sc["err_home"]) for sc in sc_list), 4),
                "mae_away": round(statistics.fmean(abs(sc["err_away"]) for sc in sc_list), 4),
                "favorite_ge_0_55": {
                    "n": len(fav_ge),
                    "bias": round(statistics.fmean(p for p, _ in fav_ge) - statistics.fmean(y for _, y in fav_ge), 4)
                    if fav_ge
                    else None,
                },
            }
        return out

    return {
        "settled_paired_n": len(settled_docs),
        "economic_eval_eligible_n": len(economic_docs),
        "pending_n": pending_n,
        "excluded_n": excluded_n,
        "degraded_comparison_rejected_n": degraded_comparison_rejected_n,
        "unverified_result_n": unverified_result_n,
        "simulated_test_n": simulated_test_n,
        "tampered_rejected": tampered,
        "evaluation_independent_of_shadow_decision": True,
        "sample_sufficiency": {
            "CLUB": {"n": len(club_docs), "target": PROSPECTIVE_MIN_SAMPLE["CLUB"], "sufficient": len(club_docs) >= PROSPECTIVE_MIN_SAMPLE["CLUB"]},
            "NATIONAL_TEAM": {"n": len(nat_docs), "target": PROSPECTIVE_MIN_SAMPLE["NATIONAL_TEAM"], "sufficient": len(nat_docs) >= PROSPECTIVE_MIN_SAMPLE["NATIONAL_TEAM"]},
        },
        "ALL": _agg(settled_docs),
        "CLUB": _agg(club_docs),
        "NATIONAL_TEAM": _agg(nat_docs),
    }
