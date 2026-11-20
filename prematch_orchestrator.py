#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI unificado V3.0 — Orquestador Multipartido con Top 3 Dinámico por Franjas."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from bet_engine.contracts.multi_slot import MADRID_TZ, MultiSlotContractError, parse_iso_utc
from bet_engine.orchestration.multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorConfig,
    scoped_external_confirmation,
)


def _parse_day(raw: str) -> date:
    if str(raw).strip().casefold() == "hoy":
        return datetime.now(MADRID_TZ).date()
    return date.fromisoformat(raw)


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    return parse_iso_utc(raw)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="Raíz del repositorio")
    parser.add_argument("--orchestrator-root", type=Path, default=None, help="Directorio destino del plan multipartido")
    parser.add_argument("--date", default="hoy", help="Fecha de ventana operativa YYYY-MM-DD o 'hoy'")
    parser.add_argument("--start", default="15:00", help="Hora inicio Europe/Madrid HH:MM (por defecto 15:00)")
    parser.add_argument("--end", default="03:00", help="Hora fin Europe/Madrid HH:MM (por defecto 03:00)")
    parser.add_argument(
        "--slots",
        default=None,
        help="Franjas personalizadas ej: 'FRANJA_A=15:00-18:00,FRANJA_B=18:00-21:00,FRANJA_C=21:00-03:00'",
    )
    parser.add_argument("--preselection", type=Path, default=None, help="Ruta explícita a preselection.json")
    parser.add_argument("--now-utc", default=None, help="Reloj de referencia ISO-8601 UTC/Madrid")
    parser.add_argument("--min-lead", type=int, default=45, help="Antelación mínima al kickoff en minutos")
    parser.add_argument("--lineup-cutoff", type=int, default=15, help="Corte PREMATCH de XI en minutos (T-15m)")
    parser.add_argument("--min-quality", type=float, default=20.0, help="Calidad mínima para elegibilidad")
    parser.add_argument("--min-readiness", type=float, default=40.0, help="Data readiness mínimo")
    parser.add_argument("--max-api-calls", type=int, default=40, help="Tope máximo de llamadas API")
    parser.add_argument("--max-scrapes", type=int, default=12, help="Tope máximo de capturas Bwin")
    parser.add_argument("--max-slot-fallbacks", type=int, default=2, help="Máximo fallbacks por franja")
    parser.add_argument("--require-bwin", action="store_true", help="Exigir event_id Bwin en ranking")
    parser.add_argument("--require-research", action="store_true", help="Pausar en WAITING_RESEARCH sin informe adjunto")
    parser.add_argument("--dry-run", action="store_true", help="Simular flujo sin escribir ni ejecutar llamadas externas")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--offline", action="store_true", help="Ejecución íntegramente offline/local (por defecto)")
    mode_group.add_argument("--online", action="store_true", help="Habilita dependencias online (requiere --confirm-external)")
    parser.add_argument(
        "--confirm-external",
        action="store_true",
        help="Confirmación explícita requerida para ejecutar llamadas externas con --online",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prematch_orchestrator.py",
        description="Bet Engine V3.0 — Orquestador Multipartido con Top 3 Dinámico por Franjas.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("plan", "Construye o carga el plan de franjas y ranking de la ventana."),
        ("top3", "Muestra el Top 3 global y el candidato líder por franja."),
        ("status", "Muestra el estado actual del plan persistido."),
        ("step", "Ejecuta un paso secuencial no bloqueante sobre las franjas."),
        ("run", "Ejecuta el ciclo secuencial reanudable de la ventana diaria."),
        ("resume", "Reanuda el expediente de ventana desde el último estado persistido."),
        ("summary", "Emite el resumen consolidado de la ventana."),
    ):
        sp = sub.add_parser(name, help=help_text)
        _add_common_flags(sp)
        if name == "run":
            sp.add_argument("--max-passes", type=int, default=3, help="Tope de pasadas por ejecución")

    sp_xi = sub.add_parser("attach-lineups", help="Incorpora JSON manual verificado de XI para un evento.")
    _add_common_flags(sp_xi)
    sp_xi.add_argument("--event-id", type=int, required=True, help="Bwin Event ID del partido")
    sp_xi.add_argument("--source", type=Path, required=True, help="Ruta al JSON manual de XI")

    sp_res = sub.add_parser("attach-research", help="Adjunta informe cualitativo a un evento.")
    _add_common_flags(sp_res)
    sp_res.add_argument("--event-id", type=int, required=True, help="Bwin Event ID del partido")
    sp_res.add_argument("--source", type=Path, required=True, help="Ruta al Markdown de investigación")

    sp_acq = sub.add_parser("acquire", help="Adquiere contexto API-Football y cuotas Bwin para los activos o un Event ID.")
    _add_common_flags(sp_acq)
    sp_acq.add_argument("--event-id", type=int, default=None, help="Bwin Event ID específico opcional")

    return parser


