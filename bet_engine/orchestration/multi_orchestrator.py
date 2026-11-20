"""Etapas 3, 4 y 5 V3.0: Orquestador secuencial reanudable por franjas, XI P0 y aislamiento Shadow."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from bet_engine.context.lineup_manager import (
    evaluate_fixture_lineup_state,
    prepare_manual_lineup_inbox,
    scan_manual_lineup_inbox,
    validate_manual_lineup_payload,
)
from bet_engine.contracts.multi_slot import (
    ACTIVE_SLOT_STATES,
    CLOSED_DOSSIER_STATES,
    MADRID_TZ,
    PROTECTED_DOSSIER_STATES,
    MultiMatchExecutionState,
    MultiSlotContractError,
    ResourceBudgetLedger,
    SlotCandidateRecord,
    TimeSlotSpec,
    WindowOrchestrationPlan,
    build_window_slots,
    parse_custom_slots_spec,
    parse_iso_utc,
)
from bet_engine.orchestration.adapters import run_shadow_for_event_dir
from bet_engine.orchestration.multi_ranker import (
    apply_dynamic_ranking_update,
    populate_and_rank_plan,
    select_best_eligible_for_slot,
)

CONFIRMED_XI_STATES = frozenset(
    {"LINEUPS_API_CONFIRMED", "LINEUPS_EXTERNAL_CONFIRMED", "LINEUPS_MANUAL_VERIFIED"}
)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(path)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class OrchestratorConfig:
    window_day: date
    window_start_hhmm: str = "15:00"
    window_end_hhmm: str = "03:00"
    slots_spec: str | None = None
    min_lead_minutes: int = 45
    lineup_cutoff_minutes: int = 15
    min_quality_score: float = 20.0
    min_readiness_score: float = 40.0
    require_bwin: bool = False
    require_research: bool = False
    max_api_calls: int = 40
    max_scrapes: int = 12
    max_slot_fallbacks: int = 2
    max_attempts_per_match: int = 5
    horizon_hours: float = 0.0
    max_matches_batch_size: int = 5
    competition_scope: str = "adult-open"
    offline: bool = True
    online: bool = False
    confirm_external: bool = False
    dry_run: bool = False
    require_full_case_artifacts: bool = True


@dataclass
class OrchestratorAdapters:
    """Puertos inyectables para ejecución offline/mock o reutilización de los módulos reales."""

    preselection_provider: Callable[[Path, date, str, str], Path] | None = None
    bwin_discovery_provider: Callable[[list[dict[str, Any]], WindowOrchestrationPlan, datetime], dict[int, dict[str, Any]]] | None = None
    case_preparer: Callable[[Path, SlotCandidateRecord, dict[str, Any], datetime], dict[str, Any]] | None = None
    context_and_odds_acquirer: Callable[[Path, SlotCandidateRecord, dict[str, Any], datetime], dict[str, Any]] | None = None
    api_lineup_fetcher: Callable[[SlotCandidateRecord, datetime], Any] | None = None
    odds_refresher: Callable[[Path, SlotCandidateRecord, datetime], dict[str, Any]] | None = None
    shadow_evaluator: Callable[[Path, SlotCandidateRecord, datetime, int, bool], dict[str, Any]] | None = None


@contextlib.contextmanager
def scoped_external_confirmation(
    *,
    enabled: bool,
    payload: dict[str, Any] | None = None,
):
    """Propaga PREMATCH_EXTERNAL_CONFIRMATION solo bajo autorización explícita, restaurando entorno al salir."""
    previous = os.environ.get("PREMATCH_EXTERNAL_CONFIRMATION")
    if enabled:
        from prematch_execution_control import digest

        os.environ["PREMATCH_EXTERNAL_CONFIRMATION"] = digest(payload or {"explicit_orchestrator": True})
    else:
        os.environ.pop("PREMATCH_EXTERNAL_CONFIRMATION", None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PREMATCH_EXTERNAL_CONFIRMATION", None)
        else:
            os.environ["PREMATCH_EXTERNAL_CONFIRMATION"] = previous


def _build_prospective_trial_args(
    config: OrchestratorConfig,
    *,
    now_utc: datetime | None = None,
) -> Any:
    """Construye el Namespace compatible con `trial.resolve_evaluation` y `trial.prepare`."""
    import argparse
    import prematch_prospective_trial as trial

    if int(config.min_lead_minutes) < 45:
        raise MultiSlotContractError(
            f"PREPARATION_MINIMUM_45_MINUTES: min_lead_minutes={config.min_lead_minutes} < 45"
        )
    ref_dt = (parse_iso_utc(now_utc) if now_utc is not None else trial.now_madrid()).astimezone(trial.TZ)
    confirmed_external = bool(config.online and config.confirm_external and not config.dry_run)
    return argparse.Namespace(
        date=config.window_day.isoformat(),
        start=trial.parse_clock(config.window_start_hhmm),
        end=trial.parse_clock(config.window_end_hhmm),
        window="normal",
        min_lead=int(config.min_lead_minutes),
        horizon_hours=float(config.horizon_hours),
        max_matches=max(1, int(config.max_matches_batch_size)),
        bwin_top=5,
        history=20,
        details=8,
        team=[],
        asof=None,
        evaluation_budget=0,
        discovery_budget=0,
        coverage_rules=None,
        competition_scope=str(config.competition_scope or "adult-open"),
        confirm_external=confirmed_external,
        execution_reference=ref_dt,
    )


def resolve_or_generate_preselection(
    root: Path,
    config: OrchestratorConfig,
    *,
    explicit_preselection: Path | None = None,
    adapters: OrchestratorAdapters | None = None,
    now_utc: datetime | None = None,
) -> tuple[Path | None, bool]:
    """Localiza `preselection.json` de la ventana exacta o encadena la generación faltante sin cruzar fechas."""
    adapters = adapters or OrchestratorAdapters()
    window_label = f"{config.window_day.isoformat()}_{config.window_start_hhmm.replace(':', '')}_{config.window_end_hhmm.replace(':', '')}"

    if explicit_preselection is not None:
        p = Path(explicit_preselection).resolve()
        if p.is_file():
            return p, False
        if config.dry_run:
            return None, False

    def _is_current_filter_version(pre_path: Path) -> bool:
        sibling_major = pre_path.parent.parent.parent / "fixtures_major.json"
        if not sibling_major.is_file():
            return True
        try:
            from filter_major_leagues import FILTER_IMPLEMENTATION
            maj = json.loads(sibling_major.read_text(encoding="utf-8"))
            impl = maj.get("filter_implementation")
            return impl is None or impl == FILTER_IMPLEMENTATION
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    window_dir = Path(root) / "outputs" / "discovery" / window_label
    eval_root = window_dir / "evaluations"
    force_new_online = bool(config.online and config.confirm_external and not config.dry_run)
    if eval_root.is_dir():
        candidates = sorted(eval_root.glob("*/prospective_trial/enriched/preselection.json"))
        # Priorizar evaluaciones compatibles con el filtro actual en cualquier modo.
        for cand in reversed(candidates):
            resolved = cand.resolve()
            if _is_current_filter_version(resolved):
                return resolved, False
        if not force_new_online and candidates:
            return candidates[-1].resolve(), False
    direct = window_dir / "prospective_trial" / "enriched" / "preselection.json"
    if direct.is_file():
        resolved_direct = direct.resolve()
        if _is_current_filter_version(resolved_direct) or not force_new_online:
            return resolved_direct, False

    if config.dry_run:
        return None, False

    if adapters.preselection_provider is not None:
        generated = adapters.preselection_provider(
            Path(root), config.window_day, config.window_start_hhmm, config.window_end_hhmm
        )
        return Path(generated).resolve(), True

    if config.online and not config.confirm_external:
        raise MultiSlotContractError(
            "EXTERNAL_CONFIRMATION_REQUIRED: --online requiere --confirm-external explícito"
        )

    if config.online and config.confirm_external:
        import prematch_prospective_trial as trial

        ns = _build_prospective_trial_args(config, now_utc=now_utc)
        window_folder = trial.window_path(Path(root), config.window_day, ns.start, ns.end)
        # Reutilizar snapshot interrumpido directo en window_folder si ya superó discovery y filtro con hashes íntegros.
        if (
            not (window_folder / "prospective_trial" / "prospective_test_summary.json").exists()
            and trial._intact_pre_enrichment_snapshot(window_folder, window_folder / "prospective_trial")
        ):
            folder = window_folder
            subset = trial.read(window_folder / "prospective_trial" / "fixtures_major_test.json")
            frozen_ref = trial.dt((subset.get("trial_subset") or {}).get("reference_madrid"))
            if frozen_ref is not None:
                ns.execution_reference = frozen_ref
        else:
            folder, eval_mode = trial.resolve_evaluation(
                window_folder, config.window_day, ns, ns.execution_reference
            )
            if eval_mode == "RESUME_FROZEN_EVALUATION":
                frozen_ref = trial.dt(trial.evaluation_manifest(folder).get("reference_madrid"))
                if frozen_ref is not None:
                    ns.execution_reference = frozen_ref

        previous_scope = os.environ.get("PREMATCH_COMPETITION_SCOPE")
        os.environ["PREMATCH_COMPETITION_SCOPE"] = ns.competition_scope
        logs_dir = folder / "prospective_trial" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript = logs_dir / f"orchestrator_prepare_{trial.now_madrid():%Y%m%dT%H%M%S%f}.log"
        try:
            with (
                scoped_external_confirmation(
                    enabled=True,
                    payload={"window": window_label, "folder": str(folder), "online": True, "confirm_external": True},
                ),
                transcript.open("w", encoding="utf-8") as log_out,
                contextlib.redirect_stdout(log_out),
            ):
                summary = trial.prepare(Path(root), folder, config.window_day, ns)
        finally:
            if previous_scope is None:
                os.environ.pop("PREMATCH_COMPETITION_SCOPE", None)
            else:
                os.environ["PREMATCH_COMPETITION_SCOPE"] = previous_scope
        pre_path = Path(summary["sources"]["preselection"]).resolve()
        return pre_path, True

    raise MultiSlotContractError(
        f"PRESELECTION_MISSING_FOR_WINDOW:{window_label}: proporcione --preselection, --online --confirm-external o un proveedor offline"
    )


_CANONICAL_CASE_SCHEMA = "2.0-canonical-prematch-case"
_BWIN_URL_RE = re.compile(r"-2:(\d{6,})$")
_DATA_READY_MANIFEST_STATUSES = frozenset(
    {
        "PREPARADO",
        "PREPARADO_PROVISIONAL",
        "VALIDADO",
        "FINALIZADO",
        "BLOQUEADO_VALIDACION",
        "ERROR_VALIDACION",
        "ERROR_MOTOR",
    }
)


def _canonical_ref(path: Path) -> dict[str, str]:
    p = Path(path).resolve()
    return {"path": str(p), "sha256": file_sha256(p)}


def _valid_bwin_url_for_event(url: str | None, event_id_str: str) -> bool:
    if not url or not str(event_id_str).isdigit():
        return False
    parsed = urlsplit(str(url).strip())
    if parsed.scheme != "https" or parsed.hostname not in ("bwin.es", "www.bwin.es"):
        return False
    m = _BWIN_URL_RE.search(unquote(parsed.path.rstrip("/")))
    return bool(m and m.group(1) == str(event_id_str))


def _resolve_candidate_bwin_url(
    candidate: SlotCandidateRecord,
    raw_fixture: dict[str, Any],
    bwin_row: dict[str, Any] | None = None,
) -> str | None:
    for src in (raw_fixture, bwin_row or {}):
        raw_url = src.get("bwin_url") or src.get("url")
        if raw_url and str(raw_url).strip():
            return str(raw_url).strip()
    return None


def _is_incomplete_v3_stub_case(event_dir: Path, event_id_str: str) -> bool:
    case_path = event_dir / "case.json"
    if not case_path.is_file():
        return False
    try:
        case_doc = json.loads(case_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if case_doc.get("schema_version") != "3.0-slot-case":
        return False
    # Seguridad estricta: nunca archivar ni tocar si existen datos reales ya adquiridos o selección previa.
    if (event_dir / "selection").exists():
        return False
    inputs_dir = event_dir / "inputs"
    real_files = (
        inputs_dir / f"cuotas_bwin_{event_id_str}.txt",
        inputs_dir / f"match_engine_{event_id_str}.txt",
        inputs_dir / f"match_engine_{event_id_str}.json",
        inputs_dir / "match_identity.json",
    )
    if any(p.is_file() for p in real_files):
        return False
    return True


def _verify_manifest_input_ref(inputs: dict[str, Any], key: str) -> tuple[bool, str | None]:
    ref = inputs.get(key)
    if not isinstance(ref, dict) or not ref.get("path") or not ref.get("sha256"):
        return False, f"MISSING_{key.upper()}"
    p = Path(str(ref["path"]))
    if not p.is_file():
        return False, f"MISSING_FILE_{key.upper()}"
    if file_sha256(p) != str(ref["sha256"]):
        return False, f"SHA_MISMATCH_{key.upper()}"
    return True, None


def inspect_operational_case_artifacts(
    root: Path,
    event_id: str | int,
    *,
    require_full_artifacts: bool = True,
) -> dict[str, Any]:
    """Audita desde disco si el expediente posee datos reales verificados por SHA256."""
    eid = str(event_id)
    event_dir = Path(root) / "outputs" / "matches" / eid
    case_path = event_dir / "case.json"
    manifest_path = event_dir / "manifest.json"
    case_doc: dict[str, Any] = {}
    manifest_doc: dict[str, Any] = {}

    if case_path.is_file():
        try:
            case_doc = json.loads(case_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            case_doc = {}
    if manifest_path.is_file():
        try:
            manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            manifest_doc = {}

    selected_ref = case_doc.get("selected") or manifest_doc.get("selected_match") or {}
    handoff_ref = case_doc.get("source_handoff") or manifest_doc.get("source_handoff") or {}
    selected_path = Path(str(selected_ref.get("path"))) if selected_ref.get("path") else None
    handoff_path = Path(str(handoff_ref.get("path"))) if handoff_ref.get("path") else None
    selected_sha_ok = bool(
        selected_path
        and selected_path.is_file()
        and selected_ref.get("sha256")
        and file_sha256(selected_path) == str(selected_ref.get("sha256"))
    )
    handoff_sha_ok = bool(
        handoff_path
        and handoff_path.is_file()
        and handoff_ref.get("sha256")
        and file_sha256(handoff_path) == str(handoff_ref.get("sha256"))
    )
    canonical_case_valid = bool(
        case_doc.get("schema_version") == _CANONICAL_CASE_SCHEMA
        and str(case_doc.get("event_id") or "") == eid
        and selected_sha_ok
        and handoff_sha_ok
    )

    inputs = manifest_doc.get("inputs") or {}
    odds_ok, odds_err = _verify_manifest_input_ref(inputs, "odds")
    ctx_txt_ok, ctx_txt_err = _verify_manifest_input_ref(inputs, "context")
    ctx_json_ok, ctx_json_err = _verify_manifest_input_ref(inputs, "context_json")
    id_ok, id_err = _verify_manifest_input_ref(inputs, "identity")
    res_ok, _ = _verify_manifest_input_ref(inputs, "qualitative_research")

    manifest_status = str(manifest_doc.get("status") or manifest_doc.get("state") or "")
    bwin_url = case_doc.get("bwin_url") or manifest_doc.get("url")

    missing: list[str] = []
    sha_mismatches: list[str] = []
    if require_full_artifacts:
        if not canonical_case_valid:
            missing.append("CANONICAL_CASE_OR_SELECTED_MATCH")
        for ok, err in ((odds_ok, odds_err), (ctx_txt_ok, ctx_txt_err), (ctx_json_ok, ctx_json_err), (id_ok, id_err)):
            if not ok and err:
                missing.append(err)
                if err.startswith("SHA_MISMATCH_"):
                    sha_mismatches.append(err)
        if manifest_status not in _DATA_READY_MANIFEST_STATUSES:
            missing.append(f"MANIFEST_STATUS:{manifest_status or 'MISSING'}")
        data_ready = bool(
            canonical_case_valid
            and odds_ok
            and ctx_txt_ok
            and ctx_json_ok
            and id_ok
            and manifest_status in _DATA_READY_MANIFEST_STATUSES
        )
        research_ready = bool(res_ok)
        research_source = str((inputs.get("qualitative_research") or {}).get("path") or "") or None
    else:
        legacy_odds = (event_dir / "inputs" / f"bwin_{eid}.txt").is_file() or odds_ok
        data_ready = bool(case_path.is_file() and manifest_path.is_file() and (legacy_odds or ctx_json_ok or manifest_status))
        research_text = _load_research_text(event_dir)
        research_ready = bool(res_ok or (research_text and research_text.strip()))
        research_source = (inputs.get("qualitative_research") or {}).get("path")
        if not research_source and (event_dir / "inputs" / "research.md").is_file():
            research_source = str(event_dir / "inputs" / "research.md")

    context_ready = bool(ctx_txt_ok and ctx_json_ok and id_ok)
    lineup_state_obj = manifest_doc.get("lineup_state") or {}
    lineup_confirmed_ready = bool(lineup_state_obj.get("confirmed_complete_22"))

    if not case_path.is_file():
        operational_stage = "NO_CASE"
    elif not data_ready:
        if odds_ok or ctx_txt_ok or ctx_json_ok or manifest_status in {"PARCIAL_SIN_CUOTAS", "ERROR_CONTEXTO", "ERROR_ARCHIVOS"}:
            operational_stage = "PARTIAL_DATA_ACQUIRED"
        else:
            operational_stage = "CASE_CREATED_PENDING_ACQUISITION"
    elif not research_ready:
        operational_stage = "DATA_READY_WAITING_RESEARCH"
    else:
        operational_stage = "DATA_READY"

    odds_ref_path = str((inputs.get("odds") or {}).get("path") or "").strip()
    if not odds_ref_path:
        for cand_odds in (
            event_dir / "inputs" / f"cuotas_bwin_{eid}.txt",
            event_dir / "inputs" / f"bwin_{eid}.txt",
        ):
            if cand_odds.is_file():
                odds_ref_path = str(cand_odds)
                break

    return {
        "event_id": eid,
        "event_dir": str(event_dir),
        "case_exists": case_path.is_file(),
        "case_schema": case_doc.get("schema_version"),
        "canonical_case_valid": canonical_case_valid,
        "selected_match_path": str(selected_path) if selected_path and selected_path.is_file() else None,
        "odds_path": odds_ref_path or None,
        "bwin_url": bwin_url,
        "manifest_exists": manifest_path.is_file(),
        "manifest_status": manifest_status,
        "identity_status": manifest_doc.get("identity_status") or case_doc.get("identity_status"),
        "odds_ready": odds_ok,
        "context_txt_ready": ctx_txt_ok,
        "context_json_ready": ctx_json_ok,
        "context_ready": context_ready,
        "identity_ready": id_ok,
        "data_ready": data_ready,
        "research_ready": research_ready,
        "research_source": research_source,
        "lineup_confirmed_ready": lineup_confirmed_ready,
        "operational_stage": operational_stage,
        "missing": missing,
        "sha_mismatches": sha_mismatches,
    }


def _sync_canonical_manifest_fields(
    root: Path,
    event_dir: Path,
    event_id_str: str,
    canonical_info: dict[str, Any],
) -> None:
    """Preserva claves canónicas (`canonical_case`, `selected_match`, `source_handoff`, `research_selection`) en `manifest.json`."""
    manifest_path = event_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    case_path = Path(canonical_info["case_path"])
    sel_path = Path(canonical_info["selected_match_path"])
    handoff_path = Path(canonical_info["handoff_path"])
    if not (case_path.is_file() and sel_path.is_file() and handoff_path.is_file()):
        return

    changed = False
    case_ref = _canonical_ref(case_path)
    sel_ref = _canonical_ref(sel_path)
    handoff_ref = _canonical_ref(handoff_path)
    if manifest.get("canonical_case") != case_ref:
        manifest["canonical_case"] = case_ref
        changed = True
    if manifest.get("selected_match") != sel_ref:
        manifest["selected_match"] = sel_ref
        changed = True
    if manifest.get("source_handoff") != handoff_ref:
        manifest["source_handoff"] = handoff_ref
        changed = True
    if canonical_info.get("research_selection") and manifest.get("research_selection") != canonical_info["research_selection"]:
        manifest["research_selection"] = canonical_info["research_selection"]
        changed = True
    if canonical_info.get("kickoff_madrid") and not manifest.get("kickoff_madrid"):
        manifest["kickoff_madrid"] = canonical_info["kickoff_madrid"]
        changed = True
    if changed:
        atomic_write_json(manifest_path, manifest)


def _write_factual_research_brief(
    event_dir: Path,
    candidate: SlotCandidateRecord,
    *,
    bwin_url: str | None,
    selected_path: Path | None,
    art: dict[str, Any],
) -> Path:
    """Genera `research/research_brief_<EVENT_ID>.md` usando únicamente hechos y archivos locales verificados."""
    eid = str(candidate.event_id)
    brief_path = event_dir / "research" / f"research_brief_{eid}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Brief de Investigación PREMATCH — Event ID {eid}",
        "",
        f"- Partido: **{candidate.home_team} – {candidate.away_team}**",
        f"- Fixture ID (API-Football): `{candidate.fixture_id}`",
        f"- Competición: `{candidate.league_name}` (league_id `{candidate.league_id}`, temporada `{candidate.season}`)",
        f"- Kickoff UTC: `{candidate.kickoff_utc}`",
        f"- Kickoff Europe/Madrid: `{candidate.kickoff_madrid}`",
        f"- Franja V3: `{candidate.slot_id}` (rank global `{candidate.global_rank}`, rank franja `{candidate.slot_rank}`)",
        f"- Bwin URL: `{bwin_url or 'PENDIENTE'}`",
        f"- Estado operativo actual: `{art.get('operational_stage')}` (manifest: `{art.get('manifest_status') or 'N/A'}`)",
        "",
        "## Snapshots Locales Verificados",
        f"- `selected_match.json`: `{selected_path}`" if selected_path else "- `selected_match.json`: `PENDIENTE`",
        f"- Cuotas Bwin (`cuotas_bwin_{eid}.txt`): `{'VERIFICADO' if art.get('odds_ready') else 'PENDIENTE'}`",
        f"- Contexto API TXT (`match_engine_{eid}.txt`): `{'VERIFICADO' if art.get('context_txt_ready') else 'PENDIENTE'}`",
        f"- Contexto API JSON (`match_engine_{eid}.json`): `{'VERIFICADO' if art.get('context_json_ready') else 'PENDIENTE'}`",
        f"- Identidad (`match_identity.json`): `{'VERIFICADO' if art.get('identity_ready') else 'PENDIENTE'}`",
        "",
        "## Reglas y Siguiente Paso",
        "- No ejecutar investigación cualitativa definitiva hasta que cuotas Bwin y contexto API-Football estén `VERIFICADO`.",
        "- Cualquier informe adjunto debe declarar `Event ID`, `Fixture ID`, kickoff y fuentes fechadas anteriores al kickoff.",
        "- Sin 22 titulares confirmados al corte (P0) nunca habrá apuesta accionable.",
    ]
    if selected_path:
        lines.extend(
            [
                "",
                "## Comandos Canónicos Verificados",
                f"- Estado caso: `.venv/bin/python -B prematch_case.py status --event-id {eid}`",
                f"- Adjuntar informe: `.venv/bin/python -B prematch_orchestrator.py attach-research --event-id {eid} --source <RUTA_INFORME.md>`",
                f"- Captura XI vía CLI caso: `.venv/bin/python -B prematch_case.py acquire-lineups --selected {selected_path}`",
            ]
        )
    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return brief_path


def ensure_canonical_slot_selection(
    root: Path,
    candidate: SlotCandidateRecord,
    raw_fixture: dict[str, Any],
    now_utc: datetime,
    *,
    bwin_row: dict[str, Any] | None = None,
    preselection_path: str | None = None,
) -> dict[str, Any]:
    """Construye o reutiliza de forma idempotente `selected_match.json`, `selection_decision.json` y `case.json` canónico."""
    if candidate.event_id is None:
        raise RuntimeError(f"EVENT_ID_BWIN_AUSENTE_PARA_FIXTURE_{candidate.fixture_id}")
    eid = str(candidate.event_id)
    event_dir = Path(root) / "outputs" / "matches" / eid
    event_dir.mkdir(parents=True, exist_ok=True)
    (event_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (event_dir / "analysis").mkdir(parents=True, exist_ok=True)

    bwin_url = _resolve_candidate_bwin_url(candidate, raw_fixture, bwin_row)
    if not _valid_bwin_url_for_event(bwin_url, eid):
        raise RuntimeError(f"BWIN_URL_AUSENTE_O_INVALIDA_PARA_EVENTO_{eid}")
    assert bwin_url is not None

    if _is_incomplete_v3_stub_case(event_dir, eid):
        archive_dir = event_dir / "archives" / f"v3_stub_{parse_iso_utc(now_utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("case.json", "manifest.json"):
            fpath = event_dir / fname
            if fpath.is_file():
                shutil.move(str(fpath), str(archive_dir / fname))

    case_path = event_dir / "case.json"
    manifest_path = event_dir / "manifest.json"
    sel_dir = event_dir / "selection"
    selected_path = sel_dir / "selected_match.json"
    decision_path = sel_dir / "selection_decision.json"
    handoff_path = sel_dir / "v3_handoff.json"

    if case_path.is_file():
        existing_case = json.loads(case_path.read_text(encoding="utf-8-sig"))
        if str(existing_case.get("event_id") or "") != eid or int(existing_case.get("fixture_id") or 0) != int(candidate.fixture_id):
            raise RuntimeError(f"CANONICAL_CASE_IDENTITY_CONFLICT:{eid}")
        if existing_case.get("schema_version") == _CANONICAL_CASE_SCHEMA:
            sel_ref_path = Path((existing_case.get("selected") or {}).get("path") or selected_path)
            dec_ref_path = sel_ref_path.parent / "selection_decision.json"
            hnd_ref_path = Path((existing_case.get("source_handoff") or {}).get("path") or handoff_path)
            if sel_ref_path.is_file() and dec_ref_path.is_file() and hnd_ref_path.is_file():
                prepare_manual_lineup_inbox(
                    event_dir,
                    event_id=eid,
                    fixture_id=candidate.fixture_id,
                    home_id=raw_fixture.get("home_id"),
                    away_id=raw_fixture.get("away_id"),
                    home_name=candidate.home_team,
                    away_name=candidate.away_team,
                    kickoff_iso=candidate.kickoff_utc,
                )
                art = inspect_operational_case_artifacts(root, eid, require_full_artifacts=True)
                brief_path = _write_factual_research_brief(
                    event_dir,
                    candidate,
                    bwin_url=bwin_url,
                    selected_path=sel_ref_path,
                    art=art,
                )
                sel_doc = json.loads(sel_ref_path.read_text(encoding="utf-8-sig"))
                res_sel = {
                    "selection_tier": sel_doc.get("selection_tier", "A"),
                    "research_eligible": True,
                    "eligible": sel_doc.get("eligible", True),
                    "research_missing_evidence": sel_doc.get("research_missing_evidence", []),
                    "hard_block_reasons": sel_doc.get("hard_block_reasons", []),
                    "selected_sha256": file_sha256(sel_ref_path),
                    "decision_sha256": file_sha256(dec_ref_path),
                    "actionable_bet": False,
                }
                return {
                    "case_path": str(case_path.resolve()),
                    "selected_match_path": str(sel_ref_path.resolve()),
                    "decision_path": str(dec_ref_path.resolve()),
                    "handoff_path": str(hnd_ref_path.resolve()),
                    "research_brief_path": str(brief_path.resolve()),
                    "bwin_url": bwin_url,
                    "kickoff_madrid": existing_case.get("kickoff_madrid") or candidate.kickoff_madrid,
                    "research_selection": res_sel,
                }

    sel_dir.mkdir(parents=True, exist_ok=True)
    asof_madrid = parse_iso_utc(now_utc).astimezone(MADRID_TZ).isoformat()
    ko_madrid = parse_iso_utc(candidate.kickoff_utc).astimezone(MADRID_TZ).isoformat()

    bwin_status = str((bwin_row or {}).get("bwin_match_status") or raw_fixture.get("bwin_match_status") or "")
    time_verified = (bwin_status == "IDENTITY_VERIFIED_AUTOMATICALLY") or bool(raw_fixture.get("time_verified"))
    if time_verified:
        sel_tier = "A"
        sel_eligible = True
        missing_ev: list[str] = []
    else:
        sel_tier = "B"
        sel_eligible = False
        missing_ev = [
            f"BWIN_KICKOFF_NOT_MACHINE_VERIFIED:{bwin_status or 'UNVERIFIED'}"
        ]

    handoff_doc = {
        "schema_version": "1.0-v3-orchestrator-handoff",
        "event_id": eid,
        "fixture_id": candidate.fixture_id,
        "slot_id": candidate.slot_id,
        "global_rank": candidate.global_rank,
        "slot_rank": candidate.slot_rank,
        "bwin_url": bwin_url,
        "bwin_discovery_status": bwin_status or None,
        "preselection_path": preselection_path,
        "created_utc": parse_iso_utc(now_utc).isoformat(),
    }
    atomic_write_json(handoff_path, handoff_doc)

    common_payload = {
        "home_api_id": raw_fixture.get("home_id"),
        "away_api_id": raw_fixture.get("away_id"),
        "league_id": candidate.league_id,
        "season": candidate.season,
        "gender": raw_fixture.get("gender") or "MALE_OR_OPEN",
        "fixture_id": candidate.fixture_id,
        "bwin_event_id": eid,
        "bwin_url": bwin_url,
        "home": candidate.home_team,
        "away": candidate.away_team,
        "kickoff_madrid": ko_madrid,
        "selection_asof_madrid": asof_madrid,
        "selection_score_not_probability": candidate.quality_score,
        "selection_tier": sel_tier,
        "eligible": sel_eligible,
        "research_eligible": True,
        "research_missing_evidence": missing_ev,
        "hard_block_reasons": [],
        "actionable_bet": False,
        **{
            k: raw_fixture[k]
            for k in ("round", "category_level", "category_evidence_status", "category_provenance", "national_team_evidence")
            if raw_fixture.get(k) is not None
        },
    }
    decision_doc = {
        "schema_version": "1.0-v3-slot-selection-decision",
        "status": "SELECTED",
        "method": "V3_MULTI_SLOT_RANKER_OPERATIONAL_BRIDGE",
        "asof_madrid": asof_madrid,
        "provisional_or_selected": {
            **common_payload,
            "market_families_ready": [],
        },
        "actionable_bet": False,
    }
    atomic_write_json(decision_path, decision_doc)

    selection_doc = {
        "schema_version": "1.0-selected-single-fixture",
        "status": "SELECTED_FOR_DEEP_RESEARCH",
        **common_payload,
        "market_families_ready_for_research": [],
        "source_decision": str(decision_path.resolve()),
        "source_handoff": str(handoff_path.resolve()),
        "warning": "Seleccionado por V3.0; requiere adquisición Bwin + API-Football y confirmación XI P0.",
    }
    # Garantiza compatibilidad 100% con prematch_single.py y prematch_case.py antes de persistir.
    import prematch_single

    prematch_single.validate_selected(selection_doc, decision_doc)
    atomic_write_json(selected_path, selection_doc)

    case_doc = {
        "schema_version": _CANONICAL_CASE_SCHEMA,
        "event_id": eid,
        "fixture_id": candidate.fixture_id,
        "bwin_url": bwin_url,
        "slot_id": candidate.slot_id,
        "home": {"name": candidate.home_team, "api_team_id": raw_fixture.get("home_id")},
        "away": {"name": candidate.away_team, "api_team_id": raw_fixture.get("away_id")},
        "competition": {
            "league_id": candidate.league_id,
            "name": candidate.league_name,
            "season": candidate.season,
        },
        "kickoff_utc": candidate.kickoff_utc,
        "kickoff_madrid": ko_madrid,
        "identity_status": (
            "V3_DISCOVERY_VERIFIED_CONTEXT_PENDING" if time_verified else "V3_BWIN_TIME_UNVERIFIED_CONTEXT_PENDING"
        ),
        "pipeline_version": _CANONICAL_CASE_SCHEMA,
        "source_kind": "V3_SLOT_ORCHESTRATOR",
        "selected": _canonical_ref(selected_path),
        "source_handoff": _canonical_ref(handoff_path),
        "created_utc": parse_iso_utc(now_utc).isoformat(),
        "actionable_bet": False,
        "ready_to_bet": False,
        "qualitative_research": {"attached": False, "sources": []},
    }
    atomic_write_json(case_path, case_doc)

    res_sel = {
        "selection_tier": sel_tier,
        "research_eligible": True,
        "eligible": sel_eligible,
        "research_missing_evidence": missing_ev,
        "hard_block_reasons": [],
        "selected_sha256": file_sha256(selected_path),
        "decision_sha256": file_sha256(decision_path),
        "actionable_bet": False,
    }
    if not manifest_path.is_file():
        atomic_write_json(
            manifest_path,
            {
                "schema_version": _CANONICAL_CASE_SCHEMA,
                "event_id": eid,
                "fixture_id": candidate.fixture_id,
                "url": bwin_url,
                "prepared_at": parse_iso_utc(now_utc).isoformat(),
                "status": "CASE_CREATED_PENDING_ACQUISITION",
                "identity_status": case_doc["identity_status"],
                "source_kind": "V3_SLOT_ORCHESTRATOR",
                "inputs": {},
                "steps": {},
                "kickoff_madrid": ko_madrid,
                "canonical_case": _canonical_ref(case_path),
                "selected_match": _canonical_ref(selected_path),
                "source_handoff": _canonical_ref(handoff_path),
                "research_selection": res_sel,
            },
        )
    else:
        _sync_canonical_manifest_fields(
            root,
            event_dir,
            eid,
            {
                "case_path": str(case_path),
                "selected_match_path": str(selected_path),
                "handoff_path": str(handoff_path),
                "kickoff_madrid": ko_madrid,
                "research_selection": res_sel,
            },
        )

    prepare_manual_lineup_inbox(
        event_dir,
        event_id=eid,
        fixture_id=candidate.fixture_id,
        home_id=raw_fixture.get("home_id"),
        away_id=raw_fixture.get("away_id"),
        home_name=candidate.home_team,
        away_name=candidate.away_team,
        kickoff_iso=candidate.kickoff_utc,
    )
    art = inspect_operational_case_artifacts(root, eid, require_full_artifacts=True)
    brief_path = _write_factual_research_brief(
        event_dir,
        candidate,
        bwin_url=bwin_url,
        selected_path=selected_path,
        art=art,
    )
    return {
        "case_path": str(case_path.resolve()),
        "selected_match_path": str(selected_path.resolve()),
        "decision_path": str(decision_path.resolve()),
        "handoff_path": str(handoff_path.resolve()),
        "research_brief_path": str(brief_path.resolve()),
        "bwin_url": bwin_url,
        "kickoff_madrid": ko_madrid,
        "research_selection": res_sel,
    }


def default_context_and_odds_acquirer(
    root: Path,
    candidate: SlotCandidateRecord,
    raw_fixture: dict[str, Any],
    now_utc: datetime,
    *,
    bwin_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reutiliza `prematch_multi.py` (prepare / retry-scraper / retry-context) sobre el expediente canónico."""
    eid = str(candidate.event_id)
    root = Path(root)
    event_dir = root / "outputs" / "matches" / eid
    art = inspect_operational_case_artifacts(root, eid, require_full_artifacts=True)
    bwin_url = _resolve_candidate_bwin_url(candidate, raw_fixture, bwin_row) or art.get("bwin_url")
    if not _valid_bwin_url_for_event(bwin_url, eid):
        raise RuntimeError(f"BWIN_URL_AUSENTE_O_INVALIDA_PARA_ADQUISICION_{eid}")
    assert bwin_url is not None

    manifest_status = art.get("manifest_status") or ""
    if manifest_status == "PARCIAL_SIN_CUOTAS" and art.get("context_ready"):
        action = "retry-scraper"
        cmd = [
            sys.executable,
            str(root / "prematch_multi.py"),
            "--root",
            str(root),
            "--confirm-external",
            "retry-scraper",
            "--event-id",
            eid,
            "--workers",
            "1",
        ]
    elif manifest_status in {"ERROR_CONTEXTO", "ERROR_ARCHIVOS"} and art.get("odds_ready"):
        action = "retry-context"
        cmd = [
            sys.executable,
            str(root / "prematch_multi.py"),
            "--root",
            str(root),
            "--confirm-external",
            "retry-context",
            "--event-id",
            eid,
            "--workers",
            "1",
        ]
    else:
        action = "prepare"
        cmd = [
            sys.executable,
            str(root / "prematch_multi.py"),
            "--root",
            str(root),
            "--confirm-external",
            "prepare",
            "--url",
            bwin_url,
            "--workers",
            "1",
        ]

    logs_dir = event_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"orchestrator_acquire_{action}_{parse_iso_utc(now_utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    with (
        scoped_external_confirmation(enabled=True, payload={"event_id": eid, "action": action, "url": bwin_url}),
        log_path.open("w", encoding="utf-8") as log_fh,
    ):
        proc = subprocess.run(cmd, cwd=root, stdout=log_fh, stderr=subprocess.STDOUT, check=False)

    # Sincroniza referencias canónicas tras save_manifest de prematch_multi.
    if (event_dir / "case.json").is_file():
        case_doc = json.loads((event_dir / "case.json").read_text(encoding="utf-8-sig"))
        _sync_canonical_manifest_fields(
            root,
            event_dir,
            eid,
            {
                "case_path": str(event_dir / "case.json"),
                "selected_match_path": (case_doc.get("selected") or {}).get("path") or str(event_dir / "selection" / "selected_match.json"),
                "handoff_path": (case_doc.get("source_handoff") or {}).get("path") or str(event_dir / "selection" / "v3_handoff.json"),
                "kickoff_madrid": case_doc.get("kickoff_madrid") or candidate.kickoff_madrid,
            },
        )

    post_art = inspect_operational_case_artifacts(root, eid, require_full_artifacts=True)
    return {
        "action": action,
        "returncode": proc.returncode,
        "log_path": str(log_path),
        **post_art,
    }


