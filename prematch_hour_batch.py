"""Commercial screening and shortlist refresh on an existing PREMATCH run."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from bet_engine.context.hour_batch_players import link_player_props, load_player_history
from bet_engine.markets.bwin_catalog import COLLECTIVE_COUNT_METRICS, extract_bwin_catalog
from bet_engine.markets.tactical_market_comparator import build_comparison_option, evaluate_price_validity

CAPTURE_MIN_LEAD = 45
FINAL_CAPTURE_MIN_LEAD = 30
CONFIRMED_XI_MIN_LEAD = 5
LINEUP_CHECK_LEAD = 75
SCREEN_PRICE_AGE = 180
FINAL_PRICE_AGE = 20
MAX_PREFINALISTS = 4


def _window_bounds(run, steps):
    path = steps._require_file(run / "fixtures.json", "discover")
    window = steps._read_json(path).get("window") or {}

    def civil(key):
        stamp = str(window.get(key) or "").split(" ")[0]
        if not stamp:
            raise ValueError(f"HOUR_BATCH_MISSING_ORIGINAL_WINDOW:{path}")
        value = datetime.fromisoformat(stamp)
        return value.astimezone(steps.trial.TZ) if value.tzinfo else value.replace(tzinfo=steps.trial.TZ)

    return civil("start_madrid"), civil("end_madrid")


def _scope(args, steps, now):
    root, run, _, _ = steps._resolve_eval_dir(args, now)
    begin, end = _window_bounds(run, steps)
    day = begin.date()
    days = []
    while day <= end.date():
        slot = datetime.combine(day, args.hour, tzinfo=steps.trial.TZ)
        if slot < end and slot + timedelta(hours=1) > begin:
            days.append(slot)
        day += timedelta(days=1)
    if args.date:
        days = [slot for slot in days if slot.date() == steps.df.parse_day(args.date)]
    if len(days) != 1:
        raise ValueError("HOUR_OUTSIDE_OR_AMBIGUOUS_IN_RUN: comprueba --hour y, si procede, --date Madrid")
    slot = days[0]
    return root, run, slot, run / "hour_batches" / slot.strftime("%Y-%m-%d_%H%M")


def _batch_end(run, steps, slot):
    """Return the exclusive end of the lot inside the original civil window."""
    _, window_end = _window_bounds(run, steps)
    # Late-night runs intentionally cover the remainder of the original window.
    # In the usual 15:00–03:00 run, 23:00, 00:00, 01:00 and 02:00 all end at 03:00.
    if slot.hour >= 23 or (slot.date() == window_end.date() and slot < window_end):
        return window_end
    return min(slot + timedelta(hours=2), window_end)


def _kickoff(steps, target):
    return steps._now_utc(target.get("kickoff_utc") or target["kickoff_madrid"])


def _lead(steps, target, now):
    return (_kickoff(steps, target) - now).total_seconds() / 60


def _capture(steps, root, run, target, *, min_lead=CAPTURE_MIN_LEAD):
    args = argparse.Namespace(
        command="odds-capture", root=root, eval_dir=run, asof=None,
        fixture_id=[target["fixture_id"]], event_id=target["bwin_event_id"],
        min_lead=min_lead, snapshot_file=None, confirm_external=True,
        headless=True, timeout=25000,
    )
    steps.cmd_odds_capture(args)
    return steps._resolve_odds_snapshot(run, target, steps._now_utc(), required=True)


def _capture_final(steps, root, run, target):
    args = argparse.Namespace(
        command="odds-capture", root=root, eval_dir=run, asof=None,
        fixture_id=[target["fixture_id"]], event_id=target["bwin_event_id"],
        min_lead=FINAL_CAPTURE_MIN_LEAD, snapshot_file=None, confirm_external=True,
        headless=True, timeout=25000,
    )
    steps.cmd_odds_capture(args)
    return steps._resolve_odds_snapshot(run, target, steps._now_utc(), required=True)


def _catalog(target, snapshot):
    return extract_bwin_catalog(
        Path(snapshot["path"]), event_id=str(target["bwin_event_id"]),
        fixture_id=target["fixture_id"], home_name=target["home"], away_name=target["away"],
        captured_at_utc=snapshot["captured_at_utc"],
        diagnostics_path=Path(snapshot["diagnostics_path"]) if snapshot["diagnostics_path"] else None,
    )


def _price(catalog, steps, target, now, max_age):
    return evaluate_price_validity(captured_at_utc=catalog["captured_at_utc"], asof_utc=now,
                                   kickoff_utc=_kickoff(steps, target), max_age_minutes=max_age)


def _usable_offer(entry):
    return entry.get("odd") is not None and entry.get("verdict") not in {
        "INAPPLICABLE_PREMATCH", "CONFLICTING_CAPTURE_PRICES",
    }


def _verified_catalog_prices(entries):
    prices = defaultdict(set)
    for entry in entries:
        key = entry.get("canonical_key")
        if (key and _usable_offer(entry) and entry.get("contract_id")
                and entry["settlement_status"] == "VERIFIED_STANDARD"):
            prices[key].add(Decimal(entry["odd"]))
    conflicts = {key: [str(value) for value in sorted(values)] for key, values in prices.items() if len(values) > 1}
    return {key: float(next(iter(values))) for key, values in prices.items() if len(values) == 1}, conflicts


def _card_review(entry, pre_match):
    """Use paired yellows as context, with the Bwin contract kept separate."""
    family = entry["market_family"]
    if not family.startswith("CARDS_") or entry["settlement_status"] != "SETTLEMENT_UNVERIFIED":
        return None
    if family == "CARDS_BOTH_TEAMS" and (not entry["line"] or entry["direction"] not in {"yes", "no"}):
        return None
    review = {"status": "EXPLORATORY_SETTLEMENT_UNVERIFIED",
              "referee": pre_match.get("referee") or None,
              "evidence": "FULL_TIME_YELLOW_CARDS_PROXY" if entry["temporal_scope"] == "FT" else "NO_PERIOD_SPECIFIC_CARD_EVIDENCE"}
    if entry["temporal_scope"] != "FT":
        return review
    if family == "CARDS_BOTH_TEAMS":
        threshold = int(entry["line"])
        samples = {}
        for side in ("home", "away"):
            pairs = {}
            for row in (pre_match.get("statistical_observations") or {}).get(side) or []:
                if row.get("status") != "FT" or row.get("league_id") != pre_match.get("league_id"):
                    continue
                own = (row.get("own") or {}).get("yellow")
                conceded = (row.get("conceded") or {}).get("yellow")
                if row.get("fixture_id") and all(type(value) in (int, float) and value >= 0 for value in (own, conceded)):
                    pairs[row["fixture_id"]] = own >= threshold and conceded >= threshold
            samples[side] = {"n_valid": len(pairs), "both_reached_threshold": sum(pairs.values())}
        review["both_yellows_proxy_by_team_history"] = samples
    return review


def _market_options(allowed, target, pre_match, tactical, now, price_status, player_options):
    from bet_engine.context.observation_store import ObservationStore
    from bet_engine.markets.bwin_evaluator import BwinMarketEvaluator
    from bet_engine.markets.staking_and_policy import (
        compute_market_devig_map,
        evaluate_collective_quant_policy,
        evaluate_player_prop_quant,
        is_cross_league_club_competition,
    )
    from bet_engine.modeling.distributions import CoherentMatchModeler
    from bet_engine.modeling.tactical_enhancements import enhance_match_model
    from prematch_tactical_signal_engine import classify_market

    bundle = ObservationStore.from_preselection_match(pre_match, cutoff_utc=now)
    model = enhance_match_model(bundle, CoherentMatchModeler.build_model(bundle))
    cross_league = is_cross_league_club_competition(
        pre_match.get("league") or target.get("league"),
        target.get("country"),
    )
    accredited_fams = frozenset({"GOALS_FT"}) if not cross_league else frozenset()
    # Use the exact catalogued price and rule. Conflicting quotes for the same
    # canonical key stay exploratory rather than selecting the favourable price.
    odds, _ = _verified_catalog_prices(allowed)
    devig_map = compute_market_devig_map(odds)
    evaluated = {
        opt.selection_key: opt
        for opt in BwinMarketEvaluator.evaluate_standard_markets(
            bundle,
            model,
            odds_map=odds,
            calibration_accredited_families=accredited_fams,
            allow_central_ev_authorization=True,
        )
        if opt.odd is not None and opt.decision_category != "MERCADO_NO_MODELADO"
    }
    non_actionable_families = {"EXACT_SCORE", "EXACT_SCORE_H1", "EXACT_SCORE_H2", "COMBO_OR_HTFT"}
    signals = [s for s in tactical.get("signals") or [] if s.get("status") == "SUPPORTED_DESCRIPTIVE_SIGNAL"]
    options = []
    for entry in allowed:
        if entry["market_family"] in non_actionable_families:
            continue
        if entry["selection_id"] in player_options:
            p_ev = player_options[entry["selection_id"]]
            odd_num = float(entry["odd"]) if entry.get("odd") is not None else None
            options.append({
                **build_comparison_option(entry, price_validity=price_status),
                "signals": [], "direction": entry["direction"], "contract_id": entry.get("contract_id"),
                "model_evidence": None, "player_evidence": p_ev,
                "quant_policy": evaluate_player_prop_quant(
                    p_ev,
                    odd=odd_num,
                    market_family=entry["market_family"],
                    lineup_confirmed=False,
                ),
            })
            continue
        if entry["target_scope"] not in {"MATCH", "HOME", "AWAY"}:
            continue
        match = classify_market(entry["market_header_literal"], target)
        metric = COLLECTIVE_COUNT_METRICS.get(entry["market_family"])
        if metric:
            match = (metric, None if entry["target_scope"] == "MATCH" else entry["target_scope"].lower())
        linked = []
        if match and entry["temporal_scope"] == "FT" and entry["line"] is not None:
            metric, side = match
            linked = [
                {"metric": metric, "side": signal["team_side"], "score": signal["priority_score"],
                 "relation": "DIRECT_TEAM_MARKET" if side else "INDIRECT_TEAM_TO_MATCH_TOTAL",
                 "production": {k: signal["own"].get(k) for k in ("mean", "n")},
                 "concession": {k: signal["opponent_conceded"].get(k) for k in ("mean", "n")}}
                for signal in signals if signal["metric"] == metric and side in (None, signal["team_side"])
            ]
        option = evaluated.get(entry.get("canonical_key"))
        if option and (not entry.get("contract_id") or option.odd != float(entry["odd"])
                       or entry["settlement_status"] != "VERIFIED_STANDARD"):
            option = None
        card_review = _card_review(entry, pre_match)
        if not (linked or option or card_review):
            continue
        accredited = bool(option and option.calibration_status == "CALIBRATED_OUT_OF_SAMPLE"
                          and option.raw_sample_n is not None and option.raw_sample_n >= 12
                          and option.p_model is not None and option.p_low is not None)
        row = build_comparison_option(entry, price_validity=price_status,
                                      accredited_p_model=option.p_model if accredited else None)
        row["signals"] = linked
        row["direction"] = entry["direction"]
        row["contract_id"] = entry.get("contract_id")
        if entry.get("settlement_provider"):
            row["settlement_provenance"] = {key: entry.get(key) for key in (
                "settlement_provider", "settlement_basis", "historical_evidence_provider",
                "provider_reconciliation_status", "post_event_correction_policy",
            )}
        if card_review:
            row["card_review"] = card_review
        row["model_evidence"] = ({
            "calibration_status": option.calibration_status, "raw_n": option.raw_sample_n,
            "effective_n": option.effective_sample_n,
            "p_model": option.p_model if accredited else None,
            "ev_model": option.ev_model if accredited and price_status["status"] == "VALID_PREMATCH" else None,
            "p_low": option.p_low if accredited else None,
            "p_high": option.p_high if accredited else None,
            "conservative_ev": option.ev_conservative_low if accredited and price_status["status"] == "VALID_PREMATCH" else None,
            "diagnostic_uncalibrated": (
                {"estimate": option.p_model, "low": option.p_low, "high": option.p_high, "ev_central": option.ev_model}
                if not accredited and (option.raw_sample_n or 0) >= 4 and option.p_model is not None else None
            ),
            **(
                {
                    "equivalent_best_selection": option.equivalent_best_selection,
                    "equivalent_best_odd": option.equivalent_best_odd,
                }
                if option.equivalent_best_selection is not None and option.equivalent_best_odd is not None
                else {}
            ),
        } if option else None)
        row["quant_policy"] = evaluate_collective_quant_policy(
            market_family=option.market_family if option else entry["market_family"],
            direction=entry.get("direction"),
            temporal_scope=entry.get("temporal_scope"),
            odd=float(entry["odd"]) if entry.get("odd") is not None else None,
            p_model=option.p_model if option else None,
            p_low=option.p_low if option else None,
            p_high=option.p_high if option else None,
            raw_sample_n=option.raw_sample_n if option else None,
            effective_sample_n=option.effective_sample_n if option else None,
            devig_info=devig_map.get(entry.get("canonical_key") or ""),
            is_cross_league=cross_league,
            tactical_warnings=option.tactical_warnings if option else (),
            settlement_verified=(entry.get("settlement_status") == "VERIFIED_STANDARD"),
        )
        options.append(row)
    return sorted(options, key=_option_order)


def _option_order(row):
    qp = row.get("quant_policy") or {}
    leak_blocked = bool(qp.get("leak_guard_blocked"))
    q_auth = bool(qp.get("bet_authorized") or str(qp.get("status") or "").startswith("CANDIDATO_PROP_PENDIENTE_XI"))
    q_ev = qp.get("ev_model")
    ev = (row.get("model_evidence") or {}).get("conservative_ev")
    direct = any(s["relation"] == "DIRECT_TEAM_MARKET" for s in row["signals"])
    return (
        leak_blocked,
        not q_auth,
        ev is None and (q_ev is None or q_ev <= 0),
        -(ev if ev is not None else (q_ev or -999.0)),
        not direct,
        -max((s["score"] for s in row["signals"]), default=0),
        row["market_header_literal"],
        row["selection_literal"],
    )


def _target_row(target):
    return {key: target[key] for key in ("fixture_id", "bwin_event_id", "home", "away", "kickoff_madrid", "kickoff_utc")}


def _write_markdown(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_agent_prompt(prompt, output, lines, *, instruction_name):
    instruction = (f'Lee "{prompt}" y los archivos locales que referencia. '
                   f'Sigue el encargo y guarda el resultado directamente en "{output}".')
    instruction_file = prompt.with_name(instruction_name)
    _write_markdown(instruction_file, [instruction])
    _write_markdown(prompt, [
        lines[0], "", "## Instrucción para enviar al agente local", "",
        "```text", instruction, "```", "", "## Encargo", "", *lines[2:],
    ])
    return instruction_file, instruction


def _todo_view(steps, snapshot):
    path = (snapshot or {}).get("diagnostics_path")
    return (steps._read_json(Path(path)).get("vista_todo") or {}) if path else {}


def _prepare_event(steps, args, root, run, output, target, position, player_history):
    now = steps._now_utc()
    if not target["temporal_eligible"] or _lead(steps, target, now) < steps.ANALYSIS_MIN_LEAD_MINUTES:
        return {"status": "BLOQUEO_OPERATIVO", "reason": target.get("temporal_reason") or "ANALYSIS_LEAD_BELOW_30"}
    pre_path, pre, tactical_path, tactical = steps._fixture_statistical_inputs(run, target, now)
    if pre is None:
        return {"status": "HISTORICO_PENDIENTE", "reason": "FIXTURE_NOT_IN_PRESELECTION: ejecuta stats-fetch y patterns"}
    snapshot = steps._resolve_odds_snapshot(run, target, now)
    captured = steps._now_utc(snapshot["captured_at_utc"]) if snapshot and snapshot["captured_at_utc"] else None
    expired = captured is None or captured < now - timedelta(minutes=SCREEN_PRICE_AGE)
    todo = _todo_view(steps, snapshot)
    needs_todo = args.refresh_todo and todo.get("status") != "TODO_ACTIVO_EN_DOM"
    can_capture = _lead(steps, target, now) >= CAPTURE_MIN_LEAD
    if (expired or needs_todo) and args.confirm_external and can_capture:
        snapshot = _capture(steps, root, run, target)
        todo = _todo_view(steps, snapshot)
        needs_todo = args.refresh_todo and todo.get("status") != "TODO_ACTIVO_EN_DOM"
    if not snapshot:
        return {"status": "CAPTURA_PENDIENTE", "reason": "NO_RUN_CAPTURE_OR_CAPTURE_LEAD_BELOW_45"}
    catalog = _catalog(target, snapshot)
    now = steps._now_utc()
    if _lead(steps, target, now) < steps.ANALYSIS_MIN_LEAD_MINUTES:
        return {"status": "BLOQUEO_OPERATIVO", "reason": "ANALYSIS_LEAD_BELOW_30", "snapshot": snapshot}
    price_status = _price(catalog, steps, target, now, SCREEN_PRICE_AGE)
    offers = [entry for entry in catalog["entries"] if _usable_offer(entry)]
    player_options, player_coverage, player_sources = link_player_props(offers, player_history, pre, now)
    options = _market_options(offers, target, pre, tactical, now, price_status, player_options)
    event_dir = output / "events" / str(target["fixture_id"])
    steps._prepare_research_inbox(event_dir / "research" / "responses", str(target["bwin_event_id"]))
    path = event_dir / "catalogo.json"
    steps._atomic_json(path, catalog)
    reason = None
    if needs_todo:
        reason = "TODO_VIEW_NOT_VERIFIED" if can_capture else "TODO_REFRESH_LEAD_BELOW_45"
    elif not offers:
        reason = "NO_USABLE_OFFERS_IN_CAPTURE"
    elif not options:
        reason = "NO_STATISTICAL_LINKS_FOR_CAPTURED_MARKETS"
    elif price_status["status"] != "VALID_PREMATCH":
        reason = price_status["status"]
    return {
        "status": "REVISABLE" if options and price_status["status"] == "VALID_PREMATCH" and not needs_todo else "COBERTURA_LIMITADA",
        "reason": reason,
        "all_markets_view_status": todo.get("status", "TODO_VIEW_NOT_RECORDED"),
        "statistical_position": position, "snapshot": snapshot, "catalog": str(path),
        "statistical_sources": {
            name: {"path": str(source), "sha256": steps._sha256_file(source)}
            for name, source in (("preselection", pre_path), ("tactical", tactical_path)) if source
        },
        "football_context": {
            "competition": pre.get("league"), "season": pre.get("season"),
            "referee": pre.get("referee") or target.get("referee"),
            "statistics_cutoff_utc": pre.get("analysis_cutoff_utc"),
            "standings": {side: (pre.get(f"{side}_evidence") or {}).get("standings")
                          for side in ("home", "away")},
        },
        "price_status": price_status, "catalog_coverage": catalog["counts_by_commercial_coverage"],
        "catalog_by_family": catalog["coverage_by_family"],
        "capture_coverage": catalog["capture_coverage"],
        "player_prop_coverage": player_coverage, "player_history_sources": player_sources,
        "priced_and_linked_options": options, "contradictions": tactical.get("contradictions") or [],
    }


def _market_review_rules():
    return [
        "Inventaría todas las familias de los catálogos. Usa priced_and_linked_options como cruce estadístico, "
        "y revisa las cuotas del catálogo que quedaron fuera del cruce. Resume por familia con recuentos y exclusiones.",
        "Dentro de tarjetas y córners distingue total, equipo, ambos equipos con umbral individual, quién recibe o consigue más, "
        "primera parte, segunda parte y jugador. Aplica el mismo control de periodo, alcance y liquidación a tiros, faltas y otros mercados.",
        "Indica si cada familia está solo capturada, enlazada con evidencia descriptiva, sin datos del periodo, "
        "pendiente de liquidación o respaldada por un modelo acreditado. Registra qué dato falta en cada caso.",
        "Para cada candidato, empieza por la mejor explicación futbolística a favor y busca evidencia fechada que la refuerce.",
        "Cuando el histórico lo permita, contrasta forma reciente con muestras de 10 y 20 partidos. Separa liga, localía, "
        "periodo y etapa del entrenador. Usa 30 solo si están disponibles y siguen siendo comparables. Indica n válido y datos ausentes.",
        "Indica qué cambio concreto de XI, rol o precio cambiaría el análisis. Un vínculo indirecto equipo→total requiere justificar la contribución del rival.",
        "En cada candidato escribe requires_confirmed_xi como booleano. Usa false solo si es un mercado colectivo y "
        "la tesis se sostiene con las variantes plausibles de XI. Explica esas variantes en lineup_condition. "
        "Usa true si depende de una titularidad, un rol, minutos o una estructura aún por confirmar.",
        "Compara el precio con una estimación acreditada para el mismo contrato. Usa 1/cuota como umbral bruto en contratos binarios sin devolución.",
        "Revisa `quant_policy` en cada opción: prioriza contratos con `bet_authorized = true` o `CANDIDATO_PROP_PENDIENTE_XI` (`GOALS_FT` con n>=12 y EV>=+5%, "
        "y `PLAYER_PROPS` de tiros/faltas con muestra titular). Descarta opciones con `leak_guard_blocked = true` (Córners UNDER con n<14, 1ª parte con muestra corta "
        "y 1X2/DNB/DC inter-liga sin ajuste Elo) y no repitas dos contratos del mismo `correlation_axis` en un mismo partido.",
        "Trata diagnostic_uncalibrated y card_review como exploratorios. Distingue amarillas de API-Football de tarjetas liquidadas en Bwin.",
        "Para props de jugador, revisa player_prop_coverage y player_evidence. Coteja identidad, competición, minutos, "
        "titularidad histórica, posición, muestra válida y ausencias de datos para el umbral exacto. "
        "Un 2+ incluye exactamente 2. Amarillas es una referencia parcial para recibir una tarjeta.",
        "Relaciona el prop con el rival y el papel previsto del jugador. Indica la condición de XI, rol y exposición "
        "que lo mantendría como hipótesis, y qué lo descartaría. Un resultado histórico del tipo 4 de 6 es descriptivo. "
        "Los props enlazados solo se pueden incluir como prefinalistas exploratorios mientras falte acreditar "
        "la liquidación o la estimación probabilística.",
        "Selecciona hasta tres prefinalistas y un suplente del cruce cuando proceda. Usa solo fixture_id y selection_id de priced_and_linked_options "
        "en fixtures REVISABLE. Las demás cuotas quedan en el inventario. Reserva PICK, NO BET y stake para el cierre.",
    ]


def _export_gemini(steps, output, packet, response):
    """A portable inventory without workstation paths, with a row for every quote."""
    fields = ("selection_id", "market_header_literal", "selection_literal", "odd", "market_family", "temporal_scope",
              "target_scope", "target_entity", "direction", "line", "settlement_status", "verdict", "contract_id")
    fixtures = []
    for row in packet["fixtures"]:
        exported = {k: row[k] for k in ("fixture_id", "bwin_event_id", "home", "away", "kickoff_madrid", "status",
                    "reason", "catalog_by_family", "football_context", "priced_and_linked_options", "contradictions",
                    "all_markets_view_status", "player_prop_coverage") if k in row}
        if row.get("catalog"):
            catalog = steps._read_json(Path(row["catalog"]))
            exported["captured_at_utc"] = catalog["captured_at_utc"]
            exported["source_sha256"] = catalog["source_txt_sha256"]
            exported["quotes"] = [[entry.get(key) for key in fields] for entry in catalog["entries"]]
        fixtures.append(exported)
    inventory = output / "mercados_gemini.json"
    steps._atomic_json(inventory, {"packet_id": packet["packet_id"], "hour_madrid": packet["hour_madrid"],
                                  "quote_columns": fields, "fixtures": fixtures})
    prompt = output / "encargo_gemini.md"
    _write_markdown(prompt, [
        "# Revisión comercial del lote en Gemini", "",
        "Lee el archivo adjunto mercados_gemini.json. Cada fila de quotes usa el orden de quote_columns.",
        *_market_review_rules(),
        "Devuelve primero un inventario resumido por familia y después un bloque JSON separado con los prefinalistas.",
        "Incluye en cada candidato fixture_id, selection_id, role (prefinalist o reserve), case_for, lineup_condition, requires_confirmed_xi y watch_players (opcional).",
        f'Formato raíz {json.dumps({"packet_id": packet["packet_id"], "candidates": []})}',
    ])
    print(f"  inventario_gemini: {inventory}")
    print(f"  prompt_gemini: {prompt}")
    print(f"  guarda el bloque JSON de Gemini en: {response}")


def _build_auto_quant_shortlist(packet):
    """Genera un prefinalistas_quant_auto.json determinista y libre de fugas (<1s)
    para operar cuando no hay margen horario para esperar DeepSearch o pase LLM de Fase 1.
    Desduplica por (fixture_id, correlation_axis) y limita a 2 ejes independientes por partido.
    """
    pool = []
    for fx in packet.get("fixtures") or []:
        if fx.get("status") != "REVISABLE":
            continue
        fid = fx["fixture_id"]
        for opt in fx.get("priced_and_linked_options") or []:
            qp = opt.get("quant_policy") or {}
            if qp.get("leak_guard_blocked"):
                continue
            is_prop_cand = str(qp.get("status") or "").startswith("CANDIDATO_PROP_PENDIENTE_XI")
            if not (qp.get("bet_authorized") or is_prop_cand):
                continue
            ev = float(qp.get("ev_model") or 0.0)
            stake = float(qp.get("raw_kelly_stake_pct") or qp.get("projected_stake_if_starter_pct") or 0.0)
            if ev < 0.05 or stake <= 0.0:
                continue
            pool.append((round(ev * stake, 6), ev, fx, opt, qp, is_prop_cand))

    pool.sort(key=lambda item: (-item[0], -item[1], item[2]["fixture_id"], item[3]["selection_id"]))
    chosen = []
    seen_axis = set()
    per_fixture_count = defaultdict(int)
    for _, ev, fx, opt, qp, is_prop in pool:
        fid = fx["fixture_id"]
        axis = str(qp.get("correlation_axis") or "OTHER")
        if (fid, axis) in seen_axis or per_fixture_count[fid] >= 2:
            continue
        seen_axis.add((fid, axis))
        per_fixture_count[fid] += 1
        fair = qp.get("fair_odd")
        min_odd = round(max(1.25, float(fair) * 1.04), 2) if fair else None
        role = "prefinalist" if len(chosen) < 3 else "reserve"
        if is_prop:
            pe = opt.get("player_evidence") or {}
            case_txt = (
                f"[AUTO-QUANT] {opt['market_header_literal']} -> {opt['selection_literal']} @ {opt['odd']} | "
                f"p_model={qp.get('p_model')} (tasa/90 ajustada={qp.get('shrunk_rate_per90')}, "
                f"n_titular={qp.get('starter_sample_n')}, min={qp.get('usable_minutes')}) | "
                f"EV={ev:+.1%} | cuota_justa={fair}."
            )
            cond_txt = (
                f"Exige titularidad oficial confirmada de {opt.get('target_entity')} (ID {pe.get('player_id')}) "
                f"y cuota de cierre no inferior a {min_odd:.2f}." if min_odd else
                f"Exige titularidad oficial confirmada de {opt.get('target_entity')} (ID {pe.get('player_id')})."
            )
            req_xi = True
            watch = [f"{opt.get('target_entity')} ({pe.get('player_id')})"] if pe.get("player_id") else []
        else:
            case_txt = (
                f"[AUTO-QUANT] {opt['market_header_literal']} -> {opt['selection_literal']} @ {opt['odd']} | "
                f"p_model={qp.get('p_model')} vs p_mkt_limpia={qp.get('p_market_fair')} "
                f"(overround={qp.get('market_overround')}, n={qp.get('raw_sample_n')}) | "
                f"EV={ev:+.1%} | cuota_justa={fair}."
            )
            cond_txt = (
                f"Mercado colectivo limpio de fugas; invalidar si la cuota cae por debajo de {min_odd:.2f}."
                if min_odd else "Mercado colectivo limpio de fugas."
            )
            req_xi = False
            watch = []
        cand_obj = {
            "fixture_id": fid,
            "selection_id": opt["selection_id"],
            "role": role,
            "case_for": case_txt,
            "lineup_condition": cond_txt,
            "requires_confirmed_xi": req_xi,
        }
        if min_odd is not None:
            cand_obj["min_odd"] = min_odd
        if watch:
            cand_obj["watch_players"] = watch
        chosen.append(cand_obj)
        if len(chosen) >= MAX_PREFINALISTS:
            break
    return {"packet_id": packet["packet_id"], "candidates": chosen}


def prepare(args, steps):
    now = steps._now_utc()
    root, run, slot, folder = _scope(args, steps, now)
    batch_end = _batch_end(run, steps, slot)
    _, major = steps._load_major_doc(run)
    rows = steps._bwin_reconcile_records(run, major, now, min_lead_minutes=0)["all"]
    ranked_path = run / "prospective_trial/tactical/tactical_signal_engine.json"
    ranked = steps._read_json(ranked_path) if ranked_path.is_file() else {}
    pre_path = run / "prospective_trial/enriched/preselection.json"
    rank_matches_history = (ranked.get("sources") or {}).get("preselection_sha256") == steps._sha256_file(pre_path)
    positions = {r["fixture_id"]: r.get("position") for r in ranked.get("statistical_ranking", [])} if rank_matches_history else {}
    members = [
        r for r in rows if r["identity_state"] == "VERIFIED" and (r.get("kickoff_utc") or r.get("kickoff_madrid"))
        and (not args.fixture_id or r["fixture_id"] in args.fixture_id)
        and slot <= _kickoff(steps, r).astimezone(steps.trial.TZ) < batch_end
    ]
    if not members:
        raise ValueError("NO_VERIFIED_FIXTURES_IN_HOUR")
    preselection = steps._read_json(pre_path) if pre_path.is_file() else {}
    member_ids = {target["fixture_id"] for target in members}
    historical_ids = {
        row["fixture_id"] for match in preselection.get("matches") or [] if match["fixture_id"] in member_ids
        for side in ("home", "away") for row in (match.get("statistical_observations") or {}).get(side, [])
        if row.get("fixture_id") is not None and row.get("status") == "FT"
    }
    ranked_sources = (ranked.get("sources") or {}) if rank_matches_history else {}
    player_history = load_player_history(preselection, ranked_sources, historical_ids, now) if historical_ids else {}
    packet_id = uuid4().hex
    output = folder / "packets" / packet_id
    results = []
    for target in sorted(members, key=lambda r: _kickoff(steps, r)):
        reason = None
        try:
            result = _prepare_event(steps, args, root, run, output, target, positions.get(target["fixture_id"]), player_history)
        except (steps.StepError, OSError, ValueError, RuntimeError) as exc:
            reason = str(exc)
            result = {"status": "ERROR_REINTENTABLE", "reason": reason}
        record = {**_target_row(target), **result}
        results.append(record)
        steps._atomic_json(output / "events" / str(target["fixture_id"]) / "estado.json", record)
        print(f"[hour-batch-prepare] {target['fixture_id']} {record['status']}" + (f" | {reason}" if reason else ""))

    results.sort(key=lambda row: (
        row["status"] != "REVISABLE",
        _option_order(row["priced_and_linked_options"][0])[:4] if row.get("priced_and_linked_options") else (True, 0, True, 0),
        row.get("statistical_position") or float("inf"),
    ))
    packet = {"packet_id": packet_id, "eval_dir": str(run), "hour_madrid": slot.isoformat(),
              "batch_end_madrid": batch_end.isoformat(),
              "created_at_utc": steps._now_utc().isoformat(), "fixtures": results}
    path = output / "lote.json"
    steps._atomic_json(path, packet)
    auto_shortlist_path = output / "prefinalistas_quant_auto.json"
    steps._atomic_json(auto_shortlist_path, _build_auto_quant_shortlist(packet))
    prompt = output / "encargo_analista_codex_argon.md"
    response = output / "prefinalistas.json"
    lines = [
        f"# Revisión comercial del lote {slot:%Y-%m-%d %H:%M}–{batch_end:%Y-%m-%d %H:%M} Europe/Madrid", "",
        f"Abre `{path}` desde el proyecto y revisa `priced_and_linked_options` de cada fixture `REVISABLE`.",
        "Lee los catálogos y contrasta los finalistas con los TXT de cuotas y las fuentes estadísticas locales indicadas abajo. "
        "Coteja identidad, corte, línea, selección y precio en los archivos del partido.",
        "Usa el lote y los snapshots guardados en esta corrida como evidencia. Acota las lecturas al lote y a las filas pertinentes de cada archivo compartido.",
        *_market_review_rules(),
        f"Escribe el resumen de cobertura en `{output / 'revision_mercados.md'}`.",
        f"Escribe directamente `{response}` como JSON sin Markdown. Selecciona de cero a tres prefinalistas "
        "y, si aporta valor, un suplente. Usa los fixture_id y selection_id de lote.json.",
        "Incluye en cada objeto fixture_id, selection_id, role (prefinalist o reserve), case_for, lineup_condition, requires_confirmed_xi y watch_players (opcional, lista de nombres o IDs en texto).",
        f'Formato raíz {json.dumps({"packet_id": packet_id, "candidates": []})}',
        "", "Los mercados ampliados y el inventario completo de cada captura quedan en el catálogo por evento.",
    ]
    for row in results:
        lines.extend(["", f"## {row['fixture_id']} · {row['home']} – {row['away']}",
                      f"Inicio {row['kickoff_madrid']} · estado {row['status']} · puesto estadístico {row.get('statistical_position') or 'N/D'}"])
        if row.get("reason"):
            lines.append(f"- {row['reason']}")
        if row.get("catalog"):
            lines.append(f"- Catálogo íntegro `{row['catalog']}`.")
        if row.get("snapshot"):
            price = (row.get("price_status") or {}).get("status", "PENDIENTE")
            snapshot = row["snapshot"]
            lines.append(f"- Cuotas `{snapshot['path']}` · SHA-256 `{snapshot['sha256']}` · UTC {snapshot['captured_at_utc']} · {price}.")
            if snapshot["diagnostics_path"]:
                lines.append(f"- Diagnóstico de captura `{snapshot['diagnostics_path']}`.")
        for name, ref in (row.get("statistical_sources") or {}).items():
            lines.append(f"- {name} `{ref['path']}` · SHA-256 `{ref['sha256']}`.")
        if row.get("player_prop_coverage", {}).get("priced_player_options"):
            lines.append("- Cobertura de props y referencias históricas verificadas en `player_prop_coverage` y `player_history_sources` de este fixture.")
    instruction_file, instruction = _write_agent_prompt(
        prompt, response, lines, instruction_name="instruccion_analista_cuotas.txt")
    print(f"  lote_json: {path}")
    print(f"  prefinalistas_quant_auto_json: {auto_shortlist_path}")
    print(f"  prompt_analista_cuotas: {prompt}")
    print(f"  prefinalistas_json: {response}")
    print(f"  archivo_instruccion_analista_cuotas: {instruction_file}")
    if args.export_gemini:
        _export_gemini(steps, output, packet, response)
    print(f"  instruccion_analista_cuotas: {instruction}")
    return 0


def _read_shortlist(steps, run, slot, folder, path):
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        kind = "DIRECTORY" if resolved.is_dir() else "MISSING"
        raise ValueError(
            f"HOUR_SHORTLIST_FILE_REQUIRED:{kind}:{resolved} "
            "(--shortlist debe apuntar al archivo prefinalistas.json, no al directorio del paquete)"
        )
    raw = resolved.read_bytes()
    response = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(response, dict) or set(response) != {"packet_id", "candidates"}:
        raise ValueError("INVALID_HOUR_SHORTLIST_SCHEMA")
    packet_id = response["packet_id"]
    if not isinstance(packet_id, str) or len(packet_id) != 32 or any(c not in "0123456789abcdef" for c in packet_id):
        raise ValueError("INVALID_HOUR_PACKET_ID")
    packet = steps._read_json(folder / "packets" / packet_id / "lote.json")
    if packet["eval_dir"] != str(run) or packet["hour_madrid"] != slot.isoformat() or packet["packet_id"] != packet_id:
        raise ValueError("HOUR_PACKET_RUN_OR_WINDOW_MISMATCH")
    candidates = response["candidates"]
    if not isinstance(candidates, list) or len(candidates) > MAX_PREFINALISTS:
        raise ValueError("HOUR_SHORTLIST_MAX_3_PLUS_RESERVE")
    offered = {(row["fixture_id"], option["selection_id"]): (row, option)
               for row in packet["fixtures"] if row["status"] == "REVISABLE"
               for option in row["priced_and_linked_options"]}
    selected = {}
    for item in candidates:
        keys = {"fixture_id", "selection_id", "role", "case_for", "lineup_condition"}
        min_odd_val = item.get("min_odd")
        valid_min_odd = (
            min_odd_val is None
            or (isinstance(min_odd_val, (int, float)) and not isinstance(min_odd_val, bool) and float(min_odd_val) > 1.0)
        )
        if (not isinstance(item, dict) or not keys <= item.keys()
                or item.keys() - keys - {"watch_players", "requires_confirmed_xi", "min_odd"}
                or not valid_min_odd
                or type(item.get("requires_confirmed_xi", True)) is not bool
                or type(item["fixture_id"]) is not int
                or any(not isinstance(item[k], str) or not item[k].strip() for k in keys - {"fixture_id"})
                or item["role"] not in {"prefinalist", "reserve"}
                or not isinstance(item.get("watch_players", []), list)
                or any(not isinstance(p, str) or not p.strip() for p in item.get("watch_players", []))):
            raise ValueError("INVALID_HOUR_CANDIDATE")
        key = (item["fixture_id"], item["selection_id"])
        if key in selected or key not in offered:
            raise ValueError(f"HOUR_CANDIDATE_OUTSIDE_PACKET_OR_DUPLICATE:{key}")
        selected[key] = (item, *offered[key])
    roles = [item[0]["role"] for item in selected.values()]
    if roles.count("prefinalist") > 3 or roles.count("reserve") > 1:
        raise ValueError("HOUR_SHORTLIST_MAX_3_PLUS_RESERVE")
    return packet_id, list(selected.values()), hashlib.sha256(raw).hexdigest()


def _research_questions(row, candidates, mode):
    families = sorted({option["market_family"] for _, option in candidates})
    questions = []
    if mode == "deep":
        questions.extend([
            "Comprueba bajas, sanciones, dudas, regresos, entrenador actual y adaptación de la plantilla.",
            "Revisa alineaciones publicadas o probables, variantes, rotaciones, roles y gestión de minutos.",
            "Contrasta estrategias, necesidad según la tabla, descanso, viajes y partidos siguientes con fuentes fechadas.",
            "Busca designación arbitral, clima o superficie si afectan los mercados candidatos.",
        ])
    elif mode == "focused":
        questions.append("Prioriza bajas, sanciones y alineaciones (publicadas o probables) que cambien el rol, la exposición o el planteamiento de un candidato.")
        if row.get("contradictions"):
            questions.append("De las contradicciones del JSON, comprueba solo la que pueda cambiar más una tesis. Distingue evidencia y rumor.")
        if any(family.startswith("CARDS_") for family in families):
            referee = (row.get("football_context") or {}).get("referee")
            questions.append(f"Comprueba el árbitro ({referee or 'sin designación acreditada en los insumos'}) y su relevancia para las tarjetas candidatas.")
    else:
        questions.append("Prioriza novedades de XI, bajas, roles, árbitro o contexto que cambien una condición concreta del candidato.")
    if "ON_TARGET_COLLECTIVE" in families:
        questions.append(
            "Para tiros a portería, usa Opta/Stats Perform como proveedor de liquidación Bwin según la política aportada. "
            "Separa esa liquidación del histórico API-Football y señala cualquier diferencia documentada de definición o corrección."
        )
    for item, option in candidates:
        questions.append(
            f"Audita `{option['market_header_literal']} → {option['selection_literal']}`. Tesis: {item['case_for']} "
            f"Condición declarada: {item['lineup_condition']}"
        )
    return questions


def _research_reports(packet_root, root, row):
    from bet_engine.analytics.early_tactical_scout import RESEARCH_PROVIDERS

    eid = str(row["bwin_event_id"])
    event_dir = packet_root / "events" / str(row["fixture_id"])
    responses = event_dir / "research" / "responses"
    reports = []
    for provider in RESEARCH_PROVIDERS:
        name = f"{provider}_{eid}.md"
        paths = (responses / name, *sorted(responses.glob(f"{provider}_{eid}_*.md")),
                 event_dir / name, root / "outputs/matches" / eid / "research" / name)
        for path in paths:
            if path.is_file():
                reports.append({"source": provider, "path": str(path), "discovered": True})
    return reports


def research(args, steps):
    """Create one portable, fixture-specific manual research handoff per shortlisted event."""
    from bet_engine.analytics.early_tactical_scout import RESEARCH_PLACEHOLDER

    now = steps._now_utc()
    root, run, slot, folder = _scope(args, steps, now)
    packet_id, selections, _ = _read_shortlist(steps, run, slot, folder, args.shortlist)
    packet_root = folder / "packets" / packet_id
    packet = steps._read_json(packet_root / "lote.json")
    rows = {row["fixture_id"]: row for row in packet["fixtures"]}
    grouped = defaultdict(list)
    for item, row, option in selections:
        if not args.fixture_id or item["fixture_id"] in args.fixture_id:
            grouped[item["fixture_id"]].append((item, option))
    if not grouped:
        raise ValueError("NO_SHORTLIST_FIXTURES_SELECTED_FOR_RESEARCH")

    generated = 0
    for fid, candidates in sorted(grouped.items()):
        row = rows[fid]
        if _lead(steps, row, now) <= 0:
            print(f"[hour-batch-research] {fid} CIERRE_OPERATIVO | KICKOFF_REACHED")
            continue
        eid = str(row["bwin_event_id"])
        research_root = packet_root / "events" / str(fid) / "research"
        requests = research_root / "requests"
        responses = research_root / "responses"
        prompt = requests / f"encargo_{args.provider}_{eid}_{args.research_mode}.md"
        inventory = requests / f"insumos_{args.provider}_{eid}_{args.research_mode}.json"
        response = responses / f"{args.provider}_{eid}_{args.research_mode}.md"
        catalog = steps._read_json(Path(row["catalog"]))
        families = {option["market_family"] for _, option in candidates}
        candidate_ids = {option["selection_id"] for _, option in candidates}
        relevant_quotes = [entry for entry in catalog["entries"] if
                           (entry["market_family"] in families if args.research_mode == "deep"
                            else entry["selection_id"] in candidate_ids)]
        previous_report = None
        if args.research_mode == "update":
            reports = [item for item in _research_reports(packet_root, root, row)
                       if not Path(item["path"]).name.endswith("_update.md")]
            prior = steps._evaluate_research_inbox(reports, row, now)["accepted"]
            previous_report = max(prior, key=lambda item: item["generated_utc"], default=None)
        payload = {
            "packet_id": packet_id,
            "generated_at_utc": now.isoformat(),
            "research_mode": args.research_mode,
            "provider": args.provider,
            "fixture": {key: row.get(key) for key in (
                "fixture_id", "bwin_event_id", "home", "away", "kickoff_madrid", "kickoff_utc",
                "football_context", "contradictions", "catalog_by_family", "all_markets_view_status",
            )},
            "candidates": [{**item, "market": option} for item, option in candidates],
            "relevant_quotes": relevant_quotes,
            "settlement_policies": catalog.get("settlement_policies") or {},
        }
        steps._atomic_json(inventory, payload)
        try:
            response.parent.mkdir(parents=True, exist_ok=True)
            with response.open("x", encoding="utf-8") as handle:
                handle.write(RESEARCH_PLACEHOLDER + "\n")
        except FileExistsError:
            pass
        questions = _research_questions(row, candidates, args.research_mode)
        if args.research_mode == "focused":
            scope = ("Usa una consulta normal o una búsqueda breve. Limita la búsqueda a una pregunta por candidato y hasta dos "
                     "comprobaciones compartidas, preferentemente de los últimos 14 días, con hasta 4 fuentes primarias o medios locales. "
                     "Revisa entrenador, estrategia, tabla, calendario o rumores solo si cambian la tesis. Devuelve hasta 6 hallazgos. "
                     "Si falta una fuente fiable, registra el faltante y termina.")
        elif args.research_mode == "deep":
            scope = ("Usa Deep Research si hay tiempo. Prioriza las últimas 4 semanas y amplía solo para contrastar un cambio de régimen. "
                     "Usa hasta 8 fuentes pertinentes. Evita recapitulaciones de estadísticas ya aportadas.")
        elif previous_report:
            scope = (f"Adjunta el informe anterior impreso por el CLI. Busca solo novedades desde su corte "
                     f"{previous_report['cutoff_utc'] or previous_report['generated_utc']}, con hasta 3 fuentes. "
                     "Registra únicamente cambios materiales o indica que no encontraste cambios.")
        else:
            scope = "Sin informe anterior acreditado, revisa solo novedades materiales de las últimas 24 horas y usa hasta 3 fuentes."
        lines = [
            f"# Investigación {args.research_mode} · {row['home']} – {row['away']} · {args.provider}", "",
            f"Fixture `{fid}` · Event ID `{eid}` · kickoff `{row['kickoff_madrid']}`.",
            f"Lee el archivo adjunto `{inventory.name}`. Analiza únicamente este evento y sus candidatos.",
            "La investigación externa es opcional y no decide BET/NO BET. Aporta hechos cualitativos, fuentes y contradicciones al cierre.",
            "Usa información disponible antes del kickoff. Cita cada fuente como enlace Markdown con medio y fecha de publicación. "
            "Indica cuándo varias notas repiten la misma fuente.",
            "Clasifica cada hallazgo como CONFIRMADO_OFICIAL, PUBLICADO_PREPARTIDO, PROBABLE, INFERIDO o NO_VERIFICADO.",
            "No inventes probabilidades, cuotas actuales, alineaciones ni reglas ausentes. La cuota adjunta es un snapshot.",
            scope,
            "Si una conclusión depende del XI, indica la condición exacta y el mercado afectado.", "",
            "## Preguntas específicas", "", *[f"- {question}" for question in questions], "",
            "## Entrega", "",
            f"Abre el informe con `fixture_id: {fid}` y `event_id: {eid}` en líneas separadas. "
            "Añade `analysis_cutoff_utc` y `generated_at_utc` con timestamps reales ISO 8601 y offset y fecha cada fuente.",
            f"Devuelve Markdown destinado a `{response.name}` con identidad, corte declarado, hechos y fuentes, contradicciones, "
            "impacto por candidato, faltantes críticos y una conclusión cualitativa. No emitas la decisión financiera final.",
        ]
        _write_markdown(prompt, lines)
        print(f"[hour-batch-research] {fid} {args.provider} {args.research_mode}")
        print(f"  research_prompt: {prompt}")
        print(f"  research_input: {inventory}")
        print(f"  research_response: {response}")
        if previous_report:
            print(f"  research_previous_report: {previous_report['path']}")
        generated += 1
    return 0 if generated else 2


def _fetch_lineups(fixture_id):
    from generate_context import API_KEY, APIFootballClient

    client = APIFootballClient(API_KEY)
    try:
        payload = client.get("fixtures/lineups", {"fixture": fixture_id}, use_cache=False)
    finally:
        client.session.close()
    if (payload.get("get") != "fixtures/lineups" or payload.get("errors")
            or str((payload.get("parameters") or {}).get("fixture")) != str(fixture_id)
            or not isinstance(payload.get("response"), list)):
        raise ValueError("LINEUP_API_RESPONSE_UNBOUND_OR_INVALID")
    return payload


def _refresh_lineup(steps, args, run, output, target, previous, now, lineup_path):
    lineup = steps._load_current_lineup_refresh(lineup_path, run, target, now)
    if args.manual_lineups_json:
        from bet_engine.context.lineup_manager import load_and_validate_manual_lineup_file
        from bet_engine.markets.tactical_market_comparator import refresh_expedient_with_lineups
        source = args.manual_lineups_json.expanduser().resolve()
        manual = load_and_validate_manual_lineup_file(
            source, expected_fixture_id=target["fixture_id"], expected_event_id=str(target["bwin_event_id"]),
            expected_home_id=target["home_id"], expected_away_id=target["away_id"],
            expected_home_name=target["home"], expected_away_name=target["away"],
            kickoff_utc=_kickoff(steps, target), now_utc=now,
        )
        for screenshot in manual["screenshots"]:
            path = Path(screenshot["path"]).expanduser().resolve()
            if not path.is_file() or screenshot.get("sha256") != steps._sha256_file(path):
                raise ValueError(f"MANUAL_LINEUP_SCREENSHOT_HASH_MISMATCH:{path}")
        watch = {p for item, _, _ in previous for p in item.get("watch_players", [])}
        refreshed = refresh_expedient_with_lineups(
            scout_report={}, fixture_id=target["fixture_id"], event_id=target["bwin_event_id"],
            home_id=target["home_id"], away_id=target["away_id"],
            home_name=target["home"], away_name=target["away"], kickoff_utc=_kickoff(steps, target),
            now_utc=now, target_status=target["fixture_status"], manual_lineup=manual,
            watch_players=sorted(watch),
        )
        destination = steps._preserved_output_dir(lineup_path.parent, lineup_path.name) / lineup_path.name
        refreshed.update(eval_dir=str(run), source_manual={"path": str(source), "sha256": manual["source_file_sha256"],
                                                    "screenshots": manual["screenshots"]})
        steps._atomic_json(destination, refreshed)
        return {**refreshed, "artifact_path": str(destination)}
    if lineup and lineup["confirmed_complete_22"]:
        return lineup
    if not args.confirm_external or _lead(steps, target, now) > LINEUP_CHECK_LEAD:
        return lineup
    from bet_engine.markets.tactical_market_comparator import refresh_expedient_with_lineups

    raw = _fetch_lineups(target["fixture_id"])
    steps._atomic_json(output / "lineups_api.json", raw)
    now = steps._now_utc()
    if _lead(steps, target, now) < CONFIRMED_XI_MIN_LEAD:
        return None
    steps._validate_snapshot_times((raw.get("_acquired_at_utc"),), target, now, source="LINEUP_API")
    watch = {p for item, _, _ in previous for p in item.get("watch_players", [])}
    watch.update(str(option["player_evidence"]["player_id"]) for _, _, option in previous if option.get("player_evidence"))
    lineup = refresh_expedient_with_lineups(
        scout_report={}, fixture_id=target["fixture_id"], event_id=target["bwin_event_id"],
        home_id=target["home_id"], away_id=target["away_id"], home_name=target["home"], away_name=target["away"],
        kickoff_utc=_kickoff(steps, target), now_utc=now, target_status=target["fixture_status"],
        api_lineups_response=raw["response"], watch_players=sorted(watch),
    )
    destination = steps._preserved_output_dir(lineup_path.parent, lineup_path.name) / lineup_path.name
    lineup.update(eval_dir=str(run), source_api={"path": str(output / "lineups_api.json"),
                                                 "sha256": steps._sha256_file(output / "lineups_api.json")})
    steps._atomic_json(destination, lineup)
    return {**lineup, "artifact_path": str(destination)}


def _compare_price(before, catalog):
    matches = [e for e in catalog["entries"] if e["market_header_literal"] == before["market_header_literal"]
               and e["selection_literal"] == before["selection_literal"]]
    scope = ("market_family", "target_scope", "temporal_scope", "target_entity", "direction")
    alternatives = [
        {key: e[key] for key in ("selection_id", "selection_literal", "market_header_literal", "line", "odd", "settlement_status")}
        for e in catalog["entries"] if _usable_offer(e) and e["line"] is not None and e["line"] != before["line"]
        and all(e.get(key) == before.get(key) for key in scope)
    ]
    unavailable = {"odd_before": before["odd"], "line_before": before["line"], "neighboring_lines": alternatives}
    if any(entry["verdict"] == "CONFLICTING_CAPTURE_PRICES" for entry in matches):
        return {**unavailable, "status": "CUOTAS_EN_CONFLICTO",
                "observed_prices": sorted({entry["odd"] for entry in matches if entry["odd"] is not None})}
    if not matches:
        return {**unavailable, "status": "SELECCION_ORIGINAL_NO_LOCALIZADA"}
    after = matches[0]
    if not _usable_offer(after) or after["selection_id"] != before["selection_id"]:
        return {**unavailable, "status": "PRECIO_O_IDENTIDAD_NO_VERIFICADOS"}
    return {"status": "COMPARABLE", "line_before": before["line"], "line_after": after["line"],
            "odd_before": before["odd"], "odd_after": after["odd"],
            "settlement_before": before["settlement_status"], "settlement_after": after["settlement_status"],
            "canonical_key_after": after["canonical_key"], "contract_id_after": after["contract_id"],
            "odd_delta": str(Decimal(after["odd"]) - Decimal(before["odd"])), "neighboring_lines": alternatives}


def _refresh_event(steps, args, root, run, output, target, previous):
    now = steps._now_utc()
    early = args.lineup_mode == "sin-xi"
    minimum = steps.ANALYSIS_MIN_LEAD_MINUTES if early else CONFIRMED_XI_MIN_LEAD
    if not target["temporal_eligible"] or _lead(steps, target, now) < minimum:
        return {"status": "CIERRE_OPERATIVO",
                "reason": target.get("temporal_reason") or f"MODEL_LEAD_BELOW_{minimum}"}
    for _, old, _ in previous:
        if _target_row(target) != {k: old[k] for k in _target_row(target)}:
            raise ValueError("HOUR_SHORTLIST_FIXTURE_IDENTITY_OR_KICKOFF_CHANGED")
        old_capture = old["snapshot"]
        if steps._sha256_file(Path(old_capture["path"])) != old_capture["sha256"]:
            raise ValueError("HOUR_INITIAL_SNAPSHOT_HASH_MISMATCH")
    steps._fixture_statistical_inputs(run, target, now, required=True)
    if not previous:
        return {"status": "XI_PENDIENTE", "reason": "NO_CANDIDATES_EXPLICITLY_INDEPENDENT_OF_CONFIRMED_XI"}
    eid = target["bwin_event_id"]
    lineup_path = root / "outputs/matches" / eid / "market_phase2" / f"lineup_refresh_{eid}.json"
    lineup = (steps._load_current_lineup_refresh(lineup_path, run, target, now) if early
              else _refresh_lineup(steps, args, run, output, target, previous, now, lineup_path))
    now = steps._now_utc()
    if _lead(steps, target, now) < minimum:
        return {"status": "CIERRE_OPERATIVO", "reason": f"MODEL_LEAD_BELOW_{minimum}", "lineup": lineup}
    if not early and (not lineup or not lineup["confirmed_complete_22"]):
        return {"status": "XI_PENDIENTE", "lineup": lineup,
                "reason": "CHECK_WINDOW_T75" if _lead(steps, target, now) > LINEUP_CHECK_LEAD else "BOTH_XI_NOT_ACCREDITED"}

    snapshot = steps._resolve_odds_snapshot(run, target, now)
    initial_times = [steps._now_utc(row["snapshot"]["captured_at_utc"]) for _, row, _ in previous]
    initial_latest = max(initial_times)
    earliest = (steps._now_utc(lineup["refreshed_at_utc"])
                if early and lineup and lineup["confirmed_complete_22"] else initial_latest)

    def valid_second_capture(ref, now):
        if not ref or not ref.get("captured_at_utc"):
            return False
        timestamp = steps._now_utc(ref["captured_at_utc"])
        return timestamp >= earliest and timestamp > initial_latest and now - timestamp <= timedelta(minutes=FINAL_PRICE_AGE)

    capture_minimum = FINAL_CAPTURE_MIN_LEAD if early else CONFIRMED_XI_MIN_LEAD
    capture_error = None
    if not valid_second_capture(snapshot, now) and args.confirm_external and _lead(steps, target, now) >= capture_minimum:
        try:
            snapshot = (_capture_final(steps, root, run, target) if early
                        else _capture(steps, root, run, target, min_lead=CONFIRMED_XI_MIN_LEAD))
        except (steps.StepError, OSError, ValueError, RuntimeError) as exc:
            capture_error = str(exc)
        now = steps._now_utc()
    pending_price = not valid_second_capture(snapshot, now)
    price_reason = None
    if pending_price:
        price_reason = "FRESH_CAPTURE_AFTER_INITIAL_REQUIRED" if early else "RECENT_CAPTURE_AFTER_INITIAL_REQUIRED"
        if _lead(steps, target, now) < capture_minimum:
            price_reason = f"FINAL_CAPTURE_LEAD_BELOW_{capture_minimum}"
        elif capture_error:
            price_reason = "FINAL_ODDS_CAPTURE_FAILED"
        if not snapshot:
            return {"status": "PRECIO_PENDIENTE", "lineup": lineup, "snapshot": None,
                    "reason": price_reason, "capture_error": capture_error}
    catalog = _catalog(target, snapshot)
    catalog_path = output / "catalogo_cierre.json"
    steps._atomic_json(catalog_path, catalog)
    verified_odds, canonical_conflicts = _verified_catalog_prices(catalog["entries"])
    comparisons = [
        {**item, "initial_snapshot": row["snapshot"], "market_header_literal": option["market_header_literal"],
         "selection_literal": option["selection_literal"], "settlement_status": option["settlement_status"],
         "canonical_key": option["canonical_key"], "settlement_provenance": option.get("settlement_provenance"),
         "price": _compare_price(option, catalog)}
        for item, row, option in previous
    ]
    for comparison, (_, _, option) in zip(comparisons, previous):
        price = comparison["price"]
        key = price.get("canonical_key_after")
        if price["status"] != "COMPARABLE":
            reason = price["status"]
        elif price.get("settlement_after") != "VERIFIED_STANDARD":
            reason = "SETTLEMENT_UNVERIFIED"
        elif key in canonical_conflicts:
            reason = "CANONICAL_PRICE_CONFLICT"
            price["conflicting_canonical_prices"] = canonical_conflicts[key]
        elif not price.get("contract_id_after") or key not in verified_odds:
            reason = "CONTRACT_MAPPING_PENDING"
        else:
            reason = None
        if pending_price and reason is None:
            reason = price_reason
        comparison["shadow_evaluation"] = {"status": "NOT_EVALUATED", "reason": reason}
        if evidence := option.get("player_evidence"):
            matches = [player for player in (lineup or {}).get("api_roster") or []
                       if (player.get("player_id"), player.get("side")) == (evidence["player_id"], evidence["side"])]
            comparison["player_evidence"] = evidence
            comparison["player_lineup_context"] = matches[0] if len(matches) == 1 else {"role": "EXPOSICION_INCIERTA"}
    price_time = steps._now_utc(snapshot["captured_at_utc"])
    xi_time = steps._now_utc(lineup["refreshed_at_utc"]) if lineup and lineup["confirmed_complete_22"] else None
    result = {"status": "CONTRATO_PENDIENTE", "lineup": lineup, "snapshot": snapshot,
              "price_age_minutes_at_review": round((steps._now_utc() - price_time).total_seconds() / 60, 2),
              "price_captured_before_confirmed_xi": bool(xi_time and price_time < xi_time),
              "price_status": "SNAPSHOT_ONLY" if pending_price else "RECENT_PREMATCH_SNAPSHOT",
              "capture_error": capture_error,
              "catalog": str(catalog_path), "capture_coverage": catalog["capture_coverage"],
              "comparisons": comparisons}
    if _lead(steps, target, steps._now_utc()) < minimum:
        return {**result, "status": "CIERRE_OPERATIVO", "reason": f"MODEL_LEAD_BELOW_{minimum}"}
    if pending_price:
        return {**result, "status": "PRECIO_PENDIENTE", "reason": price_reason}
    eligible_keys = frozenset(row["price"]["canonical_key_after"] for row in comparisons
                              if row["shadow_evaluation"]["reason"] is None)
    if not eligible_keys:
        reasons = list(dict.fromkeys(row["shadow_evaluation"]["reason"] for row in comparisons))
        status = "LIQUIDACION_PENDIENTE" if reasons == ["SETTLEMENT_UNVERIFIED"] else "CONTRATO_PENDIENTE"
        return {**result, "status": status, "reason": ",".join(reasons)}
    model_dir = output / "model"
    from bet_engine.markets.staking_and_policy import is_cross_league_club_competition
    cross_league = is_cross_league_club_competition(target.get("league"), target.get("country"))
    accredited_fams = frozenset({"GOALS_FT"}) if not cross_league else frozenset()
    stages = argparse.Namespace(
        command="model", root=root, eval_dir=run, asof=None, fixture_id=target["fixture_id"],
        event_id=eid, min_lead=minimum, odds_file=Path(snapshot["path"]), output_dir=model_dir,
        lineup_independent_keys=eligible_keys if early else frozenset(), verified_catalog_odds=verified_odds,
        calibration_accredited_families=accredited_fams, allow_central_ev_authorization=True,
    )
    model_path = model_dir / f"model_report_{eid}.json"
    try:
        steps.cmd_model(stages)
        report = steps._read_json(model_path)
    except (steps.StepError, OSError, ValueError, RuntimeError) as exc:
        for row in comparisons:
            if row["shadow_evaluation"]["reason"] is None:
                row["shadow_evaluation"]["reason"] = "SHADOW_EXECUTION_FAILED"
        return {**result, "status": "ERROR_REINTENTABLE", "reason": f"SHADOW_EXECUTION_FAILED:{exc}"}
    finished = steps._now_utc()
    status, reason = "LISTO_REVISION_FINAL", None
    if _lead(steps, target, finished) < minimum:
        status, reason = "CIERRE_OPERATIVO", f"MODEL_FINISHED_BELOW_T{minimum}"
    elif not valid_second_capture(snapshot, finished):
        status, reason = "PRECIO_PENDIENTE", "PRICE_EXPIRED_DURING_MODEL"
        result["price_status"] = "SNAPSHOT_ONLY"
    options = {row.get("selection_key"): row for row in report.get("market_comparisons") or []}
    for row in comparisons:
        if row["shadow_evaluation"]["reason"] is not None:
            continue
        option = options.get(row["price"].get("canonical_key_after"))
        if not option or option.get("decision_category") == "MERCADO_NO_MODELADO":
            row["shadow_evaluation"]["reason"] = "MARKET_NOT_MODELED"
        elif option.get("odd") is None or Decimal(str(option["odd"])) != Decimal(row["price"]["odd_after"]):
            row["shadow_evaluation"]["reason"] = "SHADOW_PRICE_NOT_MATCHED"
        elif option.get("decision_category") == "BLOQUEO_TECNICO":
            row["shadow_evaluation"]["reason"] = "SHADOW_TECHNICAL_BLOCK"
        else:
            row["shadow_evaluation"] = {"status": "EVALUATED", "reason": None}
            row["shadow_option"] = {k: option.get(k) for k in (
                "calibration_status", "decision_category", "raw_sample_n", "effective_sample_n", "pending_conditions",
                "p_model", "p_low", "p_high", "ev_model", "ev_conservative_low", "fair_odd", "tactical_warnings",
            )}
    if status == "LISTO_REVISION_FINAL" and not any(row["shadow_evaluation"]["status"] == "EVALUATED" for row in comparisons):
        status, reason = "COBERTURA_MODELO_PENDIENTE", ",".join(
            dict.fromkeys(row["shadow_evaluation"]["reason"] for row in comparisons))
    return {**result, "status": status, "reason": reason, "model_json": str(model_path), "model_sha256": steps._sha256_file(model_path),
            "model_decision": report.get("decision"), "model_classification": report.get("decision_classification"),
            "model_reasons": report.get("no_bet_reasons"),
            "model_deadline_utc": (_kickoff(steps, target) - timedelta(minutes=minimum)).isoformat(),
            "price_deadline_utc": (steps._now_utc(snapshot["captured_at_utc"]) + timedelta(minutes=FINAL_PRICE_AGE)).isoformat()}


def _refresh_output_dir(refresh_root: Path, now_utc: datetime, lineup_mode: str, tz) -> Path:
    refresh_root.mkdir(parents=True, exist_ok=True)
    local_now = now_utc.astimezone(tz)
    mode_slug = re.sub(r"[^a-z0-9-]+", "-", (lineup_mode or "refresh").lower()).strip("-") or "refresh"
    base = f"{local_now:%Y-%m-%d_%H-%M-%S}_{mode_slug}"
    for idx in range(1, 1000):
        name = base if idx == 1 else f"{base}_{idx:02d}"
        candidate = refresh_root / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"REFRESH_DIR_COLLISION:{base}")


def _finalize_quant_tickets_and_boleta(steps, output, slot, results, targets, by_fixture, published, *, bankroll, currency):
    """Evalúa cada candidato refrescado con las reglas anti-fugas, piso de cuota,
    desduplicación por correlación intra-partido y Kelly Fraccional, generando
    `boleta_ejecucion.json` y `boleta_ejecucion.md` al instante (<2s).
    """
    from bet_engine.markets.staking_and_policy import (
        allocate_match_portfolio_stakes,
        compute_market_devig_map,
        evaluate_collective_quant_policy,
        evaluate_player_prop_quant,
        extract_min_acceptable_odd,
        is_cross_league_club_competition,
    )

    boleta_fixtures = []
    total_batch_bets = 0
    total_batch_pct = 0.0
    total_batch_amt = 0.0

    for row in results:
        fid = row["fixture_id"]
        target = targets.get(fid) or row
        prev_map = {opt["selection_id"]: (item, opt) for item, _, opt in (by_fixture.get(fid) or [])}
        cross_league = is_cross_league_club_competition(target.get("league"), target.get("country"))

        devig_map = {}
        if row.get("catalog") and Path(row["catalog"]).is_file():
            try:
                cat_doc = steps._read_json(Path(row["catalog"]))
                v_odds, _ = _verified_catalog_prices(cat_doc.get("entries") or [])
                devig_map = compute_market_devig_map(v_odds)
            except (OSError, ValueError, KeyError):
                devig_map = {}

        lineup_ok = bool((row.get("lineup") or {}).get("confirmed_complete_22"))
        lead_ok = _lead(steps, target, published) >= (
            steps.ANALYSIS_MIN_LEAD_MINUTES if row.get("effective_lineup_mode") == "sin-xi" else CONFIRMED_XI_MIN_LEAD
        )
        price_fresh = row.get("price_status") == "RECENT_PREMATCH_SNAPSHOT"
        collective_gate_open = (row.get("status") == "LISTO_REVISION_FINAL") and lead_ok and price_fresh
        prop_gate_open = (row.get("status") in {"LISTO_REVISION_FINAL", "LIQUIDACION_PENDIENTE"}) and lineup_ok and lead_ok and price_fresh

        cand_inputs = []
        for comp in row.get("comparisons") or []:
            sel_id = comp.get("selection_id")
            orig_item, orig_opt = prev_map.get(sel_id, (comp, {}))
            min_odd = extract_min_acceptable_odd(orig_item)
            price_block = comp.get("price") or {}
            odd_after_str = price_block.get("odd_after")
            odd_after = float(odd_after_str) if (price_block.get("status") == "COMPARABLE" and odd_after_str is not None) else None
            fam = orig_opt.get("market_family") or ""

            if comp.get("player_evidence") is not None:
                p_role = (comp.get("player_lineup_context") or {}).get("role")
                qd = evaluate_player_prop_quant(
                    comp["player_evidence"],
                    odd=odd_after,
                    market_family=fam,
                    lineup_role=p_role,
                    lineup_confirmed=lineup_ok,
                    min_acceptable_odd=min_odd,
                )
                if not prop_gate_open and qd.get("bet_authorized"):
                    qd = {
                        **qd,
                        "bet_authorized": False,
                        "raw_kelly_stake_pct": 0.0,
                        "status": f"BLOQUEO_OPERATIVO_CIERRE({row.get('status')})",
                    }
            else:
                sh_opt = comp.get("shadow_option") or {}
                qd = evaluate_collective_quant_policy(
                    market_family=fam,
                    direction=orig_opt.get("direction"),
                    temporal_scope=orig_opt.get("temporal_scope"),
                    odd=odd_after,
                    p_model=sh_opt.get("p_model"),
                    p_low=sh_opt.get("p_low"),
                    p_high=sh_opt.get("p_high"),
                    raw_sample_n=sh_opt.get("raw_sample_n"),
                    effective_sample_n=sh_opt.get("effective_sample_n"),
                    devig_info=devig_map.get(price_block.get("canonical_key_after") or ""),
                    is_cross_league=cross_league,
                    tactical_warnings=sh_opt.get("tactical_warnings") or (),
                    min_acceptable_odd=min_odd,
                    settlement_verified=(price_block.get("settlement_after") == "VERIFIED_STANDARD"),
                )
                if not collective_gate_open and qd.get("bet_authorized"):
                    qd = {
                        **qd,
                        "bet_authorized": False,
                        "raw_kelly_stake_pct": 0.0,
                        "status": f"BLOQUEO_OPERATIVO_CIERRE({row.get('status')})",
                    }
            comp["min_acceptable_odd"] = min_odd
            comp["quant_decision"] = qd
            cand_inputs.append({
                "selection_id": sel_id,
                "market_header_literal": comp.get("market_header_literal"),
                "selection_literal": comp.get("selection_literal"),
                "odd": odd_after,
                "quant_decision": qd,
            })

        portfolio = allocate_match_portfolio_stakes(
            cand_inputs,
            bankroll=bankroll,
            currency=currency,
        )
        row["execution_ticket"] = portfolio
        row["financial_decision"] = portfolio["match_decision"]
        total_batch_bets += portfolio["authorized_picks_count"]
        total_batch_pct = round(total_batch_pct + portfolio["total_match_stake_pct"], 2)
        total_batch_amt = round(total_batch_amt + portfolio["total_match_amount"], 2)
        boleta_fixtures.append({
            "fixture_id": fid,
            "bwin_event_id": row.get("bwin_event_id"),
            "match": f"{row.get('home')} vs {row.get('away')}",
            "kickoff_madrid": row.get("kickoff_madrid"),
            "operational_status": row.get("status"),
            "effective_lineup_mode": row.get("effective_lineup_mode"),
            **portfolio,
        })

    boleta_doc = {
        "evaluated_at_utc": published.isoformat(),
        "hour_madrid": slot.isoformat(),
        "bankroll_reference": bankroll,
        "currency": currency,
        "total_authorized_bets": total_batch_bets,
        "total_batch_stake_pct": total_batch_pct,
        "total_batch_amount": total_batch_amt,
        "fixtures": boleta_fixtures,
    }
    boleta_json = output / "boleta_ejecucion.json"
    boleta_md = output / "boleta_ejecucion.md"
    steps._atomic_json(boleta_json, boleta_doc)

    md = [
        f"# Boleta de Ejecución Cuantitativa · Lote {slot:%Y-%m-%d %H:%M} Madrid",
        "",
        f"- **Bankroll de referencia:** `{bankroll:.2f} {currency}` (`1u = 1% = {bankroll * 0.01:.2f} {currency}`)",
        f"- **Apuestas `BET` autorizadas:** `{total_batch_bets}` | **Exposición total del lote:** `{total_batch_pct:.2f}%` (`{total_batch_amt:.2f} {currency}`)",
        "- **Reglas activas:** Anti-fugas (`Córners UNDER n<14`, `1ª parte`, `1X2 inter-liga sin Elo`), piso automático de cuota, máx. 1 pick por eje de correlación y tope `3.00%` por partido.",
        "",
    ]
    for fx in boleta_fixtures:
        md.append(f"## {fx['match']} (`{fx['fixture_id']}` · Bwin `{fx['bwin_event_id']}`)")
        md.append(f"- Inicio: `{fx['kickoff_madrid']}` | Modo XI: `{fx['effective_lineup_mode']}` | Decisión partido: **`{fx['match_decision']}`** (`{fx['total_match_stake_pct']:.2f}%` = `{fx['total_match_amount']:.2f} {currency}`)")
        if not fx["allocations"]:
            md.append("- Sin candidatos elegibles en esta modalidad.")
        for alloc in fx["allocations"]:
            odd_txt = f"{alloc['odd']:.2f}" if alloc.get("odd") else "N/D"
            ev_txt = f"{alloc['ev_model']:+.1%}" if alloc.get("ev_model") is not None else "N/D"
            p_txt = f"{alloc['p_model']:.1%}" if alloc.get("p_model") is not None else "N/D"
            if alloc["decision"] == "BET":
                md.append(
                    f"- ✅ **BET** · `{alloc['market_header_literal']} → {alloc['selection_literal']}` @ **`{odd_txt}`** "
                    f"| `p_model={p_txt}` · `EV={ev_txt}` · Eje `{alloc['correlation_axis']}` "
                    f"→ **Stake: `{alloc['stake_pct_bank']:.2f}%` (`{alloc['stake_units']:.2f}u` = `{alloc['stake_amount']:.2f} {currency}`)**"
                )
            else:
                md.append(
                    f"- ⛔ **{alloc['decision']}** (`0 {currency}`) · `{alloc['market_header_literal']} → {alloc['selection_literal']}` @ `{odd_txt}` "
                    f"| `p_model={p_txt}` · `EV={ev_txt}` · Motivo: `{alloc['reason']}`"
                )
        md.append("")
    _write_markdown(boleta_md, md)
    return boleta_json, boleta_md, boleta_doc


def refresh(args, steps):
    now = steps._now_utc()
    root, run, slot, folder = _scope(args, steps, now)
    if args.manual_lineups_json and (args.lineup_mode != "xi-confirmado" or len(args.fixture_id) != 1):
        raise ValueError("MANUAL_LINEUPS_REQUIRE_XI_CONFIRMED_AND_ONE_FIXTURE_ID")
    packet_id, selections, shortlist_digest = _read_shortlist(steps, run, slot, folder, args.shortlist)
    if args.fixture_id:
        selections = [item for item in selections if item[0]["fixture_id"] in args.fixture_id]
        if not selections:
            raise ValueError("NO_SHORTLIST_CANDIDATES_FOR_REQUESTED_FIXTURE")
    output = _refresh_output_dir(folder / "packets" / packet_id / "refresh", now, args.lineup_mode, steps.trial.TZ)
    _, major = steps._load_major_doc(run)
    rows = steps._bwin_reconcile_records(run, major, now, min_lead_minutes=0)["all"]
    targets = {row["fixture_id"]: row for row in rows if row["identity_state"] == "VERIFIED"}
    by_fixture = defaultdict(list)
    for item in selections:
        by_fixture[item[0]["fixture_id"]].append(item)
    results = []
    for fid, previous in sorted(by_fixture.items(), key=lambda item: item[1][0][1]["kickoff_utc"]):
        target = targets.get(fid)
        event_output = output / str(fid)
        effective_mode = args.lineup_mode
        auto_lineup = None
        auto_lineup_reason = None
        if args.lineup_mode == "auto" and target is not None:
            lineup_now = steps._now_utc()
            if target["temporal_eligible"] and _lead(steps, target, lineup_now) >= CONFIRMED_XI_MIN_LEAD:
                eid = target["bwin_event_id"]
                lineup_path = root / "outputs/matches" / eid / "market_phase2" / f"lineup_refresh_{eid}.json"
                try:
                    auto_lineup = _refresh_lineup(steps, args, run, event_output, target, previous, lineup_now, lineup_path)
                except (steps.StepError, OSError, ValueError, RuntimeError) as exc:
                    auto_lineup_reason = f"AUTO_LINEUP_CHECK_FAILED:{exc}"
            else:
                auto_lineup_reason = target.get("temporal_reason") or f"LINEUP_LEAD_BELOW_{CONFIRMED_XI_MIN_LEAD}"
            effective_mode = ("xi-confirmado" if auto_lineup and auto_lineup.get("confirmed_complete_22")
                              else "sin-xi")
        event_args = argparse.Namespace(**vars(args))
        event_args.lineup_mode = effective_mode
        eligible, pending = [], []
        for item, row, option in previous:
            reason = None
            if effective_mode == "sin-xi":
                if option["target_scope"] not in {"MATCH", "HOME", "AWAY"}:
                    reason = "MARKET_SCOPE_REQUIRES_CONFIRMED_XI"
                elif "requires_confirmed_xi" not in item:
                    reason = "XI_DEPENDENCY_NOT_DECLARED"
                elif item["requires_confirmed_xi"]:
                    reason = "REQUIRES_CONFIRMED_XI"
            if reason:
                pending.append({**item, "reason": reason})
            else:
                eligible.append((item, row, option))
        try:
            if target is None:
                raise ValueError("FIXTURE_OR_BWIN_IDENTITY_NOT_VERIFIED_IN_RUN")
            result = _refresh_event(steps, event_args, root, run, event_output, target, eligible)
        except (steps.StepError, OSError, ValueError, RuntimeError) as exc:
            result = {"status": "ERROR_REINTENTABLE", "reason": str(exc)}
        record = {**_target_row(target or previous[0][1]), **result,
                  "financial_decision": None,
                  "requested_lineup_mode": args.lineup_mode, "effective_lineup_mode": effective_mode,
                  "auto_lineup_check_reason": auto_lineup_reason,
                  "pending_lineup_candidates": pending}
        research_audit = steps._evaluate_research_inbox(
            _research_reports(output.parent.parent, root, record), record, steps._now_utc())
        research_path = event_output / "auditoria_informes.json"
        steps._atomic_json(research_path, research_audit)
        record["research"] = {"audit_path": str(research_path), **{
            key: len(research_audit[key]) for key in ("accepted", "rejected", "ignored")}}
        results.append(record)
    summary = output / "cierre.json"
    published = steps._now_utc()
    for row in results:
        if row["status"] != "LISTO_REVISION_FINAL":
            continue
        if published > steps._now_utc(row["model_deadline_utc"]):
            minimum = steps.ANALYSIS_MIN_LEAD_MINUTES if row["effective_lineup_mode"] == "sin-xi" else CONFIRMED_XI_MIN_LEAD
            row.update(status="CIERRE_OPERATIVO", reason=f"PUBLISHED_BELOW_T{minimum}")
        elif published > steps._now_utc(row["price_deadline_utc"]):
            row.update(status="PRECIO_PENDIENTE", reason="PRICE_EXPIRED_DURING_BATCH", price_status="SNAPSHOT_ONLY")
    bankroll_ref = float(getattr(args, "bankroll", 1000.0) or 1000.0)
    currency_ref = str(getattr(args, "currency", "EUR") or "EUR")
    boleta_json, boleta_md, boleta_doc = _finalize_quant_tickets_and_boleta(
        steps, output, slot, results, targets, by_fixture, published,
        bankroll=bankroll_ref, currency=currency_ref,
    )
    for row in results:
        steps._atomic_json(output / str(row["fixture_id"]) / "estado.json", row)
        reason = f" | {row['reason']}" if row.get("reason") else ""
        mode = f" | modo={row.get('effective_lineup_mode')}" if args.lineup_mode == "auto" else ""
        fin = f" | decision_financiera={row.get('financial_decision')}" if row.get("financial_decision") else ""
        print(f"[hour-batch-refresh] {row['fixture_id']} {row['status']}{mode}{fin}{reason}")
        if row.get("snapshot"):
            print(f"  snapshot_cierre [{row['fixture_id']}]: {row['snapshot']['path']}")
        if row.get("capture_error"):
            print(f"  error_captura [{row['fixture_id']}]: {row['capture_error']}")
        for alloc in (row.get("execution_ticket") or {}).get("allocations") or []:
            if alloc.get("decision") == "BET":
                print(
                    f"  [BET] {row['fixture_id']} · {alloc['market_header_literal']} -> {alloc['selection_literal']} "
                    f"@ {alloc['odd']:.2f} | EV={alloc['ev_model']:+.1%} | "
                    f"Stake={alloc['stake_pct_bank']:.2f}% ({alloc['stake_amount']:.2f} {currency_ref})"
                )
    shortlist_path = args.shortlist.expanduser().resolve()
    steps._atomic_json(summary, {"packet_id": packet_id, "eval_dir": str(run), "hour_madrid": slot.isoformat(),
                                 "shortlist_path": str(shortlist_path), "shortlist_sha256": shortlist_digest,
                                 "evaluated_at_utc": published.isoformat(), "lineup_mode": args.lineup_mode,
                                 "bankroll_reference": bankroll_ref, "currency": currency_ref,
                                 "boleta_json": str(boleta_json), "boleta_md": str(boleta_md),
                                 "fixtures": results})
    original = folder / "packets" / packet_id / "lote.json"
    generated_prompts = []
    for row in results:
        event_output = output / str(row["fixture_id"])
        prompt = event_output / "revision_final_codex_argon.md"
        response = event_output / "resultado_cierre_llm.md"
        lines = [
            f"# Revisión final · {row['fixture_id']} · {row['home']} – {row['away']}", "",
            f"Analiza exclusivamente el fixture {row['fixture_id']} y el Event ID {row['bwin_event_id']}. "
            "No incorpores candidatos, datos ni conclusiones de otros partidos del lote.",
            f"Usa el lote inicial `{original}` solo para localizar los datos y candidatos de este evento. "
            "Para la primera lectura consulta las fuentes originales y omite `model_evidence` y los veredictos de modelos.",
            f"Inicio {row['kickoff_madrid']} · estado operativo {row['status']} · {row.get('reason') or 'sin motivo adicional'}.",
            f"Modalidad solicitada {args.lineup_mode}; modalidad efectiva para este evento "
            f"{row.get('effective_lineup_mode', args.lineup_mode)}.",
            "## 1. Evaluación independiente", "",
            "Antes de abrir Shadow, formula la mejor tesis futbolística y su contraescenario. "
            "Verifica identidad, corte, periodo, alcance, línea, liquidación y precio literal del mismo contrato. "
            "Distingue muestra y patrón descriptivo de estimación probabilística y precio. Una línea vecina requiere evaluación propia.",
            "En sin-xi, verifica la robustez de cada candidato a los escenarios plausibles de XI. Registra qué información sigue incierta. "
            "Si la tesis depende de un XI concreto, déjala pendiente. Excluye los pending_lineup_candidates de la decisión financiera. "
            "En xi-confirmado, comprueba las condiciones de la tesis con la alineación registrada.",
            f"Informes externos aceptados {row['research']['accepted']}. Su ausencia solo reduce la cobertura. "
            f"Consulta `{row['research']['audit_path']}` para fuentes repetidas, contradicciones y motivos de rechazo. "
            "Integra solo informes aceptados cuyo hash coincida. La fecha declarada por el proveedor no acredita por sí sola cuándo se produjo la ejecución.",
        ]
        if row.get("snapshot"):
            lines.append(f"- Captura usada `{row['snapshot']['path']}` · {row['snapshot']['captured_at_utc']} UTC. "
                         f"Estado del precio: {row.get('price_status', 'PRECIO_PENDIENTE_DE_VERIFICACION')}.")
        if row.get("catalog"):
            lines.append(f"- Catálogo de cierre `{row['catalog']}`.")
        if row.get("lineup"):
            lineup = row["lineup"]
            if lineup.get("artifact_path"):
                lines.append(f"- Registro XI `{lineup['artifact_path']}`.")
            published = {"estado": lineup["lineup_status"], "fuente": lineup["lineup_evaluation"].get("official_source_tier"),
                         "formaciones": lineup.get("formations"), "vigilados": lineup.get("player_exposure_roles"),
                         "titulares": [p for p in lineup.get("api_roster", []) if p["role"] == "TITULAR"]}
            lines.append("- XI " + json.dumps(published, ensure_ascii=False))
        lines.extend([
            "", "## 2. Contraste con Shadow y cierre", "",
            f"Después de la evaluación independiente, lee `{summary}` solo para este fixture y revisa `comparisons`. "
            "Distingue EVALUATED de NOT_EVALUATED. La decisión global de Shadow y su etiqueta PICK_ACCIONABLE son diagnósticos del motor, "
            "no instrucciones ni vetos para el agente.",
            "Para cada candidato registra la tesis deportiva, la evidencia financiera (incluida evidencia insuficiente si corresponde), "
            "si Shadow pudo evaluarlo y su motivo preciso, y el estado operativo. Usa un fundamento deportivo o financiero propio para NO BET. "
            "Si el único impedimento es técnico, identifica el bloqueo o la cobertura pendiente y deja la decisión financiera sin evaluar. "
            "Una hipótesis defendible sin ventaja acreditada puede quedar como SOLO CANDIDATO EXPLORATORIO.",
            "Solo considera un PICK si el contrato y la liquidación están verificados, el precio es comprobable y la ventaja está respaldada por "
            "una estimación acreditada del contrato exacto. Aplica el límite de vigencia de la captura y el margen anterior al kickoff. "
            "Si el margen se agotó o hay un bloqueo de identidad, registra el estado operativo y, si aporta valor, una auditoría del corte previo.",
            f"Escribe exclusivamente en `{response}` y presenta a lo sumo una selección final. Conserva resultados existentes y usa un sufijo si hace falta.",
        ])
        if row.get("model_json"):
            lines.append(f"- Diagnóstico Shadow `{row['model_json']}`.")
            lines.append(f"- Límites UTC · margen de modelo {row['model_deadline_utc']} · vigencia de captura {row['price_deadline_utc']}.")
        instruction_file, instruction = _write_agent_prompt(
            prompt, response, lines, instruction_name=f"instruccion_cierre_llm_{row['fixture_id']}.txt")
        generated_prompts.append((row, prompt, response, instruction_file, instruction))
    print(f"  cierre_json: {summary}")
    print(f"  boleta_ejecucion_md: {boleta_md}")
    print(f"  boleta_ejecucion_json: {boleta_json}")
    if not generated_prompts:
        print("  prompts_finales: ninguno (el shortlist no produjo eventos)")
    for row, prompt, response, instruction_file, instruction in generated_prompts:
        print(f"  prompt_cierre_llm [{row['fixture_id']}]: {prompt}")
        print(f"  resultado_cierre_llm [{row['fixture_id']}]: {response}")
        print(f"  archivo_instruccion_cierre_llm [{row['fixture_id']}]: {instruction_file}")
        print(f"  instruccion_cierre_llm [{row['fixture_id']}]: {instruction}")
    return 0
