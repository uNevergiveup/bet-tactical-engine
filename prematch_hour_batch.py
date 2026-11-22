"""Commercial screening and shortlist refresh on an existing PREMATCH run."""
from __future__ import annotations

import argparse
import json
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


def _scope(args, steps, now):
    root, run, _, _ = steps._resolve_eval_dir(args, now)
    path = steps._require_file(run / "fixtures.json", "discover")
    window = steps._read_json(path).get("window") or {}

    def civil(key):
        stamp = str(window.get(key) or "").split(" ")[0]
        if not stamp:
            raise ValueError(f"HOUR_BATCH_MISSING_ORIGINAL_WINDOW:{path}")
        value = datetime.fromisoformat(stamp)
        return value.astimezone(steps.trial.TZ) if value.tzinfo else value.replace(tzinfo=steps.trial.TZ)

    begin, end = civil("start_madrid"), civil("end_madrid")
    day = begin.date()
    days = []
    while day <= end.date():
        slot = datetime.combine(day, args.hour, tzinfo=steps.trial.TZ)
        if slot <= end and slot + timedelta(hours=1) > begin:
            days.append(slot)
        day += timedelta(days=1)
    if args.date:
        days = [slot for slot in days if slot.date() == steps.df.parse_day(args.date)]
    if len(days) != 1:
        raise ValueError("HOUR_OUTSIDE_OR_AMBIGUOUS_IN_RUN: comprueba --hour y, si procede, --date Madrid")
    slot = days[0]
    return root, run, slot, run / "hour_batches" / slot.strftime("%Y-%m-%d_%H%M")


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
    from bet_engine.modeling.distributions import CoherentMatchModeler
    from prematch_tactical_signal_engine import classify_market

    bundle = ObservationStore.from_preselection_match(pre_match, cutoff_utc=now)
    model = CoherentMatchModeler.build_model(bundle)
    # Use the exact catalogued price and rule. Conflicting quotes for the same
    # canonical key stay exploratory rather than selecting the favourable price.
    prices = defaultdict(set)
    for entry in allowed:
        if entry.get("contract_id") and entry.get("settlement_status") == "VERIFIED_STANDARD":
            prices[entry["canonical_key"]].add(float(entry["odd"]))
    odds = {key: next(iter(values)) for key, values in prices.items() if len(values) == 1}
    evaluated = {opt.selection_key: opt for opt in
                 BwinMarketEvaluator.evaluate_standard_markets(bundle, model, odds_map=odds) if opt.odd is not None}
    signals = [s for s in tactical.get("signals") or [] if s.get("status") == "SUPPORTED_DESCRIPTIVE_SIGNAL"]
    options = []
    for entry in allowed:
        if entry["selection_id"] in player_options:
            options.append({
                **build_comparison_option(entry, price_validity=price_status),
                "signals": [], "direction": entry["direction"], "contract_id": entry.get("contract_id"),
                "model_evidence": None, "player_evidence": player_options[entry["selection_id"]],
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
                          and option.raw_sample_n is not None and option.raw_sample_n >= 4
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
            "p_low": option.p_low if accredited else None,
            "p_high": option.p_high if accredited else None,
            "conservative_ev": option.ev_conservative_low if accredited and price_status["status"] == "VALID_PREMATCH" else None,
            "diagnostic_uncalibrated": (
                {"estimate": option.p_model, "low": option.p_low, "high": option.p_high}
                if not accredited and (option.raw_sample_n or 0) >= 4 and option.p_model is not None else None
            ),
        } if option else None)
        options.append(row)
    return sorted(options, key=_option_order)


def _option_order(row):
    ev = (row.get("model_evidence") or {}).get("conservative_ev")
    direct = any(s["relation"] == "DIRECT_TEAM_MARKET" for s in row["signals"])
    return (ev is None, -(ev or 0), not direct, -max((s["score"] for s in row["signals"]), default=0),
            row["market_header_literal"], row["selection_literal"])


def _target_row(target):
    return {key: target[key] for key in ("fixture_id", "bwin_event_id", "home", "away", "kickoff_madrid", "kickoff_utc")}


