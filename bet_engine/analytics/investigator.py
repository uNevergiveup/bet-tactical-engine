"""Codex como investigador táctico LLM (Etapa 3): contratos estructurados, modo manual por defecto,
adaptador CLI opcional agnóstico del proveedor, verificación determinista en Python y registro versionado.
"""

from __future__ import annotations

import json
import subprocess
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from bet_engine.context.observation_store import MatchObservationBundle
from bet_engine.contracts import (
    EpistemicContractError,
    PrematchAvailability,
    TacticalHypothesis,
)
from bet_engine.contracts.epistemic import validate_declarative_dsl

from .discovery import classify_dsl_availability
from .registry import AnalyticsEngineResult, build_feature_rows, eval_declarative_dsl


HYPOTHESIS_REGISTRY_SCHEMA = "1.0-hypothesis-registry"


@dataclass(frozen=True)
class VerifiedHypothesisRecord:
    """Resultado determinista en Python de contrastar una TacticalHypothesis."""

    hypothesis_id: str
    origin: str
    side: str
    target_metric: str
    target_threshold: float | str | bool
    prematch_availability: PrematchAvailability
    validation_state: str  # DESCRIPTIVELY_SUPPORTED | CONTRADICTED_BY_DATA | EXPLORATORY_SMALL_SAMPLE | REQUIRES_UNAVAILABLE_DATA
    evaluated_n: int
    condition_support: int
    hits: int
    hit_rate: float | None
    baseline_rate: float | None
    lift: float | None
    supporting_fixture_ids: tuple[int, ...]
    counterexample_fixture_ids: tuple[int, ...]
    competition_scope: str
    warnings: tuple[str, ...]
    authorized_for_probabilistic_adjustment: bool = False  # Siempre False hasta Etapa 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "origin": self.origin,
            "side": self.side,
            "target_metric": self.target_metric,
            "target_threshold": self.target_threshold,
            "prematch_availability": self.prematch_availability.value,
            "validation_state": self.validation_state,
            "evaluated_n": self.evaluated_n,
            "condition_support": self.condition_support,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "baseline_rate": self.baseline_rate,
            "lift": self.lift,
            "supporting_fixture_ids": list(self.supporting_fixture_ids),
            "counterexample_fixture_ids": list(self.counterexample_fixture_ids),
            "competition_scope": self.competition_scope,
            "warnings": list(self.warnings),
            "authorized_for_probabilistic_adjustment": False,
        }


