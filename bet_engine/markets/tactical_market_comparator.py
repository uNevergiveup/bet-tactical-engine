"""TACTICAL_MARKET_COMPARATOR, segunda pasada de IA, estados y trazabilidad NO BET (Fase 2 - Tareas D, E, F, G, H).

Permite comparar alternativas reales de Bwin usando Decimal, probabilidad implícita bruta (1/cuota),
vigencia de captura y evidencia estadística con dispersión y exposición individual, sin forzar
ganadores ni calcular EV ficticio cuando no existe probabilidad predictiva acreditada.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from bet_engine.markets.bwin_catalog import check_equivalence_forbidden

COMPARISON_STATES = frozenset({
    "TACTICAL_HYPOTHESIS",
    "EXPLORATORY_CANDIDATE",
    "INSUFFICIENT_EVIDENCE",
    "CONTRADICTED_HYPOTHESIS",
    "PRICE_UNVERIFIED",
    "LINEUP_DEPENDENCY_PENDING",
    "UNSUPPORTED_MARKET",
    "PREDICTIVE_CALIBRATION_PENDING",
})

NO_BET_AND_OPERATIONAL_CATEGORIES = (
    "EXPEDIENTE_INCOMPLETO",
    "FALLO_OPERATIVO",
    "CUOTA_NO_DISPONIBLE",
    "REGLA_COMERCIAL_NO_CONCILIADA",
    "ALINEACION_PENDIENTE",
    "FALTA_COBERTURA_HISTORICA",
    "CALIBRACION_PREDICTIVA_PENDIENTE",
    "RECHAZO_ESTADISTICO_JUSTIFICADO",
    "HIPOTESIS_TACTICA_CONTRADICHA",
    "DECISION_FORMAL_SIN_BET_AUTORIZADO",
)


class MarketComparisonError(RuntimeError):
    """Error explícito de validación en TACTICAL_MARKET_COMPARATOR."""


def _parse_dt(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def _to_dec(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        d = Decimal(str(val).strip().replace(",", "."))
        return d if d > Decimal("1.0") else None
    except (InvalidOperation, ValueError):
        return None


def compute_implied_break_even(odd_raw: Any) -> Decimal | None:
    """Calcula la probabilidad implícita bruta de equilibrio (1 / cuota) usando Decimal."""
    odd = _to_dec(odd_raw)
    if odd is None:
        return None
    return (Decimal("1") / odd).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def evaluate_price_validity(
    *,
    captured_at_utc: str | None,
    asof_utc: datetime,
    kickoff_utc: datetime | None,
    max_age_minutes: int = 180,
) -> dict[str, Any]:
    """Evalúa la vigencia temporal de una captura Bwin sin usar datos posteriores al kickoff."""
    cap_dt = _parse_dt(captured_at_utc)
    if cap_dt is None:
        return {"status": "PRICE_UNVERIFIED", "age_minutes": None, "reason": "MISSING_CAPTURE_TIMESTAMP"}
    if kickoff_utc is not None and cap_dt > kickoff_utc:
        return {"status": "PRICE_INVALID_POST_KICKOFF", "age_minutes": None, "reason": "CAPTURED_AFTER_KICKOFF"}
    age_min = round((asof_utc - cap_dt).total_seconds() / 60.0, 2)
    if age_min < 0:
        return {"status": "PRICE_UNVERIFIED", "age_minutes": age_min, "reason": "CAPTURE_IN_FUTURE_VS_ASOF"}
    if age_min > max_age_minutes:
        return {"status": "PRICE_EXPIRED", "age_minutes": age_min, "reason": f"CAPTURE_OLDER_THAN_{max_age_minutes}M"}
    return {"status": "VALID_PREMATCH", "age_minutes": age_min, "reason": "OK"}


def build_comparison_option(
    entry: dict[str, Any],
    *,
    price_validity: dict[str, Any],
    accredited_p_model: float | None = None,
) -> dict[str, Any]:
    """Construye la ficha cuantitativa y de precio de una selección para comparar alternativas."""
    odd_dec = _to_dec(entry.get("odd"))
    imp = compute_implied_break_even(odd_dec)

    if accredited_p_model is not None and entry.get("settlement_status") == "VERIFIED_STANDARD":
        pred_status = "ACCREDITED_MODEL_PROBABILITY"
    else:
        pred_status = "PREDICTIVE_PROBABILITY_NOT_ACCREDITED"

    return {
        "selection_id": entry.get("selection_id"),
        "market_family": entry.get("market_family"),
        "canonical_key": entry.get("canonical_key"),
        "market_header_literal": entry.get("market_header_literal"),
        "selection_literal": entry.get("selection_literal"),
        "line": entry.get("line"),
        "odd": str(odd_dec) if odd_dec is not None else None,
        "implied_break_even_prob": str(imp) if imp is not None else None,
        "temporal_scope": entry.get("temporal_scope"),
        "target_scope": entry.get("target_scope"),
        "target_entity": entry.get("target_entity"),
        "settlement_status": entry.get("settlement_status"),
        "commercial_coverage": entry.get("commercial_coverage"),
        "captured_at_utc": entry.get("captured_at_utc"),
        "price_validity": price_validity,
        "predictive_probability_status": pred_status,
        "accredited_p_model": accredited_p_model if pred_status == "ACCREDITED_MODEL_PROBABILITY" else None,
    }


def compare_option_pair(opt_a: dict[str, Any], opt_b: dict[str, Any]) -> dict[str, Any]:
    """Compara dos opciones concretas sin forzar un ganador ni inventar EV."""
    odd_a = _to_dec(opt_a.get("odd"))
    odd_b = _to_dec(opt_b.get("odd"))
    imp_a = _to_dec(opt_a.get("implied_break_even_prob")) if opt_a.get("implied_break_even_prob") else compute_implied_break_even(odd_a)
    imp_b = _to_dec(opt_b.get("implied_break_even_prob")) if opt_b.get("implied_break_even_prob") else compute_implied_break_even(odd_b)

    eq_block = check_equivalence_forbidden(
        str(opt_a.get("market_family") or ""),
        str(opt_b.get("market_family") or ""),
    )

    odd_diff = str((odd_a - odd_b).quantize(Decimal("0.01"))) if (odd_a is not None and odd_b is not None) else None
    imp_diff = str((imp_a - imp_b).quantize(Decimal("0.0001"))) if (imp_a is not None and imp_b is not None) else None

    return {
        "option_a": opt_a,
        "option_b": opt_b,
        "odd_delta_a_minus_b": odd_diff,
        "implied_prob_delta_a_minus_b": imp_diff,
        "equivalence_warning": eq_block,
        "predictive_ev_allowed": False,
        "comparison_verdict": "REQUIRES_AI_FOOTBALL_AND_ROBUSTNESS_EVALUATION",
    }


def evaluate_prematch_temporal_policy(
    *,
    scouting_start_utc: datetime | str,
    evaluation_start_utc: datetime | str,
    formal_decision_utc: datetime | str,
    kickoff_utc: datetime | str,
    bwin_captured_at_utc: datetime | str | None,
    target_status: str = "NS",
    lineup_status: str = "LINEUPS_PENDING",
    identity_consistent: bool = True,
    predictive_calibration_accredited: bool = False,
    commercial_settlement_verified: bool = False,
    market_requires_official_xi: bool = True,
    max_exploratory_price_age_minutes: int = 180,
    max_formal_price_age_minutes: int = 20,
    analysis_cutoff_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Política temporal compuesta (sustituye T-45 rígido sin relajar controles de seguridad)."""
    ko_dt = _parse_dt(kickoff_utc)
    scout_dt = _parse_dt(scouting_start_utc)
    eval_dt = _parse_dt(evaluation_start_utc)
    dec_dt = _parse_dt(formal_decision_utc)
    cap_dt = _parse_dt(bwin_captured_at_utc)
    cutoff_dt = _parse_dt(analysis_cutoff_utc) if analysis_cutoff_utc else None

    if ko_dt is None or dec_dt is None or eval_dt is None or scout_dt is None:
        return {
            "policy_mode": "COMPOSITE_PREMATCH_V2",
            "exploratory_allowed": False,
            "formal_evaluation_eligible": False,
            "formal_bet_authorized": False,
            "blocking_reasons": ["MISSING_REQUIRED_TIMESTAMPS"],
            "lead_minutes_at_decision": None,
        }

    status_up = str(target_status or "NS").strip().upper()
    is_live_or_post = status_up in {"1H", "HT", "2H", "ET", "BT", "P", "FT", "AET", "PEN", "LIVE"}
    lead_minutes = round((ko_dt - dec_dt).total_seconds() / 60.0, 2)
    is_prematch_clock = (not is_live_or_post) and (dec_dt < ko_dt) and (eval_dt < ko_dt) and (scout_dt < ko_dt)

    # Exploratory analysis is permitted anytime while real clock < kickoff (including T-45..T-5)
    exploratory_reasons: list[str] = []
    if not is_prematch_clock:
        exploratory_reasons.append("POST_KICKOFF_OR_LIVE_BLOCKED")
    if not identity_consistent:
        exploratory_reasons.append("IDENTITY_MISMATCH")

    # Formal authorization checks (strict, never relies on old analysis_cutoff_utc)
    formal_blocks: list[str] = list(exploratory_reasons)
    if cutoff_dt is not None and cutoff_dt < dec_dt and (dec_dt - cutoff_dt).total_seconds() > 3600 * 6:
        # Stale cutoff cannot substitute real execution time; real dec_dt < ko_dt is still enforced
        pass
    if cap_dt is None:
        formal_blocks.append("PRICE_UNVERIFIED_MISSING_CAPTURE")
    elif cap_dt >= ko_dt:
        formal_blocks.append("PRICE_CAPTURED_AT_OR_AFTER_KICKOFF")
    else:
        age_min = round((dec_dt - cap_dt).total_seconds() / 60.0, 2)
        if age_min < 0:
            formal_blocks.append("PRICE_CAPTURE_AFTER_DECISION")
        elif age_min > max_formal_price_age_minutes:
            formal_blocks.append(f"PRICE_STALE_FOR_FORMAL_BET({age_min}m>{max_formal_price_age_minutes}m)")

    xi_confirmed = lineup_status in {"LINEUPS_API_CONFIRMED", "LINEUPS_EXTERNAL_CONFIRMED", "LINEUPS_MANUAL_VERIFIED"}
    if market_requires_official_xi and not xi_confirmed:
        formal_blocks.append(f"OFFICIAL_XI_REQUIRED({lineup_status})")
    if not commercial_settlement_verified:
        formal_blocks.append("COMMERCIAL_SETTLEMENT_UNVERIFIED")
    if not predictive_calibration_accredited:
        formal_blocks.append("PREDICTIVE_CALIBRATION_PENDING")

    return {
        "policy_mode": "COMPOSITE_PREMATCH_V2",
        "scouting_start_utc": scout_dt.isoformat(),
        "evaluation_start_utc": eval_dt.isoformat(),
        "formal_decision_utc": dec_dt.isoformat(),
        "bwin_captured_at_utc": cap_dt.isoformat() if cap_dt else None,
        "kickoff_utc": ko_dt.isoformat(),
        "lead_minutes_at_decision": lead_minutes,
        "t15_lineup_reference_reached": lead_minutes <= 15.0,
        "t15_is_lineup_control_not_bet_close": True,
        "exploratory_allowed": len(exploratory_reasons) == 0,
        "exploratory_blocks": exploratory_reasons,
        "formal_evaluation_eligible": len(formal_blocks) == 0,
        "formal_bet_authorized": len(formal_blocks) == 0,
        "formal_blocks": formal_blocks,
    }


