"""Modelos experimentales aislados de Iteración 2.1B (NO sustituyen a CoherentMatchModeler en producción).

Implementa M0 (control L20), M1 (interacción multiplicativa), M2 (fuerza del rival SoS + Elo as-of
separado por estrato), M3 (recencia exponencial + factor campo) y M4 (priors y contracción shrink_k/rho).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    same_competition_season,
    same_meaningful_stratum,
)
from bet_engine.modeling.calibration import parse_utc_timestamp
from bet_engine.modeling.distributions import (
    _estimate_team_rate_detailed,
    _resolve_prior,
    dixon_coles_joint_grid,
)


@dataclass(frozen=True)
class ExperimentalConfig:
    name: str
    multiplicative: bool = False
    use_sos_opp_rates: bool = False
    sos_alpha: float = 0.35
    use_elo: bool = False
    elo_weight: float = 0.25
    elo_hfa: float = 55.0
    half_life_days: float | None = None
    venue_blend: float = 0.25
    shrink_k: float = 8.0
    rho: float = -0.04
    base_rates_by_stratum: dict[str, tuple[float, float]] | None = None
    elo_k_factor: float = 20.0
    unknown_red_weight: float = 1.0
    strict_competition_identity: bool = False


DEFAULT_BASE_RATES: dict[str, tuple[float, float]] = {
    "CLUB": (1.6165, 1.3391),
    "NATIONAL_TEAM": (1.6562, 1.2812),
}


def _stratum_key(stratum: str | None) -> str:
    return "NATIONAL_TEAM" if stratum == "NATIONAL_TEAM" else "CLUB"


FINISHED_ELO_STATUSES = frozenset({"FT", "AET", "PEN"})
MIN_FINISH_OFFSET_MINUTES: dict[str, int] = {
    "FT": 125,
    "AET": 165,
    "PEN": 180,
}
CONSERVATIVE_UNKNOWN_STATUS_OFFSET_MINUTES = 180


def resolve_elo_available_dt(fx: dict[str, Any]) -> tuple[datetime | None, str]:
    """Determina la disponibilidad as-of para actualizar Elo.

    Prioriza timestamps verificables de finalización/publicación cuando existen
    (`observed_available_at_utc`, `finished_at_utc`, `published_at_utc`).
    Cuando no existen, aplica offsets mínimos *estimados* por estado:
    - Excluye partidos no terminados, aplazados o interrumpidos (fuera de {FT, AET, PEN}).
    - FT (+125m), AET (+165m), PEN (+180m) son estimaciones conservadoras, NO garantías
      absolutas frente a retrasos o publicaciones tardías sin timestamp verificado.
    - Si faltan disponibilidad verificada y estado, aplica fallback estimado +180m.
    """
    raw_status = fx.get("status")
    status = str(raw_status).strip().upper() if raw_status is not None else None
    if status is not None and status not in FINISHED_ELO_STATUSES:
        return None, f"EXCLUDED_NON_FINISHED_{status or 'EMPTY'}"

    ko_dt = fx.get("ko_dt")
    if ko_dt is None and fx.get("kickoff_utc"):
        ko_dt = parse_utc_timestamp(str(fx["kickoff_utc"]))

    min_offset = (
        MIN_FINISH_OFFSET_MINUTES[status]
        if status in MIN_FINISH_OFFSET_MINUTES
        else CONSERVATIVE_UNKNOWN_STATUS_OFFSET_MINUTES
    )
    min_floor_dt = (ko_dt + timedelta(minutes=min_offset)) if ko_dt is not None else None

    verified_raw = (
        fx.get("observed_available_at_utc")
        or fx.get("finished_at_utc")
        or fx.get("published_at_utc")
    )
    verified_dt = (
        verified_raw
        if isinstance(verified_raw, datetime)
        else (parse_utc_timestamp(str(verified_raw)) if verified_raw else None)
    )
    if verified_dt is not None:
        if ko_dt is not None and verified_dt < ko_dt:
            # Timestamp verificado inconsistente (anterior al kickoff): caer a estimación conservadora.
            return min_floor_dt, f"ESTIMATED_CONSERVATIVE_{status or 'UNKNOWN'}_{min_offset}M"
        resolved = max(verified_dt, min_floor_dt) if min_floor_dt is not None else verified_dt
        return resolved, "VERIFIED_TIMESTAMP"

    est_raw = fx.get("avail_dt")
    est_dt = (
        est_raw
        if isinstance(est_raw, datetime)
        else (parse_utc_timestamp(str(est_raw)) if est_raw else None)
    )
    if est_dt is not None and min_floor_dt is not None:
        if status is None and ko_dt is not None and est_dt <= ko_dt + timedelta(minutes=125):
            # Compatibilidad con fixtures legados que ya traen avail_dt=ko+125m sin campo status.
            return est_dt, "ESTIMATED_LEGACY_125M"
        return max(est_dt, min_floor_dt), f"ESTIMATED_CONSERVATIVE_{status or 'UNKNOWN'}_{min_offset}M"
    if est_dt is not None:
        return est_dt, "ESTIMATED_LEGACY_AVAIL_DT"
    if min_floor_dt is not None:
        return min_floor_dt, f"ESTIMATED_CONSERVATIVE_{status or 'UNKNOWN'}_{min_offset}M"
    return None, "EXCLUDED_MISSING_TIMESTAMPS"


class AsOfEloTracker:
    """Rating Elo secuencial estrictamente as-of y aislado por componente de estrato (CLUB vs NATIONAL_TEAM)."""

    def __init__(self, *, k_factor: float = 20.0, hfa: float = 55.0) -> None:
        self.k_factor = k_factor
        self.hfa = hfa
        # (stratum_pool, team_id) -> list of (available_dt, rating_after, n_matches_after)
        self.history: dict[tuple[str, int], list[tuple[datetime, float, int]]] = {}
        self.provenance_counts: dict[str, int] = {}
        self.excluded_non_finished: int = 0
        self.deduplicated_skipped: int = 0

    @classmethod
    def build_from_fixtures(
        cls,
        ordered_fixtures: list[dict[str, Any]],
        *,
        k_factor: float = 20.0,
        hfa: float = 55.0,
    ) -> AsOfEloTracker:
        tracker = cls(k_factor=k_factor, hfa=hfa)
        current: dict[tuple[str, int], tuple[float, int]] = {}
        seen_fids: set[int] = set()
        resolved_rows: list[tuple[datetime, datetime, int, dict[str, Any], str]] = []

        for fx in ordered_fixtures:
            fid = int(fx.get("fixture_id") or 0)
            if fid and fid in seen_fids:
                tracker.deduplicated_skipped += 1
                continue
            avail_dt, prov = resolve_elo_available_dt(fx)
            if avail_dt is None:
                tracker.excluded_non_finished += 1
                tracker.provenance_counts[prov] = tracker.provenance_counts.get(prov, 0) + 1
                continue
            if fid:
                seen_fids.add(fid)
            ko_dt = fx.get("ko_dt") or (
                parse_utc_timestamp(str(fx["kickoff_utc"])) if fx.get("kickoff_utc") else avail_dt
            )
            resolved_rows.append((avail_dt, ko_dt, fid, fx, prov))

        resolved_rows.sort(key=lambda item: (item[0], item[1], item[2]))

        for avail_dt, _, _, fx, prov in resolved_rows:
            tracker.provenance_counts[prov] = tracker.provenance_counts.get(prov, 0) + 1
            s_pool = _stratum_key(fx.get("stratum"))
            hid, aid = int(fx["home_id"]), int(fx["away_id"])
            hg, ag = float(fx["hg"]), float(fx["ag"])
            rh, nh = current.get((s_pool, hid), (1500.0, 0))
            ra, na = current.get((s_pool, aid), (1500.0, 0))

            exp_h = 1.0 / (1.0 + 10.0 ** (-((rh + hfa) - ra) / 400.0))
            act_h = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
            gd = abs(hg - ag)
            mov = math.log1p(gd) if gd > 0 else 1.0
            delta = k_factor * mov * (act_h - exp_h)

            nh_new, na_new = nh + 1, na + 1
            rh_new, ra_new = rh + delta, ra - delta
            current[(s_pool, hid)] = (rh_new, nh_new)
            current[(s_pool, aid)] = (ra_new, na_new)
            tracker.history.setdefault((s_pool, hid), []).append((avail_dt, rh_new, nh_new))
            tracker.history.setdefault((s_pool, aid), []).append((avail_dt, ra_new, na_new))
        return tracker

    def get_rating_asof(self, stratum: str | None, team_id: int, cutoff_dt: datetime) -> tuple[float, int]:
        s_pool = _stratum_key(stratum)
        entries = self.history.get((s_pool, int(team_id)))
        if not entries:
            return 1500.0, 0
        # Búsqueda binaria sobre available_dt <= cutoff_dt
        lo, hi = 0, len(entries) - 1
        idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if entries[mid][0] <= cutoff_dt:
                idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if idx < 0:
            return 1500.0, 0
        _, r_val, n_val = entries[idx]
        return r_val, n_val


def _comp_weight(
    m: HistoricalTeamMatch,
    *,
    target_league_id: int,
    target_season: int,
    target_stratum: str | None,
    strict_competition_identity: bool = False,
) -> float:
    if strict_competition_identity:
        if same_competition_season(m, target_league_id, target_season):
            return 1.0
        if same_meaningful_stratum(m, target_stratum):
            return 0.50
        return 0.25
    if m.league_id == target_league_id and m.season == target_season:
        return 1.0
    if target_stratum and m.competition_stratum == target_stratum:
        return 0.50
    return 0.25


def _estimate_experimental_side_rate(
    own_matches: tuple[HistoricalTeamMatch, ...],
    opp_matches: tuple[HistoricalTeamMatch, ...],
    *,
    target_ko_dt: datetime,
    target_league_id: int,
    target_season: int,
    target_stratum: str | None,
    own_venue: str,
    opp_venue: str,
    own_prior: float,
    opp_prior: float,
    stratum_avg: float,
    cfg: ExperimentalConfig,
    team_ordered_by_id: dict[int, list[HistoricalTeamMatch]] | None = None,
    history_limit: int = 20,
) -> dict[str, Any]:
    """Calcula tasa ofensiva propia contraída y tasa defensiva rival contraída con SoS y recencia."""

    def _clean(ms: tuple[HistoricalTeamMatch, ...]) -> list[HistoricalTeamMatch]:
        out = [
            m
            for m in ms
            if not m.red_card_distortion and m.own_metrics.get("goals") is not None and m.conceded_metrics.get("goals") is not None
        ]
        return out[:history_limit]

    own_clean = _clean(own_matches)
    opp_clean = _clean(opp_matches)

    def _side_shrunk(
        ms: list[HistoricalTeamMatch],
        *,
        is_own_attack: bool,
        venue_target: str,
        prior_val: float,
        shared_ids: set[int],
    ) -> tuple[float, float, int, int]:
        if not ms:
            return prior_val, 0.0, 0, 0
        same_comp = (
            [m for m in ms if same_competition_season(m, target_league_id, target_season)]
            if cfg.strict_competition_identity
            else [m for m in ms if m.league_id == target_league_id and m.season == target_season]
        )
        use_all = len(same_comp) < 4
        sel = ms if use_all else same_comp

        vals: list[float] = []
        ws: list[float] = []
        sos_covered = 0
        sos_checked = 0

        for m in sel:
            raw_v = float(m.own_metrics["goals"] if is_own_attack else m.conceded_metrics["goals"])
            w = 1.0 if not use_all else _comp_weight(
                m,
                target_league_id=target_league_id,
                target_season=target_season,
                target_stratum=target_stratum,
                strict_competition_identity=cfg.strict_competition_identity,
            )
            if (not is_own_attack) and m.fixture_id in shared_ids:
                w *= 0.50
            if cfg.unknown_red_weight < 1.0 and (m.red_card_detail or {}).get("red_card_status") == "RED_CARD_UNKNOWN":
                w *= cfg.unknown_red_weight
            if cfg.half_life_days is not None and cfg.half_life_days > 0:
                age_days = max(0.0, (target_ko_dt - parse_utc_timestamp(m.kickoff_utc)).total_seconds() / 86400.0)
                w *= 0.5 ** (age_days / cfg.half_life_days)

            adj_v = raw_v
            if cfg.use_sos_opp_rates and team_ordered_by_id is not None and m.opponent_id:
                sos_checked += 1
                m_ko = parse_utc_timestamp(m.kickoff_utc)
                opp_hist = [
                    x
                    for x in team_ordered_by_id.get(m.opponent_id, ())
                    if parse_utc_timestamp(x.available_at_utc) <= m_ko
                    and parse_utc_timestamp(x.kickoff_utc) < m_ko
                    and not x.red_card_distortion
                    and x.own_metrics.get("goals") is not None
                    and x.conceded_metrics.get("goals") is not None
                ][-20:]
                if len(opp_hist) >= 3:
                    sos_covered += 1
                    # Si medimos ataque propio, ajustamos contra calidad defensiva del rival (conceded)
                    opp_metric_vals = [
                        float(x.conceded_metrics["goals"] if is_own_attack else x.own_metrics["goals"])
                        for x in opp_hist
                    ]
                    opp_shrunk_ref = (len(opp_metric_vals) * statistics.fmean(opp_metric_vals) + 6.0 * stratum_avg) / (
                        len(opp_metric_vals) + 6.0
                    )
                    ratio = max(0.55, min(1.80, stratum_avg / max(0.30, opp_shrunk_ref)))
                    adj_v = raw_v * (ratio ** cfg.sos_alpha)

            vals.append(adj_v)
            ws.append(w)

        w_sum = sum(ws)
        if w_sum <= 0:
            return prior_val, 0.0, sos_covered, sos_checked
        w_mean = sum(v * w for v, w in zip(vals, ws)) / w_sum
        if len(vals) >= 7:
            l6_ws = ws[:6]
            l6_vals = vals[:6]
            l6_sum = sum(l6_ws)
            l6_mean = (sum(v * w for v, w in zip(l6_vals, l6_ws)) / l6_sum) if l6_sum > 0 else w_mean
            est = 0.35 * l6_mean + 0.65 * w_mean
        else:
            est = w_mean

        # Tamaño muestral efectivo Kish
        w_sq = sum(w * w for w in ws)
        n_eff = (w_sum * w_sum / w_sq) if w_sq > 0 else 0.0
        shrunk = (n_eff * est + cfg.shrink_k * prior_val) / (n_eff + cfg.shrink_k)

        if cfg.venue_blend > 0:
            v_vals = [v for m, v in zip(sel, vals) if m.venue == venue_target]
            if len(v_vals) >= 3:
                shrunk = (1.0 - cfg.venue_blend) * shrunk + cfg.venue_blend * statistics.fmean(v_vals)

        return max(0.05, shrunk), n_eff, sos_covered, sos_checked

    shared = {m.fixture_id for m in own_clean} & {m.fixture_id for m in opp_clean}
    att_s, att_neff, att_cov, att_chk = _side_shrunk(
        own_clean, is_own_attack=True, venue_target=own_venue, prior_val=own_prior, shared_ids=set()
    )
    def_s, def_neff, def_cov, def_chk = _side_shrunk(
        opp_clean, is_own_attack=False, venue_target=opp_venue, prior_val=opp_prior, shared_ids=shared
    )
    return {
        "own_shrunk": att_s,
        "opp_shrunk": def_s,
        "own_n_eff": round(att_neff, 4),
        "opp_n_eff": round(def_neff, 4),
        "sos_covered": att_cov + def_cov,
        "sos_checked": att_chk + def_chk,
    }


def predict_experimental_match(
    home_matches: tuple[HistoricalTeamMatch, ...],
    away_matches: tuple[HistoricalTeamMatch, ...],
    *,
    home_id: int,
    away_id: int,
    target_ko_dt: datetime,
    target_league_id: int,
    target_season: int,
    target_stratum: str | None,
    cfg: ExperimentalConfig,
    team_ordered_by_id: dict[int, list[HistoricalTeamMatch]] | None = None,
    elo_tracker: AsOfEloTracker | None = None,
    history_limit: int = 20,
) -> dict[str, Any]:
    """Evalúa un partido con la variante experimental M0..M4 sin tocar producción."""
    s_key = _stratum_key(target_stratum)
    base_map = cfg.base_rates_by_stratum or DEFAULT_BASE_RATES
    mu_home, mu_away = base_map.get(s_key, DEFAULT_BASE_RATES["CLUB"])
    stratum_avg = 0.5 * (mu_home + mu_away)

    if not cfg.multiplicative and not cfg.use_sos_opp_rates and not cfg.use_elo and cfg.half_life_days is None and cfg.venue_blend == 0.25 and cfg.shrink_k == 8.0 and cfg.rho == -0.04:
        # M0 exacto (o variante aislada strict_competition_identity): delega a producción
        eh = _estimate_team_rate_detailed(
            home_matches, away_matches, metric="goals",
            target_league_id=target_league_id, target_season=target_season,
            target_stratum=target_stratum, own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", target_stratum, "HOME"),
            history_limit=history_limit,
            strict_competition_identity=cfg.strict_competition_identity,
        )
        ea = _estimate_team_rate_detailed(
            away_matches, home_matches, metric="goals",
            target_league_id=target_league_id, target_season=target_season,
            target_stratum=target_stratum, own_venue="AWAY", opp_venue="HOME",
            prior_mean=_resolve_prior("goals", target_stratum, "AWAY"),
            history_limit=history_limit,
            strict_competition_identity=cfg.strict_competition_identity,
        )
        lh, la = float(eh["lambda"]), float(ea["lambda"])
        comp = {
            "home_own_shrunk": eh.get("own_rate"),
            "away_def_shrunk": eh.get("opp_rate"),
            "away_own_shrunk": ea.get("own_rate"),
            "home_def_shrunk": ea.get("opp_rate"),
            "home_n_eff": eh["n_eff"],
            "away_n_eff": ea["n_eff"],
            "sos_coverage_rate": 0.0,
            "elo_home": None,
            "elo_away": None,
            "elo_coverage_ok": False,
        }
    else:
        h_prior = _resolve_prior("goals", target_stratum, "HOME")
        a_prior = _resolve_prior("goals", target_stratum, "AWAY")
        # En formulación multiplicativa, las tasas propias/concedidas del equipo se contraen hacia la media de liga/estrato
        # para evitar doble conteo del factor local en alpha y beta.
        own_p_h = stratum_avg if cfg.multiplicative else h_prior
        opp_p_a = stratum_avg if cfg.multiplicative else h_prior
        own_p_a = stratum_avg if cfg.multiplicative else a_prior
        opp_p_h = stratum_avg if cfg.multiplicative else a_prior

        res_h = _estimate_experimental_side_rate(
            home_matches, away_matches,
            target_ko_dt=target_ko_dt, target_league_id=target_league_id,
            target_season=target_season, target_stratum=target_stratum,
            own_venue="HOME", opp_venue="AWAY",
            own_prior=own_p_h, opp_prior=opp_p_a, stratum_avg=stratum_avg,
            cfg=cfg, team_ordered_by_id=team_ordered_by_id, history_limit=history_limit,
        )
        res_a = _estimate_experimental_side_rate(
            away_matches, home_matches,
            target_ko_dt=target_ko_dt, target_league_id=target_league_id,
            target_season=target_season, target_stratum=target_stratum,
            own_venue="AWAY", opp_venue="HOME",
            own_prior=own_p_a, opp_prior=opp_p_h, stratum_avg=stratum_avg,
            cfg=cfg, team_ordered_by_id=team_ordered_by_id, history_limit=history_limit,
        )
        if cfg.multiplicative:
            lh = mu_home * (res_h["own_shrunk"] / stratum_avg) * (res_h["opp_shrunk"] / stratum_avg)
            la = mu_away * (res_a["own_shrunk"] / stratum_avg) * (res_a["opp_shrunk"] / stratum_avg)
        else:
            lh = 0.55 * res_h["own_shrunk"] + 0.45 * res_h["opp_shrunk"]
            la = 0.55 * res_a["own_shrunk"] + 0.45 * res_a["opp_shrunk"]

        elo_h, elo_hn = (1500.0, 0)
        elo_a, elo_an = (1500.0, 0)
        elo_ok = False
        if cfg.use_elo and elo_tracker is not None:
            elo_h, elo_hn = elo_tracker.get_rating_asof(target_stratum, home_id, target_ko_dt)
            elo_a, elo_an = elo_tracker.get_rating_asof(target_stratum, away_id, target_ko_dt)
            if elo_hn >= 3 and elo_an >= 3:
                elo_ok = True
                # Ajuste simétrico en log-tasa acotado por cobertura mínima
                conf = min(1.0, min(elo_hn, elo_an) / 10.0)
                diff_scaled = max(-1.5, min(1.5, (elo_h - elo_a) / 400.0))
                mult = math.exp(cfg.elo_weight * conf * diff_scaled)
                lh *= mult
                la /= mult

        lh = max(0.10, min(6.50, lh))
        la = max(0.10, min(6.50, la))
        tot_chk = res_h["sos_checked"] + res_a["sos_checked"]
        tot_cov = res_h["sos_covered"] + res_a["sos_covered"]
        comp = {
            "home_own_shrunk": round(res_h["own_shrunk"], 4),
            "away_def_shrunk": round(res_h["opp_shrunk"], 4),
            "away_own_shrunk": round(res_a["own_shrunk"], 4),
            "home_def_shrunk": round(res_a["opp_shrunk"], 4),
            "home_n_eff": round(0.5 * (res_h["own_n_eff"] + res_h["opp_n_eff"]), 4),
            "away_n_eff": round(0.5 * (res_a["own_n_eff"] + res_a["opp_n_eff"]), 4),
            "sos_coverage_rate": round(tot_cov / tot_chk, 4) if tot_chk > 0 else 0.0,
            "elo_home": round(elo_h, 2) if cfg.use_elo else None,
            "elo_away": round(elo_a, 2) if cfg.use_elo else None,
            "elo_home_n": elo_hn if cfg.use_elo else 0,
            "elo_away_n": elo_an if cfg.use_elo else 0,
            "elo_coverage_ok": elo_ok,
        }

    grid = dixon_coles_joint_grid(lh, la, rho=cfg.rho, max_goals=12)
    p1 = sum(grid[i][j] for i in range(13) for j in range(13) if i > j)
    px = sum(grid[i][i] for i in range(13))
    p2 = sum(grid[i][j] for i in range(13) for j in range(13) if i < j)
    po25 = sum(grid[i][j] for i in range(13) for j in range(13) if i + j > 2)
    pbtts = sum(grid[i][j] for i in range(1, 13) for j in range(1, 13))
    return {
        "lambda_home": round(lh, 4),
        "lambda_away": round(la, 4),
        "p_home": p1,
        "p_draw": px,
        "p_away": p2,
        "p_over_2_5": po25,
        "p_btts_yes": pbtts,
        "components": comp,
    }