def _make_config(args: argparse.Namespace) -> OrchestratorConfig:
    if args.command == "acquire" and not args.dry_run:
        if not args.online or not args.confirm_external:
            raise MultiSlotContractError(
                "EXTERNAL_CONFIRMATION_REQUIRED: 'acquire' sin --dry-run requiere --online --confirm-external explícitos"
            )
    if args.online and not args.confirm_external and not args.dry_run:
        raise MultiSlotContractError(
            "EXTERNAL_CONFIRMATION_REQUIRED: --online sin --dry-run requiere --confirm-external explícito"
        )
    online = bool(args.online)
    return OrchestratorConfig(
        window_day=_parse_day(args.date),
        window_start_hhmm=args.start,
        window_end_hhmm=args.end,
        slots_spec=args.slots,
        min_lead_minutes=args.min_lead,
        lineup_cutoff_minutes=args.lineup_cutoff,
        min_quality_score=args.min_quality,
        min_readiness_score=args.min_readiness,
        require_bwin=bool(args.require_bwin),
        require_research=bool(args.require_research),
        max_api_calls=args.max_api_calls,
        max_scrapes=args.max_scrapes,
        max_slot_fallbacks=args.max_slot_fallbacks,
        offline=not online,
        online=online,
        confirm_external=bool(args.confirm_external),
        dry_run=bool(args.dry_run),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _make_config(args)
        now_utc = _parse_now(args.now_utc)
        orch = MultiSlotOrchestrator(
            args.root,
            config,
            orchestrator_root=args.orchestrator_root,
        )
        external_allowed = bool(config.online and config.confirm_external and not config.dry_run)

        with scoped_external_confirmation(
            enabled=external_allowed,
            payload={"command": args.command, "window": orch.window_label, "online": config.online, "confirm_external": config.confirm_external},
        ):
            if args.command == "plan":
                plan = orch.init_or_load_plan(now_utc=now_utc, explicit_preselection=args.preselection, check_staleness=True)
                payload = {"mode": "dry_run" if config.dry_run else "persisted", "plan_path": str(orch.plan_path), **orch.render_summary(plan)}
            elif args.command == "top3":
                plan = orch.init_or_load_plan(now_utc=now_utc, explicit_preselection=args.preselection, check_staleness=False)
                summary = orch.render_summary(plan)
                payload = {"window_label": plan.window_label, "plan_version": plan.plan_version, "preselection_path": plan.preselection_path, "top_3_global": summary["top_3_global"], "slots": summary["slots"]}
            elif args.command == "status":
                if not orch.plan_exists() and config.dry_run:
                    payload = {"window_label": orch.window_label, "status": "DRY_RUN_NO_PERSISTED_PLAN", "plan_path": str(orch.plan_path)}
                else:
                    plan = orch.load_plan()
                    payload = orch.render_summary(plan)
            elif args.command == "step":
                plan = orch.step(now_utc=now_utc, explicit_preselection=args.preselection)
                payload = orch.render_summary(plan)
            elif args.command in {"run", "resume"}:
                max_passes = getattr(args, "max_passes", 3)
                plan = orch.run_cycle(now_utc=now_utc, explicit_preselection=args.preselection, max_passes=max_passes)
                payload = orch.render_summary(plan)
            elif args.command == "attach-lineups":
                if config.dry_run:
                    payload = {"mode": "dry_run", "would_attach_lineups": {"event_id": args.event_id, "source": str(args.source)}}
                else:
                    plan = orch.attach_manual_lineup_file(event_id=args.event_id, manual_json_path=args.source, now_utc=now_utc)
                    payload = orch.render_summary(plan)
            elif args.command == "attach-research":
                if config.dry_run:
                    payload = {"mode": "dry_run", "would_attach_research": {"event_id": args.event_id, "source": str(args.source)}}
                else:
                    plan = orch.attach_research_file(event_id=args.event_id, research_path=args.source, now_utc=now_utc)
                    payload = orch.render_summary(plan)
            elif args.command == "acquire":
                if config.dry_run:
                    plan = orch.load_plan() if orch.plan_exists() else orch.init_or_load_plan(now_utc=now_utc, explicit_preselection=args.preselection)
                    payload = {"mode": "dry_run", "would_acquire_event_id": args.event_id, **orch.render_summary(plan)}
                else:
                    plan = orch.acquire_events(now_utc=now_utc, event_id=args.event_id)
                    payload = orch.render_summary(plan)
            elif args.command == "summary":
                plan = orch.init_or_load_plan(now_utc=now_utc, explicit_preselection=args.preselection)
                payload = orch.render_summary(plan)
            else:
                parser.error(f"Comando desconocido: {args.command}")
                return 2

        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (MultiSlotContractError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