def build_temporal_control_audit(
    *,
    analysis_cutoff_utc: str | None,
    execution_utc: datetime,
    captured_at_utc: str | None,
    kickoff_utc: datetime | None,
    lineup_status: str = "PROBABLE",
) -> dict[str, Any]:
    """Documenta la interacción temporal PREMATCH y evalúa la política compuesta junto a T-45."""
    lead_minutes = round((kickoff_utc - execution_utc).total_seconds() / 60.0, 2) if kickoff_utc else None
    t45_blocked = lead_minutes is not None and lead_minutes < 45.0
    t15_blocked = lead_minutes is not None and lead_minutes < 15.0

    composite = evaluate_prematch_temporal_policy(
        scouting_start_utc=analysis_cutoff_utc or execution_utc,
        evaluation_start_utc=execution_utc,
        formal_decision_utc=execution_utc,
        kickoff_utc=kickoff_utc or execution_utc,
        bwin_captured_at_utc=captured_at_utc,
        lineup_status=lineup_status,
    ) if kickoff_utc else {}

    return {
        "analysis_cutoff_utc": analysis_cutoff_utc,
        "execution_utc": execution_utc.isoformat(),
        "bwin_captured_at_utc": captured_at_utc,
        "kickoff_utc": kickoff_utc.isoformat() if kickoff_utc else None,
        "lead_minutes_to_kickoff": lead_minutes,
        "lineup_status": lineup_status,
        "productive_rules_preserved": {
            "T_45_rule_active": True,
            "T_45_would_block_productive_bet": t45_blocked,
            "T_15_operational_cutoff_active": True,
            "T_15_would_block_productive_bet": t15_blocked,
        },
        "composite_temporal_policy_v2": composite,
    }