def default_case_preparer(
    root: Path,
    candidate: SlotCandidateRecord,
    raw_fixture: dict[str, Any],
    now_utc: datetime,
    *,
    bwin_row: dict[str, Any] | None = None,
    preselection_path: str | None = None,
    require_full_case_artifacts: bool = False,
) -> dict[str, Any]:
    """Prepara expediente canónico aislado `outputs/matches/<EVENT_ID>/` + buzón manual P0."""
    if candidate.event_id is None:
        raise RuntimeError(f"EVENT_ID_BWIN_AUSENTE_PARA_FIXTURE_{candidate.fixture_id}")
    event_id_str = str(candidate.event_id)
    event_dir = Path(root) / "outputs" / "matches" / event_id_str
    bwin_url = _resolve_candidate_bwin_url(candidate, raw_fixture, bwin_row)

    if require_full_case_artifacts or _valid_bwin_url_for_event(bwin_url, event_id_str):
        canonical_info = ensure_canonical_slot_selection(
            root,
            candidate,
            raw_fixture,
            now_utc,
            bwin_row=bwin_row,
            preselection_path=preselection_path,
        )
        art = inspect_operational_case_artifacts(root, event_id_str, require_full_artifacts=require_full_case_artifacts)
        return {
            "event_dir": str(event_dir),
            "odds_state": "CAPTURED_VALID" if art["odds_ready"] else "NOT_CAPTURED",
            **canonical_info,
        }

    # Modo compacto de pruebas unitarias offline (sin bwin_url de 6+ dígitos): mantiene buzón y estructura mínima.
    (event_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (event_dir / "analysis").mkdir(parents=True, exist_ok=True)
    case_path = event_dir / "case.json"
    manifest_path = event_dir / "manifest.json"
    if not case_path.is_file():
        atomic_write_json(
            case_path,
            {
                "schema_version": "3.0-slot-case",
                "event_id": candidate.event_id,
                "fixture_id": candidate.fixture_id,
                "slot_id": candidate.slot_id,
                "home": candidate.home_team,
                "away": candidate.away_team,
                "home_id": raw_fixture.get("home_id"),
                "away_id": raw_fixture.get("away_id"),
                "league_id": candidate.league_id,
                "league_name": candidate.league_name,
                "season": candidate.season,
                "kickoff_utc": candidate.kickoff_utc,
                "kickoff_madrid": candidate.kickoff_madrid,
                "prepared_at_utc": now_utc.isoformat(),
                "qualitative_research": {"attached": False, "sources": []},
            },
        )
    if not manifest_path.is_file():
        atomic_write_json(
            manifest_path,
            {
                "schema_version": "1.0-event-manifest",
                "event_id": event_id_str,
                "fixture_id": candidate.fixture_id,
                "state": "PREPARED",
                "created_at_utc": now_utc.isoformat(),
            },
        )
    prepare_manual_lineup_inbox(
        event_dir,
        event_id=event_id_str,
        fixture_id=candidate.fixture_id,
        home_id=raw_fixture.get("home_id"),
        away_id=raw_fixture.get("away_id"),
        home_name=candidate.home_team,
        away_name=candidate.away_team,
        kickoff_iso=candidate.kickoff_utc,
    )
    odds_snapshot = raw_fixture.get("odds_snapshot_text")
    odds_captured = bool(raw_fixture.get("odds_captured"))
    if isinstance(odds_snapshot, str) and odds_snapshot.strip():
        (event_dir / "inputs" / f"bwin_{event_id_str}.txt").write_text(odds_snapshot, encoding="utf-8")
        odds_captured = True

    return {
        "event_dir": str(event_dir),
        "odds_state": "CAPTURED_VALID" if odds_captured else "NOT_CAPTURED",
    }


def _default_shadow_evaluator(
    root: Path,
    candidate: SlotCandidateRecord,
    now_utc: datetime,
    lineup_cutoff_minutes: int,
    user_manual_absent: bool,
) -> dict[str, Any]:
    if candidate.event_id is None:
        return {"status": "BLOCKED", "decision_taxonomy": "BLOQUEO_TECNICO", "reasons": ["MISSING_EVENT_ID"]}
    event_dir = Path(root) / "outputs" / "matches" / str(candidate.event_id)
    if (event_dir / "case.json").is_file() and (event_dir / "inputs").is_dir():
        res = run_shadow_for_event_dir(
            Path(root),
            str(candidate.event_id),
            now_utc=now_utc,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
            user_manual_absent=user_manual_absent,
        )
        # Normaliza respuesta de shadow_pipeline si existe report JSON
        report_path = event_dir / "analysis" / "shadow_mode_report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            return {
                "status": "OK",
                "decision_taxonomy": report.get("decision_taxonomy", "NO_BET_POR_EVIDENCIA"),
                "taxonomy_breakdown": report.get("taxonomy_breakdown", {}),
                "candidate": (report.get("candidates") or [None])[0],
                "blocking_reasons": report.get("no_bet_reasons", []),
                "report_path": str(report_path),
                "adapter_result": res,
            }
        return {"status": res.get("status", "OK"), "decision_taxonomy": "BLOQUEO_TECNICO", "adapter_result": res}
    return {"status": "BLOCKED", "decision_taxonomy": "BLOQUEO_TECNICO", "reasons": ["MISSING_CASE_DIRECTORY"]}


