"""Evaluador paralelo y registro prospectivo inmutable de CoherentMatchModeler_cand_2b3b_v1 (Fase 2B.4).

No sustituye a CoherentMatchModeler_baseline_2.0_2.1A ni autoriza apuestas BET.
Reutiliza la infraestructura de prospective_registry.py, ObservationStore y calibration.py
preservando intactos los archivos congelados por MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    ObservationStore,
    same_competition_season,
    same_meaningful_stratum,
)
from bet_engine.modeling.calibration import (
    brier_score,
    expected_calibration_error,
    log_loss,
    paired_bootstrap_delta_ci,
    parse_utc_timestamp,
    summarize_probability_calibration,
)
from bet_engine.modeling.distributions import (
    DEFAULT_PRIORS,
    FAMILY_DISPERSION_FLOORS,
    MODEL_VERSION as BASELINE_MODEL_VERSION,
    CoherentMatchModeler,
    _gamma_rate_bounds,
    _red_card_weight,
    _resolve_prior,
    dixon_coles_joint_grid,
)
from bet_engine.modeling.experimental_v2_1b import FINISHED_ELO_STATUSES, resolve_elo_available_dt
from bet_engine.modeling.prospective_registry import (
    CORPUS_CUTOFF_UTC,
    MADRID_TZ,
    PROSPECTIVE_MIN_SAMPLE,
    ProspectiveRegistryError,
    _canonical_sha256,
    _classify_lineup_state,
    _classify_odds_status,
    _has_verified_provider_provenance,
    _quantile_range_from_grid,
    _verify_target_fixture_identity,
    default_settlement_path,
    verify_authoritative_result,
)

BASELINE_2B3B_KEY = "B0_baseline"
CANDIDATE_2B3B_KEY = "CAND_2B3B_V1"
CANDIDATE_2B3B_VERSION = "CoherentMatchModeler_cand_2b3b_v1"
MODEL_PAIR_KEYS = (BASELINE_2B3B_KEY, CANDIDATE_2B3B_KEY)
REGISTRY_2B3B_SCHEMA_VERSION = "2B.4-prospective-record-v1"
SETTLEMENT_2B3B_SCHEMA_VERSION = "2B.4-prospective-settlement-v1"
CAND_2B3B_SPEC_PARAMS: dict[str, Any] = {
    "version": CANDIDATE_2B3B_VERSION,
    "spec_id": "INT_B3_A6a_kish_k8_C4",
    "metric": "goals",
    "history_limit": 20,
    "shrink_k": 8.0,
    "use_kish_neff": True,
    "venue_mode": "shrunk_anchor_50",
    "venue_blend": 0.50,
    "venue_k": 4.0,
    "recency_l6_weight": 0.35,
    "recency_l20_weight": 0.65,
    "own_attack_weight": 0.55,
    "opp_defense_weight": 0.45,
    "shared_h2h_discount": 0.50,
    "red_card_policy": "exclude_any",
    "strict_competition_identity": True,
    "rho": -0.04,
    "max_goals": 12,
    "experimental_only": True,
    "bet_authorization_allowed": False,
}


def resolve_target_stratum(bundle: MatchObservationBundle) -> str | None:
    """Resuelve estrato objetivo de manera idéntica a CoherentMatchModeler.build_model."""
    return next(
        (
            m.competition_stratum
            for m in (bundle.home_matches + bundle.away_matches)
            if m.league_id == bundle.league_id
            and m.competition_stratum
            and m.competition_stratum != "UNKNOWN"
        ),
        None,
    )


def estimate_cand_2b3b_side_rate(
    own_matches: tuple[HistoricalTeamMatch, ...],
    opp_matches: tuple[HistoricalTeamMatch, ...],
    *,
    target_league_id: int,
    target_season: int,
    target_stratum: str | None,
    own_venue: str,
    opp_venue: str,
    prior_mean: float,
    other_venue_prior_mean: float,
    history_limit: int = 20,
) -> dict[str, Any]:
    """Estimador congelado CoherentMatchModeler_cand_2b3b_v1 (B3 + Kish k=8 + C4)."""
    if history_limit <= 0:
        raise ValueError(f"INVALID_HISTORY_LIMIT:{history_limit}")

    def filter_valid(ms: tuple[HistoricalTeamMatch, ...], field_map: str) -> list[HistoricalTeamMatch]:
        ordered = sorted(ms, key=lambda m: m.kickoff_utc, reverse=True)
        valid = [
            m
            for m in ordered
            if _red_card_weight(m, policy="exclude_any") > 0.0
            and (m.own_metrics if field_map == "own" else m.conceded_metrics).get("goals") is not None
        ]
        return valid[:history_limit]

    own_sel = filter_valid(own_matches, "own")
    opp_sel = filter_valid(opp_matches, "conceded")
    if not own_sel and not opp_sel:
        return {
            "lambda": float(prior_mean),
            "n_eff": 0.0,
            "n_own": 0,
            "n_opp": 0,
            "own_venue_n": 0,
            "opp_venue_n": 0,
            "warnings": ["NO_OBSERVATIONS_USING_PRIOR"],
        }

    shared_h2h_ids = {m.fixture_id for m in own_sel} & {m.fixture_id for m in opp_sel}
    warnings: list[str] = []
    if any(not same_competition_season(m, target_league_id, target_season) for m in own_sel + opp_sel):
        warnings.append("COMPETITION_MIX_FALLBACK")
    if shared_h2h_ids:
        warnings.append(f"SHARED_H2H_DEDUPLICATED_{len(shared_h2h_ids)}")

    def comp_weight(m: HistoricalTeamMatch) -> float:
        if same_competition_season(m, target_league_id, target_season):
            return 1.0
        if same_meaningful_stratum(m, target_stratum):
            return 0.50
        return 0.25

    def kish_shrunk(ms: list[HistoricalTeamMatch], getter: Any, *, apply_h2h_discount: bool) -> tuple[float, float]:
        if not ms:
            return float(prior_mean), 0.0
        pairs: list[tuple[float, float]] = []
        for m in ms:
            v = getter(m)
            if v is None:
                continue
            w = comp_weight(m)
            if apply_h2h_discount and m.fixture_id in shared_h2h_ids:
                w *= 0.50
            pairs.append((float(v), w))
        if not pairs:
            return float(prior_mean), 0.0
        w_sum = sum(w for _, w in pairs)
        l20_mean = sum(v * w for v, w in pairs) / w_sum
        p6 = pairs[:6]
        w6_sum = sum(w for _, w in p6)
        l6_mean = sum(v * w for v, w in p6) / w6_sum if w6_sum > 0 else l20_mean
        sample_est = (0.35 * l6_mean + 0.65 * l20_mean) if len(pairs) >= 7 else l20_mean
        w_sq = sum(w * w for _, w in pairs)
        n_eff = (w_sum * w_sum / w_sq) if w_sq > 0 else 0.0
        shrunk = (n_eff * sample_est + 8.0 * float(prior_mean)) / (n_eff + 8.0)
        return shrunk, n_eff

    own_shrunk, own_neff = kish_shrunk(own_sel, lambda m: m.own_metrics.get("goals"), apply_h2h_discount=False)
    opp_shrunk, opp_neff = kish_shrunk(opp_sel, lambda m: m.conceded_metrics.get("goals"), apply_h2h_discount=True)

    own_vpairs = [(float(m.own_metrics["goals"]), comp_weight(m)) for m in own_sel if m.venue == own_venue]
    opp_vpairs = [(float(m.conceded_metrics["goals"]), comp_weight(m)) for m in opp_sel if m.venue == opp_venue]

    prior_mid = max(0.25, 0.5 * (float(prior_mean) + float(other_venue_prior_mean)))
    venue_mult = float(prior_mean) / prior_mid

    def apply_venue(base_shrunk: float, vpairs: list[tuple[float, float]]) -> float:
        anchor = max(0.15, base_shrunk * venue_mult)
        if not vpairs:
            return 0.50 * base_shrunk + 0.50 * anchor
        vw = sum(w for _, w in vpairs)
        vm = sum(v * w for v, w in vpairs) / vw
        v_shrunk = (vw * vm + 4.0 * anchor) / (vw + 4.0)
        return 0.50 * base_shrunk + 0.50 * v_shrunk

    own_final = apply_venue(own_shrunk, own_vpairs)
    opp_final = apply_venue(opp_shrunk, opp_vpairs)
    lam = max(0.05, 0.55 * own_final + 0.45 * opp_final)
    n_eff = round(0.55 * own_neff + 0.45 * opp_neff, 4)
    return {
        "lambda": lam,
        "n_eff": n_eff,
        "n_own": len(own_sel),
        "n_opp": len(opp_sel),
        "own_venue_n": len(own_vpairs),
        "opp_venue_n": len(opp_vpairs),
        "warnings": list(dict.fromkeys(warnings)),
    }


def _grid_probs_from_lambdas(lh: float, la: float) -> dict[str, Any]:
    grid = dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=12)
    mg = len(grid) - 1
    p_home = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i > j)
    p_draw = sum(grid[i][i] for i in range(mg + 1))
    p_away = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i < j)
    p_o25 = sum(grid[i][j] for i in range(mg + 1) for j in range(mg + 1) if i + j > 2)
    p_btts = sum(grid[i][j] for i in range(1, mg + 1) for j in range(1, mg + 1))
    return {
        "grid": grid,
        "home_win": p_home,
        "draw": p_draw,
        "away_win": p_away,
        "over_2_5": p_o25,
        "btts_yes": p_btts,
        "total_goals_q10_q90": _quantile_range_from_grid(grid, 0.10, 0.90),
    }


def _extract_bundle_data_quality_and_coaches(bundle: MatchObservationBundle) -> dict[str, Any]:
    """Extrae profundidad de sede, cobertura L20, calidad analítica y régimen de entrenadores."""
    hp_valid = [m for m in sorted(bundle.home_matches, key=lambda x: x.kickoff_utc, reverse=True) if not m.red_card_distortion and m.own_metrics.get("goals") is not None][:20]
    ap_valid = [m for m in sorted(bundle.away_matches, key=lambda x: x.kickoff_utc, reverse=True) if not m.red_card_distortion and m.own_metrics.get("goals") is not None][:20]

    own_home_n = sum(1 for m in hp_valid if m.venue == "HOME")
    opp_away_conceded_n = sum(1 for m in ap_valid if m.venue == "AWAY")
    own_away_n = sum(1 for m in ap_valid if m.venue == "AWAY")
    opp_home_conceded_n = sum(1 for m in hp_valid if m.venue == "HOME")
    min_venue_n = min(own_home_n, opp_away_conceded_n, own_away_n, opp_home_conceded_n)

    l20_h = (bundle.windows.get("home") or {}).get("L20_all_competitions")
    l20_a = (bundle.windows.get("away") or {}).get("L20_all_competitions")
    if l20_h is not None and l20_a is not None:
        h_avail, a_avail = l20_h.total_matches_available, l20_a.total_matches_available
        h_det, a_det = l20_h.detailed_matches_valid, l20_a.detailed_matches_valid
        exact_pct = (l20_h.competition_mix.get("exact_competition_pct", 0.0) + l20_a.competition_mix.get("exact_competition_pct", 0.0)) / 2.0
    else:
        h_avail, a_avail = len(bundle.home_matches), len(bundle.away_matches)
        h_det = sum(1 for m in bundle.home_matches[:20] if m.has_detailed_stats)
        a_det = sum(1 for m in bundle.away_matches[:20] if m.has_detailed_stats)
        same_h = sum(1 for m in bundle.home_matches[:20] if same_competition_season(m, bundle.league_id, bundle.season))
        same_a = sum(1 for m in bundle.away_matches[:20] if same_competition_season(m, bundle.league_id, bundle.season))
        pct_h = (100.0 * same_h / len(bundle.home_matches[:20])) if bundle.home_matches else 0.0
        pct_a = (100.0 * same_a / len(bundle.away_matches[:20])) if bundle.away_matches else 0.0
        exact_pct = 0.5 * (pct_h + pct_a)

    score = round(min(1.0, (h_avail + a_avail) / 32.0) * 40.0 + min(1.0, (h_det + a_det) / 10.0) * 40.0 + (exact_pct / 100.0) * 20.0, 2)
    tier = "A_FULL" if score >= 80.0 else ("B_PARTIAL" if score >= 58.0 else ("C_LOW" if score >= 35.0 else "D_MINIMAL"))

    coaches: dict[str, Any] = {}
    for ev in bundle.contextual_evidence:
        if ev.category == "COACH_REGIME":
            side = str(ev.facts.get("side") or "")
            if side in ("home", "away"):
                coaches[side] = {
                    "coach_status": ev.facts.get("coach_status"),
                    "verification_status": ev.verification_status.value,
                    "current_coach_id": ev.facts.get("current_coach_id"),
                    "current_coach_name": ev.facts.get("current_coach_name"),
                    "coach_provenance": ev.facts.get("coach_provenance"),
                    "source_tier": ev.facts.get("source_tier"),
                    "primary_official_verified": ev.facts.get("primary_official_verified"),
                    "continuity_verified": ev.facts.get("continuity_verified"),
                }

    red_unk_h = sum(1 for m in bundle.home_matches if (m.red_card_detail or {}).get("red_card_status") == "RED_CARD_UNKNOWN")
    red_unk_a = sum(1 for m in bundle.away_matches if (m.red_card_detail or {}).get("red_card_status") == "RED_CARD_UNKNOWN")
    return {
        "history_coverage": {
            "home_raw_n": len(bundle.home_matches),
            "away_raw_n": len(bundle.away_matches),
            "home_valid_l20_n": len(hp_valid),
            "away_valid_l20_n": len(ap_valid),
            "min_depth": min(len(hp_valid), len(ap_valid)),
            "own_home_n": own_home_n,
            "opp_away_conceded_n": opp_away_conceded_n,
            "own_away_n": own_away_n,
            "opp_home_conceded_n": opp_home_conceded_n,
            "min_venue_n": min_venue_n,
            "insufficient_venue_any_lt3": min_venue_n < 3,
            "insufficient_own_home_lt3": own_home_n < 3,
            "red_card_unknown_home_n": red_unk_h,
            "red_card_unknown_away_n": red_unk_a,
        },
        "data_quality": {
            "analytical_quality_score": score,
            "coverage_tier": tier,
            "home_detailed_n": h_det,
            "away_detailed_n": a_det,
            "exact_competition_pct": round(exact_pct, 2),
        },
        "coach_regime": coaches,
    }


def evaluate_parallel_bundle_2b3b(
    bundle: MatchObservationBundle,
    *,
    history_limit: int = 20,
    simulate_failure: bool = False,
) -> dict[str, Any]:
    """Evalúa B0_baseline y CoherentMatchModeler_cand_2b3b_v1 en paralelo sobre el mismo bundle."""
    if simulate_failure:
        raise RuntimeError("SIMULATED_EXPERIMENTAL_2B3B_EVALUATOR_FAILURE")

    target_stratum = resolve_target_stratum(bundle)
    h_prior = _resolve_prior("goals", target_stratum, "HOME")
    a_prior = _resolve_prior("goals", target_stratum, "AWAY")

    # Baseline de producción: ejecuta directamente CoherentMatchModeler.build_model(bundle)
    b0_model = CoherentMatchModeler.build_model(bundle, history_limit=history_limit)
    b0_gf = b0_model.families["GOALS_FT"]
    b0_o25 = float(b0_gf.prob_over("total", 2.5)["p_over"])

    cand_h = estimate_cand_2b3b_side_rate(
        bundle.home_matches,
        bundle.away_matches,
        target_league_id=bundle.league_id,
        target_season=bundle.season,
        target_stratum=target_stratum,
        own_venue="HOME",
        opp_venue="AWAY",
        prior_mean=h_prior,
        other_venue_prior_mean=a_prior,
        history_limit=history_limit,
    )
    cand_a = estimate_cand_2b3b_side_rate(
        bundle.away_matches,
        bundle.home_matches,
        target_league_id=bundle.league_id,
        target_season=bundle.season,
        target_stratum=target_stratum,
        own_venue="AWAY",
        opp_venue="HOME",
        prior_mean=a_prior,
        other_venue_prior_mean=h_prior,
        history_limit=history_limit,
    )
    clh, cla = float(cand_h["lambda"]), float(cand_a["lambda"])
    cg = _grid_probs_from_lambdas(clh, cla)

    # Cotas epistémicas consistentes con distributions.py para ambos modelos
    def _unc_block(lh: float, la: float, neh: float, nea: float, grid_q: list[int]) -> dict[str, Any]:
        phi_g = FAMILY_DISPERSION_FLOORS.get("goals", 1.18)
        lh_lo, lh_hi = _gamma_rate_bounds(lh, max(1.0, neh + 8.0), phi_g)
        la_lo, la_hi = _gamma_rate_bounds(la, max(1.0, nea + 8.0), phi_g)
        corners = [
            _grid_probs_from_lambdas(a, b)["home_win"]
            for a in (lh_lo, lh_hi)
            for b in (la_lo, la_hi)
        ]
        return {
            "p_home_epistemic_bounds": [round(min(corners), 4), round(max(corners), 4)],
            "total_goals_q10_q90": grid_q,
        }

    b0_entry = {
        "model_version": BASELINE_MODEL_VERSION,
        "role": "PRODUCTION_BASELINE",
        "bet_authorization_allowed": True,  # sujeto a los gates reales de Shadow/XI/Calibración
        "lambda_home": float(b0_gf.lambda_home),
        "lambda_away": float(b0_gf.lambda_away),
        "lambda_total": float(b0_gf.lambda_total),
        "outcomes_1x2": {
            "home_win": round(float(b0_model.outcomes_1x2["home_win"]), 6),
            "draw": round(float(b0_model.outcomes_1x2["draw"]), 6),
            "away_win": round(float(b0_model.outcomes_1x2["away_win"]), 6),
        },
        "p_over_2_5": round(b0_o25, 6),
        "p_btts_yes": round(float(b0_model.btts["yes"]), 6),
        "home_n_eff": float(b0_gf.home_n_eff or 0.0),
        "away_n_eff": float(b0_gf.away_n_eff or 0.0),
        "home_sample_n": int(b0_gf.home_sample_n),
        "away_sample_n": int(b0_gf.away_sample_n),
        "warnings": list(b0_gf.warnings),
    }

    cand_entry = {
        "model_version": CANDIDATE_2B3B_VERSION,
        "role": "EXPERIMENTAL_CANDIDATE_ONLY",
        "bet_authorization_allowed": False,
        "calibration_status": "PENDIENTE_VALIDACION_PROSPECTIVA",
        "lambda_home": round(clh, 4),
        "lambda_away": round(cla, 4),
        "lambda_total": round(clh + cla, 4),
        "outcomes_1x2": {
            "home_win": round(float(cg["home_win"]), 6),
            "draw": round(float(cg["draw"]), 6),
            "away_win": round(float(cg["away_win"]), 6),
        },
        "p_over_2_5": round(float(cg["over_2_5"]), 6),
        "p_btts_yes": round(float(cg["btts_yes"]), 6),
        "home_n_eff": float(cand_h["n_eff"]),
        "away_n_eff": float(cand_a["n_eff"]),
        "home_sample_n": int(cand_h["n_own"]),
        "away_sample_n": int(cand_a["n_own"]),
        "uncertainty": _unc_block(clh, cla, float(cand_h["n_eff"]), float(cand_a["n_eff"]), cg["total_goals_q10_q90"]),
        "warnings": list(dict.fromkeys(cand_h["warnings"] + cand_a["warnings"])),
    }

    return {
        "experimental_only": True,
        "candidate_cannot_authorize_bet": True,
        "target_stratum": target_stratum or "UNKNOWN",
        "baseline_version": BASELINE_MODEL_VERSION,
        "candidate_version": CANDIDATE_2B3B_VERSION,
        BASELINE_2B3B_KEY: b0_entry,
        CANDIDATE_2B3B_KEY: cand_entry,
        "deltas_cand_minus_b0": {
            "p_home": round(cand_entry["outcomes_1x2"]["home_win"] - b0_entry["outcomes_1x2"]["home_win"], 6),
            "p_draw": round(cand_entry["outcomes_1x2"]["draw"] - b0_entry["outcomes_1x2"]["draw"], 6),
            "p_away": round(cand_entry["outcomes_1x2"]["away_win"] - b0_entry["outcomes_1x2"]["away_win"], 6),
            "p_over_2_5": round(cand_entry["p_over_2_5"] - b0_entry["p_over_2_5"], 6),
            "p_btts_yes": round(cand_entry["p_btts_yes"] - b0_entry["p_btts_yes"], 6),
            "lambda_home": round(cand_entry["lambda_home"] - b0_entry["lambda_home"], 4),
            "lambda_away": round(cand_entry["lambda_away"] - b0_entry["lambda_away"], 4),
        },
        "context_diagnostics": _extract_bundle_data_quality_and_coaches(bundle),
    }


def verify_cand_2b3b_manifest(manifest_path: Path) -> dict[str, Any]:
    """Verifica hashes SHA-256 de código y especificación declarados en MANIFEST_CANDIDATA_2B3B.json."""
    if not manifest_path.is_file():
        raise ProspectiveRegistryError(f"CAND_2B3B_MANIFEST_MISSING:{manifest_path}")
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    if doc.get("candidate_version") != CANDIDATE_2B3B_VERSION:
        raise ProspectiveRegistryError(f"CAND_2B3B_VERSION_MISMATCH:{doc.get('candidate_version')}")
    repo_root = Path.cwd()
    for cand in (manifest_path.resolve().parent.parent.parent, manifest_path.resolve().parent.parent, Path.cwd()):
        if (cand / "bet_engine").is_dir():
            repo_root = cand
            break
    verified: dict[str, str] = {}
    for section in ("code_hashes", "artifact_hashes"):
        for rel, exp in (doc.get(section) or {}).items():
            p = repo_root / rel
            if not p.is_file():
                raise ProspectiveRegistryError(f"MANIFEST_FILE_MISSING:{rel}")
            actual = hashlib.sha256(p.read_bytes()).hexdigest()
            if actual != exp:
                raise ProspectiveRegistryError(f"MANIFEST_HASH_MISMATCH:{rel}:{actual}!={exp}")
            verified[rel] = actual
    return {"manifest": doc, "verified_hashes": verified}


def record_2b3b_prospective_prediction(
    *,
    match_engine_payload: dict[str, Any],
    records_dir: Path,
    manifest_path: Path | None = None,
    bwin_odds: dict[str, Any] | None = None,
    odds_captured_utc: str | None = None,
    odds_sha256: str | None = None,
    shadow_decision: str | None = None,
    qualitative_markdown: str | None = None,
    source_paths: dict[str, str] | None = None,
    now_utc: datetime | None = None,
    min_corpus_cutoff_utc: str = CORPUS_CUTOFF_UTC,
    verify_manifest: bool = True,
    allow_overwrite: bool = False,
) -> Path:
    """Registra predicciones pareadas pre-kickoff B0 vs CAND_2B3B_V1 de forma inmutable."""
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
        raise ProspectiveRegistryError(f"NON_PROSPECTIVE_KICKOFF:kickoff({ko_dt.isoformat()})<=corpus_cutoff({corpus_cut_dt.isoformat()})")

    cutoff_raw = match_engine_payload.get("generated_at") or now_dt.isoformat()
    cutoff_dt = parse_utc_timestamp(str(cutoff_raw))
    if cutoff_dt > now_dt:
        raise ProspectiveRegistryError("FUTURE_CUTOFF_TIMESTAMP_INVALID")
    if cutoff_dt >= ko_dt:
        raise ProspectiveRegistryError("RETROSPECTIVE_RECORD_FORBIDDEN:cutoff>=kickoff")

    manifest_sha = None
    manifest_ver = "2B.4-unverified-manifest"
    if manifest_path is not None:
        if verify_manifest:
            mv = verify_cand_2b3b_manifest(manifest_path)
            manifest_ver = str(mv["manifest"].get("schema_version") or "2B.4-cand-manifest")
            manifest_sha = _canonical_sha256(mv["manifest"])
        elif manifest_path.is_file():
            m_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_ver = str(m_doc.get("schema_version") or "2B.4-cand-manifest")
            manifest_sha = _canonical_sha256(m_doc)

    bundle = ObservationStore.from_match_engine_json(
        match_engine_payload,
        cutoff_utc=cutoff_dt,
        qualitative_markdown=qualitative_markdown,
    )
    identity_check = _verify_target_fixture_identity(match_engine_payload, bundle, ko_dt)

    # Anti-duplicación estricta por fixture_id (evita registros duplicados bajo otro event_id)
    records_dir.mkdir(parents=True, exist_ok=True)
    existing_for_fid = list(records_dir.glob(f"{bundle.fixture_id}_*.json"))
    if existing_for_fid:
        raise ProspectiveRegistryError(f"IMMUTABLE_RECORD_ALREADY_EXISTS:{existing_for_fid[0]}")

    parallel = evaluate_parallel_bundle_2b3b(bundle, history_limit=20)
    odds_status, normalized_odds_cap = _classify_odds_status(bwin_odds, odds_captured_utc, odds_sha256, now_dt, ko_dt)
    exclusions: list[str] = []
    if len(bundle.home_matches) < 6 or len(bundle.away_matches) < 6:
        exclusions.append(f"LOW_HISTORY_LT_6(home={len(bundle.home_matches)},away={len(bundle.away_matches)})")
    if odds_status == "NO_ODDS":
        exclusions.append("NO_BWIN_ODDS_CAPTURED")
    elif odds_status == "ODDS_UNVERIFIED":
        exclusions.append("BWIN_ODDS_UNVERIFIED_TIMESTAMP")

    stratum = parallel["target_stratum"]
    stratum_norm = "NATIONAL_TEAM" if stratum == "NATIONAL_TEAM" else ("DOMESTIC_LEAGUE" if stratum == "DOMESTIC_LEAGUE" else str(stratum or "UNKNOWN"))
    obs_sha = _canonical_sha256(
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
        }
    )

    event_id_str = str(match_meta.get("event_id") or bundle.event_id or "")
    pre_block = {
        "schema_version": REGISTRY_2B3B_SCHEMA_VERSION,
        "fixture_id": bundle.fixture_id,
        "event_id": event_id_str,
        "competition": {
            "league_id": bundle.league_id,
            "league_name": str(fx.get("league_name") or ""),
            "season": bundle.season,
            "stratum": stratum_norm,
            "is_national_team": stratum_norm == "NATIONAL_TEAM",
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
            "registry_schema_version": REGISTRY_2B3B_SCHEMA_VERSION,
            "baseline_model_version": BASELINE_MODEL_VERSION,
            "candidate_model_version": CANDIDATE_2B3B_VERSION,
            "input_sha256": bundle.source_sha256,
            "match_engine_payload_sha256": _canonical_sha256(match_engine_payload),
            "observations_sha256": obs_sha,
            "odds_sha256": odds_sha256,
            "manifest_sha256": manifest_sha,
            "manifest_version": manifest_ver,
            "spec_params_sha256": _canonical_sha256(CAND_2B3B_SPEC_PARAMS),
            "source_paths": dict(source_paths or {}),
        },
        "diagnostics": parallel["context_diagnostics"],
        "lineup_status": _classify_lineup_state(bundle, match_engine_payload),
        "bwin_odds": {
            "odds_status": odds_status,
            "economic_eval_eligible": odds_status == "ODDS_VERIFIED",
            "captured_utc": normalized_odds_cap or odds_captured_utc,
            "quotes": dict(bwin_odds or {}) if odds_status == "ODDS_VERIFIED" else {},
        },
        "governance": {
            "production_model": BASELINE_MODEL_VERSION,
            "experimental_model": CANDIDATE_2B3B_VERSION,
            "experimental_bet_authorized": False,
            "calibration_accredited": False,
            "shadow_decision_at_record": shadow_decision,
            "evaluation_coupled_to_shadow": False,
        },
        "exclusion_reasons": exclusions,
        "eligible_for_paired_eval": not any(e.startswith("LOW_HISTORY_LT_6") for e in exclusions),
        "predictions": {
            BASELINE_2B3B_KEY: parallel[BASELINE_2B3B_KEY],
            CANDIDATE_2B3B_KEY: parallel[CANDIDATE_2B3B_KEY],
        },
        "deltas_cand_minus_b0": parallel["deltas_cand_minus_b0"],
    }

    slug = f"{bundle.fixture_id}_{event_id_str or 'noevent'}.json"
    out_path = records_dir / slug
    full_doc = {
        "schema_version": REGISTRY_2B3B_SCHEMA_VERSION,
        "pre_kickoff": pre_block,
        "pre_kickoff_sha256": _canonical_sha256(pre_block),
        "settlement_mode": "SEPARATE_FILE_IMMUTABLE_PRE_KICKOFF",
    }
    encoded = json.dumps(full_doc, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        with out_path.open("xb") as fh:
            fh.write(encoded)
    except FileExistsError as exc:
        raise ProspectiveRegistryError(f"IMMUTABLE_RECORD_ALREADY_EXISTS:{out_path}") from exc
    return out_path


def record_2b3b_batch(
    *,
    matches_root: Path,
    records_dir: Path,
    manifest_path: Path | None = None,
    now_utc: datetime | None = None,
    verify_manifest: bool = True,
) -> dict[str, Any]:
    """Registra en lote expedientes PREMATCH futuros sin detenerse ante fallos individuales."""
    recorded: list[str] = []
    skipped_already_recorded: list[str] = []
    insufficient_or_blocked: list[dict[str, str]] = []
    capture_failures: list[dict[str, str]] = []

    if not matches_root.is_dir():
        return {"recorded": [], "skipped_already_recorded": [], "insufficient_or_blocked": [], "capture_failures": [{"path": str(matches_root), "error": "MATCHES_ROOT_MISSING"}]}

    from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt
    from bet_engine.modeling.prospective_registry import extract_bwin_capture_utc

    for ev_dir in sorted(p for p in matches_root.iterdir() if p.is_dir()):
        me_files = sorted((ev_dir / "inputs").glob("match_engine_*.json"))
        if not me_files:
            continue
        me_path = me_files[0]
        try:
            payload = json.loads(me_path.read_text(encoding="utf-8"))
        except Exception as exc:
            capture_failures.append({"event_dir": ev_dir.name, "error": f"INVALID_JSON:{exc}"})
            continue

        odds = None
        odds_sha = None
        odds_cap_utc = None
        bwin_files = sorted((ev_dir / "inputs").glob("cuotas_bwin_*.txt"))
        if bwin_files:
            try:
                fx = payload.get("fixture") or {}
                odds = parse_bwin_odds_txt(bwin_files[0], home_name=fx.get("home_name"), away_name=fx.get("away_name"))
                odds_sha = hashlib.sha256(bwin_files[0].read_bytes()).hexdigest()
                odds_cap_utc = extract_bwin_capture_utc(bwin_files[0])
            except Exception as exc:
                capture_failures.append({"event_dir": ev_dir.name, "error": f"ODDS_PARSE_FAIL:{exc}"})

        shadow_decision = None
        sh_files = sorted((ev_dir / "shadow").glob("shadow_summary_*.json"))
        if sh_files:
            try:
                sdoc = json.loads(sh_files[0].read_text(encoding="utf-8"))
                shadow_decision = str(((sdoc.get("shadow_report") or {}).get("decision")) or "")
            except Exception:
                pass

        qual_md = None
        qual_files = sorted((ev_dir / "instructions").glob("reporte_cualitativo_*.md"))
        if qual_files:
            try:
                qual_md = qual_files[0].read_text(encoding="utf-8-sig", errors="replace")
            except Exception:
                pass

        try:
            out_p = record_2b3b_prospective_prediction(
                match_engine_payload=payload,
                records_dir=records_dir,
                manifest_path=manifest_path,
                bwin_odds=odds,
                odds_captured_utc=odds_cap_utc,
                odds_sha256=odds_sha,
                shadow_decision=shadow_decision,
                qualitative_markdown=qual_md,
                source_paths={"match_engine": str(me_path), "bwin_txt": str(bwin_files[0]) if bwin_files else ""},
                now_utc=now_utc,
                verify_manifest=verify_manifest,
            )
            recorded.append(str(out_p))
        except ProspectiveRegistryError as exc:
            msg = str(exc)
            if "IMMUTABLE_RECORD_ALREADY_EXISTS" in msg:
                skipped_already_recorded.append(ev_dir.name)
            else:
                insufficient_or_blocked.append({"event_dir": ev_dir.name, "reason": msg})
        except Exception as exc:
            capture_failures.append({"event_dir": ev_dir.name, "error": f"{type(exc).__name__}:{exc}"})

    # Registrar log de incidencias sin sobrescribir registros existentes
    incidents_path = records_dir / "capture_and_exclusion_log.json"
    records_dir.mkdir(parents=True, exist_ok=True)
    incidents_doc = {
        "updated_utc": (now_utc or datetime.now(timezone.utc)).isoformat(),
        "recorded_count": len(recorded),
        "skipped_already_recorded_count": len(skipped_already_recorded),
        "insufficient_or_blocked": insufficient_or_blocked,
        "capture_failures": capture_failures,
    }
    incidents_path.write_text(json.dumps(incidents_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return incidents_doc


def settle_2b3b_prospective_record(
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
    """Liquida resultados reales en archivo separado sin reescribir el registro pre_kickoff."""
    raw_bytes = record_path.read_bytes()
    record_file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    doc = json.loads(raw_bytes.decode("utf-8"))
    pre = doc["pre_kickoff"]
    if _canonical_sha256(pre) != doc.get("pre_kickoff_sha256"):
        raise ProspectiveRegistryError("PRE_KICKOFF_RECORD_TAMPERED")

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
        raise ProspectiveRegistryError(f"PREMATURE_SETTLEMENT:settled_at({now_dt.isoformat()})<min_finish({min_avail_dt.isoformat()})")

    verification_detail: dict[str, Any] | None = None
    prov_ok = False
    prov_reason = "NONE"
    if simulation_mode:
        verification_status = "SIMULATED_TEST_ONLY"
        creditable = False
        prov_reason = "SIMULATION_MODE"
    elif authoritative_fixture is not None:
        verification_detail = verify_authoritative_result(
            pre, hg=int(hg), ag=int(ag), match_status=st, authoritative_fixture=authoritative_fixture
        )
        prov_ok, prov_reason = _has_verified_provider_provenance(
            authoritative_fixture, source_path=source_path, provider_provenance_verified=provider_provenance_verified
        )
        verification_status = "VERIFIED_AUTHORITATIVE" if prov_ok else "UNVERIFIED_SOURCE_PROVENANCE"
        creditable = bool(prov_ok)
    else:
        verification_status = "UNVERIFIED_RESULT"
        creditable = False
        prov_reason = "MANUAL_NO_SOURCE"

    y_1x2 = "HOME" if hg > ag else ("DRAW" if hg == ag else "AWAY")
    y_hw, y_dr, y_aw = (1 if hg > ag else 0), (1 if hg == ag else 0), (1 if hg < ag else 0)
    y_o25 = 1 if (hg + ag) > 2.5 else 0
    y_btts = 1 if (hg > 0 and ag > 0) else 0

    scores: dict[str, Any] = {}
    eps = 1e-6
    for mname in MODEL_PAIR_KEYS:
        pred = pre["predictions"][mname]
        lh, la = float(pred["lambda_home"]), float(pred["lambda_away"])
        p1 = float(pred["outcomes_1x2"]["home_win"])
        px = float(pred["outcomes_1x2"]["draw"])
        p2 = float(pred["outcomes_1x2"]["away_win"])
        po25 = float(pred["p_over_2_5"])
        pbtts = float(pred["p_btts_yes"])
        rps = ((p1 - y_hw) ** 2 + ((p1 + px) - (y_hw + y_dr)) ** 2) / 2.0
        ll_1x2 = -math.log(max(eps, p1 if y_1x2 == "HOME" else (px if y_1x2 == "DRAW" else p2)))
        brier_1x2 = ((p1 - y_hw) ** 2 + (px - y_dr) ** 2 + (p2 - y_aw) ** 2) / 3.0
        pnll_tot = -(hg * math.log(max(1e-9, lh)) - lh - math.lgamma(hg + 1.0)) - (ag * math.log(max(1e-9, la)) - la - math.lgamma(ag + 1.0))
        scores[mname] = {
            "rps": round(rps, 6),
            "ll_1x2": round(ll_1x2, 6),
            "brier_1x2_per_out": round(brier_1x2, 6),
            "brier_o25": round((po25 - y_o25) ** 2, 6),
            "ll_o25": round(-(y_o25 * math.log(max(eps, po25)) + (1 - y_o25) * math.log(max(eps, 1.0 - po25))), 6),
            "brier_btts": round((pbtts - y_btts) ** 2, 6),
            "ll_btts": round(-(y_btts * math.log(max(eps, pbtts)) + (1 - y_btts) * math.log(max(eps, 1.0 - pbtts))), 6),
            "poisson_nll_total": round(pnll_tot, 6),
            "err_home": round(lh - hg, 4),
            "err_away": round(la - ag, 4),
            "err_total": round((lh + la) - (hg + ag), 4),
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
        "schema_version": SETTLEMENT_2B3B_SCHEMA_VERSION,
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


def _block_bootstrap_paired_2b3b(
    base_vals: list[float],
    cand_vals: list[float],
    day_keys: list[str],
    *,
    n_boot: int = 2000,
    seed: int = 42,
) -> dict[str, Any]:
    n = len(base_vals)
    if n == 0:
        return {"n": 0, "n_days": 0, "mean_delta": None, "ci_95": None, "prob_cand_improves": None, "bootstrap_mode": "EMPTY"}
    deltas = [cand_vals[i] - base_vals[i] for i in range(n)]
    mean_d = statistics.fmean(deltas)
    blocks: dict[str, list[float]] = defaultdict(list)
    for dk, d in zip(day_keys, deltas):
        blocks[dk].append(d)
    if len(blocks) < 5:
        fb = paired_bootstrap_delta_ci(base_vals, cand_vals, n_boot=n_boot, seed=seed)
        return {
            "n": n,
            "n_days": len(blocks),
            "mean_delta": fb["mean_delta"],
            "ci_95": [fb["ci_95_low"], fb["ci_95_high"]],
            "prob_cand_improves": fb["prob_alt_improves"],
            "bootstrap_mode": "MATCH_LEVEL_FALLBACK_LT_5_DAYS",
        }
    bsums = [(sum(blocks[k]), len(blocks[k])) for k in sorted(blocks.keys())]
    m = len(bsums)
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        s = 0.0
        c = 0
        for _ in range(m):
            bs, bn = bsums[rng.randrange(m)]
            s += bs
            c += bn
        boots.append(s / max(1, c))
    boots.sort()
    lo = boots[int(0.025 * (n_boot - 1))]
    hi = boots[int(0.975 * (n_boot - 1))]
    p_imp = sum(1 for v in boots if v < 0.0) / n_boot
    k_bad = round((1.0 - p_imp) * n_boot)
    p_two = min(1.0, 2.0 * min((round(p_imp * n_boot) + 1.0) / (n_boot + 1.0), (k_bad + 1.0) / (n_boot + 1.0)))
    return {
        "n": n,
        "n_days": m,
        "mean_delta": round(mean_d, 6),
        "ci_95": [round(lo, 6), round(hi, 6)],
        "prob_cand_improves": round(p_imp, 4),
        "p_two_sided": round(p_two, 5),
        "bootstrap_mode": "DAILY_BLOCK_BOOTSTRAP",
    }


def evaluate_2b3b_prospective_records(
    records_dir: Path,
    *,
    settlements_dir: Path | None = None,
    bootstrap_replicates: int = 2000,
) -> dict[str, Any]:
    """Evalúa registros prospectivos 2B.3B pareados sin auto-activar acreditación."""
    settled_docs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending_n = 0
    insufficient_history_n = 0
    unverified_result_n = 0
    simulated_test_n = 0
    tampered: list[str] = []

    for p in sorted(records_dir.glob("*.json")):
        if p.name == "capture_and_exclusion_log.json":
            continue
        raw_bytes = p.read_bytes()
        file_sha = hashlib.sha256(raw_bytes).hexdigest()
        doc = json.loads(raw_bytes.decode("utf-8"))
        pre = doc.get("pre_kickoff") or {}
        if _canonical_sha256(pre) != doc.get("pre_kickoff_sha256"):
            tampered.append(p.name)
            continue
        s_path = default_settlement_path(p, settlements_dir=settlements_dir)
        if not s_path.is_file():
            pending_n += 1
            continue
        s_doc = json.loads(s_path.read_text(encoding="utf-8"))
        if s_doc.get("pre_kickoff_sha256") != doc.get("pre_kickoff_sha256") or s_doc.get("record_file_sha256") != file_sha:
            tampered.append(p.name)
            continue
        s_obj = s_doc.get("settlement") or {}
        if s_obj.get("status") != "SETTLED":
            pending_n += 1
            continue
        if s_obj.get("verification_status") == "SIMULATED_TEST_ONLY" or s_obj.get("is_simulation"):
            simulated_test_n += 1
            continue
        if s_obj.get("verification_status") != "VERIFIED_AUTHORITATIVE" or not s_obj.get("creditable", False):
            unverified_result_n += 1
            continue
        if not pre.get("eligible_for_paired_eval", True):
            insufficient_history_n += 1
            continue
        if all(m in (pre.get("predictions") or {}) for m in MODEL_PAIR_KEYS):
            settled_docs.append((doc, s_obj))

    def _agg(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        if not items:
            return {"n_fixtures": 0}
        models_out: dict[str, Any] = {}
        day_keys = [d["pre_kickoff"]["timing"]["kickoff_utc"][:10] for d, _ in items]
        for mname in MODEL_PAIR_KEYS:
            sc_list = [s["model_scores"][mname] for _, s in items]
            hw_pairs = [(float(d["pre_kickoff"]["predictions"][mname]["outcomes_1x2"]["home_win"]), 1 if s["actual_1x2"] == "HOME" else 0) for d, s in items]
            aw_pairs = [(float(d["pre_kickoff"]["predictions"][mname]["outcomes_1x2"]["away_win"]), 1 if s["actual_1x2"] == "AWAY" else 0) for d, s in items]
            o25_pairs = [(float(d["pre_kickoff"]["predictions"][mname]["p_over_2_5"]), int(s["actual_o25"])) for d, s in items]
            btts_pairs = [(float(d["pre_kickoff"]["predictions"][mname]["p_btts_yes"]), int(s["actual_btts"])) for d, s in items]
            fav_h = [(p, y) for p, y in hw_pairs if p >= 0.55]
            fav_a = [(p, y) for p, y in aw_pairs if p >= 0.55]
            models_out[mname] = {
                "rps": round(statistics.fmean(x["rps"] for x in sc_list), 5),
                "log_loss_1x2": round(statistics.fmean(x["ll_1x2"] for x in sc_list), 5),
                "brier_1x2": round(statistics.fmean(x["brier_1x2_per_out"] for x in sc_list), 5),
                "brier_o25": brier_score(o25_pairs),
                "log_loss_o25": log_loss(o25_pairs),
                "brier_btts": brier_score(btts_pairs),
                "log_loss_btts": log_loss(btts_pairs),
                "poisson_nll_total": round(statistics.fmean(x["poisson_nll_total"] for x in sc_list), 5),
                "ece_5_hw": expected_calibration_error(hw_pairs, n_bins=5),
                "ece_5_aw": expected_calibration_error(aw_pairs, n_bins=5),
                "calibration_hw": summarize_probability_calibration(hw_pairs, n_bins=5),
                "bias_home_win": round(statistics.fmean(p - y for p, y in hw_pairs), 4),
                "bias_away_win": round(statistics.fmean(p - y for p, y in aw_pairs), 4),
                "bias_lambda_home": round(statistics.fmean(x["err_home"] for x in sc_list), 4),
                "bias_lambda_away": round(statistics.fmean(x["err_away"] for x in sc_list), 4),
                "bias_lambda_total": round(statistics.fmean(x["err_total"] for x in sc_list), 4),
                "favorite_home_ge_0_55": {"n": len(fav_h), "bias": round(statistics.fmean(p - y for p, y in fav_h), 4) if fav_h else None},
                "favorite_away_ge_0_55": {"n": len(fav_a), "bias": round(statistics.fmean(p - y for p, y in fav_a), 4) if fav_a else None},
            }

        paired_deltas: dict[str, Any] = {}
        gate_metrics = ("rps", "ll_1x2", "brier_o25", "brier_btts")
        for met in ("rps", "ll_1x2", "brier_1x2_per_out", "brier_o25", "brier_btts", "poisson_nll_total"):
            bv = [float(s["model_scores"][BASELINE_2B3B_KEY][met]) for _, s in items]
            cv = [float(s["model_scores"][CANDIDATE_2B3B_KEY][met]) for _, s in items]
            paired_deltas[met] = _block_bootstrap_paired_2b3b(bv, cv, day_keys, n_boot=bootstrap_replicates, seed=42)
            if met in gate_metrics and paired_deltas[met].get("p_two_sided") is not None:
                paired_deltas[met]["bonferroni_m4_p"] = round(min(1.0, float(paired_deltas[met]["p_two_sided"]) * 4.0), 5)

        return {"n_fixtures": len(items), "models": models_out, "paired_delta_cand_minus_b0": paired_deltas}

    club_docs = [(d, s) for d, s in settled_docs if not d["pre_kickoff"]["competition"].get("is_national_team")]
    league_docs = [(d, s) for d, s in settled_docs if d["pre_kickoff"]["competition"].get("stratum") == "DOMESTIC_LEAGUE"]
    nat_docs = [(d, s) for d, s in settled_docs if d["pre_kickoff"]["competition"].get("is_national_team")]
    insuf_venue_docs = [(d, s) for d, s in settled_docs if d["pre_kickoff"]["diagnostics"]["history_coverage"]["insufficient_venue_any_lt3"]]
    own_home_lt3_docs = [(d, s) for d, s in settled_docs if d["pre_kickoff"]["diagnostics"]["history_coverage"]["insufficient_own_home_lt3"]]
    economic_docs = [(d, s) for d, s in settled_docs if (d["pre_kickoff"].get("bwin_odds") or {}).get("economic_eval_eligible")]

    # Desglose por liga, profundidad histórica y calidad de fuentes / XI
    by_league_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    by_depth_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    by_quality_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    by_lineup_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for d, s in settled_docs:
        pre = d["pre_kickoff"]
        comp = pre["competition"]
        by_league_map[f"{comp.get('league_id')}:{comp.get('league_name') or 'UNKNOWN'}"].append((d, s))
        md = int(pre["diagnostics"]["history_coverage"]["min_depth"])
        dkey = "DEPTH_6_9" if md <= 9 else ("DEPTH_10_14" if md <= 14 else "DEPTH_GE_15")
        by_depth_map[dkey].append((d, s))
        by_quality_map[str(pre["diagnostics"]["data_quality"]["coverage_tier"])].append((d, s))
        by_lineup_map[str(pre["lineup_status"]["lineup_state"])].append((d, s))

    club_sufficient = len(club_docs) >= PROSPECTIVE_MIN_SAMPLE["CLUB"]
    nat_sufficient = len(nat_docs) >= PROSPECTIVE_MIN_SAMPLE["NATIONAL_TEAM"]
    accreditation_status = "MUESTRA_INSUFICIENTE"
    if club_sufficient:
        club_agg = _agg(club_docs)
        pd = club_agg["paired_delta_cand_minus_b0"]
        rps_ok = (pd["rps"].get("prob_cand_improves") or 0.0) >= 0.975
        ll_ok = (pd["ll_1x2"].get("prob_cand_improves") or 0.0) >= 0.975
        o25_ok = (pd["brier_o25"].get("mean_delta") or 999.0) <= 0.0005 and (pd["brier_o25"].get("prob_cand_improves") or 0.0) >= 0.05
        btts_ok = (pd["brier_btts"].get("mean_delta") or 999.0) <= 0.0005 and (pd["brier_btts"].get("prob_cand_improves") or 0.0) >= 0.05
        if rps_ok and ll_ok and o25_ok and btts_ok:
            accreditation_status = "CRITERIOS_DE_ACREDITACION_PROSPECTIVA_CUMPLIDOS_SIN_ACTIVACION_AUTOMATICA"
        else:
            accreditation_status = "CRITERIOS_PROSPECTIVOS_NO_SUPERADOS"

    inc_path = records_dir / "capture_and_exclusion_log.json"
    capture_log = json.loads(inc_path.read_text(encoding="utf-8")) if inc_path.is_file() else {}

    return {
        "schema_version": "2B.4-prospective-eval-v1",
        "baseline_version": BASELINE_MODEL_VERSION,
        "candidate_version": CANDIDATE_2B3B_VERSION,
        "production_calibrated_out_of_sample": False,
        "auto_promotion_disabled": True,
        "accreditation_status": accreditation_status,
        "settled_paired_n": len(settled_docs),
        "economic_eval_eligible_n": len(economic_docs),
        "pending_n": pending_n,
        "insufficient_history_n": insufficient_history_n,
        "unverified_result_n": unverified_result_n,
        "simulated_test_n": simulated_test_n,
        "tampered_rejected": tampered,
        "capture_log_summary": capture_log,
        "sample_sufficiency": {
            "CLUB": {"n": len(club_docs), "target": PROSPECTIVE_MIN_SAMPLE["CLUB"], "sufficient": club_sufficient},
            "NATIONAL_TEAM": {"n": len(nat_docs), "target": PROSPECTIVE_MIN_SAMPLE["NATIONAL_TEAM"], "sufficient": nat_sufficient},
        },
        "ALL": _agg(settled_docs),
        "CLUB": _agg(club_docs),
        "DOMESTIC_LEAGUE": _agg(league_docs),
        "NATIONAL_TEAM": _agg(nat_docs),
        "INSUFFICIENT_VENUE_ANY_LT3": _agg(insuf_venue_docs),
        "INSUFFICIENT_OWN_HOME_LT3": _agg(own_home_lt3_docs),
        "by_league": {k: _agg(v) for k, v in sorted(by_league_map.items())},
        "by_depth_bucket": {k: _agg(v) for k, v in sorted(by_depth_map.items())},
        "by_quality_tier": {k: _agg(v) for k, v in sorted(by_quality_map.items())},
        "by_lineup_state": {k: _agg(v) for k, v in sorted(by_lineup_map.items())},
    }