class HypothesisRegistry:
    """Registro persistente y versionado de hipótesis propuestas y verificadas."""

    def __init__(self, registry_path: Path) -> None:
        self.registry_path = Path(registry_path)

    def load(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return {"schema": HYPOTHESIS_REGISTRY_SCHEMA, "version": 1, "entries": []}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def record_verifications(
        self,
        *,
        event_id: str,
        fixture_id: int,
        verifications: list[VerifiedHypothesisRecord],
    ) -> dict[str, Any]:
        doc = self.load()
        now_utc = datetime.now(timezone.utc).isoformat()
        existing = {
            (e.get("fixture_id"), e.get("hypothesis_id")): e
            for e in doc.get("entries", [])
            if isinstance(e, dict)
        }
        for v in verifications:
            key = (fixture_id, v.hypothesis_id)
            prev = existing.get(key)
            revision = int(prev.get("revision", 0)) + 1 if prev else 1
            entry = {
                "event_id": event_id,
                "fixture_id": fixture_id,
                "revision": revision,
                "updated_at_utc": now_utc,
                **v.to_dict(),
            }
            existing[key] = entry

        doc["schema"] = HYPOTHESIS_REGISTRY_SCHEMA
        doc["version"] = int(doc.get("version", 1))
        doc["updated_at_utc"] = now_utc
        doc["entries"] = list(existing.values())

        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return doc


class LLMInvestigatorPort(Protocol):
    """Puerto agnóstico al proveedor LLM para solicitar hipótesis estructuradas."""

    def propose_hypotheses(self, dossier: dict[str, Any]) -> list[dict[str, Any]]: ...


def verify_hypotheses(
    bundle: MatchObservationBundle,
    hypotheses: list[TacticalHypothesis | dict[str, Any]],
    *,
    registry: HypothesisRegistry | None = None,
) -> list[VerifiedHypothesisRecord]:
    """Verifica hipótesis propuestas por Codex o plantillas contra datos históricos en Python.

    Nunca ejecuta código Python arbitrario ni altera probabilidades de modelos.
    """
    home_rows = build_feature_rows(bundle.home_matches, side="home")
    away_rows = build_feature_rows(bundle.away_matches, side="away")
    results: list[VerifiedHypothesisRecord] = []

    for raw_h in hypotheses:
        if isinstance(raw_h, TacticalHypothesis):
            hid = raw_h.hypothesis_id
            origin = raw_h.origin
            dsl = raw_h.python_test
            side = "home"
            target_metric = "own.corners"
            target_cmp = "ge"
            target_val: Any = None
            families = raw_h.target_market_families
        elif isinstance(raw_h, dict):
            hid = str(raw_h.get("hypothesis_id") or "")
            if not hid:
                raise EpistemicContractError("HYPOTHESIS_ID_REQUIRED")
            origin = str(raw_h.get("origin") or "LLM_INVESTIGATOR_CODEX")
            dsl = raw_h.get("python_test")
            side = str(raw_h.get("side") or "home").lower()
            target_metric = str(raw_h.get("target_variable") or "own.corners")
            target_cmp = str(raw_h.get("target_cmp") or "ge")
            target_val = raw_h.get("target_threshold")
            families = tuple(raw_h.get("target_market_families") or ())
        else:
            raise EpistemicContractError("INVALID_HYPOTHESIS_OBJECT")

        if dsl is None:
            results.append(
                VerifiedHypothesisRecord(
                    hypothesis_id=hid,
                    origin=origin,
                    side=side,
                    target_metric=target_metric,
                    target_threshold="NONE",
                    prematch_availability=PrematchAvailability.MIXED,
                    validation_state="REQUIRES_UNAVAILABLE_DATA",
                    evaluated_n=0,
                    condition_support=0,
                    hits=0,
                    hit_rate=None,
                    baseline_rate=None,
                    lift=None,
                    supporting_fixture_ids=(),
                    counterexample_fixture_ids=(),
                    competition_scope="NONE",
                    warnings=("MISSING_DECLARATIVE_PYTHON_TEST",),
                )
            )
            continue

        validate_declarative_dsl(dsl)
        avail = classify_dsl_availability(dsl)

        rows = home_rows if side == "home" else away_rows
        exact_rows = [r for r in rows if r["league_id"] == bundle.league_id and r["season"] == bundle.season]
        if len(exact_rows) >= 6:
            eval_rows = exact_rows
            comp_scope = "exact_competition"
            warnings: list[str] = []
        else:
            eval_rows = rows
            comp_scope = "all_competitions_mixed"
            warnings = ["COMPETITION_MIX_HETEROGENEOUS"] if len(exact_rows) < len(rows) else []

        if avail != PrematchAvailability.PREMATCH_OBSERVABLE:
            warnings.append(f"NON_PREMATCH_TRIGGER_AVAILABILITY:{avail.value}")

        metric_leaf = target_metric.split(".")[-1]
        metric_prefix = "conceded" if target_metric.startswith("conceded.") else "own"

        # Filtra filas donde la métrica objetivo existe (null != 0)
        outcome_pairs: list[tuple[dict[str, Any], float]] = []
        for r in eval_rows:
            v = r[metric_prefix].get(metric_leaf)
            if v is not None:
                outcome_pairs.append((r, float(v)))

        if not outcome_pairs:
            results.append(
                VerifiedHypothesisRecord(
                    hypothesis_id=hid,
                    origin=origin,
                    side=side,
                    target_metric=target_metric,
                    target_threshold=target_val if target_val is not None else "MEDIAN",
                    prematch_availability=avail,
                    validation_state="REQUIRES_UNAVAILABLE_DATA",
                    evaluated_n=0,
                    condition_support=0,
                    hits=0,
                    hit_rate=None,
                    baseline_rate=None,
                    lift=None,
                    supporting_fixture_ids=(),
                    counterexample_fixture_ids=(),
                    competition_scope=comp_scope,
                    warnings=tuple(warnings + ["NO_OBSERVED_TARGET_VALUES"]),
                )
            )
            continue

        threshold_val: float = (
            float(target_val)
            if isinstance(target_val, (int, float))
            else statistics.median(v for _, v in outcome_pairs)
        )

        evaluated_n = 0
        cond_support = 0
        hits = 0
        base_hits = 0
        support_ids: list[int] = []
        counter_ids: list[int] = []

        for r, y in outcome_pairs:
            cond_res = eval_declarative_dsl(dsl, r)
            if cond_res is None:
                continue
            evaluated_n += 1
            if target_cmp == "gt":
                hit = y > threshold_val
            elif target_cmp == "le":
                hit = y <= threshold_val
            elif target_cmp == "lt":
                hit = y < threshold_val
            else:
                hit = y >= threshold_val

            if hit:
                base_hits += 1
            if cond_res:
                cond_support += 1
                if hit:
                    hits += 1
                    support_ids.append(r["fixture_id"])
                else:
                    counter_ids.append(r["fixture_id"])

        if evaluated_n == 0:
            state = "REQUIRES_UNAVAILABLE_DATA"
            hit_rate = baseline_rate = lift = None
        else:
            baseline_rate = round(base_hits / evaluated_n, 4)
            if cond_support == 0:
                state = "EXPLORATORY_SMALL_SAMPLE"
                hit_rate = lift = None
            else:
                hit_rate = round(hits / cond_support, 4)
                lift = round(hit_rate - baseline_rate, 4)
                if evaluated_n < 6 or cond_support < 3:
                    state = "EXPLORATORY_SMALL_SAMPLE"
                elif lift > 0.05 and hit_rate >= 0.55:
                    state = "DESCRIPTIVELY_SUPPORTED"
                elif lift <= 0.0 or hit_rate < 0.50:
                    state = "CONTRADICTED_BY_DATA"
                else:
                    state = "EXPLORATORY_SMALL_SAMPLE"

        results.append(
            VerifiedHypothesisRecord(
                hypothesis_id=hid,
                origin=origin,
                side=side,
                target_metric=target_metric,
                target_threshold=round(threshold_val, 4),
                prematch_availability=avail,
                validation_state=state,
                evaluated_n=evaluated_n,
                condition_support=cond_support,
                hits=hits,
                hit_rate=hit_rate,
                baseline_rate=baseline_rate,
                lift=lift,
                supporting_fixture_ids=tuple(support_ids),
                counterexample_fixture_ids=tuple(counter_ids),
                competition_scope=comp_scope,
                warnings=tuple(warnings),
            )
        )

    if registry is not None:
        registry.record_verifications(
            event_id=bundle.event_id,
            fixture_id=bundle.fixture_id,
            verifications=results,
        )
    return results


class TacticalInvestigator:
    """Orquestador del bucle investigador Codex <-> Python (modo manual por defecto)."""

    SCHEMA = "1.0-tactical-research-dossier"

    def __init__(
        self,
        *,
        registry: HypothesisRegistry | None = None,
        cli_port: LLMInvestigatorPort | None = None,
    ) -> None:
        self.registry = registry
        self.cli_port = cli_port

    def build_dossier(
        self,
        bundle: MatchObservationBundle,
        engine_result: AnalyticsEngineResult,
    ) -> dict[str, Any]:
        """Genera el dossier estructurado para Codex en modo manual o automático."""
        return {
            "schema": self.SCHEMA,
            "mode": "MANUAL_DEFAULT" if self.cli_port is None else "CLI_ADAPTER_CONFIGURED",
            "event_id": bundle.event_id,
            "fixture_id": bundle.fixture_id,
            "match": f"{bundle.home_name} vs {bundle.away_name}",
            "kickoff_utc": bundle.kickoff_utc,
            "cutoff_utc": bundle.cutoff_utc,
            "observations_summary": {
                "home_matches_n": len(bundle.home_matches),
                "away_matches_n": len(bundle.away_matches),
                "home_windows": {k: w.to_dict() for k, w in bundle.windows["home"].items() if k.endswith("_all_competitions") or k.endswith("_exact_competition")},
                "away_windows": {k: w.to_dict() for k, w in bundle.windows["away"].items() if k.endswith("_all_competitions") or k.endswith("_exact_competition")},
            },
            "contextual_evidence": [c.to_dict() for c in bundle.contextual_evidence],
            "analytics": engine_result.to_dict(),
            "rules": {
                "forbidden": [
                    "No inventar hechos, estadísticas ni probabilidades (p_model)",
                    "No emitir código Python arbitrario; usar únicamente python_test DSL",
                    "No confundir mecanismos INTRA_MATCH_MECHANISM con predictores PREMATCH_TRIGGER",
                ],
                "allowed_dsl_fields": [
                    "context.venue",
                    "context.observed_gap_days",
                    "context.league_id",
                    "context.coach_id",
                    "lag.own.shots_l5",
                    "lag.own.corners_l5",
                    "lag.own.fouls_l5",
                    "own.shots",
                    "own.blocked_shots",
                    "own.corners",
                    "score_state.minutes_trailing",
                    "score_state.red_card_distortion",
                ],
            },
        }

    def render_markdown_prompt(self, dossier: dict[str, Any]) -> str:
        """Renderiza un dossier legible en Markdown para Codex (modo manual por defecto)."""
        an = dossier["analytics"]
        lines = [
            f"# DOSSIER DE INVESTIGACIÓN TÁCTICA — {dossier['match']} (`{dossier['event_id']}`)",
            f"- Fixture ID: `{dossier['fixture_id']}` | Kickoff UTC: `{dossier['kickoff_utc']}` | Cutoff UTC: `{dossier['cutoff_utc']}`",
            f"- Patrones PREMATCH detectados: `{an['counts']['prematch_patterns']}`",
            f"- Mecanismos internos históricos: `{an['counts']['internal_mechanisms']}`",
            f"- Splits descriptivos (incl. SoS / Rojas / H1-H2): `{an['counts']['descriptive_splits']}`",
            "",
            "## Instrucciones para Codex (Investigador Táctico)",
            "1. Analiza contradicciones, contexto de árbitro, descanso multicompetición y régimen de entrenador.",
            "2. Propón hipótesis nuevas comprobables mediante JSON `python_test` (DSL declarativo puro, sin código Python).",
            "3. Python contrastará tus hipótesis y te devolverá soporte, hit_rate, lift y contraejemplos antes del análisis final.",
        ]
        coach_items = [c for c in dossier.get("contextual_evidence", []) if c.get("category") == "COACH_REGIME"]
        if coach_items:
            lines.extend(["", "## Régimen de Entrenadores y Procedencia"])
            for c in coach_items:
                f = c.get("facts") or {}
                lines.append(
                    f"- `{f.get('side')}` | status=`{f.get('coach_status')}` (`{c.get('verification_status')}`)"
                    f" | tier=`{f.get('source_tier')}` | primary_verified=`{f.get('primary_official_verified')}`"
                    f" | actual=`{f.get('current_coach_name')}` | último_histórico=`{f.get('last_historical_coach_name')}`"
                    f" | continuidad_verificada=`{f.get('continuity_verified')}`"
                    f" | partidos_bajo_actual=`{f.get('historical_matches_under_current_coach_n')}`"
                )
        qc_items = [c for c in dossier.get("contextual_evidence", []) if (c.get("provenance") or {}).get("source_type") == "QUICK_CONTEXT_MARKDOWN"]
        if qc_items:
            lines.extend(["", "## Quick Context Cualitativo Importado (No altera p_model)"])
            for c in qc_items:
                summary = (c.get("facts") or {}).get("summary", "")
                lines.append(f"- [`{c.get('verification_status')}` | {c.get('category')}] {summary}")
        return "\n".join(lines) + "\n"

    def write_manual_handoff(
        self,
        output_dir: Path,
        bundle: MatchObservationBundle,
        engine_result: AnalyticsEngineResult,
    ) -> dict[str, Path]:
        """Guarda dossier JSON + prompt Markdown para flujo manual de Codex."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        dossier = self.build_dossier(bundle, engine_result)
        dossier_path = out / f"tactical_dossier_{bundle.event_id}.json"
        prompt_path = out / f"CODEX_INVESTIGATOR_{bundle.event_id}.md"
        dossier_path.write_text(json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt_path.write_text(self.render_markdown_prompt(dossier), encoding="utf-8")
        return {"dossier_json": dossier_path, "prompt_md": prompt_path}

    def render_final_verification_report(
        self,
        dossier: dict[str, Any],
        verifications: list[VerifiedHypothesisRecord],
    ) -> str:
        """Renderiza el reporte final tras contrastar las hipótesis de Codex en Python."""
        lines = [
            f"# REPORTE DE VERIFICACIÓN TÁCTICA — {dossier['match']} (`{dossier['event_id']}`)",
            f"- Fixture ID: `{dossier['fixture_id']}` | Cutoff UTC: `{dossier['cutoff_utc']}`",
            f"- Hipótesis contrastadas en Python: `{len(verifications)}`",
            "",
            "## Resultados por Hipótesis y Contraejemplos",
        ]
        for v in verifications:
            lines.append(
                f"- **`{v.hypothesis_id}`** (`{v.side}` -> `{v.target_metric} >= {v.target_threshold}`): "
                f"estado=`{v.validation_state}`, disponibilidad=`{v.prematch_availability.value}`, "
                f"soporte=`{v.hits}/{v.condition_support}` (n={v.evaluated_n}), "
                f"hit_rate=`{v.hit_rate}`, baseline=`{v.baseline_rate}`, lift=`{v.lift}`, "
                f"apoyos=`{list(v.supporting_fixture_ids)}`, contraejemplos=`{list(v.counterexample_fixture_ids)}`, "
                f"warnings=`{list(v.warnings)}`, altera_p_model=`{v.authorized_for_probabilistic_adjustment}`"
            )
        return "\n".join(lines) + "\n"

    def complete_manual_exchange(
        self,
        output_dir: Path,
        bundle: MatchObservationBundle,
        engine_result: AnalyticsEngineResult,
        hypotheses_json_path: Path,
    ) -> dict[str, Any]:
        """Ejecuta el intercambio manual completo:
        1) Dossier JSON + Prompt MD
        2) Lectura de hipótesis tácticas propuestas en JSON
        3) Comprobación determinista en Python con extracción de contraejemplos
        4) Reporte final Markdown + JSON verificado + persistencia en HypothesisRegistry.
        """
        out = Path(output_dir)
        handoff = self.write_manual_handoff(out, bundle, engine_result)
        raw_doc = json.loads(Path(hypotheses_json_path).read_text(encoding="utf-8"))
        hyps = raw_doc.get("hypotheses") if isinstance(raw_doc, dict) else raw_doc
        if not isinstance(hyps, list):
            raise EpistemicContractError("MANUAL_HYPOTHESES_FILE_MUST_CONTAIN_LIST")

        verified = verify_hypotheses(bundle, hyps, registry=self.registry)
        dossier = json.loads(handoff["dossier_json"].read_text(encoding="utf-8"))
        report_md = self.render_final_verification_report(dossier, verified)

        verified_json_path = out / f"verified_hypotheses_{bundle.event_id}.json"
        final_report_path = out / f"FINAL_TACTICAL_REPORT_{bundle.event_id}.md"
        verified_json_path.write_text(
            json.dumps([v.to_dict() for v in verified], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        final_report_path.write_text(report_md, encoding="utf-8")
        return {
            **handoff,
            "verified_json": verified_json_path,
            "final_report_md": final_report_path,
            "verifications": verified,
        }

    def run_automatic_cli(
        self,
        bundle: MatchObservationBundle,
        engine_result: AnalyticsEngineResult,
    ) -> list[VerifiedHypothesisRecord]:
        """Ejecuta el adaptador CLI opcional (si fue configurado) y verifica en Python."""
        if self.cli_port is None:
            raise EpistemicContractError("CLI_ADAPTER_NOT_CONFIGURED: usando modo manual por defecto")
        dossier = self.build_dossier(bundle, engine_result)
        proposed = self.cli_port.propose_hypotheses(dossier)
        return verify_hypotheses(bundle, proposed, registry=self.registry)


class SubprocessCLILlmAdapter:
    """Adaptador CLI opcional con timeout, reintentos y validación JSON estricta."""

    def __init__(self, command: list[str], *, timeout_seconds: int = 45, max_retries: int = 1) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def propose_hypotheses(self, dossier: dict[str, Any]) -> list[dict[str, Any]]:
        payload = json.dumps(dossier, ensure_ascii=False)
        last_err = ""
        for _ in range(self.max_retries + 1):
            try:
                proc = subprocess.run(
                    self.command,
                    input=payload,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=True,
                )
                parsed = json.loads(proc.stdout)
                hyps = parsed.get("hypotheses") if isinstance(parsed, dict) else parsed
                if not isinstance(hyps, list):
                    raise ValueError("CLI_OUTPUT_HYPOTHESES_NOT_LIST")
                return hyps
            except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
                last_err = str(exc)
        raise EpistemicContractError(f"CLI_INVESTIGATOR_FAILED: {last_err}")