def _write_markdown(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_agent_prompt(prompt, output, lines):
    instruction = (f'Lee "{prompt}" y los archivos locales que referencia. '
                   f'Sigue el encargo y guarda el resultado directamente en "{output}".')
    instruction_file = prompt.with_name("instruccion_agente_local.txt")
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
    path = output / "events" / str(target["fixture_id"]) / "catalogo.json"
    steps._atomic_json(path, catalog)
    return {
        "status": "REVISABLE" if options and price_status["status"] == "VALID_PREMATCH" and not needs_todo else "COBERTURA_LIMITADA",
        "reason": ("TODO_VIEW_NOT_VERIFIED" if can_capture else "TODO_REFRESH_LEAD_BELOW_45") if needs_todo else None,
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
        "Indica qué cambio concreto de XI, rol o precio cambiaría el análisis. Un vínculo indirecto equipo→total requiere justificar la contribución del rival.",
        "En cada candidato escribe requires_confirmed_xi como booleano. Usa false solo si es un mercado colectivo y "
        "la tesis se sostiene con las variantes plausibles de XI. Explica esas variantes en lineup_condition. "
        "Usa true si depende de una titularidad, un rol, minutos o una estructura aún por confirmar.",
        "Compara el precio con una estimación acreditada para el mismo contrato. Usa 1/cuota como umbral bruto en contratos binarios sin devolución.",
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


def prepare(args, steps):
    now = steps._now_utc()
    root, run, slot, folder = _scope(args, steps, now)
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
        and slot <= _kickoff(steps, r).astimezone(steps.trial.TZ) < slot + timedelta(hours=1)
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
              "created_at_utc": steps._now_utc().isoformat(), "fixtures": results}
    path = output / "lote.json"
    steps._atomic_json(path, packet)
    prompt = output / "encargo_codex_argon.md"
    response = output / "prefinalistas.json"
    lines = [
        f"# Revisión comercial del lote {slot:%Y-%m-%d %H:%M} Europe/Madrid", "",
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
    instruction_file, instruction = _write_agent_prompt(prompt, response, lines)
    print(f"  lote_json: {path}")
    print(f"  prompt_codex_argon: {prompt}")
    print(f"  prefinalistas_json: {response}")
    print(f"  archivo_instruccion_agente_local: {instruction_file}")
    if args.export_gemini:
        _export_gemini(steps, output, packet, response)
    print(f"  instruccion_agente_local: {instruction}")
    return 0


def _read_shortlist(steps, run, slot, folder, path):
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        kind = "DIRECTORY" if resolved.is_dir() else "MISSING"
        raise ValueError(
            f"HOUR_SHORTLIST_FILE_REQUIRED:{kind}:{resolved} "
            "(--shortlist debe apuntar al archivo prefinalistas.json, no al directorio del paquete)"
        )
    response = steps._read_json(resolved)
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
        if (not isinstance(item, dict) or not keys <= item.keys()
                or item.keys() - keys - {"watch_players", "requires_confirmed_xi"}
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
    return packet_id, list(selected.values())


def _research_questions(row, candidates, mode):
    families = sorted({option["market_family"] for _, option in candidates})
    questions = [
        "Comprueba bajas, sanciones, dudas, regresos, entrenador actual, cambios recientes y adaptación de la plantilla.",
        "Revisa XI oficial si ya existe y, mientras no exista, XI probables, variantes, rotaciones, roles y gestión de minutos.",
        "Contrasta estrategias, necesidad según la tabla, descanso, viajes y partidos siguientes con fuentes fechadas.",
        "Busca designación arbitral, clima o superficie solo cuando puedan afectar las familias candidatas.",
        "Distingue fuentes primarias, prensa local, rumores y repeticiones de la misma fuente. Registra contradicciones.",
    ]
    if "ON_TARGET_COLLECTIVE" in families:
        questions.append(
            "Para tiros a portería, usa Opta/Stats Perform como proveedor de liquidación Bwin según la política aportada. "
            "Separa esa liquidación del histórico API-Football y señala cualquier diferencia documentada de definición o corrección."
        )
    if mode == "update":
        questions.append("Prioriza novedades posteriores al informe anterior y explica qué condición del candidato cambió.")
    for item, option in candidates:
        questions.append(
            f"Audita `{option['market_header_literal']} → {option['selection_literal']}`. Tesis: {item['case_for']} "
            f"Condición declarada: {item['lineup_condition']}"
        )
    return questions


def research(args, steps):
    """Create one portable, fixture-specific manual research handoff per shortlisted event."""
    from bet_engine.analytics.early_tactical_scout import RESEARCH_PLACEHOLDER

    now = steps._now_utc()
    _, run, slot, folder = _scope(args, steps, now)
    packet_id, selections = _read_shortlist(steps, run, slot, folder, args.shortlist)
    packet_root = folder / "packets" / packet_id
    packet = steps._read_json(packet_root / "lote.json")
    rows = {row["fixture_id"]: row for row in packet["fixtures"]}
    grouped = defaultdict(list)
    for item, row, option in selections:
        if not args.fixture_id or item["fixture_id"] in args.fixture_id:
            grouped[item["fixture_id"]].append((item, option))
    if not grouped:
        raise ValueError("NO_SHORTLIST_FIXTURES_SELECTED_FOR_RESEARCH")

    for fid, candidates in sorted(grouped.items()):
        row = rows[fid]
        eid = str(row["bwin_event_id"])
        research_root = packet_root / "events" / str(fid) / "research"
        requests = research_root / "requests"
        responses = research_root / "responses"
        prompt = requests / f"encargo_{args.provider}_{eid}_{args.research_mode}.md"
        inventory = requests / f"insumos_{args.provider}_{eid}_{args.research_mode}.json"
        response = responses / f"{args.provider}_{eid}_{args.research_mode}.md"
        catalog = steps._read_json(Path(row["catalog"]))
        families = {option["market_family"] for _, option in candidates}
        relevant_quotes = [entry for entry in catalog["entries"] if entry["market_family"] in families]
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
        lines = [
            f"# Investigación {args.research_mode} · {row['home']} – {row['away']} · {args.provider}", "",
            f"Fixture `{fid}` · Event ID `{eid}` · kickoff `{row['kickoff_madrid']}`.",
            f"Lee el archivo adjunto `{inventory.name}`. Analiza únicamente este evento y sus candidatos.",
            "La investigación externa es opcional y no decide BET/NO BET. Aporta hechos cualitativos, fuentes y contradicciones al cierre.",
            "Usa información disponible antes del kickoff. Incluye URL, medio, fecha de publicación y relación entre fuentes repetidas.",
            "Clasifica cada hallazgo como CONFIRMADO_OFICIAL, PUBLICADO_PREPARTIDO, PROBABLE, INFERIDO o NO_VERIFICADO.",
            "No inventes probabilidades, cuotas actuales, alineaciones ni reglas ausentes. La cuota adjunta es un snapshot.",
            "Revisa reportes, bajas, rumores, entrenador, alineaciones, estrategias, necesidad, tabla, descanso y partidos siguientes.",
            "Si una conclusión depende del XI, indica la condición exacta y el mercado afectado.", "",
            "## Preguntas específicas", "", *[f"- {question}" for question in questions], "",
            "## Entrega", "",
            f"Devuelve Markdown destinado a `{response}` con identidad, corte declarado, hechos y fuentes, contradicciones, "
            "impacto por candidato, faltantes críticos y una conclusión cualitativa. No emitas la decisión financiera final.",
        ]
        _write_markdown(prompt, lines)
        print(f"[hour-batch-research] {fid} {args.provider} {args.research_mode}")
        print(f"  research_prompt: {prompt}")
        print(f"  research_input: {inventory}")
        print(f"  research_response: {response}")
    return 0


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
    matches = [e for e in catalog["entries"] if _usable_offer(e) and e["selection_id"] == before["selection_id"]
               and e["market_header_literal"] == before["market_header_literal"]
               and e["selection_literal"] == before["selection_literal"]]
    after = matches[0] if len(matches) == 1 else None
    scope = ("market_family", "target_scope", "temporal_scope", "target_entity", "direction")
    alternatives = [
        {key: e[key] for key in ("selection_id", "selection_literal", "market_header_literal", "line", "odd", "settlement_status")}
        for e in catalog["entries"] if _usable_offer(e) and e["line"] is not None and e["line"] != before["line"]
        and all(e.get(key) == before.get(key) for key in scope)
    ]
    if after is None:
        return {"status": "SELECCION_ORIGINAL_NO_LOCALIZADA", "odd_before": before["odd"], "line_before": before["line"],
                "neighboring_lines": alternatives}
    return {"status": "COMPARABLE", "line_before": before["line"], "line_after": after["line"],
            "odd_before": before["odd"], "odd_after": after["odd"],
            "settlement_before": before["settlement_status"], "settlement_after": after["settlement_status"],
            "canonical_key_after": after["canonical_key"],
            "odd_delta": str(Decimal(after["odd"]) - Decimal(before["odd"])), "neighboring_lines": alternatives}


def _refresh_event(steps, args, root, run, output, target, previous):
    now = steps._now_utc()
    early = args.lineup_mode == "sin-xi"
    minimum = steps.ANALYSIS_MIN_LEAD_MINUTES if early else CONFIRMED_XI_MIN_LEAD
    if not target["temporal_eligible"] or _lead(steps, target, now) < minimum:
        return {"status": "CIERRE_OPERATIVO", "reason": f"MODEL_LEAD_BELOW_{minimum}"}
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
    if not valid_second_capture(snapshot, now) and args.confirm_external and _lead(steps, target, now) >= capture_minimum:
        snapshot = _capture_final(steps, root, run, target) if early else _capture(steps, root, run, target, min_lead=CONFIRMED_XI_MIN_LEAD)
        now = steps._now_utc()
    if not valid_second_capture(snapshot, now):
        price_reason = "FRESH_CAPTURE_AFTER_INITIAL_REQUIRED" if early else "RECENT_CAPTURE_AFTER_INITIAL_REQUIRED"
        return {"status": "PRECIO_PENDIENTE", "lineup": lineup, "snapshot": snapshot,
                "reason": price_reason if _lead(steps, target, now) >= capture_minimum else f"FINAL_CAPTURE_LEAD_BELOW_{capture_minimum}"}
    catalog = _catalog(target, snapshot)
    catalog_path = output / "catalogo_cierre.json"
    steps._atomic_json(catalog_path, catalog)
    comparisons = [
        {**item, "initial_snapshot": row["snapshot"], "market_header_literal": option["market_header_literal"],
         "selection_literal": option["selection_literal"], "settlement_status": option["settlement_status"],
         "canonical_key": option["canonical_key"], "settlement_provenance": option.get("settlement_provenance"),
         "price": _compare_price(option, catalog)}
        for item, row, option in previous
    ]
    for comparison, (_, _, option) in zip(comparisons, previous):
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
              "catalog": str(catalog_path), "comparisons": comparisons}
    if not any(row["price"]["status"] == "COMPARABLE" for row in comparisons):
        return result
    if not any(row["price"]["status"] == "COMPARABLE" and row["price"].get("settlement_after") == "VERIFIED_STANDARD"
               for row in comparisons):
        return {**result, "status": "LIQUIDACION_PENDIENTE", "reason": "SETTLEMENT_UNVERIFIED"}
    if _lead(steps, target, steps._now_utc()) < minimum:
        return {**result, "status": "CIERRE_OPERATIVO", "reason": f"MODEL_LEAD_BELOW_{minimum}"}
    early_keys = frozenset(row["price"].get("canonical_key_after") for row in comparisons if early
                           and row["price"]["status"] == "COMPARABLE"
                           and row["price"].get("settlement_after") == "VERIFIED_STANDARD"
                           and row["price"].get("canonical_key_after"))
    if early and not early_keys:
        return {**result, "reason": "NO_COMPARABLE_VERIFIED_CANONICAL_SELECTION"}
    model_dir = output / "model"
    stages = argparse.Namespace(
        command="model", root=root, eval_dir=run, asof=None, fixture_id=target["fixture_id"],
        event_id=eid, min_lead=minimum, odds_file=Path(snapshot["path"]), output_dir=model_dir,
        lineup_independent_keys=early_keys,
    )
    steps.cmd_model(stages)
    model_path = model_dir / f"model_report_{eid}.json"
    report = steps._read_json(model_path)
    finished = steps._now_utc()
    status, reason = "LISTO_REVISION_FINAL", None
    if _lead(steps, target, finished) < minimum:
        status, reason = "CIERRE_OPERATIVO", "MODEL_FINISHED_BELOW_T30"
    elif not valid_second_capture(snapshot, finished):
        status, reason = "PRECIO_PENDIENTE", "PRICE_EXPIRED_DURING_MODEL"
    options = {row.get("selection_key"): row for row in report.get("market_comparisons") or []}
    for row in comparisons:
        option = options.get(row["price"].get("canonical_key_after"))
        if (option and row["price"]["status"] == "COMPARABLE" and option.get("odd") is not None
                and Decimal(str(option["odd"])) == Decimal(row["price"]["odd_after"])
                and row["price"].get("settlement_after") == "VERIFIED_STANDARD"):
            row["shadow_option"] = {k: option.get(k) for k in (
                "calibration_status", "decision_category", "raw_sample_n", "effective_sample_n", "pending_conditions",
            )}
            if option.get("calibration_status") == "CALIBRATED_OUT_OF_SAMPLE":
                row["shadow_option"].update({k: option.get(k) for k in ("p_model", "p_low", "p_high", "ev_conservative_low")})
    return {**result, "status": status, "reason": reason, "model_json": str(model_path), "model_sha256": steps._sha256_file(model_path),
            "model_decision": report.get("decision"), "model_classification": report.get("decision_classification"),
            "model_reasons": report.get("no_bet_reasons"),
            "model_deadline_utc": (_kickoff(steps, target) - timedelta(minutes=minimum)).isoformat(),
            "price_deadline_utc": (steps._now_utc(snapshot["captured_at_utc"]) + timedelta(minutes=FINAL_PRICE_AGE)).isoformat()}


def refresh(args, steps):
    now = steps._now_utc()
    root, run, slot, folder = _scope(args, steps, now)
    if args.manual_lineups_json and (args.lineup_mode != "xi-confirmado" or len(args.fixture_id) != 1):
        raise ValueError("MANUAL_LINEUPS_REQUIRE_XI_CONFIRMED_AND_ONE_FIXTURE_ID")
    packet_id, selections = _read_shortlist(steps, run, slot, folder, args.shortlist)
    if args.fixture_id:
        selections = [item for item in selections if item[0]["fixture_id"] in args.fixture_id]
        if not selections:
            raise ValueError("NO_SHORTLIST_CANDIDATES_FOR_REQUESTED_FIXTURE")
    output = folder / "packets" / packet_id / "refresh" / uuid4().hex
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
        eligible, pending = [], []
        for item, row, option in previous:
            reason = None
            if args.lineup_mode == "sin-xi":
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
            result = _refresh_event(steps, args, root, run, event_output, target, eligible)
        except (steps.StepError, OSError, ValueError, RuntimeError) as exc:
            result = {"status": "ERROR_REINTENTABLE", "reason": str(exc)}
        record = {**_target_row(target or previous[0][1]), **result, "pending_lineup_candidates": pending}
        results.append(record)
    summary = output / "cierre.json"
    published = steps._now_utc()
    for row in results:
        if row["status"] != "LISTO_REVISION_FINAL":
            continue
        if published > steps._now_utc(row["model_deadline_utc"]):
            row.update(status="CIERRE_OPERATIVO", reason="PUBLISHED_BELOW_T30")
        elif published > steps._now_utc(row["price_deadline_utc"]):
            row.update(status="PRECIO_PENDIENTE", reason="PRICE_EXPIRED_DURING_BATCH")
    for row in results:
        steps._atomic_json(output / str(row["fixture_id"]) / "estado.json", row)
        reason = f" | {row['reason']}" if row.get("reason") else ""
        print(f"[hour-batch-refresh] {row['fixture_id']} {row['status']}{reason}")
    steps._atomic_json(summary, {"packet_id": packet_id, "eval_dir": str(run), "hour_madrid": slot.isoformat(),
                                 "shortlist_sha256": steps._sha256_file(args.shortlist.expanduser().resolve()),
                                 "evaluated_at_utc": published.isoformat(), "lineup_mode": args.lineup_mode, "fixtures": results})
    prompt = output / "revision_final_codex_argon.md"
    response = output / "resultado_final.md"
    original = folder / "packets" / packet_id / "lote.json"
    lines = [
        f"# Revisión final del lote {slot:%Y-%m-%d %H:%M} Europe/Madrid", "",
        f"Lee el cierre `{summary}` y el lote inicial `{original}`. Sigue las rutas locales de ambos snapshots, "
        "catálogos, registro de XI y reporte Shadow para los prefinalistas que vas a evaluar.",
        f"Modalidad de cierre {args.lineup_mode}.",
        "Revisa la tesis con el precio nuevo del mismo contrato. Una línea vecina requiere evaluación propia.",
        "En sin-xi, verifica la robustez de cada candidato a los escenarios plausibles de XI. Registra qué información sigue incierta. "
        "Si la tesis termina dependiendo de un XI concreto, déjala pendiente. Excluye los pending_lineup_candidates de la decisión financiera. "
        "En xi-confirmado, comprueba las condiciones de la tesis con la alineación registrada.",
        "Consulta el modelo Shadow y distingue precio, calibración, liquidación y condición futbolística. Una hipótesis no habilita un PICK.",
        "Solo considera para PICK los contratos COMPARABLES de eventos LISTO_REVISION_FINAL cuya opción de Shadow "
        "esté marcada PICK_ACCIONABLE. Coteja el precio y el margen al decidir.",
        f"Escribe el resultado directamente en `{response}`. Incluye a lo sumo un PICK defendible o NO BET financiero. "
        "Si todos los candidatos están pendientes o el lote cerró por tiempo, registra solo el estado operativo. "
        "Si hay candidatos evaluados y otros pendientes, registra los pendientes por separado.",
    ]
    for row in results:
        lines.extend(["", f"## {row['fixture_id']} · {row['home']} – {row['away']}",
                      f"Inicio {row['kickoff_madrid']} · {row['status']} · {row.get('reason') or ''}"])
        if row.get("model_json"):
            lines.append(f"- Shadow {row['model_decision']} ({row['model_classification']}) · `{row['model_json']}` · {row['model_reasons']}")
            lines.append(f"- Límites UTC · margen de modelo {row['model_deadline_utc']} · vigencia de captura {row['price_deadline_utc']}.")
        if row.get("snapshot"):
            lines.append(f"- Captura actual `{row['snapshot']['path']}` · {row['snapshot']['captured_at_utc']} UTC.")
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
        eid = str(row.get("bwin_event_id") or "")
        response_dir = original.parent / "events" / str(row["fixture_id"]) / "research" / "responses"
        qualitative_candidates = [
            *sorted(response_dir.glob(f"gemini_{eid}_*.md")),
            response_dir / f"gemini_{eid}.md",
            original.parent / "events" / str(row["fixture_id"]) / f"gemini_{eid}.md",
            root / "outputs/matches" / eid / "research" / f"gemini_{eid}.md",
        ]
        seen_reports = set()
        for report_path in qualitative_candidates:
            if report_path in seen_reports:
                continue
            seen_reports.add(report_path)
            raw_report = report_path.read_text(encoding="utf-8", errors="replace") if report_path.is_file() else ""
            if raw_report.strip() and "PREMATCH: INFORME PENDIENTE" not in raw_report:
                lines.append(f"- Informe cualitativo Gemini detectado `{report_path}`. Integra sus hechos, fechas, fuentes y contradicciones sin convertirlo en decisión automática.")
        for item in row.get("comparisons", []):
            lines.append("- " + json.dumps(item, ensure_ascii=False))
    if not results:
        lines.extend(["", "El análisis inicial dejó la lista de prefinalistas vacía."])
    instruction_file, instruction = _write_agent_prompt(prompt, response, lines)
    print(f"  cierre_json: {summary}")
    print(f"  prompt_revision_final: {prompt}")
    print(f"  resultado_final: {response}")
    print(f"  archivo_instruccion_agente_local: {instruction_file}")
    print(f"  instruccion_agente_local: {instruction}")
    return 0
