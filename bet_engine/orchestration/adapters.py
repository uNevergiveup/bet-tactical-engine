"""Adaptadores compatibles para conectar Shadow Mode y el ciclo T-24h/proximidad
con prematch_daily.py, prematch_single.py y prematch_multi.py sin duplicar lógica ni
alterar el flujo operativo actual.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bet_engine.context import MatchObservationBundle, ObservationStore
from bet_engine.contracts import (
    MatchScheduleCandidate,
    PreAnalysisT24Snapshot,
    ProximityPlanState,
    ScheduleExecutionStatus,
)
from bet_engine.contracts.epistemic import parse_utc_timestamp

from .scheduler import PrematchLifecycleController
from .shadow_pipeline import ShadowPipelineRunner, parse_bwin_odds_txt


def _load_plan_state(path: Path) -> ProximityPlanState:
    data = json.loads(path.read_text(encoding="utf-8"))
    candidates = tuple(
        MatchScheduleCandidate(
            **{**c, "execution_status": ScheduleExecutionStatus(c["execution_status"])}
        )
        for c in data.get("candidates", [])
    )
    return ProximityPlanState(
        plan_id=data["plan_id"],
        target_date_local=data["target_date_local"],
        available_from_local=data.get("available_from_local", "13:00"),
        timezone_name=data.get("timezone_name", "Europe/Madrid"),
        min_quality_score=float(data.get("min_quality_score", 50.0)),
        superior_quality_delta=float(data.get("superior_quality_delta", 15.0)),
        candidates=candidates,
        active_event_id=data.get("active_event_id"),
        completed_event_ids=tuple(data.get("completed_event_ids", ())),
        failed_event_ids=tuple(data.get("failed_event_ids", ())),
    )


def _append_shadow_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def _extract_legacy_motor_comparison(root: Path, event_id: str) -> dict[str, Any]:
    """Lee artefactos producidos por motor_probabilistico.py para comparación en paralelo."""
    prob_dir = root / "outputs" / "matches" / event_id / "probabilistic"
    ranking_path = prob_dir / f"ranking_probabilistico_{event_id}.json"
    matrix_path = prob_dir / f"matriz_evidencia_{event_id}.json"
    res: dict[str, Any] = {"present": False, "ranking_path": str(ranking_path), "top_selections": []}
    if ranking_path.is_file():
        res["present"] = True
        try:
            rank_doc = json.loads(ranking_path.read_text(encoding="utf-8"))
            rows = rank_doc if isinstance(rank_doc, list) else (rank_doc.get("rows") or rank_doc.get("ranking") or [])
            res["total_ranked"] = len(rows)
            for r in rows[:8]:
                if isinstance(r, dict):
                    res["top_selections"].append(
                        {
                            "market": r.get("market") or r.get("mercado"),
                            "selection": r.get("selection") or r.get("seleccion"),
                            "odd": r.get("odd") or r.get("cuota"),
                            "hit_rate": r.get("hit_rate") or r.get("rate"),
                            "sample_n": r.get("n") or r.get("sample"),
                        }
                    )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            res["parse_error"] = str(exc)
    res["matrix_present"] = matrix_path.is_file()
    return res


def run_shadow_for_event_dir(
    root: Path,
    event_id: str,
    *,
    historical: bool = False,
    max_odds_age_minutes: int = 120,
    now_utc: str | datetime | None = None,
    min_lead_minutes: int = 45,
    lineup_cutoff_minutes: int = 15,
    user_manual_absent: bool = False,
) -> dict[str, Any]:
    """Ejecuta Shadow Mode de forma no intrusiva sobre outputs/matches/<event_id>/ tras finish.

    - Nunca modifica manifest.json, CODEX_<event>.md ni resultados de motor_probabilistico.py.
    - En cualquier excepción, registra el error en logs/shadow_mode.log sin romper el flujo legacy.
    """
    t0 = time.perf_counter()
    root = Path(root).resolve()
    ev_dir = root / "outputs" / "matches" / str(event_id)
    log_path = ev_dir / "logs" / "shadow_mode.log"
    shadow_dir = ev_dir / "shadow"

    try:
        manifest_path = ev_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"MANIFEST_MISSING:{manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        inputs = manifest.get("inputs") or {}
        ctx_json_path = Path((inputs.get("context_json") or {}).get("path") or (ev_dir / "inputs" / f"match_engine_{event_id}.json"))
        ctx_txt_path = Path((inputs.get("context") or {}).get("path") or (ev_dir / "inputs" / f"match_engine_{event_id}.txt"))
        odds_txt_path = Path((inputs.get("odds") or {}).get("path") or "") if (inputs.get("odds") or {}).get("path") else None

        if not ctx_json_path.is_file():
            raise FileNotFoundError(f"CONTEXT_JSON_MISSING:{ctx_json_path}")

        payload = json.loads(ctx_json_path.read_text(encoding="utf-8"))
        cutoff_override = now_utc
        # En auditoría histórica retrospectiva permitimos evaluar el snapshot en su fecha de generación
        # siempre que el cutoff efectivo siga siendo estrictamente <= kickoff (conservador T-60m)
        if cutoff_override is None and historical:
            gen_dt = parse_utc_timestamp(payload.get("generated_at") or manifest.get("finish_completed_at") or payload["fixture"]["date"])
            ko_dt = parse_utc_timestamp(payload["fixture"]["date"])
            cutoff_override = gen_dt if gen_dt < ko_dt else ko_dt
        elif cutoff_override is None and not historical:
            diff_acq = (payload.get("differential_refresh") or {}).get("acquired_at_utc")
            if diff_acq:
                acq_dt = parse_utc_timestamp(diff_acq)
                gen_dt = parse_utc_timestamp(payload["generated_at"]) if payload.get("generated_at") else acq_dt
                ko_dt = parse_utc_timestamp(payload["fixture"]["date"])
                latest = max(acq_dt, gen_dt)
                if latest < ko_dt:
                    cutoff_override = latest

        bundle = ObservationStore.from_match_engine_json(payload, cutoff_utc=cutoff_override)

        # Verificación de frescura y hash de cuotas (no reutilizar cuotas caducadas ni desincronizadas)
        import hashlib
        import re

        ctx_json_sha256 = hashlib.sha256(ctx_json_path.read_bytes()).hexdigest()
        odds_map: dict[str, float] = {}
        odds_freshness_status = "NO_ODDS_FILE"
        odds_sha256: str | None = None
        odds_captured_utc: str | None = None
        if odds_txt_path and odds_txt_path.is_file():
            odds_sha256 = hashlib.sha256(odds_txt_path.read_bytes()).hexdigest()
            manifest_odds_sha = (inputs.get("odds") or {}).get("sha256")
            if not historical and not manifest_odds_sha:
                raise ValueError("ODDS_MANIFEST_SHA_MISSING")
            if manifest_odds_sha and manifest_odds_sha != odds_sha256:
                raise ValueError(f"ODDS_MANIFEST_SHA_MISMATCH:{odds_sha256}!={manifest_odds_sha}")
            match_meta = payload.get("match") or {}
            parsed_odds = parse_bwin_odds_txt(
                odds_txt_path,
                home_name=match_meta.get("home_name") or bundle.home_name,
                away_name=match_meta.get("away_name") or bundle.away_name,
                home_aliases=(bundle.home_name, match_meta.get("home_name_raw") or ""),
                away_aliases=(bundle.away_name, match_meta.get("away_name_raw") or ""),
                gender=match_meta.get("gender"),
            )
            txt_head = odds_txt_path.read_text(encoding="utf-8-sig", errors="replace")[:1500]
            cap = re.search(r"(?:Fecha captura|Captura)[^:]*:\s*(\S+)", txt_head)
            if not cap:
                odds_freshness_status = "MISSING_ODDS_CAPTURE_TIMESTAMP"
                odds_map = {}
            else:
                cap_raw = cap.group(1).strip()
                cap_dt: datetime | None = None
                try:
                    iso_str = cap_raw[:-1] + "+00:00" if cap_raw.endswith("Z") else cap_raw
                    dt_parsed = datetime.fromisoformat(iso_str)
                    if dt_parsed.tzinfo is not None:
                        cap_dt = dt_parsed.astimezone(timezone.utc)
                except ValueError:
                    cap_dt = None

                if cap_dt is None:
                    odds_freshness_status = "INVALID_ODDS_CAPTURE_TIMESTAMP"
                    odds_map = {}
                else:
                    odds_captured_utc = cap_dt.isoformat()
                    ko_dt_ev = parse_utc_timestamp(bundle.kickoff_utc)
                    cutoff_dt_ev = parse_utc_timestamp(bundle.cutoff_utc)
                    if historical:
                        if cap_dt >= ko_dt_ev or cap_dt > cutoff_dt_ev + timedelta(minutes=2):
                            odds_freshness_status = "POST_CUTOFF_OR_LIVE_ODDS_EXCLUDED"
                            odds_map = {}
                        else:
                            odds_map = parsed_odds
                            odds_freshness_status = "LOADED"
                    else:
                        ref_dt = parse_utc_timestamp(cutoff_override) if cutoff_override is not None else datetime.now(timezone.utc)
                        if cap_dt >= ko_dt_ev or cap_dt > ref_dt + timedelta(minutes=2):
                            odds_freshness_status = "POST_CUTOFF_OR_LIVE_ODDS_EXCLUDED"
                            odds_map = {}
                        else:
                            age_min = (ref_dt - cap_dt).total_seconds() / 60.0
                            if age_min > max_odds_age_minutes:
                                odds_freshness_status = f"EXPIRED_ODDS_EXCLUDED(age={age_min:.1f}m>{max_odds_age_minutes}m)"
                                odds_map = {}
                            else:
                                odds_map = parsed_odds
                                odds_freshness_status = "LOADED"

        # Leer investigación cualitativa y XI manual validado si están adjuntos en manifest
        qualitative_md: str | None = None
        for r_key in ("qualitative_research", "report"):
            r_ref = inputs.get(r_key) or {}
            if r_ref.get("path") and Path(r_ref["path"]).is_file():
                qualitative_md = Path(r_ref["path"]).read_text(encoding="utf-8-sig", errors="replace")
                break
        manual_lu_doc: dict[str, Any] | None = None
        ml_ref = inputs.get("manual_lineups") or {}
        if ml_ref.get("path") and Path(ml_ref["path"]).is_file():
            try:
                manual_lu_doc = json.loads(Path(ml_ref["path"]).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, json.JSONDecodeError):
                manual_lu_doc = None

        # Si historical=True, usamos min_lead=0 en Shadow retrospectivo pero etiquetando histórico
        effective_min_lead = 0 if historical else min_lead_minutes
        target_status = "NS" if historical else str((payload.get("fixture") or {}).get("status") or "NS")
        runner = ShadowPipelineRunner(
            min_lead_minutes=effective_min_lead,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
        )
        report = runner.run_shadow_on_bundle(
            bundle,
            target_status=target_status,
            odds_map=odds_map,
            legacy_txt_path=ctx_txt_path if ctx_txt_path.is_file() else None,
            output_dir=shadow_dir,
            bwin_txt_path=odds_txt_path if (odds_txt_path and odds_txt_path.is_file()) else None,
            qualitative_markdown=qualitative_md,
            manual_lineup=manual_lu_doc,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
            user_manual_absent=user_manual_absent,
        )

        legacy_summary = _extract_legacy_motor_comparison(root, str(event_id))
        prospective_2b3b_status: dict[str, Any] = {"status": "SKIPPED_HISTORICAL_MODE"}
        if not historical and target_status.upper() == "NS" and report.status != "BLOCKED_PREMATCH_GUARD":
            try:
                from bet_engine.modeling.prospective_cand_2b3b import record_2b3b_prospective_prediction
                from bet_engine.modeling.prospective_registry import ProspectiveRegistryError

                rec_now = (
                    parse_utc_timestamp(now_utc)
                    if isinstance(now_utc, str)
                    else (now_utc if isinstance(now_utc, datetime) else datetime.now(timezone.utc))
                )
                cand_manifest = root / "audit" / "fase2b_experimentos" / "MANIFEST_CANDIDATA_2B3B.json"
                rec_path = record_2b3b_prospective_prediction(
                    match_engine_payload=payload,
                    records_dir=root / "outputs" / "prospective_2b3b",
                    manifest_path=cand_manifest if cand_manifest.is_file() else None,
                    bwin_odds=odds_map if odds_map else None,
                    odds_captured_utc=odds_captured_utc,
                    odds_sha256=odds_sha256,
                    shadow_decision=report.decision,
                    qualitative_markdown=qualitative_md,
                    source_paths={
                        "match_engine": str(ctx_json_path),
                        "bwin_txt": str(odds_txt_path) if odds_txt_path else "",
                    },
                    now_utc=rec_now,
                    verify_manifest=cand_manifest.is_file(),
                )
                prospective_2b3b_status = {"status": "RECORDED_IMMUTABLE", "record_path": str(rec_path)}
            except Exception as rec_exc:
                msg = str(rec_exc)
                if "IMMUTABLE_RECORD_ALREADY_EXISTS" in msg:
                    prospective_2b3b_status = {"status": "ALREADY_RECORDED_IMMUTABLE_PRESERVED"}
                else:
                    prospective_2b3b_status = {"status": "SKIPPED_NON_BLOCKING", "reason": f"{type(rec_exc).__name__}:{rec_exc}"}
        prob_ts = report.probabilities_computed_at_utc or datetime.now(timezone.utc).isoformat()
        prices_ts = report.prices_evaluated_at_utc if odds_map else None
        granularity = getattr(report, "timestamp_granularity", "IN_PROCESS_SEQUENTIAL_SHADOW_RUN")
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        summary_doc = {
            "event_id": str(event_id),
            "fixture_id": bundle.fixture_id,
            "mode": "SHADOW_NON_INTRUSIVE",
            "historical_retrospective": bool(historical),
            "odds_freshness_status": odds_freshness_status,
            "odds_sha256": odds_sha256,
            "odds_captured_utc": odds_captured_utc,
            "odds_selection_count": len(odds_map),
            "context_json_sha256": ctx_json_sha256,
            "probabilities_computed_at_utc": prob_ts,
            "prices_evaluated_at_utc": prices_ts,
            "timestamp_granularity": granularity,
            "snapshot_provenance": {
                "odds_path": str(odds_txt_path.resolve()) if odds_txt_path and odds_txt_path.is_file() else None,
                "odds_sha256": odds_sha256,
                "odds_captured_utc": odds_captured_utc,
                "odds_selection_count": len(odds_map),
                "context_json_path": str(ctx_json_path.resolve()),
                "context_json_sha256": ctx_json_sha256,
                "probabilities_computed_at_utc": prob_ts,
                "prices_evaluated_at_utc": prices_ts,
                "timestamp_granularity": granularity,
            },
            "lineup_gate": report.lineup_gate,
            "eligible_for_final_action": report.eligible_for_final_action,
            "elapsed_ms": elapsed_ms,
            "legacy_motor_summary": legacy_summary,
            "prospective_2b3b_status": prospective_2b3b_status,
            "shadow_report": report.to_dict(),
        }
        shadow_dir.mkdir(parents=True, exist_ok=True)
        out_path = shadow_dir / f"shadow_summary_{event_id}.json"
        tmp_path = shadow_dir / f".shadow_summary_{event_id}.json.tmp"
        tmp_path.write_text(json.dumps(summary_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(out_path)
        _append_shadow_log(
            log_path,
            f"SHADOW_OK event={event_id} status={report.status} decision={report.decision} elapsed_ms={elapsed_ms} out={out_path}",
        )
        return {"ok": True, "path": str(out_path), "elapsed_ms": elapsed_ms, "status": report.status}
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        stale_out = shadow_dir / f"shadow_summary_{event_id}.json"
        if not historical and stale_out.is_file():
            try:
                inv_dir = ev_dir / "archives" / f"invalidated_shadow_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                inv_dir.mkdir(parents=True, exist_ok=True)
                (inv_dir / stale_out.name).write_bytes(stale_out.read_bytes())
                stale_out.unlink()
            except OSError:
                pass
        _append_shadow_log(log_path, f"SHADOW_FAILED_NON_BLOCKING event={event_id} err={type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "elapsed_ms": elapsed_ms}


def run_shadow_daily_optin(root: Path, folder: Path, day_str: str) -> dict[str, Any]:
    """Ejecuta Shadow Mode offline sobre la preselección congelada del escaneo diario."""
    t0 = time.perf_counter()
    prosp = Path(folder) / "prospective_trial"
    pre_path = prosp / "enriched" / "preselection.json"
    shadow_dir = prosp / "shadow"
    log_path = prosp / "logs" / "shadow_mode.log"

    try:
        if not pre_path.is_file():
            return {"ok": False, "skipped": f"PRESELECTION_MISSING:{pre_path}"}
        pre_doc = json.loads(pre_path.read_text(encoding="utf-8"))
        cutoff = pre_doc.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
        runner = ShadowPipelineRunner(min_lead_minutes=45)
        results: list[dict[str, Any]] = []
        for match in pre_doc.get("matches", [])[:10]:
            try:
                bundle = ObservationStore.from_preselection_match(match, cutoff_utc=cutoff)
                rep = runner.run_shadow_on_bundle(bundle, output_dir=shadow_dir / str(bundle.event_id))
                results.append(
                    {
                        "event_id": bundle.event_id,
                        "fixture_id": bundle.fixture_id,
                        "match": f"{bundle.home_name} vs {bundle.away_name}",
                        "status": rep.status,
                        "decision": rep.decision,
                        "home_matches_new": rep.historical_depth_comparison.get("new_home_matches_n"),
                        "home_matches_legacy_p6": rep.historical_depth_comparison.get("legacy_p6_home_matches_n"),
                        "no_bet_reasons": list(rep.no_bet_reasons),
                    }
                )
            except Exception as exc:
                results.append({"fixture_id": match.get("fixture_id"), "shadow_error": str(exc)})

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        summary = {
            "schema": "1.0-shadow-daily-summary",
            "date": day_str,
            "mode": "SHADOW_NON_INTRUSIVE_ZERO_API_CALLS",
            "elapsed_ms": elapsed_ms,
            "evaluated_count": len(results),
            "results": results,
        }
        shadow_dir.mkdir(parents=True, exist_ok=True)
        out_path = shadow_dir / "shadow_daily_summary.json"
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _append_shadow_log(log_path, f"SHADOW_DAILY_OK date={day_str} count={len(results)} elapsed_ms={elapsed_ms}")
        return {"ok": True, "path": str(out_path), "elapsed_ms": elapsed_ms, "count": len(results)}
    except Exception as exc:
        _append_shadow_log(log_path, f"SHADOW_DAILY_FAILED_NON_BLOCKING err={type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_daily_shadow_lifecycle(
    root: Path,
    *,
    action: str,  # preanalyze | status | finalize
    date_str: str,
    preselection_path: Path | None = None,
    shadow_dir: Path | None = None,
    available_from: str = "13:00",
    now_utc: str | None = None,
    manual_select_event_id: str | None = None,
    event_id: str | None = None,
    odds_json_path: Path | None = None,
    max_odds_age_minutes: int = 60,
    min_lead_minutes: int = 45,
    manual_lineup_path: Path | None = None,
    lineup_cutoff_minutes: int = 15,
    fallback_on_missing_lineups: bool = False,
    user_manual_absent: bool = False,
    auto_continue_next: bool = False,
    next_odds_json_path: Path | None = None,
    next_manual_lineup_path: Path | None = None,
) -> dict[str, Any]:
    """Ejecuta el ciclo completo T-24h persistente y reiniciable (preanalyze -> status -> finalize)."""
    t0 = time.perf_counter()
    root = Path(root).resolve()
    ws_dir = Path(shadow_dir).resolve() if shadow_dir else (root / "outputs" / "shadow_lifecycle" / date_str)
    ctrl = PrematchLifecycleController(
        ws_dir,
        available_from_local=available_from,
        timezone_name="Europe/Madrid",
        min_quality_score=40.0,
        min_lead_minutes=min_lead_minutes,
        safety_margin_minutes=10,
        lineup_cutoff_minutes=lineup_cutoff_minutes,
    )
    now_dt = parse_utc_timestamp(now_utc) if now_utc else datetime.now(timezone.utc)

    if action == "preanalyze":
        if preselection_path is None or not Path(preselection_path).is_file():
            raise FileNotFoundError(f"PRESELECTION_REQUIRED_FOR_PREANALYZE:{preselection_path}")
        pre_doc = json.loads(Path(preselection_path).read_text(encoding="utf-8"))
        cutoff = pre_doc.get("generated_at_utc") or now_dt.isoformat()
        bundles: list[MatchObservationBundle] = []
        for idx, m in enumerate(pre_doc.get("matches", [])):
            # Conserva fixture_id/event_id explícitos sin inventar datos
            b = ObservationStore.from_preselection_match(m, cutoff_utc=cutoff)
            bundles.append(b)
        res = ctrl.preanalyze(
            target_date_local=date_str,
            bundles=bundles,
            generated_at_utc=cutoff,
        )
        # Persistimos también el índice de preselección fuente para poder reconstruir sin llamadas API tras reinicio
        (ws_dir / "t24_source_index.json").write_text(
            json.dumps(
                {
                    "preselection_path": str(Path(preselection_path).resolve()),
                    "generated_at_utc": cutoff,
                    "event_count": len(bundles),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "action": "shadow_preanalyze",
            "workspace": str(ws_dir),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "api_calls_executed": 0,
            **res,
        }

    plan_path = ws_dir / "proximity_plan_state.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"PROXIMITY_PLAN_MISSING:{plan_path}")
    plan_state = _load_plan_state(plan_path)

    if action == "status":
        st = ctrl.status(
            plan_state=plan_state,
            now_utc=now_dt,
            manual_override_event_id=manual_select_event_id,
        )
        return {
            "action": "shadow_status",
            "workspace": str(ws_dir),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            **st,
        }

    if action == "finalize":
        st = ctrl.status(
            plan_state=plan_state,
            now_utc=now_dt,
            manual_override_event_id=manual_select_event_id,
        )
        chosen_id = event_id or manual_select_event_id or st["next_action"].get("selected_event_id")
        if not chosen_id:
            return {
                "action": "shadow_finalize_blocked",
                "reason": st["next_action"].get("status"),
                "status": st,
            }

        # Recupera históricos T-24h desde disco sin repetir consultas API históricas
        idx_path = ws_dir / "t24_source_index.json"
        src_pre_path = (
            Path(preselection_path)
            if preselection_path
            else Path(json.loads(idx_path.read_text(encoding="utf-8"))["preselection_path"])
        )
        pre_doc = json.loads(src_pre_path.read_text(encoding="utf-8"))
        target_match = next(
            (m for m in pre_doc.get("matches", []) if str(m.get("event_id") or m.get("fixture_id")) == str(chosen_id)),
            None,
        )
        if target_match is None:
            raise ValueError(f"EVENT_NOT_FOUND_IN_T24_SOURCE:{chosen_id}")

        bundle = ObservationStore.from_preselection_match(target_match, cutoff_utc=now_dt)

        def _load_fresh_odds(odds_path: Path | None, target_eid: str) -> tuple[dict[str, float], str]:
            if not odds_path or not Path(odds_path).is_file():
                return {}, "NO_FRESH_ODDS_SUPPLIED"
            odds_doc = json.loads(Path(odds_path).read_text(encoding="utf-8"))
            if str(odds_doc.get("event_id") or "") not in ("", str(target_eid)):
                raise ValueError(f"ODDS_EVENT_ID_MISMATCH:{odds_doc.get('event_id')}!={target_eid}")
            cap_utc = odds_doc.get("captured_at_utc")
            if cap_utc:
                age_min = (now_dt - parse_utc_timestamp(cap_utc)).total_seconds() / 60.0
                if age_min > max_odds_age_minutes:
                    raise ValueError(f"EXPIRED_ODDS_REJECTED(age={age_min:.1f}m>{max_odds_age_minutes}m)")
                if age_min < 0:
                    raise ValueError("FUTURE_ODDS_TIMESTAMP_REJECTED")
            return {k: float(v) for k, v in (odds_doc.get("odds") or {}).items()}, "FRESH_ODDS_VERIFIED"

        def _load_manual_lu(lu_path: Path | None, target_bundle: MatchObservationBundle) -> dict[str, Any] | None:
            if not lu_path or not Path(lu_path).is_file():
                return None
            from bet_engine.context.lineup_manager import load_and_validate_manual_lineup_file

            return load_and_validate_manual_lineup_file(
                Path(lu_path),
                expected_fixture_id=target_bundle.fixture_id,
                expected_event_id=target_bundle.event_id,
                expected_home_id=target_bundle.home_id,
                expected_away_id=target_bundle.away_id,
                expected_home_name=target_bundle.home_name,
                expected_away_name=target_bundle.away_name,
                kickoff_utc=target_bundle.kickoff_utc,
                now_utc=now_dt,
            )

        updated_odds, odds_validation = _load_fresh_odds(odds_json_path, str(chosen_id))
        validated_manual_lu = _load_manual_lu(manual_lineup_path, bundle)

        rep, updated_plan, next_st = ctrl.finalize(
            plan_state=plan_state,
            bundle=bundle,
            now_utc=now_dt,
            target_status="NS",
            updated_odds_map=updated_odds,
            manual_override_event_id=manual_select_event_id,
            manual_lineup=validated_manual_lu,
            lineup_cutoff_minutes=lineup_cutoff_minutes,
            fallback_on_missing_lineups=fallback_on_missing_lineups,
            user_manual_absent=user_manual_absent,
        )

        continued_result: dict[str, Any] | None = None
        if auto_continue_next and str(chosen_id) in updated_plan.failed_event_ids:
            next_eid = (next_st.get("next_action") or {}).get("selected_event_id")
            if next_eid:
                next_match = next(
                    (m for m in pre_doc.get("matches", []) if str(m.get("event_id") or m.get("fixture_id")) == str(next_eid)),
                    None,
                )
                if next_match is not None:
                    b_next = ObservationStore.from_preselection_match(next_match, cutoff_utc=now_dt)
                    next_odds, next_odds_val = _load_fresh_odds(next_odds_json_path, str(next_eid))
                    next_lu = _load_manual_lu(next_manual_lineup_path, b_next)
                    rep_next, updated_plan, next_st = ctrl.finalize(
                        plan_state=updated_plan,
                        bundle=b_next,
                        now_utc=now_dt,
                        target_status="NS",
                        updated_odds_map=next_odds,
                        manual_lineup=next_lu,
                        lineup_cutoff_minutes=lineup_cutoff_minutes,
                        fallback_on_missing_lineups=False,
                    )
                    continued_result = {
                        "event_id": str(next_eid),
                        "odds_validation": next_odds_val,
                        "shadow_report": rep_next.to_dict(),
                    }

        return {
            "action": "shadow_finalize",
            "event_id": str(chosen_id),
            "workspace": str(ws_dir),
            "reused_t24_cache": (ws_dir / "t24_cache" / f"t24_evidence_{chosen_id}.json").is_file(),
            "api_historical_calls_repeated": 0,
            "odds_validation": odds_validation,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "shadow_report": rep.to_dict(),
            "next_schedule_status": next_st,
            "continued_next_candidate": continued_result,
        }

    raise ValueError(f"UNKNOWN_SHADOW_LIFECYCLE_ACTION:{action}")