def refresh_expedient_with_lineups(
    *,
    scout_report: dict[str, Any],
    fixture_id: int,
    event_id: str | None,
    home_id: int,
    away_id: int,
    home_name: str,
    away_name: str,
    kickoff_utc: str | datetime,
    now_utc: str | datetime,
    api_lineups_response: Any = None,
    manual_lineup: dict[str, Any] | None = None,
    qualitative_markdown: str | None = None,
    target_status: str = "NS",
    watch_players: list[str] | None = None,
) -> dict[str, Any]:
    """Refresca un expediente al llegar los XI sin perder las hipótesis tempranas ni convertirlo en LIVE (Sección 8)."""
    from bet_engine.context.lineup_manager import (
        evaluate_fixture_lineup_state,
        is_confirmed_complete,
    )

    ko_dt = _parse_dt(kickoff_utc)
    now_dt = _parse_dt(now_utc)
    if ko_dt is None or now_dt is None:
        raise MarketComparisonError("LINEUP_REFRESH_INVALID_TIMESTAMPS")
    if str(target_status or "NS").strip().upper() in {"1H", "HT", "2H", "ET", "BT", "P", "FT", "AET", "PEN", "LIVE"} or now_dt >= ko_dt:
        raise MarketComparisonError("LINEUP_REFRESH_BLOCKED_LIVE_OR_POST_KICKOFF")

    lineup_eval = evaluate_fixture_lineup_state(
        api_lineups_response=api_lineups_response,
        manual_lineup=manual_lineup,
        qualitative_markdown=qualitative_markdown,
        fixture_id=fixture_id,
        event_id=str(event_id or ""),
        home_id=home_id,
        away_id=away_id,
        home_name=home_name,
        away_name=away_name,
        kickoff_utc=ko_dt,
        now_utc=now_dt,
    )

    # Extract verified starter/sub sets when API lineups are valid
    starters_by_name: dict[str, dict[str, Any]] = {}
    subs_by_name: dict[str, dict[str, Any]] = {}
    if isinstance(api_lineups_response, list):
        for blk in api_lineups_response:
            tid = (blk.get("team") or {}).get("id")
            if tid not in (home_id, away_id):
                continue
            side = "home" if tid == home_id else "away"
            for entry in blk.get("startXI") or []:
                p = entry.get("player") or {}
                nm = str(p.get("name") or "").strip().lower()
                if nm:
                    starters_by_name[nm] = {"player_id": p.get("id"), "name": p.get("name"), "side": side, "pos": p.get("pos"), "grid": p.get("grid")}
            for entry in blk.get("substitutes") or []:
                p = entry.get("player") or {}
                nm = str(p.get("name") or "").strip().lower()
                if nm:
                    subs_by_name[nm] = {"player_id": p.get("id"), "name": p.get("name"), "side": side, "pos": p.get("pos")}

    official_confirmed = is_confirmed_complete(lineup_eval.get("status"))
    player_exposure_roles: list[dict[str, Any]] = []
    for wp in watch_players or []:
        w_low = wp.strip().lower()
        if not official_confirmed:
            player_exposure_roles.append({"player": wp, "role": "EXPOSICION_INCIERTA", "source_status": lineup_eval.get("status")})
        elif any(w_low in k for k in starters_by_name):
            info = next(v for k, v in starters_by_name.items() if w_low in k)
            player_exposure_roles.append({"player": wp, "role": "TITULAR", **info})
        elif any(w_low in k for k in subs_by_name):
            info = next(v for k, v in subs_by_name.items() if w_low in k)
            player_exposure_roles.append({"player": wp, "role": "SUPLENTE", **info})
        else:
            player_exposure_roles.append({"player": wp, "role": "AUSENTE", "source_status": lineup_eval.get("status")})

    initial_hyps = (scout_report.get("analysis") or {}).get("hypotheses") or []
    lineup_changes: list[str] = []
    discarded_hyps: list[dict[str, Any]] = []
    new_conditions: list[str] = []

    if not official_confirmed:
        new_conditions.append(f"XI aún no confirmado oficialmente ({lineup_eval.get('status')}); mantener como hipótesis temprana.")
    else:
        for idx, h in enumerate(initial_hyps, start=1):
            tp = str(h.get("team_and_players") or "").lower()
            absent_hit = [r["player"] for r in player_exposure_roles if r["role"] in ("AUSENTE", "SUPLENTE") and r["player"].lower() in tp]
            if absent_hit:
                discarded_hyps.append({"hypothesis_index": idx, "reason": f"Jugador clave no titular tras XI oficial: {absent_hit}"})
                lineup_changes.append(f"Hipotesis {idx} afectada por ausencia/suplencia de {absent_hit}")
            else:
                new_conditions.append(f"Hipotesis {idx} compatible con XI oficial verificado ({lineup_eval.get('source_origin')}).")

    return {
        "schema_version": "1.0-lineup-refresh-audit",
        "refreshed_at_utc": now_dt.isoformat(),
        "fixture_id": fixture_id,
        "event_id": event_id,
        "lineup_status": lineup_eval.get("status"),
        "official_confirmed_22": official_confirmed,
        "lineup_evaluation": lineup_eval,
        "initial_hypotheses_count": len(initial_hyps),
        "initial_hypotheses_preserved": initial_hyps,
        "lineup_changes": lineup_changes,
        "discarded_hypotheses": discarded_hyps,
        "new_conditions": new_conditions,
        "player_exposure_roles": player_exposure_roles,
        "update_source": lineup_eval.get("source_origin"),
    }