def _load_research_text(event_dir: Path) -> str | None:
    for rel in (
        "inputs/research.md",
        "inputs/grok_report.md",
        "inputs/perplexity_report.md",
        "inputs/codex_research.md",
    ):
        p = event_dir / rel
        if p.is_file():
            return p.read_text(encoding="utf-8")
    case_path = event_dir / "case.json"
    if case_path.is_file():
        doc = json.loads(case_path.read_text(encoding="utf-8"))
        for src in (doc.get("qualitative_research") or {}).get("sources", []):
            sp = Path(src)
            if sp.is_file():
                return sp.read_text(encoding="utf-8")
    return None


def _slot_has_final_decision(plan: WindowOrchestrationPlan, slot_id: str) -> bool:
    return any(
        c.slot_id == slot_id
        and c.execution_state in {MultiMatchExecutionState.FINALIZED, MultiMatchExecutionState.NO_BET}
        for c in plan.candidates
    )


def _slot_exhausted_or_closed(plan: WindowOrchestrationPlan, slot_id: str) -> bool:
    if _slot_has_final_decision(plan, slot_id):
        return True
    active_fid = plan.active_by_slot.get(slot_id)
    if active_fid is not None:
        return False
    # Si ya se alcanzó el presupuesto de fallback en esta franja después de un rechazo, se cierra la franja.
    if plan.budget.slot_fallbacks_used.get(slot_id, 0) > 0 and not plan.budget.can_fallback_slot(slot_id):
        return True
    return False