def build_market_comparison_prompt(comp_input: dict[str, Any]) -> str:
    """Genera el prompt de segunda pasada de IA para comparar mercados sin ranking previo del motor."""
    ident = comp_input["identity"]
    matcher = comp_input["matcher_summary"]
    options = comp_input["candidate_options"]
    contrast = comp_input["evidence_summary"]

    lines = [
        f"# TACTICAL MARKET COMPARATOR (Segunda Pasada IA) — {ident['home']} vs {ident['away']}",
        "",
        "## Reglas Obligatorias",
        "- Modo: `EXPERIMENTAL_SHADOW` (Fase 2). **NO recibes ranking previo del motor; no autorizas BET.**",
        "- Distingue probabilidad implícita bruta (`1 / cuota`) de probabilidad real del evento.",
        "- Si `predictive_probability_status == PREDICTIVE_PROBABILITY_NOT_ACCREDITED`, **NO inventes `p_model` ni EV**.",
        "- Si `settlement_status == SETTLEMENT_UNVERIFIED`, **NO asumas equivalencia con contadores de API-Football**.",
        "- Si la evidencia no permite decidir qué línea ofrece mejor valor, concluye `INDETERMINATE_NO_CLEAR_VALUE_WINNER`.",
        "",
        "## Identidad y Control Temporal",
        f"- `fixture_id`: `{ident['fixture_id']}` | `event_id`: `{ident.get('event_id')}`",
        f"- Kickoff UTC: `{ident.get('kickoff_utc')}` | Captura Bwin: `{comp_input['temporal_audit'].get('bwin_captured_at_utc')}`",
        "",
        "## Preguntas Clave que Debe Responder la Comparación",
        "1. ¿Qué línea representa mejor el mecanismo futbolístico identificado en el scouting?",
        "2. ¿La producción depende principalmente de un equipo o es compartida?",
        "3. ¿Qué tiene que permitir el rival y qué cambia si el entrenador ajusta su estructura?",
        "4. ¿La diferencia de cuota compensa el incremento de exigencia entre líneas vecinas?",
        "5. ¿Qué dato falta (XI oficial, minutos de jugador, árbitro, liquidación) para decidir?",
        "6. ¿Qué mercado sería preferible estudiar antes que goles?",
        "",
        "## Hipótesis Tácticas Vinculadas",
        "```json",
        json.dumps(matcher, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Opciones Reales Bwin y Precios (Decimal + 1/cuota)",
        "```json",
        json.dumps(options, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Resumen Estadístico y Exposición Individual",
        "```json",
        json.dumps(contrast, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Formato JSON Esperado de Respuesta",
        "Devuelve un objeto JSON con: `comparisons` (lista de comparaciones con `pair_label`, `preferred_option_or_indeterminate`, `football_mechanism_fit`, `price_vs_requirement_analysis`, `counter_arguments`, `missing_data`), `exploratory_candidates` (lista con `selection_id`, `canonical_key`, `state` en `COMPARISON_STATES`, `hypothesis_index`, `rationale`, `critical_uncertainties`, `no_bet_category`), y `overall_conclusion`.",
    ]
    return "\n".join(lines)


def prepare_market_comparison_package(
    *,
    scout_report: dict[str, Any],
    bwin_catalog: dict[str, Any],
    matcher_result: dict[str, Any],
    evidence_bundle: dict[str, Any] | None,
    output_dir: Path,
    asof_utc: datetime | None = None,
    max_price_age_minutes: int = 180,
) -> dict[str, Any]:
    """Etapa A de Tarea E: prepara contexto y prompt de comparación de mercados sin ranking previo."""
    ident = scout_report.get("identity") or {}
    dossier_id = ident.get("dossier_id") or str(ident.get("event_id") or f"research_fixture_{ident.get('fixture_id')}")
    now_utc = asof_utc or datetime.now(timezone.utc)
    ko_dt = _parse_dt(ident.get("kickoff_utc"))

    cap_ts = bwin_catalog.get("captured_at_utc")
    price_val = evaluate_price_validity(
        captured_at_utc=cap_ts,
        asof_utc=now_utc,
        kickoff_utc=ko_dt,
        max_age_minutes=max_price_age_minutes,
    )

    # Collect unique offered + nearby lines from matcher links
    seen_sids: set[str] = set()
    candidate_options: list[dict[str, Any]] = []
    for link in matcher_result.get("links") or []:
        for e in (link.get("offered_bwin_lines") or []) + (link.get("nearby_alternatives") or []):
            sid = str(e.get("selection_id") or "")
            if sid and sid in seen_sids:
                continue
            seen_sids.add(sid)
            candidate_options.append(build_comparison_option(e, price_validity=price_val))

    # Build compact evidence summary for the comparator
    ev_summary: dict[str, Any] = {"available": False}
    if evidence_bundle:
        from bet_engine.context.evidence_aggregator import (
            _find_player_record,
            build_player_fouls_duel_context,
            build_player_shots_context,
        )

        fouls_contexts: list[dict[str, Any]] = []
        shots_contexts: list[dict[str, Any]] = []
        seen_foul_players: set[str] = set()
        seen_shot_players: set[str] = set()
        for opt in candidate_options:
            if opt.get("target_scope") != "PLAYER" or not opt.get("target_entity"):
                continue
            pname = str(opt["target_entity"])
            p_side = None
            for s_key in ("home", "away"):
                if _find_player_record((evidence_bundle.get("players") or {}).get(s_key) or {}, pname):
                    p_side = s_key
                    break
            if not p_side:
                continue
            if opt.get("market_family") == "PLAYER_FOULS_COMMITTED" and pname.lower() not in seen_foul_players:
                seen_foul_players.add(pname.lower())
                fouls_contexts.append(
                    build_player_fouls_duel_context(
                        evidence_bundle=evidence_bundle,
                        player_name=pname,
                        side=p_side,
                        bwin_catalog=bwin_catalog,
                    )
                )
            elif opt.get("market_family") in ("PLAYER_SHOTS", "PLAYER_ON_TARGET") and pname.lower() not in seen_shot_players:
                seen_shot_players.add(pname.lower())
                shots_contexts.append(
                    build_player_shots_context(
                        evidence_bundle=evidence_bundle,
                        player_name=pname,
                        side=p_side,
                    )
                )

        ev_summary = {
            "available": True,
            "raw_fixtures_loaded": evidence_bundle.get("raw_fixtures_loaded"),
            "opponent_contrast": {
                m: {
                    "home_own_home_mean": (c["home_production_at_home"] or {}).get("mean"),
                    "home_own_home_n": (c["home_production_at_home"] or {}).get("n"),
                    "away_conc_away_mean": (c["away_conceded_away"] or {}).get("mean"),
                    "away_conc_away_n": (c["away_conceded_away"] or {}).get("n"),
                    "away_own_away_mean": (c["away_production_away"] or {}).get("mean"),
                    "away_own_away_n": (c["away_production_away"] or {}).get("n"),
                    "home_conc_home_mean": (c["home_conceded_at_home"] or {}).get("mean"),
                    "home_conc_home_n": (c["home_conceded_at_home"] or {}).get("n"),
                }
                for m, c in (evidence_bundle.get("opponent_contrast") or {}).items()
            },
            "players_count": {
                "home": len((evidence_bundle.get("players") or {}).get("home") or {}),
                "away": len((evidence_bundle.get("players") or {}).get("away") or {}),
            },
            "player_fouls_duels": fouls_contexts,
            "player_shots_limited": shots_contexts,
        }

    temporal_audit = build_temporal_control_audit(
        analysis_cutoff_utc=scout_report.get("generated_at_utc"),
        execution_utc=now_utc,
        captured_at_utc=cap_ts,
        kickoff_utc=ko_dt,
    )

    comp_input = {
        "schema_version": "1.0-market-comparison-input",
        "mode": "EXPERIMENTAL_SHADOW",
        "generated_at_utc": now_utc.isoformat(),
        "identity": ident,
        "temporal_audit": temporal_audit,
        "price_validity": price_val,
        "matcher_summary": [
            {
                "hypothesis_index": l["hypothesis_index"],
                "tactical_mechanism": l["tactical_mechanism"],
                "evidence_quality": l["evidence_quality"],
                "match_status": l["match_status"],
                "offered_count": l["offered_bwin_lines_count"],
                "nearby_count": l["nearby_alternatives_count"],
                "missing_data": l["missing_data"],
            }
            for l in (matcher_result.get("links") or [])
        ],
        "candidate_options": candidate_options,
        "evidence_summary": ev_summary,
    }

    prompt_md = build_market_comparison_prompt(comp_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    in_path = output_dir / f"market_comparison_input_{dossier_id}.json"
    pr_path = output_dir / f"market_comparison_prompt_{dossier_id}.md"
    in_path.write_text(json.dumps(comp_input, ensure_ascii=False, indent=2), encoding="utf-8")
    pr_path.write_text(prompt_md, encoding="utf-8")

    return {
        "comparison_input": comp_input,
        "prompt_markdown": prompt_md,
        "input_json_path": str(in_path),
        "prompt_md_path": str(pr_path),
    }


def _validate_comparison_ai(ai_doc: dict[str, Any]) -> None:
    if not isinstance(ai_doc, dict):
        raise MarketComparisonError("COMPARISON_AI_OUTPUT_MUST_BE_OBJECT")
    if ai_doc.get("actionable_bet") is True or ai_doc.get("bet_authorized") is True:
        raise MarketComparisonError("PHASE2_CANNOT_AUTHORIZE_BET")
    for k in ("p_model", "calibrated_p_model", "ev", "stake"):
        if ai_doc.get(k) is not None:
            raise MarketComparisonError(f"PHASE2_FORBIDDEN_UNACCREDITED_METRIC:{k}")

    cands = ai_doc.get("exploratory_candidates") or []
    if not isinstance(cands, list):
        raise MarketComparisonError("EXPLORATORY_CANDIDATES_MUST_BE_LIST")
    for idx, c in enumerate(cands):
        if not isinstance(c, dict):
            raise MarketComparisonError(f"CANDIDATE_{idx}_NOT_OBJECT")
        st = str(c.get("state") or "")
        if st not in COMPARISON_STATES:
            raise MarketComparisonError(f"CANDIDATE_{idx}_INVALID_STATE:{st}")
        if c.get("actionable_bet") is True or c.get("bet_authorized") is True:
            raise MarketComparisonError(f"CANDIDATE_{idx}_CANNOT_AUTHORIZE_BET")


def build_no_bet_ledger(
    *,
    comp_input: dict[str, Any],
    ai_doc: dict[str, Any],
) -> dict[str, Any]:
    """Tarea G: clasifica claramente expedientes incompletos, fallos, candidatos y NO BET sin mezclar contadores."""
    cands = ai_doc.get("exploratory_candidates") or []
    pv = comp_input.get("price_validity", {}).get("status")
    ev_avail = comp_input.get("evidence_summary", {}).get("available", False)

    counts = {k: 0 for k in NO_BET_AND_OPERATIONAL_CATEGORIES}
    if not ev_avail:
        counts["EXPEDIENTE_INCOMPLETO"] += 1
        counts["FALTA_COBERTURA_HISTORICA"] += 1
    if pv in ("PRICE_UNVERIFIED", "PRICE_EXPIRED", "PRICE_INVALID_POST_KICKOFF"):
        counts["CUOTA_NO_DISPONIBLE"] += 1

    exploratory_count = 0
    for c in cands:
        st = c.get("state")
        if st == "EXPLORATORY_CANDIDATE":
            exploratory_count += 1
        elif st == "LINEUP_DEPENDENCY_PENDING":
            counts["ALINEACION_PENDIENTE"] += 1
        elif st == "PREDICTIVE_CALIBRATION_PENDING":
            counts["CALIBRACION_PREDICTIVA_PENDIENTE"] += 1
        elif st == "CONTRADICTED_HYPOTHESIS":
            counts["HIPOTESIS_TACTICA_CONTRADICHA"] += 1
        elif st == "PRICE_UNVERIFIED":
            counts["CUOTA_NO_DISPONIBLE"] += 1
        elif st == "INSUFFICIENT_EVIDENCE":
            counts["FALTA_COBERTURA_HISTORICA"] += 1
        elif st == "UNSUPPORTED_MARKET":
            counts["REGLA_COMERCIAL_NO_CONCILIADA"] += 1

        nb_cat = c.get("no_bet_category")
        if nb_cat in counts:
            counts[nb_cat] += 1

    counts["DECISION_FORMAL_SIN_BET_AUTORIZADO"] += 1

    return {
        "complete_dossiers": 1 if ev_avail else 0,
        "incomplete_dossiers": 0 if ev_avail else 1,
        "technical_failures": 0,
        "exploratory_candidates": exploratory_count,
        "statistical_no_bet_decisions": counts["RECHAZO_ESTADISTICO_JUSTIFICADO"],
        "formal_authorized_bets": 0,
        "detailed_breakdown": counts,
    }


def finalize_market_comparison(
    *,
    comp_input: dict[str, Any],
    ai_evaluation: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Etapa B de Tarea E: valida la evaluación de IA, genera estados y persiste JSON + Markdown."""
    _validate_comparison_ai(ai_evaluation)
    ident = comp_input["identity"]
    dossier_id = ident.get("dossier_id") or str(ident.get("event_id") or f"research_fixture_{ident.get('fixture_id')}")
    ledger = build_no_bet_ledger(comp_input=comp_input, ai_doc=ai_evaluation)

    report = {
        "schema_version": "1.0-market-comparison-report",
        "mode": "EXPERIMENTAL_SHADOW",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "identity": ident,
        "formal_bet_authorized": False,
        "temporal_audit": comp_input["temporal_audit"],
        "price_validity": comp_input["price_validity"],
        "no_bet_and_operational_ledger": ledger,
        "evaluation": ai_evaluation,
    }

    md_lines = [
        f"# TACTICAL MARKET COMPARISON — {ident.get('home')} vs {ident.get('away')}",
        "",
        f"- **Modo**: `EXPERIMENTAL_SHADOW` | **BET formal autorizado**: `False`",
        f"- **Vigencia de captura Bwin**: `{comp_input['price_validity']['status']}`",
        f"- **Regla T-45 productiva**: `ACTIVA` (`would_block={comp_input['temporal_audit']['productive_rules_preserved']['T_45_would_block_productive_bet']}`)",
        "",
        "## Comparaciones de Alternativas",
    ]
    for cmp_item in ai_evaluation.get("comparisons") or []:
        md_lines += [
            f"### {cmp_item.get('pair_label')}",
            f"- **Preferencia / Veredicto**: `{cmp_item.get('preferred_option_or_indeterminate')}`",
            f"- **Ajuste al mecanismo**: {cmp_item.get('football_mechanism_fit')}",
            f"- **Precio vs exigencia**: {cmp_item.get('price_vs_requirement_analysis')}",
            f"- **Contraargumentos**: {cmp_item.get('counter_arguments')}",
            f"- **Datos faltantes**: {cmp_item.get('missing_data')}",
            "",
        ]
    md_lines.append("## Candidatos Exploratorios Auditables")
    for c in ai_evaluation.get("exploratory_candidates") or []:
        md_lines.append(f"- `{c.get('canonical_key')}` [`{c.get('state')}`]: {c.get('rationale')}")

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"market_comparison_{dossier_id}.json"
    md_path = output_dir / f"market_comparison_{dossier_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    return {
        "report": report,
        "report_json_path": str(json_path),
        "report_md_path": str(md_path),
    }