class MultiSlotOrchestrator:
    """Orquestador secuencial V3.0 con estado persistente por ventana y aislamiento por partido."""

    def __init__(
        self,
        root: Path,
        config: OrchestratorConfig,
        *,
        adapters: OrchestratorAdapters | None = None,
        orchestrator_root: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.adapters = adapters or OrchestratorAdapters()
        self.window_label = (
            f"{config.window_day.isoformat()}_{config.window_start_hhmm.replace(':', '')}"
            f"_{config.window_end_hhmm.replace(':', '')}"
        )
        base_out = Path(orchestrator_root) if orchestrator_root else self.root / "outputs" / "orchestrator"
        self.plan_dir = base_out / self.window_label
        self.plan_path = self.plan_dir / "window_plan.json"
        self.summary_json_path = self.plan_dir / "window_summary.json"
        self.summary_md_path = self.plan_dir / "window_summary.md"
        self._fixtures_by_id: dict[int, dict[str, Any]] = {}
        self._bwin_by_fixture_id: dict[int, dict[str, Any]] = {}

    def plan_exists(self) -> bool:
        return self.plan_path.is_file()

    def load_plan(self) -> WindowOrchestrationPlan:
        if not self.plan_path.is_file():
            raise MultiSlotContractError(f"No existe plan guardado en {self.plan_path}")
        raw = json.loads(self.plan_path.read_text(encoding="utf-8"))
        plan = WindowOrchestrationPlan.from_dict(raw)
        if plan.preselection_path and Path(plan.preselection_path).is_file():
            pre_path = Path(plan.preselection_path)
            self._load_fixtures_index(pre_path)
            self._load_sibling_bwin_links(pre_path)
        return plan

    def _load_sibling_bwin_links(self, pre_path: Path) -> dict[int, dict[str, Any]]:
        sibling_bwin = pre_path.parent.parent / "bwin" / "bwin_discovery.json"
        if sibling_bwin.is_file():
            bwin_doc = json.loads(sibling_bwin.read_text(encoding="utf-8"))
            links = {
                int(row["fixture_id"]): row
                for row in (bwin_doc.get("matches") or [])
                if row.get("fixture_id") is not None
            }
            self._bwin_by_fixture_id = links
            return links
        return {}

    def save_plan(self, plan: WindowOrchestrationPlan) -> Path:
        if self.config.dry_run:
            return self.plan_path
        atomic_write_json(self.plan_path, plan.to_dict())
        self.write_summary_reports(plan)
        return self.plan_path

    def _load_fixtures_index(self, preselection_path: Path) -> list[dict[str, Any]]:
        doc = json.loads(Path(preselection_path).read_text(encoding="utf-8"))
        matches = list(doc.get("matches") or doc.get("fixtures") or [])
        self._fixtures_by_id = {int(m["fixture_id"]): dict(m) for m in matches}
        return matches

    def _current_filter_implementation(self) -> str:
        from filter_major_leagues import FILTER_IMPLEMENTATION

        return str(FILTER_IMPLEMENTATION)

    def _current_policy_signature(self, specs: list[TimeSlotSpec]) -> dict[str, Any]:
        return {
            "filter_implementation": self._current_filter_implementation(),
            "competition_scope": self.config.competition_scope,
            "min_lead_minutes": int(self.config.min_lead_minutes),
            "lineup_cutoff_minutes": int(self.config.lineup_cutoff_minutes),
            "min_quality_score": float(self.config.min_quality_score),
            "min_readiness_score": float(self.config.min_readiness_score),
            "require_bwin": bool(self.config.require_bwin),
            "slots": [[s.slot_id, s.start_hhmm, s.end_hhmm] for s in specs],
        }

    def _plan_has_protected_dossiers(self, plan: WindowOrchestrationPlan) -> bool:
        return any(c.execution_state in PROTECTED_DOSSIER_STATES for c in plan.candidates)

    def _archive_plan_version(self, old_plan: WindowOrchestrationPlan) -> tuple[str, Path]:
        raw_stamp = (old_plan.created_utc or old_plan.last_updated_utc or "legacy")[:19]
        safe_stamp = raw_stamp.replace(":", "").replace("-", "").replace("T", "_")
        old_id = old_plan.plan_id or f"plan_v{old_plan.plan_version}_{safe_stamp or 'legacy'}"
        history_dir = self.plan_dir / "plan_history"
        archive_path = history_dir / f"{old_id}.json"
        if archive_path.exists():
            # Garantizar inmutabilidad si ya existiera una entrada previa con ese identificador.
            suffix = 2
            while (history_dir / f"{old_id}_{suffix}.json").exists():
                suffix += 1
            archive_path = history_dir / f"{old_id}_{suffix}.json"
        if not self.config.dry_run:
            snapshot = old_plan.to_dict()
            snapshot["plan_id"] = old_id
            atomic_write_json(archive_path, snapshot)
        return old_id, archive_path

    def _detect_plan_staleness(
        self,
        existing: WindowOrchestrationPlan,
        *,
        now_utc: datetime,
        explicit_preselection: Path | None,
        current_filter: str,
        specs: list[TimeSlotSpec],
    ) -> tuple[list[str], Path | None, bool]:
        reasons: list[str] = []
        if existing.filter_implementation != current_filter:
            reasons.append(
                f"FILTER_IMPLEMENTATION_CHANGED:{existing.filter_implementation or 'UNVERSIONED'}->{current_filter}"
            )

        current_sig = self._current_policy_signature(specs)
        if existing.policy_signature:
            for key in ("competition_scope", "require_bwin", "min_lead_minutes", "lineup_cutoff_minutes", "min_quality_score", "min_readiness_score", "slots"):
                if key in existing.policy_signature and existing.policy_signature[key] != current_sig[key]:
                    reasons.append(f"POLICY_CHANGED:{key}")

        pre_path, auto_gen = resolve_or_generate_preselection(
            self.root,
            self.config,
            explicit_preselection=explicit_preselection,
            adapters=self.adapters,
            now_utc=now_utc,
        )
        if pre_path is not None:
            resolved_sha = file_sha256(pre_path)
            if str(pre_path) != str(existing.preselection_path or ""):
                reasons.append(
                    f"PRESELECTION_PATH_CHANGED:{Path(existing.preselection_path or 'none').name}->{pre_path.name}"
                )
            elif resolved_sha != existing.preselection_sha256:
                reasons.append("PRESELECTION_SHA256_CHANGED")

        return reasons, pre_path, auto_gen

    def _load_bwin_links_for_preselection(
        self,
        pre_path: Path,
        matches: list[dict[str, Any]],
        plan: WindowOrchestrationPlan,
        now_utc: datetime,
    ) -> dict[int, dict[str, Any]] | None:
        if self.adapters.bwin_discovery_provider is not None:
            if not plan.budget.can_spend_scrape(1):
                raise MultiSlotContractError("Presupuesto de scraping insuficiente para discovery Bwin")
            plan.budget.spend_scrape(1)
            links = self.adapters.bwin_discovery_provider(matches, plan, now_utc)
            self._bwin_by_fixture_id = dict(links or {})
            return links
        links = self._load_sibling_bwin_links(pre_path)
        if links:
            return links
        return None

    def init_or_load_plan(
        self,
        *,
        now_utc: datetime,
        explicit_preselection: Path | None = None,
        check_staleness: bool = True,
    ) -> WindowOrchestrationPlan:
        now_utc = parse_iso_utc(now_utc)
        slot_defs = parse_custom_slots_spec(self.config.slots_spec) if self.config.slots_spec else None
        w_start_utc, w_end_utc, specs = build_window_slots(
            self.config.window_day,
            self.config.window_start_hhmm,
            self.config.window_end_hhmm,
            slot_definitions=slot_defs,
        )
        current_filter = self._current_filter_implementation()

        existing: WindowOrchestrationPlan | None = None
        stale_reasons: list[str] = []
        pre_path: Path | None = None
        auto_gen = False

        if self.plan_path.is_file():
            existing = self.load_plan()
            if not check_staleness:
                return existing
            stale_reasons, pre_path, auto_gen = self._detect_plan_staleness(
                existing,
                now_utc=now_utc,
                explicit_preselection=explicit_preselection,
                current_filter=current_filter,
                specs=specs,
            )
            if not stale_reasons:
                return existing
            if self.config.dry_run:
                existing.log_event(now_utc, "DRY_RUN_STALE_PLAN_DETECTED", {"reasons": stale_reasons})
                return existing

        next_version = (existing.plan_version + 1) if existing else 1
        plan_id = f"plan_v{next_version}_{now_utc.strftime('%Y%m%dT%H%M%SZ')}"
        plan = WindowOrchestrationPlan(
            window_label=self.window_label,
            window_day=self.config.window_day.isoformat(),
            window_start_utc=w_start_utc.isoformat(),
            window_end_utc=w_end_utc.isoformat(),
            window_start_madrid=w_start_utc.astimezone(MADRID_TZ).isoformat(),
            window_end_madrid=w_end_utc.astimezone(MADRID_TZ).isoformat(),
            max_total_active=len(specs),
            slots=specs,
            budget=(
                ResourceBudgetLedger.from_dict(existing.budget.to_dict())
                if existing and self._plan_has_protected_dossiers(existing)
                else ResourceBudgetLedger(
                    max_api_calls=self.config.max_api_calls,
                    max_scrapes=self.config.max_scrapes,
                    max_slot_fallbacks=self.config.max_slot_fallbacks,
                    max_attempts_per_match=self.config.max_attempts_per_match,
                )
            ),
            plan_id=plan_id,
            plan_version=next_version,
            filter_implementation=current_filter,
            policy_signature=self._current_policy_signature(specs),
            created_utc=now_utc.isoformat(),
            last_updated_utc=now_utc.isoformat(),
        )

        if pre_path is None:
            pre_path, auto_gen = resolve_or_generate_preselection(
                self.root,
                self.config,
                explicit_preselection=explicit_preselection,
                adapters=self.adapters,
                now_utc=now_utc,
            )
        if pre_path is None:
            plan.log_event(now_utc, "DRY_RUN_PLAN_NO_PRESELECTION", {"window_label": self.window_label})
            return plan

        matches = self._load_fixtures_index(pre_path)
        plan.preselection_path = str(pre_path)
        plan.preselection_sha256 = file_sha256(pre_path)
        plan.preselection_auto_generated = auto_gen

        bwin_links = self._load_bwin_links_for_preselection(pre_path, matches, plan, now_utc)
        populate_and_rank_plan(
            plan,
            matches,
            now_utc=now_utc,
            bwin_links=bwin_links,
            min_lead_minutes=self.config.min_lead_minutes,
            lineup_cutoff_minutes=self.config.lineup_cutoff_minutes,
            min_quality_score=self.config.min_quality_score,
            min_readiness_score=self.config.min_readiness_score,
            require_bwin=self.config.require_bwin,
        )

        if existing is not None:
            old_id, archive_path = self._archive_plan_version(existing)
            plan.parent_plan_id = old_id
            plan.parent_plan_path = str(archive_path.resolve())
            plan.rebuild_reason = ";".join(stale_reasons)

            if self._plan_has_protected_dossiers(existing):
                protected = {
                    c.fixture_id: SlotCandidateRecord.from_dict(c.to_dict())
                    for c in existing.candidates
                    if c.execution_state in PROTECTED_DOSSIER_STATES
                }
                merged: list[SlotCandidateRecord] = []
                seen_fids: set[int] = set()
                for cand in plan.candidates:
                    if cand.fixture_id in protected:
                        merged.append(protected[cand.fixture_id])
                    else:
                        merged.append(cand)
                    seen_fids.add(cand.fixture_id)
                for fid, prot_cand in protected.items():
                    if fid not in seen_fids:
                        merged.append(prot_cand)
                plan.candidates = merged
                # Restaurar asignación activa únicamente para franjas con expediente activo en curso.
                for sid, active_fid in existing.active_by_slot.items():
                    if active_fid is not None and active_fid in protected:
                        if protected[active_fid].execution_state in ACTIVE_SLOT_STATES:
                            plan.active_by_slot[sid] = active_fid
                # Reasignar global_rank / slot_rank sin mutar expedientes cerrados.
                from bet_engine.orchestration.multi_ranker import _assign_ranks

                _assign_ranks(plan, reason=f"MIGRATION:{plan.rebuild_reason}", now_utc=now_utc, record_history=True)
                plan.log_event(
                    now_utc,
                    "PLAN_MIGRATED_WITH_PROTECTED_DOSSIERS",
                    {
                        "parent_plan_id": old_id,
                        "protected_fixtures": sorted(protected.keys()),
                        "reasons": stale_reasons,
                    },
                )
            else:
                plan.log_event(
                    now_utc,
                    "PLAN_REBUILT_FROM_STALE_POLICY_OR_PRESELECTION",
                    {"parent_plan_id": old_id, "reasons": stale_reasons},
                )

        if not self.config.dry_run:
            self.save_plan(plan)
        return plan

    def step(
        self,
        *,
        now_utc: datetime,
        explicit_preselection: Path | None = None,
        user_manual_absent_by_fixture: dict[int, bool] | None = None,
    ) -> WindowOrchestrationPlan:
        """Ejecuta una pasada secuencial no bloqueante sobre las franjas y persiste cada transición."""
        now_utc = parse_iso_utc(now_utc)
        plan = self.init_or_load_plan(now_utc=now_utc, explicit_preselection=explicit_preselection, check_staleness=False)
        if self.config.dry_run and not plan.candidates:
            return plan

        # Re-evaluar caducidades y tiempos previos sin mutar expedientes cerrados.
        apply_dynamic_ranking_update(
            plan,
            now_utc=now_utc,
            reason="PRE_STEP_CLOCK_AND_LEAD_CHECK",
            fixture_metadata_by_id=self._fixtures_by_id,
            min_lead_minutes=self.config.min_lead_minutes,
            lineup_cutoff_minutes=self.config.lineup_cutoff_minutes,
            min_quality_score=self.config.min_quality_score,
            min_readiness_score=self.config.min_readiness_score,
            require_bwin=self.config.require_bwin,
        )

        manual_absent_map = user_manual_absent_by_fixture or {}
        for spec in sorted(plan.slots, key=lambda s: s.slot_index):
            slot_id = spec.slot_id
            if _slot_exhausted_or_closed(plan, slot_id):
                continue

            active_fid = plan.active_by_slot.get(slot_id)
            if active_fid is None:
                promoted = self._promote_next_for_slot(plan, slot_id, now_utc=now_utc)
                if promoted is None:
                    continue
                active_fid = promoted.fixture_id

            cand = plan.get_candidate(active_fid)
            if cand is None or cand.is_closed_dossier():
                plan.active_by_slot[slot_id] = None
                continue

            self._advance_candidate(
                plan,
                cand,
                now_utc=now_utc,
                user_manual_absent=bool(manual_absent_map.get(cand.fixture_id, False)),
            )

            # Si el candidato falló o fue rechazado antes de cierre analítico final, intentar fallback acotado en la misma franja.
            if cand.execution_state in {
                MultiMatchExecutionState.REJECTED_MISSING_LINEUPS,
                MultiMatchExecutionState.FAILED_TECHNICAL,
                MultiMatchExecutionState.SKIPPED_INELIGIBLE,
                MultiMatchExecutionState.EXPIRED,
            }:
                plan.active_by_slot[slot_id] = None
                if plan.budget.can_fallback_slot(slot_id):
                    fallback_next = select_best_eligible_for_slot(plan, slot_id)
                    if fallback_next is not None:
                        plan.budget.record_slot_fallback(slot_id)
                        fb = self._promote_next_for_slot(
                            plan,
                            slot_id,
                            now_utc=now_utc,
                            fallback_parent=cand,
                        )
                        if fb is not None:
                            self._advance_candidate(
                                plan,
                                fb,
                                now_utc=now_utc,
                                user_manual_absent=bool(manual_absent_map.get(fb.fixture_id, False)),
                            )
                    else:
                        plan.log_event(
                            now_utc,
                            "SLOT_LEFT_EMPTY_NO_ELIGIBLE_FALLBACK",
                            {"slot_id": slot_id, "failed_fixture_id": cand.fixture_id},
                        )

        self.save_plan(plan)
        return plan

    def run_cycle(
        self,
        *,
        now_utc: datetime,
        explicit_preselection: Path | None = None,
        max_passes: int = 3,
        user_manual_absent_by_fixture: dict[int, bool] | None = None,
    ) -> WindowOrchestrationPlan:
        """Ejecuta pasadas secuenciales hasta alcanzar estados de espera o terminales (con tope anti-bucle)."""
        last_signature: str | None = None
        plan: WindowOrchestrationPlan | None = None
        for _ in range(max_passes):
            plan = self.step(
                now_utc=now_utc,
                explicit_preselection=explicit_preselection,
                user_manual_absent_by_fixture=user_manual_absent_by_fixture,
            )
            if self.config.dry_run:
                return plan
            sig = json.dumps(
                [(c.fixture_id, c.execution_state.value, c.lineup_state, c.research_state) for c in plan.candidates],
                sort_keys=True,
            )
            if sig == last_signature:
                break
            last_signature = sig
        assert plan is not None
        return plan

    def attach_manual_lineup_file(
        self,
        *,
        event_id: int,
        manual_json_path: Path,
        now_utc: datetime,
    ) -> WindowOrchestrationPlan:
        """Registra XI manual verificado y marca candidato listo para reevaluar Shadow y cierre."""
        now_utc = parse_iso_utc(now_utc)
        plan = self.load_plan()
        cand = plan.get_by_event_id(event_id)
        if cand is None:
            raise MultiSlotContractError(f"event_id {event_id} no pertenece al plan {plan.window_label}")
        if cand.execution_state == MultiMatchExecutionState.EXPIRED:
            raise MultiSlotContractError(f"event_id {event_id} ya expiró")

        event_dir = self.root / "outputs" / "matches" / str(event_id)
        manual_dir = event_dir / "lineups" / "manual"
        manual_dir.mkdir(parents=True, exist_ok=True)
        src = Path(manual_json_path)
        payload = json.loads(src.read_text(encoding="utf-8"))
        dst = manual_dir / f"manual_lineup_{event_id}.json"
        if src.resolve() != dst.resolve():
            atomic_write_json(dst, payload)

        raw_fixture = self._fixtures_by_id.get(cand.fixture_id, {})
        validated_manual = self._validate_manual_payload(cand, raw_fixture, payload, now_utc)
        eval_res = evaluate_fixture_lineup_state(
            manual_lineup=validated_manual,
            fixture_id=cand.fixture_id,
            event_id=str(event_id),
            home_id=raw_fixture.get("home_id"),
            away_id=raw_fixture.get("away_id"),
            home_name=cand.home_team,
            away_name=cand.away_team,
            kickoff_utc=cand.kickoff_utc,
            now_utc=now_utc,
            lineup_cutoff_minutes=self.config.lineup_cutoff_minutes,
        )
        cand.lineup_state = eval_res["status"]
        eval_reasons = [r for r in [eval_res.get("reason"), *eval_res.get("validation_errors", [])] if r]
        if cand.lineup_state in CONFIRMED_XI_STATES:
            if cand.execution_state == MultiMatchExecutionState.WAITING_LINEUPS:
                cand.transition_to(MultiMatchExecutionState.READY_FOR_FINALIZATION, now_utc, "MANUAL_XI_VERIFIED")
            plan.log_event(now_utc, "MANUAL_LINEUP_ATTACHED", {"event_id": event_id, "lineup_state": cand.lineup_state})
            self._finalize_candidate_with_shadow(plan, cand, now_utc=now_utc)
        else:
            cand.blocking_reasons = list(dict.fromkeys(cand.blocking_reasons + eval_reasons))
            plan.log_event(
                now_utc,
                "MANUAL_LINEUP_NOT_CONFIRMED",
                {"event_id": event_id, "lineup_state": cand.lineup_state, "reasons": eval_reasons},
            )
        self.save_plan(plan)
        return plan

    def attach_research_file(
        self,
        *,
        event_id: int,
        research_path: Path,
        now_utc: datetime,
    ) -> WindowOrchestrationPlan:
        now_utc = parse_iso_utc(now_utc)
        plan = self.load_plan()
        cand = plan.get_by_event_id(event_id)
        if cand is None:
            raise MultiSlotContractError(f"event_id {event_id} no pertenece al plan {plan.window_label}")

        event_dir = self.root / "outputs" / "matches" / str(event_id)
        (event_dir / "inputs").mkdir(parents=True, exist_ok=True)
        src = Path(research_path)
        if not src.is_file():
            raise MultiSlotContractError(f"Informe cualitativo inexistente: {src}")

        case_path = event_dir / "case.json"
        used_canonical = False
        if self.config.require_full_case_artifacts and case_path.is_file():
            case_doc = json.loads(case_path.read_text(encoding="utf-8-sig"))
            if case_doc.get("schema_version") == _CANONICAL_CASE_SCHEMA:
                import prematch_case

                target = prematch_case.attach_research(self.root, str(event_id), src)
                used_canonical = True
                # Refleja XI extraído desde el informe cualitativo si quedó verificado en manifest.
                manifest_path = event_dir / "manifest.json"
                if manifest_path.is_file():
                    m_doc = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    lu_status = str((m_doc.get("lineup_state") or {}).get("status") or "")
                    if lu_status in CONFIRMED_XI_STATES:
                        cand.lineup_state = lu_status

        if not used_canonical:
            text = src.read_text(encoding="utf-8")
            dst = event_dir / "inputs" / "research.md"
            if src.resolve() != dst.resolve():
                dst.write_text(text, encoding="utf-8")

        art = inspect_operational_case_artifacts(
            self.root,
            str(event_id),
            require_full_artifacts=self.config.require_full_case_artifacts,
        )
        cand.research_state = "ATTACHED_VERIFIED"
        if cand.execution_state == MultiMatchExecutionState.WAITING_RESEARCH:
            if art["data_ready"]:
                cand.transition_to(MultiMatchExecutionState.WAITING_LINEUPS, now_utc, "RESEARCH_ATTACHED")
            else:
                cand.transition_to(MultiMatchExecutionState.PREPARING, now_utc, "RESEARCH_ATTACHED_PENDING_DATA")
        plan.log_event(
            now_utc,
            "RESEARCH_ATTACHED",
            {"event_id": event_id, "source": str(src), "canonical_validated": used_canonical, "data_ready": art["data_ready"]},
        )
        self.save_plan(plan)
        return plan

    def acquire_events(
        self,
        *,
        now_utc: datetime,
        event_id: int | None = None,
    ) -> WindowOrchestrationPlan:
        """Ejecuta adquisición controlada de contexto API y cuotas Bwin para el/los activos o un Event ID dado."""
        now_utc = parse_iso_utc(now_utc)
        plan = self.load_plan()
        targets: list[SlotCandidateRecord] = []
        if event_id is not None:
            c = plan.get_by_event_id(event_id)
            if c is None:
                raise MultiSlotContractError(f"event_id {event_id} no pertenece al plan {plan.window_label}")
            targets = [c]
        else:
            for spec in sorted(plan.slots, key=lambda s: s.slot_index):
                fid = plan.active_by_slot.get(spec.slot_id)
                if fid is not None:
                    c = plan.get_candidate(fid)
                    if c is not None and not c.is_closed_dossier():
                        targets.append(c)

        for cand in targets:
            if cand.execution_state == MultiMatchExecutionState.RANKED:
                slot_id = cand.slot_id
                active_fid = plan.active_by_slot.get(slot_id) if slot_id else None
                if slot_id and active_fid not in (None, cand.fixture_id):
                    raise MultiSlotContractError(
                        f"SLOT_OCCUPIED:{slot_id} ocupado por fixture_id={active_fid}; "
                        f"event_id={event_id} requiere ejecución independiente fuera de la franja"
                    )
                if slot_id and active_fid is None:
                    cand.transition_to(MultiMatchExecutionState.SELECTED, now_utc, "EXPLICIT_ACQUIRE_SELECTED")
                    plan.active_by_slot[slot_id] = cand.fixture_id
            self._advance_candidate(plan, cand, now_utc=now_utc)

        self.save_plan(plan)
        return plan

    def _promote_next_for_slot(
        self,
        plan: WindowOrchestrationPlan,
        slot_id: str,
        *,
        now_utc: datetime,
        fallback_parent: SlotCandidateRecord | None = None,
    ) -> SlotCandidateRecord | None:
        if plan.active_by_slot.get(slot_id) is not None:
            return plan.get_candidate(plan.active_by_slot[slot_id])
        current_active_total = sum(1 for v in plan.active_by_slot.values() if v is not None)
        if current_active_total >= plan.max_total_active:
            return None

        best = select_best_eligible_for_slot(plan, slot_id)
        if best is None:
            return None

        if fallback_parent is not None:
            best.fallback_parent_fixture_id = fallback_parent.fixture_id
            best.fallback_chain_depth = fallback_parent.fallback_chain_depth + 1
            reason = f"SLOT_FALLBACK_FROM_{fallback_parent.fixture_id}"
        else:
            reason = "INITIAL_TOP_ELIGIBLE_IN_SLOT"

        best.transition_to(MultiMatchExecutionState.SELECTED, now_utc, reason)
        plan.active_by_slot[slot_id] = best.fixture_id
        plan.log_event(
            now_utc,
            "SLOT_CANDIDATE_SELECTED",
            {
                "slot_id": slot_id,
                "fixture_id": best.fixture_id,
                "fallback_parent_fixture_id": best.fallback_parent_fixture_id,
                "global_rank": best.global_rank,
                "slot_rank": best.slot_rank,
            },
        )
        return best

    def _advance_candidate(
        self,
        plan: WindowOrchestrationPlan,
        cand: SlotCandidateRecord,
        *,
        now_utc: datetime,
        user_manual_absent: bool = False,
    ) -> None:
        if cand.is_closed_dossier():
            return
        if cand.attempts >= plan.budget.max_attempts_per_match:
            cand.blocking_reasons.append("MAX_ATTEMPTS_EXCEEDED")
            cand.audit_outcome = "FAILED_ISOLATED_TECH_ERROR"
            cand.transition_to(MultiMatchExecutionState.FAILED_TECHNICAL, now_utc, "MAX_ATTEMPTS_EXCEEDED")
            return

        cand.attempts += 1
        raw_fixture = self._fixtures_by_id.get(cand.fixture_id, {})
        bwin_row = self._bwin_by_fixture_id.get(cand.fixture_id)

        # Fase 1: Creación del expediente canónico aislado (sin confundir creación de carpeta con datos adquiridos)
        if cand.execution_state == MultiMatchExecutionState.SELECTED:
            cand.transition_to(MultiMatchExecutionState.PREPARING, now_utc, "START_CASE_PREPARATION")
            try:
                if self.adapters.case_preparer is not None:
                    prep_res = self.adapters.case_preparer(self.root, cand, raw_fixture, now_utc)
                else:
                    prep_res = default_case_preparer(
                        self.root,
                        cand,
                        raw_fixture,
                        now_utc,
                        bwin_row=bwin_row,
                        preselection_path=plan.preselection_path,
                        require_full_case_artifacts=self.config.require_full_case_artifacts,
                    )
                if prep_res.get("event_id") is not None and cand.event_id is None:
                    cand.event_id = int(prep_res["event_id"])
                cand.odds_state = str(prep_res.get("odds_state") or cand.odds_state)
            except Exception as exc:
                cand.blocking_reasons.append(f"PROVIDER_OR_PREP_FAILURE:{exc}")
                cand.audit_outcome = "FAILED_ISOLATED_TECH_ERROR"
                cand.transition_to(MultiMatchExecutionState.FAILED_TECHNICAL, now_utc, f"ISOLATED_FAILURE:{exc}")
                plan.log_event(
                    now_utc,
                    "ISOLATED_CANDIDATE_FAILURE",
                    {"fixture_id": cand.fixture_id, "slot_id": cand.slot_id, "error": str(exc)},
                )
                return

        if self.config.dry_run:
            return

        event_dir = self.root / "outputs" / "matches" / str(cand.event_id)
        art = inspect_operational_case_artifacts(
            self.root,
            str(cand.event_id),
            require_full_artifacts=self.config.require_full_case_artifacts,
        )

        # Reconciliación segura ante expedientes previos incompletos (p.ej. 3.0-slot-case promovidos sin datos).
        if (
            self.config.require_full_case_artifacts
            and not art["data_ready"]
            and cand.execution_state
            in {
                MultiMatchExecutionState.WAITING_RESEARCH,
                MultiMatchExecutionState.WAITING_LINEUPS,
                MultiMatchExecutionState.READY_FOR_FINALIZATION,
            }
        ):
            try:
                if not art["canonical_case_valid"]:
                    ensure_canonical_slot_selection(
                        self.root,
                        cand,
                        raw_fixture,
                        now_utc,
                        bwin_row=bwin_row,
                        preselection_path=plan.preselection_path,
                    )
                cand.transition_to(MultiMatchExecutionState.PREPARING, now_utc, "RECONCILE_MISSING_CASE_DATA")
                art = inspect_operational_case_artifacts(
                    self.root,
                    str(cand.event_id),
                    require_full_artifacts=True,
                )
            except Exception as exc:
                cand.blocking_reasons.append(f"RECONCILE_CASE_ERROR:{exc}")
                cand.audit_outcome = "FAILED_ISOLATED_TECH_ERROR"
                cand.transition_to(MultiMatchExecutionState.FAILED_TECHNICAL, now_utc, f"RECONCILE_ERROR:{exc}")
                return

        # Fase 1.5: Adquisición real de contexto API-Football y cuotas Bwin antes de avanzar a investigación/XI.
        if cand.execution_state == MultiMatchExecutionState.PREPARING and self.config.require_full_case_artifacts and not art["data_ready"]:
            # Asegura caso canónico si venía de una migración o stub previo.
            if not art["canonical_case_valid"]:
                try:
                    ensure_canonical_slot_selection(
                        self.root,
                        cand,
                        raw_fixture,
                        now_utc,
                        bwin_row=bwin_row,
                        preselection_path=plan.preselection_path,
                    )
                    art = inspect_operational_case_artifacts(self.root, str(cand.event_id), require_full_artifacts=True)
                except Exception as exc:
                    cand.blocking_reasons.append(f"CANONICAL_CASE_ERROR:{exc}")
                    cand.audit_outcome = "FAILED_ISOLATED_TECH_ERROR"
                    cand.transition_to(MultiMatchExecutionState.FAILED_TECHNICAL, now_utc, f"CASE_ERROR:{exc}")
                    return

            can_acquire = bool(
                self.adapters.context_and_odds_acquirer is not None
                or (self.config.online and self.config.confirm_external)
            )
            if not can_acquire:
                cand.odds_state = "CAPTURED_VALID" if art["odds_ready"] else "NOT_CAPTURED"
                cand.audit_outcome = (
                    "PARTIAL_DATA_ACQUIRED" if art["operational_stage"] == "PARTIAL_DATA_ACQUIRED" else "PENDING_DATA_ACQUISITION"
                )
                for miss in art["missing"]:
                    if miss not in cand.blocking_reasons:
                        cand.blocking_reasons.append(miss)
                return

            ko_utc = parse_iso_utc(cand.kickoff_utc)
            remain_min = (ko_utc - now_utc).total_seconds() / 60.0
            if remain_min < float(self.config.min_lead_minutes):
                cand.eligibility_status = "INELIGIBLE_LEAD_TIME"
                cand.audit_outcome = "EXPIRED_BEFORE_ACQUISITION"
                reason = f"INSUFFICIENT_LEAD_FOR_ACQUISITION:{remain_min:.1f}m<{self.config.min_lead_minutes}m"
                if reason not in cand.blocking_reasons:
                    cand.blocking_reasons.append(reason)
                cand.transition_to(MultiMatchExecutionState.EXPIRED, now_utc, reason)
                plan.log_event(
                    now_utc,
                    "ACQUISITION_SKIPPED_INSUFFICIENT_LEAD",
                    {"fixture_id": cand.fixture_id, "event_id": cand.event_id, "remain_min": round(remain_min, 2)},
                )
                return

            need_scrape = not art["odds_ready"]
            need_api = not art["context_ready"]
            if (need_api and not plan.budget.can_spend_api(1)) or (need_scrape and not plan.budget.can_spend_scrape(1)):
                if "BUDGET_EXCEEDED_PENDING_ACQUISITION" not in cand.blocking_reasons:
                    cand.blocking_reasons.append("BUDGET_EXCEEDED_PENDING_ACQUISITION")
                cand.audit_outcome = "PENDING_BUDGET_FOR_ACQUISITION"
                return

            if need_api:
                plan.budget.spend_api(1)
            if need_scrape:
                plan.budget.spend_scrape(1)

            try:
                if self.adapters.context_and_odds_acquirer is not None:
                    acq_res = self.adapters.context_and_odds_acquirer(self.root, cand, raw_fixture, now_utc)
                else:
                    acq_res = default_context_and_odds_acquirer(
                        self.root,
                        cand,
                        raw_fixture,
                        now_utc,
                        bwin_row=bwin_row,
                    )
            except Exception as exc:
                cand.blocking_reasons.append(f"ACQUISITION_PROVIDER_FAILURE:{exc}")
                cand.audit_outcome = "FAILED_ISOLATED_TECH_ERROR"
                cand.transition_to(MultiMatchExecutionState.FAILED_TECHNICAL, now_utc, f"ACQ_FAILURE:{exc}")
                plan.log_event(
                    now_utc,
                    "ISOLATED_ACQUISITION_FAILURE",
                    {"fixture_id": cand.fixture_id, "event_id": cand.event_id, "error": str(exc)},
                )
                return

            art = inspect_operational_case_artifacts(self.root, str(cand.event_id), require_full_artifacts=True)
            cand.odds_state = "CAPTURED_VALID" if art["odds_ready"] else "NOT_CAPTURED"
            if art.get("manifest_status") == "PREPARADO_PROVISIONAL":
                note = "BWIN_KICKOFF_UNVERIFIED_PROVISIONAL"
                if note not in cand.notes:
                    cand.notes.append(note)
            if art.get("selected_match_path"):
                _write_factual_research_brief(
                    event_dir,
                    cand,
                    bwin_url=art.get("bwin_url"),
                    selected_path=Path(art["selected_match_path"]),
                    art=art,
                )
            plan.log_event(
                now_utc,
                "CASE_ACQUISITION_RESULT",
                {
                    "fixture_id": cand.fixture_id,
                    "event_id": cand.event_id,
                    "action": (acq_res or {}).get("action"),
                    "manifest_status": art.get("manifest_status"),
                    "data_ready": art["data_ready"],
                    "odds_ready": art["odds_ready"],
                    "context_ready": art["context_ready"],
                },
            )
            if not art["data_ready"]:
                cand.audit_outcome = f"ACQUISITION_INCOMPLETE:{art.get('manifest_status') or 'MISSING'}"
                for miss in art["missing"]:
                    if miss not in cand.blocking_reasons:
                        cand.blocking_reasons.append(miss)
                return

        if art["odds_ready"]:
            cand.odds_state = "CAPTURED_VALID"
        # Limpia bloqueos transitorios de adquisición una vez verificados todos los artefactos.
        if art["data_ready"]:
            cand.blocking_reasons = [
                r
                for r in cand.blocking_reasons
                if not (
                    r.startswith("MISSING_")
                    or r.startswith("MANIFEST_STATUS:")
                    or r in {"CANONICAL_CASE_OR_SELECTED_MATCH", "BUDGET_EXCEEDED_PENDING_ACQUISITION"}
                )
            ]

        research_md = _load_research_text(event_dir)
        if not research_md and art.get("research_source") and Path(str(art["research_source"])).is_file():
            research_md = Path(str(art["research_source"])).read_text(encoding="utf-8-sig")

        research_ok = bool(art["research_ready"]) if self.config.require_full_case_artifacts else bool(research_md)

        # Fase 2: Investigación cualitativa (únicamente después de data_ready)
        if cand.execution_state == MultiMatchExecutionState.PREPARING:
            if art.get("selected_match_path"):
                _write_factual_research_brief(
                    event_dir,
                    cand,
                    bwin_url=art.get("bwin_url"),
                    selected_path=Path(art["selected_match_path"]),
                    art=art,
                )
            if research_ok:
                cand.research_state = "ATTACHED_VERIFIED"
            elif self.config.require_research:
                cand.research_state = "WAITING_RESEARCH"
                cand.audit_outcome = "AWAITING_QUALITATIVE_RESEARCH"
                cand.transition_to(MultiMatchExecutionState.WAITING_RESEARCH, now_utc, "DATA_READY_AWAITING_RESEARCH")
                return
            else:
                cand.research_state = "NOT_REQUESTED"
            cand.transition_to(MultiMatchExecutionState.WAITING_LINEUPS, now_utc, "CHECK_PREMATCH_LINEUPS")

        if cand.execution_state == MultiMatchExecutionState.WAITING_RESEARCH:
            if research_ok:
                cand.research_state = "ATTACHED_VERIFIED"
                cand.transition_to(MultiMatchExecutionState.WAITING_LINEUPS, now_utc, "RESEARCH_ARRIVED")
            else:
                return

        # Fase 3: Control P0 de XI confirmados + buzón manual
        if cand.execution_state == MultiMatchExecutionState.WAITING_LINEUPS:
            api_resp = raw_fixture.get("api_lineups_response")
            if self.adapters.api_lineup_fetcher is not None and cand.lineup_state not in CONFIRMED_XI_STATES:
                if plan.budget.can_spend_api(1):
                    plan.budget.spend_api(1)
                    try:
                        api_resp = self.adapters.api_lineup_fetcher(cand, now_utc)
                    except Exception as exc:
                        cand.notes.append(f"API_LINEUP_FETCH_WARN:{exc}")

            manual_json = self._load_manual_lineup_from_inbox(event_dir, cand, raw_fixture, now_utc)
            prob_counts = raw_fixture.get("qualitative_probable_counts")
            eval_res = evaluate_fixture_lineup_state(
                api_lineups_response=api_resp,
                manual_lineup=manual_json,
                qualitative_markdown=research_md,
                qualitative_probable_counts=prob_counts,
                fixture_id=cand.fixture_id,
                event_id=str(cand.event_id),
                home_id=raw_fixture.get("home_id"),
                away_id=raw_fixture.get("away_id"),
                home_name=cand.home_team,
                away_name=cand.away_team,
                kickoff_utc=cand.kickoff_utc,
                now_utc=now_utc,
                lineup_cutoff_minutes=self.config.lineup_cutoff_minutes,
                user_manual_absent=user_manual_absent,
            )
            cand.lineup_state = eval_res["status"]
            eval_reasons = [r for r in [eval_res.get("reason"), *eval_res.get("validation_errors", [])] if r]

            # Opcionalmente refrescar cuotas cuando corresponde
            if self.adapters.odds_refresher is not None and cand.odds_state != "CAPTURED_VALID":
                if plan.budget.can_spend_scrape(1):
                    plan.budget.spend_scrape(1)
                    try:
                        odds_res = self.adapters.odds_refresher(event_dir, cand, now_utc)
                        cand.odds_state = str(odds_res.get("odds_state") or cand.odds_state)
                    except Exception as exc:
                        cand.notes.append(f"ODDS_REFRESH_WARN:{exc}")

            if cand.lineup_state in CONFIRMED_XI_STATES:
                cand.transition_to(MultiMatchExecutionState.READY_FOR_FINALIZATION, now_utc, cand.lineup_state)
            elif eval_res.get("status") == "LINEUPS_UNAVAILABLE_FINAL" or eval_res.get("cutoff_reached"):
                cand.blocking_reasons = list(dict.fromkeys(cand.blocking_reasons + (eval_reasons or ["MISSING_CONFIRMED_LINEUPS_AT_CUTOFF"])))
                cand.audit_outcome = "REJECTED_NO_CONFIRMED_XI"
                cand.transition_to(MultiMatchExecutionState.REJECTED_MISSING_LINEUPS, now_utc, "CUTOFF_NO_CONFIRMED_XI")
                return
            elif eval_res.get("status") == "LINEUPS_PROBABLE_ONLY":
                # P0: XI probables nunca permiten PICK_ACCIONABLE. Queda en espera o NO_BET condicional según cutoff.
                cand.shadow_decision = "CANDIDATO_CONDICIONAL"
                cand.blocking_reasons = list(dict.fromkeys(cand.blocking_reasons + ["LINEUPS_PROBABLE_ONLY_BLOCK_PICK"]))
                return
            else:
                return

        # Fase 4: Evaluación Shadow independiente y cierre analítico
        if cand.execution_state == MultiMatchExecutionState.READY_FOR_FINALIZATION:
            self._finalize_candidate_with_shadow(
                plan,
                cand,
                now_utc=now_utc,
                user_manual_absent=user_manual_absent,
            )

    def _validate_manual_payload(
        self,
        cand: SlotCandidateRecord,
        raw_fixture: dict[str, Any],
        payload: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        if payload.get("lineup_status"):
            return payload
        try:
            return validate_manual_lineup_payload(
                payload,
                expected_fixture_id=cand.fixture_id,
                expected_event_id=str(cand.event_id),
                expected_home_id=raw_fixture.get("home_id"),
                expected_away_id=raw_fixture.get("away_id"),
                expected_home_name=cand.home_team,
                expected_away_name=cand.away_team,
                kickoff_utc=cand.kickoff_utc,
                now_utc=now_utc,
                allow_probable_only=True,
            )
        except ValueError as exc:
            msg = f"MANUAL_LINEUP_INVALID:{exc}"
            if msg not in cand.blocking_reasons:
                cand.blocking_reasons.append(msg)
            return None

    def _load_manual_lineup_from_inbox(
        self,
        event_dir: Path,
        cand: SlotCandidateRecord,
        raw_fixture: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        inbox = scan_manual_lineup_inbox(event_dir, event_id=str(cand.event_id))
        for p in inbox.get("json_candidates", []):
            path = Path(p)
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                validated = self._validate_manual_payload(cand, raw_fixture, raw, now_utc)
                if validated is not None:
                    return validated
        return None

    def _finalize_candidate_with_shadow(
        self,
        plan: WindowOrchestrationPlan,
        cand: SlotCandidateRecord,
        *,
        now_utc: datetime,
        user_manual_absent: bool = False,
    ) -> None:
        art: dict[str, Any] | None = None
        if self.config.require_full_case_artifacts and cand.event_id is not None:
            art = inspect_operational_case_artifacts(
                self.root,
                str(cand.event_id),
                require_full_artifacts=True,
            )
            if not art["data_ready"]:
                cand.audit_outcome = "PENDING_DATA_ACQUISITION"
                for miss in art["missing"]:
                    if miss not in cand.blocking_reasons:
                        cand.blocking_reasons.append(miss)
                cand.transition_to(MultiMatchExecutionState.PREPARING, now_utc, "RECONCILE_MISSING_CASE_DATA")
                return

        try:
            if self.adapters.shadow_evaluator is not None:
                sh = self.adapters.shadow_evaluator(
                    self.root, cand, now_utc, self.config.lineup_cutoff_minutes, user_manual_absent
                )
            else:
                sh = _default_shadow_evaluator(
                    self.root, cand, now_utc, self.config.lineup_cutoff_minutes, user_manual_absent
                )
        except Exception as exc:
            cand.blocking_reasons.append(f"SHADOW_EVALUATION_ERROR:{exc}")
            cand.audit_outcome = "FAILED_ISOLATED_TECH_ERROR"
            cand.transition_to(MultiMatchExecutionState.FAILED_TECHNICAL, now_utc, f"SHADOW_EXC:{exc}")
            plan.active_by_slot[cand.slot_id or ""] = None
            return

        tax = str(sh.get("decision_taxonomy") or "NO_BET_POR_EVIDENCIA")
        # P0 guard absoluto: sin 22 XI confirmados o con identidad Bwin provisional jamás puede marcar PICK_ACCIONABLE
        if cand.lineup_state not in CONFIRMED_XI_STATES and tax == "PICK_ACCIONABLE":
            tax = "CANDIDATO_CONDICIONAL"
            cand.blocking_reasons.append("DOWNGRADED_FROM_ACTIONABLE_MISSING_CONFIRMED_XI")
        if art and art.get("manifest_status") == "PREPARADO_PROVISIONAL" and tax == "PICK_ACCIONABLE":
            tax = "CANDIDATO_CONDICIONAL"
            if "BWIN_KICKOFF_UNVERIFIED_PROVISIONAL" not in cand.blocking_reasons:
                cand.blocking_reasons.append("BWIN_KICKOFF_UNVERIFIED_PROVISIONAL")

        cand.shadow_decision = tax
        cand.candidate_selection = sh.get("candidate")
        if sh.get("blocking_reasons"):
            cand.blocking_reasons = list(dict.fromkeys(cand.blocking_reasons + list(sh["blocking_reasons"])))

        if tax == "PICK_ACCIONABLE":
            cand.audit_outcome = "ACTIONABLE_CANDIDATE_READY"
            cand.transition_to(MultiMatchExecutionState.FINALIZED, now_utc, "PICK_ACCIONABLE_READY")
        elif tax in ("BLOQUEO_TECNICO", "BLOQUEO_OPERATIVO"):
            cand.audit_outcome = "BLOCKED_OPERATIONAL"
            cand.transition_to(MultiMatchExecutionState.NO_BET, now_utc, f"TAXONOMY:{tax}")
        else:
            cand.audit_outcome = "NO_BET_AUDITED"
            cand.transition_to(MultiMatchExecutionState.NO_BET, now_utc, f"TAXONOMY:{tax}")

        if cand.slot_id:
            plan.active_by_slot[cand.slot_id] = None

    def _operational_slot_details(self, plan: WindowOrchestrationPlan, winner: SlotCandidateRecord | None) -> dict[str, Any]:
        if winner is None or winner.event_id is None:
            return {
                "operational_stage": "EMPTY",
                "manifest_status": None,
                "data_ready": False,
                "odds_ready": False,
                "context_ready": False,
                "selected_match_path": None,
                "bwin_url": None,
                "recommended_commands": [],
            }
        art = inspect_operational_case_artifacts(
            self.root,
            str(winner.event_id),
            require_full_artifacts=self.config.require_full_case_artifacts,
        )
        eid = str(winner.event_id)
        cmds: list[str] = []
        if not art["data_ready"]:
            cmds.append(
                f".venv/bin/python -B prematch_orchestrator.py acquire --date {plan.window_day} --event-id {eid} --online --confirm-external"
            )
            if art.get("manifest_status") == "PARCIAL_SIN_CUOTAS":
                cmds.append(f".venv/bin/python -B prematch_multi.py --confirm-external retry-scraper --event-id {eid} --workers 1")
            elif art.get("manifest_status") in {"ERROR_CONTEXTO", "ERROR_ARCHIVOS"}:
                cmds.append(f".venv/bin/python -B prematch_multi.py --confirm-external retry-context --event-id {eid} --workers 1")
            elif art.get("bwin_url"):
                cmds.append(f".venv/bin/python -B prematch_multi.py --confirm-external prepare --url {art['bwin_url']} --workers 1")
        else:
            if not art.get("research_ready"):
                cmds.append(
                    f".venv/bin/python -B prematch_orchestrator.py attach-research --date {plan.window_day} --event-id {eid} --source <INFORME.md>"
                )
            odds_path = Path(art["odds_path"]) if art.get("odds_path") else None
            if (
                art.get("manifest_status") != "FINALIZADO"
                and odds_path
                and odds_path.is_file()
                and art.get("selected_match_path")
            ):
                m_cap = re.search(r"(?im)^Captura:\s*(\S+)\s*$", odds_path.read_text(encoding="utf-8-sig", errors="replace")[:6000])
                if m_cap:
                    try:
                        cap_dt = parse_iso_utc(m_cap.group(1))
                        if (datetime.now(cap_dt.tzinfo) - cap_dt).total_seconds() / 60.0 > 60.0:
                            cmds.append(
                                f".venv/bin/python -B prematch_case.py refresh-odds --selected {art['selected_match_path']}"
                            )
                    except ValueError:
                        pass
            if art.get("selected_match_path") and winner.lineup_state not in CONFIRMED_XI_STATES:
                cmds.append(
                    f".venv/bin/python -B prematch_case.py acquire-lineups --selected {art['selected_match_path']}"
                )
        return {
            "operational_stage": art["operational_stage"],
            "manifest_status": art["manifest_status"] or None,
            "data_ready": art["data_ready"],
            "odds_ready": art["odds_ready"],
            "context_ready": art["context_ready"],
            "selected_match_path": art["selected_match_path"],
            "bwin_url": art["bwin_url"],
            "recommended_commands": cmds,
        }

    def render_summary(self, plan: WindowOrchestrationPlan) -> dict[str, Any]:
        slot_rows: list[dict[str, Any]] = []
        for spec in sorted(plan.slots, key=lambda s: s.slot_index):
            cands = sorted(
                [c for c in plan.candidates if c.slot_id == spec.slot_id],
                key=lambda c: (c.slot_rank if c.slot_rank is not None else 999999, c.fixture_id),
            )
            winner = next(
                (
                    c
                    for c in cands
                    if c.execution_state
                    in {
                        MultiMatchExecutionState.FINALIZED,
                        MultiMatchExecutionState.NO_BET,
                        *ACTIVE_SLOT_STATES,
                    }
                ),
                None,
            )
            op = self._operational_slot_details(plan, winner)
            slot_rows.append(
                {
                    "slot_id": spec.slot_id,
                    "label": spec.label,
                    "start_madrid": spec.start_madrid,
                    "end_madrid": spec.end_madrid,
                    "candidate_count": len(cands),
                    "active_or_closed_fixture_id": winner.fixture_id if winner else None,
                    "active_or_closed_event_id": winner.event_id if winner else None,
                    "execution_state": winner.execution_state.value if winner else "EMPTY",
                    "shadow_decision": winner.shadow_decision if winner else None,
                    "lineup_state": winner.lineup_state if winner else None,
                    "fallbacks_used": plan.budget.slot_fallbacks_used.get(spec.slot_id, 0),
                    **op,
                }
            )
        return {
            "schema_version": "3.0-window-summary",
            "window_label": plan.window_label,
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "parent_plan_id": plan.parent_plan_id,
            "rebuild_reason": plan.rebuild_reason,
            "filter_implementation": plan.filter_implementation,
            "window_start_madrid": plan.window_start_madrid,
            "window_end_madrid": plan.window_end_madrid,
            "preselection_path": plan.preselection_path,
            "preselection_sha256": plan.preselection_sha256,
            "preselection_auto_generated": plan.preselection_auto_generated,
            "total_candidates": len(plan.candidates),
            "eligible_candidates": sum(1 for c in plan.candidates if c.eligibility_status == "ELIGIBLE"),
            "active_count": sum(1 for v in plan.active_by_slot.values() if v is not None),
            "budget": plan.budget.to_dict(),
            "decision_summary": self._render_decision_summary(plan.candidates),
            "slots": slot_rows,
            "top_3_global": [
                {
                    "global_rank": c.global_rank,
                    "slot_id": c.slot_id,
                    "fixture_id": c.fixture_id,
                    "event_id": c.event_id,
                    "match": f"{c.home_team} - {c.away_team}".strip(" -"),
                    "quality_score": c.quality_score,
                    "eligibility_status": c.eligibility_status,
                    "execution_state": c.execution_state.value,
                    "shadow_decision": c.shadow_decision,
                }
                for c in sorted(plan.candidates, key=lambda x: x.global_rank or 999999)[:3]
            ],
        }

    @staticmethod
    def _render_decision_summary(candidates: list[SlotCandidateRecord]) -> dict[str, Any]:
        """Contabiliza decisiones por partido separando NO BET estadísticos de expedientes incompletos/bloqueados."""
        bet_autorizado = 0
        no_bet_precio = 0
        no_bet_incertidumbre = 0
        bloqueo_o_incompleto = 0
        for c in candidates:
            if c.audit_outcome == "ACTIONABLE_CANDIDATE_READY" or c.shadow_decision == "PICK_ACCIONABLE":
                bet_autorizado += 1
            elif c.audit_outcome == "NO_BET_AUDITED":
                if c.shadow_decision == "NO_BET_POR_PRECIO":
                    no_bet_precio += 1
                else:
                    no_bet_incertidumbre += 1
            else:
                bloqueo_o_incompleto += 1
        stat_eval = bet_autorizado + no_bet_precio + no_bet_incertidumbre
        return {
            "total_candidates": len(candidates),
            "statistical_evaluated_matches": stat_eval,
            "bet_autorizado": bet_autorizado,
            "no_bet_estadistico_total": no_bet_precio + no_bet_incertidumbre,
            "no_bet_por_precio": no_bet_precio,
            "no_bet_por_incertidumbre": no_bet_incertidumbre,
            "incomplete_or_blocked_excluded_from_no_bet": bloqueo_o_incompleto,
            "bet_autorizado_pct_over_statistical": round((bet_autorizado / stat_eval) * 100.0, 2) if stat_eval > 0 else 0.0,
            "no_bet_estadistico_pct_over_statistical": round(((no_bet_precio + no_bet_incertidumbre) / stat_eval) * 100.0, 2) if stat_eval > 0 else 0.0,
        }

    def write_summary_reports(self, plan: WindowOrchestrationPlan) -> None:
        summary = self.render_summary(plan)
        atomic_write_json(self.summary_json_path, summary)
        lines = [
            f"# Resumen Ventana PREMATCH {plan.window_label}",
            "",
            f"- Inicio Madrid: `{plan.window_start_madrid}`",
            f"- Fin Madrid: `{plan.window_end_madrid}`",
            f"- Total candidatos: `{summary['total_candidates']}` | Elegibles: `{summary['eligible_candidates']}` | Activos: `{summary['active_count']}`",
            f"- Presupuesto API usado: `{plan.budget.api_calls_used}/{plan.budget.max_api_calls}` | Scrapes: `{plan.budget.scrapes_used}/{plan.budget.max_scrapes}`",
            "",
            "## Franjas",
            "",
            "| Franja | Partido | Fixture | Event ID | Estado | XI | Shadow | Fallbacks |",
            "|---|---|---:|---:|---|---|---|---:|",
        ]
        for row in summary["slots"]:
            fid = row["active_or_closed_fixture_id"]
            cand = plan.get_candidate(fid) if fid is not None else None
            match_name = f"{cand.home_team} - {cand.away_team}".strip(" -") if cand else "VACÍA"
            lines.append(
                f"| {row['slot_id']} | {match_name} | {fid or '—'} | {row['active_or_closed_event_id'] or '—'} | "
                f"{row['execution_state']} | {row['lineup_state'] or '—'} | {row['shadow_decision'] or '—'} | {row['fallbacks_used']} |"
            )
        self.summary_md_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
