"""EARLY_TACTICAL_SCOUT (V1 / Fase 1 — EXPERIMENTAL_SHADOW).

Construye un expediente futbolístico independiente por partido antes de interpretar
cuotas Bwin o rankings cuantitativos del motor. Python se encarga exclusivamente de
validación de identidad, control temporal PREMATCH, normalización de cachés,
detección de contradicciones y trazabilidad; el razonamiento táctico profundo
corresponde al analista de IA (Codex / LLM externo).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from bet_engine.context.research_parser import parse_research_identity_and_metadata

EARLY_SCOUT_INPUT_SCHEMA = "1.0-early-tactical-scout-input"
EARLY_SCOUT_OUTPUT_SCHEMA = "1.0-early-tactical-scout-output"

VALID_EVIDENCE_STATES = frozenset({
    "CONFIRMADO",
    "PROBABLE",
    "INFERIDO",
    "NO_VERIFICADO",
    "CONTRADICTORIO",
})

REQUIRED_HYPOTHESIS_FIELDS = (
    "tactical_mechanism",
    "team_and_players",
    "necessary_conditions",
    "supporting_stats",
    "contradictory_evidence",
    "opponent_coach_response",
    "invalidation_scenarios",
    "related_market_family",
    "evidence_quality",
    "pending_info",
)


class EarlyScoutError(RuntimeError):
    """Error explícito de validación epistémica o temporal en EARLY_TACTICAL_SCOUT."""


class EarlyScoutAnalystPort(Protocol):
    """Puerto agnóstico al proveedor para obtener el razonamiento táctico de IA."""

    def analyze(self, scout_input: dict[str, Any], prompt_markdown: str) -> dict[str, Any]: ...


def _norm_text(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9]+", " ", stripped).strip().lower())


def _parse_iso_utc(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", text)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group().replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _team_tokens(name: str) -> set[str]:
    stop = {"fc", "cf", "ec", "sc", "ac", "cd", "club", "de", "la", "el", "the", "w"}
    return {t for t in _norm_text(name).split() if len(t) >= 3 and t not in stop}


def _teams_match_in_text(raw_text: str, home: str, away: str) -> bool:
    head = _norm_text(raw_text[:3000])
    h_toks = _team_tokens(home)
    a_toks = _team_tokens(away)
    if not h_toks or not a_toks:
        return False
    return any(t in head for t in h_toks) and any(t in head for t in a_toks)


def _extract_player_status_mentions(raw_text: str, *, source_name: str, report_date: str | None) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Extrae menciones de disponibilidad por jugador (solo columna de estado) y contradicciones explícitas."""
    mentions: dict[str, list[dict[str, Any]]] = {}
    explicit_contradictions: list[dict[str, Any]] = []
    out_kw = re.compile(r"\b(baja|lesion|lesionado|suspendid[oa]|fuera|ausencia|impedido|descartad[oa]|cirugia|rotura)\b", re.I)
    in_kw = re.compile(r"\b(probable|titular|disponible|regresa|vuelve|incluido|habilitado|relacionado)\b", re.I)

    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        # Table row: | Jugador | Estado publicado | Rol/Sustitucion | ...
        if s.startswith("|"):
            cells = [re.sub(r"[`*]", "", c).strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and not re.fullmatch(r"[-:\s]+", cells[0]):
                cand_name = _norm_text(cells[0])
                if 3 <= len(cand_name) <= 35 and cand_name not in {"jugador", "n", "no", "pos", "estado", "equipo"}:
                    # Only inspect status cell (cells[1]) so replacement notes in cells[2] do not cause false positives
                    status_cell = cells[1]
                    is_out = bool(out_kw.search(status_cell))
                    is_in = bool(in_kw.search(status_cell))
                    if is_out and not is_in:
                        mentions.setdefault(cand_name, []).append({
                            "state": "OUT",
                            "source": source_name,
                            "claim": status_cell[:180],
                            "date": report_date,
                        })
                    elif is_in and not is_out:
                        mentions.setdefault(cand_name, []).append({
                            "state": "AVAILABLE",
                            "source": source_name,
                            "claim": status_cell[:180],
                            "date": report_date,
                        })
        # Explicit contradiction section: "#### Contradicción relevante: Canobbio"
        m_contra = re.match(r"^#{2,5}\s+Contradicci[oó]n[^:]*:\s*([A-ZÁÉÍÓÚÑ][a-zA-ZÁÉÍÓÚÑáéíóúñ\s-]{2,30})", s, re.I)
        if m_contra:
            p = _norm_text(m_contra.group(1))
            if p:
                block_lines = []
                for nxt in lines[idx + 1 : idx + 8]:
                    if nxt.strip().startswith("#"):
                        break
                    if nxt.strip():
                        block_lines.append(nxt.strip())
                explicit_contradictions.append({
                    "player": p,
                    "status": "CONTRADICTORIO",
                    "source_a": f"{source_name} (fuentes primarias citadas: bloque A)",
                    "claim_a": block_lines[0][:200] if len(block_lines) >= 1 else "Reportado como baja/fuera",
                    "date_a": report_date,
                    "source_b": f"{source_name} (fuentes alternativas citadas: bloque B)",
                    "claim_b": block_lines[1][:200] if len(block_lines) >= 2 else "Reportado como disponible/probable",
                    "date_b": report_date,
                    "resolution_status": "PENDIENTE_DE_RESOLUCION",
                })
    return mentions, explicit_contradictions


def evaluate_external_reports(
    reports: list[dict[str, Any]],
    *,
    fixture_id: int,
    event_id: str | None,
    home: str,
    away: str,
    kickoff_utc: datetime | None,
) -> dict[str, Any]:
    """Valida identidad, temporalidad, XI probable/oficial y contradicciones en informes externos."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    warnings: list[str] = []
    player_claims: dict[str, list[dict[str, Any]]] = {}
    explicit_contras: list[dict[str, Any]] = []

    for rep in reports:
        source_name = str(rep.get("source") or "external_ai")
        path = rep.get("path")
        raw_text = rep.get("raw_text")
        if raw_text is None and path:
            raw_text = Path(path).read_text(encoding="utf-8", errors="replace")
        raw_text = str(raw_text or "")
        if not raw_text.strip():
            rejected.append({"source": source_name, "path": str(path) if path else None, "reason": "EMPTY_REPORT"})
            warnings.append(f"EXTERNAL_REPORT_EMPTY:{source_name}")
            continue

        parsed = parse_research_identity_and_metadata(raw_text, home=home, away=away)
        ev_found = parsed["event_ids_found"]
        fix_found = parsed["fixture_ids_found"]

        # 1. Identity verification
        id_mismatch = False
        if parsed["fixture_id_conflict"] or parsed["event_id_conflict"]:
            id_mismatch = True
        if fix_found and str(fixture_id) not in fix_found:
            id_mismatch = True
        if event_id and ev_found and str(event_id) not in ev_found:
            id_mismatch = True
        if not fix_found and not ev_found and not _teams_match_in_text(raw_text, home, away):
            id_mismatch = True

        if id_mismatch:
            rejected.append({
                "source": source_name,
                "path": str(path) if path else None,
                "reason": "IDENTITY_MISMATCH",
                "found_fixture_ids": fix_found,
                "found_event_ids": ev_found,
            })
            warnings.append(f"IDENTITY_MISMATCH:{source_name}")
            continue

        # 2. Temporal verification (cutoff / generated timestamp & post-kickoff check)
        cutoff_dt = parsed.get("cutoff_utc")
        gen_dt = parsed.get("generated_utc")
        declared_ts = gen_dt or cutoff_dt
        rep_warnings: list[str] = []

        if declared_ts is None:
            rep_warnings.append("MISSING_VERIFIABLE_TIMESTAMP")
            warnings.append(f"MISSING_VERIFIABLE_TIMESTAMP:{source_name}")
        elif kickoff_utc is not None and declared_ts > kickoff_utc:
            rejected.append({
                "source": source_name,
                "path": str(path) if path else None,
                "reason": "POST_KICKOFF_CONTAMINATION",
                "declared_timestamp_utc": declared_ts.isoformat(),
                "kickoff_utc": kickoff_utc.isoformat(),
            })
            warnings.append(f"POST_KICKOFF_CONTAMINATION:{source_name}")
            continue

        # 3. Lineup certainty guard (never promote PROBABLE to CONFIRMADO)
        lineup_summary: dict[str, Any] = {}
        for side_key, side_data in (parsed.get("parallel_xi") or {}).items():
            raw_cls = str(side_data.get("classification") or "PROBABLE")
            epistemic_cls = "PROBABLE" if "PROBABLE" in raw_cls else "NO_VERIFICADO"
            lineup_summary[side_key] = {
                "players": side_data.get("players", []),
                "raw_classification": raw_cls,
                "epistemic_status": epistemic_cls,
            }

        # 4. Track player availability mentions for cross-source contradiction detection
        rep_date_iso = declared_ts.isoformat() if declared_ts else None
        p_mentions, exp_c = _extract_player_status_mentions(
            raw_text,
            source_name=source_name,
            report_date=rep_date_iso,
        )
        for pname, claims in p_mentions.items():
            player_claims.setdefault(pname, []).extend(claims)
        explicit_contras.extend(exp_c)

        accepted.append({
            "source": source_name,
            "path": str(path) if path else None,
            "sha256": _sha256_text(raw_text),
            "identity_binding": "EXACT_ID" if (fix_found or ev_found) else "PROBABLE_NAME_MATCH",
            "cutoff_utc": cutoff_dt.isoformat() if cutoff_dt else None,
            "generated_utc": gen_dt.isoformat() if gen_dt else None,
            "timestamp_status": "VERIFIED_PREMATCH" if declared_ts is not None else "NO_VERIFICADO",
            "has_source_registry": bool(parsed.get("has_source_registry")),
            "lineups": lineup_summary,
            "warnings": rep_warnings,
            "raw_excerpt": raw_text[:16000],
            "raw_char_count": len(raw_text),
        })

    contradictions: list[dict[str, Any]] = []
    seen_players: set[str] = set()
    for ec in explicit_contras:
        if ec["player"] not in seen_players:
            seen_players.add(ec["player"])
            contradictions.append(ec)

    for pname, claims in sorted(player_claims.items()):
        if pname in seen_players:
            continue
        out_claims = [c for c in claims if c["state"] == "OUT"]
        avail_claims = [c for c in claims if c["state"] == "AVAILABLE"]
        if not out_claims or not avail_claims:
            continue
        # Only flag if incompatible claims come from distinct sources
        ca = out_claims[0]
        cb = next((c for c in avail_claims if c["source"] != ca["source"]), None)
        if cb is None:
            continue
        seen_players.add(pname)
        contradictions.append({
            "player": pname,
            "status": "CONTRADICTORIO",
            "source_a": ca["source"],
            "claim_a": ca["claim"],
            "date_a": ca["date"],
            "source_b": cb["source"],
            "claim_b": cb["claim"],
            "date_b": cb["date"],
            "resolution_status": "PENDIENTE_DE_RESOLUCION",
        })

    return {
        "accepted": accepted,
        "rejected": rejected,
        "warnings": sorted(set(warnings)),
        "contradictions": contradictions,
    }


def _extract_historical_players_guard(
    pre_match: dict[str, Any] | None,
    accepted_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Separa jugadores vistos solo en historiales de aquellos acreditados en la plantilla actual."""
    current_mentioned: set[str] = set()
    for rep in accepted_reports:
        for side_data in (rep.get("lineups") or {}).values():
            for p in side_data.get("players", []):
                current_mentioned.add(_norm_text(p))
        p_mentions, _ = _extract_player_status_mentions(
            rep.get("raw_excerpt", ""),
            source_name=rep.get("source", "external"),
            report_date=rep.get("cutoff_utc") or rep.get("generated_utc"),
        )
        for pname in p_mentions:
            current_mentioned.add(pname)

    historical_only: list[str] = []
    if pre_match:
        for side_key in ("home_evidence", "away_evidence"):
            ev = pre_match.get(side_key) or {}
            for p in ev.get("historical_players", []) or []:
                pn = _norm_text(p.get("name") if isinstance(p, dict) else p)
                if pn and pn not in current_mentioned and pn not in historical_only:
                    historical_only.append(pn)

    return {
        "historical_unaccredited_players": historical_only,
        "rule": "Un jugador presente solo en XI históricos no puede tratarse como integrante confirmado de la plantilla actual.",
        "status": "NO_VERIFICADO_PLANTILLA_ACTUAL",
    }


def _compact_stat_block(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    n = raw.get("n")
    if n is None:
        return None
    out: dict[str, Any] = {
        "n": n,
        "mean": raw.get("mean"),
        "stdev": raw.get("stdev"),
        "cv": raw.get("cv"),
    }
    if raw.get("min") is not None:
        out["min"] = raw.get("min")
    if raw.get("max") is not None:
        out["max"] = raw.get("max")
    if raw.get("trend_relative") is not None:
        out["trend_relative"] = round(float(raw["trend_relative"]), 4)
    if isinstance(raw.get("venue"), dict):
        v = raw["venue"]
        out["venue"] = {
            "filter": v.get("venue_filter"),
            "n": v.get("n"),
            "mean": v.get("mean"),
            "stdev": v.get("stdev"),
            "cv": v.get("cv"),
        }
    if isinstance(raw.get("recent"), dict):
        out["recent"] = {"n": raw["recent"].get("n"), "mean": raw["recent"].get("mean")}
    if isinstance(raw.get("previous"), dict):
        out["previous"] = {"n": raw["previous"].get("n"), "mean": raw["previous"].get("mean")}
    return out


def _sanitize_stats_without_odds(
    pre_match: dict[str, Any] | None,
    tactical_fx: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extrae y unifica estadística descriptiva de preselection y tactical_signal_engine sin cuotas ni EV."""
    metrics_order = ("corners", "shots", "on_target", "fouls", "yellow", "goals", "xg", "offsides")
    metric_aliases = {"on_target shots": "on_target", "yellow cards": "yellow"}

    # 1. Extract from tactical_signal_engine.json (signals[]), stripping all market/EV/priority fields
    tactical_series: dict[str, dict[str, dict[str, Any]]] = {"home": {}, "away": {}}
    if tactical_fx and isinstance(tactical_fx.get("signals"), list):
        for sig in tactical_fx["signals"]:
            if not isinstance(sig, dict):
                continue
            side = str(sig.get("team_side") or "").lower()
            if side not in ("home", "away"):
                continue
            raw_metric = str(sig.get("metric") or "").lower().strip()
            metric = metric_aliases.get(raw_metric, raw_metric.replace(" ", "_"))
            own_blk = _compact_stat_block(sig.get("own"))
            opp_blk = _compact_stat_block(sig.get("opponent_conceded"))
            if own_blk or opp_blk:
                tactical_series[side][metric] = {
                    "own": own_blk,
                    "opponent_conceded": opp_blk,
                    "source": "tactical_signal_engine.json",
                }

    # 2. Extract from preselection.json
    pre_series: dict[str, dict[str, dict[str, Any]]] = {"home": {}, "away": {}}
    standings_ctx: dict[str, Any] = {}
    history_meta: dict[str, Any] = {}
    if pre_match:
        for side, ev_key in (("home", "home_evidence"), ("away", "away_evidence")):
            ev = pre_match.get(ev_key) or {}
            hist = ev.get("history") or {}
            det = ev.get("details") or {}
            standings_ctx[side] = ev.get("standings")
            history_meta[side] = {
                "completed_total": hist.get("completed_total"),
                "same_competition_total": hist.get("same_competition_total"),
                "home_games": hist.get("home_games"),
                "away_games": hist.get("away_games"),
                "days_since_last_match": hist.get("days_since_last_match"),
                "fixture_details_available_in_preselection": det.get("fixture_details_available"),
            }
            if hist.get("goals_for") or hist.get("goals_against"):
                pre_series[side]["goals"] = {
                    "own": _compact_stat_block(hist.get("goals_for")),
                    "opponent_conceded": _compact_stat_block(hist.get("goals_against")),
                    "same_comp_own": _compact_stat_block(hist.get("same_competition_goals_for")),
                    "same_comp_conceded": _compact_stat_block(hist.get("same_competition_goals_against")),
                    "source": "preselection.json",
                }
            own_det = det.get("own") or {}
            con_det = det.get("conceded") or {}
            for m in metrics_order:
                if m == "goals":
                    continue
                ob = _compact_stat_block(own_det.get(m))
                cb = _compact_stat_block(con_det.get(m))
                if (ob and (ob.get("n") or 0) > 0) or (cb and (cb.get("n") or 0) > 0):
                    pre_series[side][m] = {
                        "own": ob,
                        "opponent_conceded": cb,
                        "source": "preselection.json",
                    }

    # 3. Merge with explicit per-metric provenance
    merged_metrics: dict[str, dict[str, Any]] = {"home": {}, "away": {}}
    provenance_summary: dict[str, dict[str, str]] = {"home": {}, "away": {}}
    any_detailed_stats = False
    for side in ("home", "away"):
        for m in metrics_order:
            t_entry = tactical_series[side].get(m)
            p_entry = pre_series[side].get(m)
            t_n = ((t_entry or {}).get("own") or {}).get("n") or 0
            p_n = ((p_entry or {}).get("own") or {}).get("n") or 0
            if t_n > 0:
                merged_metrics[side][m] = t_entry
                if p_n == 0:
                    provenance_summary[side][m] = f"AVAILABLE_IN_TACTICAL_CACHE (n={t_n}; preselection n=0)"
                else:
                    provenance_summary[side][m] = f"AVAILABLE_IN_BOTH (tactical n={t_n}, preselection n={p_n})"
                if m in ("corners", "shots", "on_target", "fouls", "yellow"):
                    any_detailed_stats = True
            elif p_n > 0:
                merged_metrics[side][m] = p_entry
                provenance_summary[side][m] = f"AVAILABLE_IN_PRESELECTION (n={p_n})"
                if m in ("corners", "shots", "on_target", "fouls", "yellow"):
                    any_detailed_stats = True
            else:
                provenance_summary[side][m] = "ABSENT_IN_ALL_CACHES (n=0)"

    exp_ev = tactical_fx.get("expanded_statistical_evidence") if tactical_fx else None
    player_status_note = tactical_fx.get("player_status") if tactical_fx else None

    return {
        "has_preselection": pre_match is not None,
        "has_tactical_evidence": bool(tactical_fx and (any_detailed_stats or (exp_ev and exp_ev.get("status") != "NOT_AVAILABLE_FOR_FIXTURE"))),
        "any_detailed_stats_available": any_detailed_stats,
        "history_meta": history_meta,
        "standings": standings_ctx,
        "metrics_by_side": merged_metrics,
        "metric_provenance": provenance_summary,
        "player_status_summary": player_status_note,
        "h2h": pre_match.get("h2h") if pre_match else None,
        "competitive_motivation": pre_match.get("competitive_motivation") if pre_match else None,
        "expanded_statistical_evidence": exp_ev,
    }


def build_early_scout_prompt(scout_input: dict[str, Any]) -> str:
    """Genera el prompt estructurado AI-FIRST para Codex sin truncamiento ciego ni cuotas."""
    ident = scout_input["identity"]
    cov = scout_input["coverage"]
    ext = scout_input["external_reports"]
    stats = scout_input["statistical_context"]

    lines = [
        f"# EARLY TACTICAL SCOUT — {ident['home']} vs {ident['away']}",
        "",
        "## Instrucciones Epistémicas Obligatorias",
        "- Modo: `EXPERIMENTAL_SHADOW` (Fase 1 AI-FIRST). **NO uses cuotas, EV, `p_model`, stakes ni recomendaciones BET/NO BET.**",
        "- Etiqueta cada afirmación relevante con: `CONFIRMADO`, `PROBABLE`, `INFERIDO`, `NO_VERIFICADO` o `CONTRADICTORIO`.",
        "- Un XI probable NUNCA debe presentarse como confirmado.",
        "- Un jugador visto en un XI histórico sin acreditación actual debe marcarse `NO_VERIFICADO` respecto a su pertenencia actual.",
        "- Las estadísticas sirven para contrastar mecanismos futbolísticos, no para inventar explicaciones.",
        "",
        "## Identidad y Control Temporal",
        f"- `fixture_id`: `{ident['fixture_id']}`",
        f"- `event_id`: `{ident.get('event_id')}`",
        f"- Competición: `{ident.get('country')} / {ident.get('league')}`",
        f"- Kickoff UTC: `{ident.get('kickoff_utc')}` | Madrid: `{ident.get('kickoff_madrid')}`",
        f"- Cobertura documental: `{cov['coverage_level']}` (faltantes: `{', '.join(cov['missing_sources']) or 'ninguna'}`)",
        f"- Advertencias epistémicas: `{', '.join(cov['warnings']) or 'ninguna'}`",
        "",
        "## Contradicciones Detectadas Automáticamente",
    ]
    if ext["contradictions"]:
        for c in ext["contradictions"]:
            lines.append(
                f"- `{c['player']}` [`{c['status']}` | `{c['resolution_status']}`]: "
                f"{c['source_a']} ({c.get('date_a')}) -> '{c['claim_a']}' vs "
                f"{c['source_b']} ({c.get('date_b')}) -> '{c['claim_b']}'"
            )
    else:
        lines.append("- Ninguna contradicción cruzada detectada automáticamente.")

    lines += [
        "",
        "## Informes Cualitativos Externos Aceptados",
    ]
    if ext["accepted"]:
        for rep in ext["accepted"]:
            lines.append(f"### Fuente: {rep['source']} (`{rep['timestamp_status']}`, `{rep['identity_binding']}`)")
            lines.append(rep["raw_excerpt"])
            lines.append("")
    else:
        lines.append("- Sin informe cualitativo externo válido. Analizar únicamente con evidencia estadística disponible y declarar cobertura reducida.")

    lines += [
        "",
        "## Contexto Estadístico Priorizado y Trazable (Sin Cuotas)",
        f"- **Meta histórico**: `{json.dumps(stats.get('history_meta') or {}, ensure_ascii=False)}`",
        f"- **Tabla / Standings**: `{json.dumps(stats.get('standings') or {}, ensure_ascii=False)}`",
        f"- **Estado jugadores/minutos en caché**: `{stats.get('player_status_summary') or 'NO_VERIFICADO'}`",
        "",
        "### Trazabilidad de Fuentes por Métrica",
        "```json",
        json.dumps(stats.get("metric_provenance") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "### Series Descriptivas (Córners, Remates, Tiros a Puerta, Faltas, Tarjetas, Goles, xG)",
        "```json",
        json.dumps(stats.get("metrics_by_side") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Formato JSON Esperado de Respuesta",
        "Devuelve un objeto JSON con las claves: `coaches_and_systems`, `players`, `tactical_matchup`, `competitive_context`, `hypotheses` (lista de 0..N hipótesis, cada una con: `tactical_mechanism`, `team_and_players`, `necessary_conditions`, `supporting_stats`, `contradictory_evidence`, `opponent_coach_response`, `invalidation_scenarios`, `related_market_family`, `evidence_quality`, `pending_info`).",
    ]
    return "\n".join(lines)


def prepare_early_scout_package(
    *,
    identity: dict[str, Any],
    output_dir: Path,
    pre_match: dict[str, Any] | None = None,
    tactical_fx: dict[str, Any] | None = None,
    external_reports: list[dict[str, Any]] | None = None,
    asof_utc: datetime | None = None,
) -> dict[str, Any]:
    """Prepara el paquete de entrada validado y el prompt AI-FIRST aislado por partido."""
    fid = int(identity["fixture_id"])
    eid = str(identity["event_id"]) if identity.get("event_id") else None
    dossier_id = eid or f"research_fixture_{fid}"
    home = str(identity["home"])
    away = str(identity["away"])
    ko_utc = _parse_iso_utc(identity.get("kickoff_utc") or identity.get("kickoff_madrid"))
    now_utc = asof_utc or datetime.now(timezone.utc)

    if ko_utc is not None and now_utc > ko_utc:
        # En auditoría histórica se permite si el caller fija asof <= kickoff; si no, se marca advertencia explícita.
        temporal_mode = "RETROSPECTIVE_AUDIT_WARNING_ASOF_AFTER_KICKOFF"
    else:
        temporal_mode = "PREMATCH_VERIFIED"

    ext_eval = evaluate_external_reports(
        external_reports or [],
        fixture_id=fid,
        event_id=eid,
        home=home,
        away=away,
        kickoff_utc=ko_utc,
    )
    stats_ctx = _sanitize_stats_without_odds(pre_match, tactical_fx)
    hist_guard = _extract_historical_players_guard(pre_match, ext_eval["accepted"])

    missing_sources: list[str] = []
    if not ext_eval["accepted"]:
        missing_sources.append("external_qualitative_report")
    if not stats_ctx["has_preselection"]:
        missing_sources.append("api_football_preselection")
    if not stats_ctx["has_tactical_evidence"]:
        missing_sources.append("tactical_statistical_evidence")

    if ext_eval["accepted"] and stats_ctx["has_preselection"] and not ext_eval["warnings"]:
        coverage_level = "HIGH_PREMATCH_WITH_EXTERNAL"
    elif stats_ctx["has_preselection"] and not ext_eval["accepted"]:
        coverage_level = "PARTIAL_STATS_ONLY_NO_EXTERNAL_REPORT"
    elif ext_eval["accepted"] and not stats_ctx["has_preselection"]:
        coverage_level = "PARTIAL_EXTERNAL_ONLY_NO_STATS"
    elif ext_eval["accepted"] or stats_ctx["has_preselection"]:
        coverage_level = "PARTIAL_WITH_WARNINGS"
    else:
        coverage_level = "MINIMAL_INSUFFICIENT_DATA"

    warnings = list(ext_eval["warnings"])
    if temporal_mode != "PREMATCH_VERIFIED":
        warnings.append(temporal_mode)
    if not ext_eval["accepted"]:
        warnings.append("NO_EXTERNAL_QUALITATIVE_REPORT")

    scout_input = {
        "schema_version": EARLY_SCOUT_INPUT_SCHEMA,
        "mode": "EXPERIMENTAL_SHADOW",
        "generated_at_utc": now_utc.isoformat(),
        "temporal_mode": temporal_mode,
        "identity": {
            "fixture_id": fid,
            "event_id": eid,
            "dossier_id": dossier_id,
            "home": home,
            "away": away,
            "country": identity.get("country"),
            "league": identity.get("league"),
            "kickoff_utc": ko_utc.isoformat() if ko_utc else identity.get("kickoff_utc"),
            "kickoff_madrid": identity.get("kickoff_madrid"),
        },
        "odds_dependency": False,
        "coverage": {
            "coverage_level": coverage_level,
            "missing_sources": missing_sources,
            "warnings": sorted(set(warnings)),
        },
        "external_reports": ext_eval,
        "historical_player_guard": hist_guard,
        "statistical_context": stats_ctx,
    }

    prompt_md = build_early_scout_prompt(scout_input)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / f"early_scout_input_{dossier_id}.json"
    prompt_path = output_dir / f"early_scout_prompt_{dossier_id}.md"
    input_path.write_text(json.dumps(scout_input, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_path.write_text(prompt_md, encoding="utf-8")

    return {
        "scout_input": scout_input,
        "prompt_markdown": prompt_md,
        "input_json_path": str(input_path),
        "prompt_md_path": str(prompt_path),
    }


def _validate_ai_analysis(analysis: dict[str, Any]) -> None:
    if not isinstance(analysis, dict):
        raise EarlyScoutError("AI_ANALYSIS_MUST_BE_JSON_OBJECT")
    forbidden = [k for k in ("p_model", "calibrated_p_model", "ev", "stake", "recommended_bet") if analysis.get(k) is not None]
    if forbidden or analysis.get("actionable_bet") is True:
        raise EarlyScoutError(f"PHASE1_BOUNDARY_VIOLATION_PROBABILITIES_OR_BET_FORBIDDEN:{forbidden}")

    hypotheses = analysis.get("hypotheses")
    if hypotheses is None:
        return
    if not isinstance(hypotheses, list):
        raise EarlyScoutError("HYPOTHESES_MUST_BE_LIST")
    for idx, h in enumerate(hypotheses):
        if not isinstance(h, dict):
            raise EarlyScoutError(f"HYPOTHESIS_{idx}_NOT_OBJECT")
        for req in REQUIRED_HYPOTHESIS_FIELDS:
            if req not in h:
                raise EarlyScoutError(f"HYPOTHESIS_{idx}_MISSING_FIELD:{req}")
        if h.get("p_model") is not None or h.get("ev") is not None or h.get("stake") is not None:
            raise EarlyScoutError(f"HYPOTHESIS_{idx}_FORBIDDEN_ODDS_OR_EV_FIELD")
        eq = str(h.get("evidence_quality") or "").upper()
        if eq not in VALID_EVIDENCE_STATES:
            raise EarlyScoutError(f"HYPOTHESIS_{idx}_INVALID_EVIDENCE_QUALITY:{eq}")


def render_early_scout_markdown(report: dict[str, Any]) -> str:
    """Renderiza el expediente humano legible en Markdown sin cuotas ni recomendaciones."""
    ident = report["identity"]
    cov = report["coverage"]
    ana = report["analysis"]
    lines = [
        f"# EARLY TACTICAL SCOUT — {ident['home']} vs {ident['away']}",
        "",
        f"- **Modo**: `{report['mode']}` (Sin dependencia de cuotas Bwin)",
        f"- **Identidad**: `fixture_id={ident['fixture_id']}` | `event_id={ident.get('event_id')}`",
        f"- **Kickoff UTC**: `{ident.get('kickoff_utc')}` | **Madrid**: `{ident.get('kickoff_madrid')}`",
        f"- **Cobertura**: `{cov['coverage_level']}` | **Faltantes**: `{', '.join(cov['missing_sources']) or 'ninguna'}`",
        f"- **Advertencias**: `{', '.join(cov['warnings']) or 'ninguna'}`",
        "",
        "## A. Entrenadores y Sistemas",
        json.dumps(ana.get("coaches_and_systems") or {}, ensure_ascii=False, indent=2),
        "",
        "## B. Jugadores y Disponibilidad",
        json.dumps(ana.get("players") or {}, ensure_ascii=False, indent=2),
        "",
        "## C. Enfrentamiento Táctico y Contraescenarios",
        json.dumps(ana.get("tactical_matchup") or {}, ensure_ascii=False, indent=2),
        "",
        "## D. Contexto Competitivo",
        json.dumps(ana.get("competitive_context") or {}, ensure_ascii=False, indent=2),
        "",
        "## E. Hipótesis Futbolísticas Independientes",
    ]
    hyps = ana.get("hypotheses") or []
    if not hyps:
        lines.append("- Sin hipótesis formuladas.")
    for i, h in enumerate(hyps, start=1):
        lines += [
            f"### Hipótesis {i}: {h.get('related_market_family')} [`{h.get('evidence_quality')}`]",
            f"- **Mecanismo táctico**: {h.get('tactical_mechanism')}",
            f"- **Equipo y jugadores**: {h.get('team_and_players')}",
            f"- **Condiciones necesarias**: {h.get('necessary_conditions')}",
            f"- **Estadísticas de apoyo**: {h.get('supporting_stats')}",
            f"- **Evidencia contradictoria**: {h.get('contradictory_evidence')}",
            f"- **Respuesta rival plausible**: {h.get('opponent_coach_response')}",
            f"- **Escenarios de invalidación**: {h.get('invalidation_scenarios')}",
            f"- **Información pendiente**: {h.get('pending_info')}",
            "",
        ]
    return "\n".join(lines)


def finalize_early_scout(
    *,
    scout_input: dict[str, Any],
    analysis: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Valida el análisis de IA y persiste el expediente final JSON + Markdown aislado."""
    _validate_ai_analysis(analysis)
    dossier_id = scout_input["identity"]["dossier_id"]

    report = {
        "schema_version": EARLY_SCOUT_OUTPUT_SCHEMA,
        "mode": "EXPERIMENTAL_SHADOW",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "identity": scout_input["identity"],
        "odds_dependency": False,
        "coverage": scout_input["coverage"],
        "contradictions": scout_input["external_reports"]["contradictions"],
        "historical_player_guard": scout_input["historical_player_guard"],
        "analysis": analysis,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"early_scout_{dossier_id}.json"
    md_path = output_dir / f"early_scout_{dossier_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_early_scout_markdown(report), encoding="utf-8")

    return {
        "report": report,
        "report_json_path": str(json_path),
        "report_md_path": str(md_path),
    }


def run_early_scout_with_analyst(
    *,
    identity: dict[str, Any],
    output_dir: Path,
    analyst: EarlyScoutAnalystPort,
    pre_match: dict[str, Any] | None = None,
    tactical_fx: dict[str, Any] | None = None,
    external_reports: list[dict[str, Any]] | None = None,
    asof_utc: datetime | None = None,
) -> dict[str, Any]:
    """Ejecuta el flujo completo Stage A (preparación) -> IA -> Stage B (validación y guardado)."""
    prep = prepare_early_scout_package(
        identity=identity,
        output_dir=output_dir,
        pre_match=pre_match,
        tactical_fx=tactical_fx,
        external_reports=external_reports,
        asof_utc=asof_utc,
    )
    analysis = analyst.analyze(prep["scout_input"], prep["prompt_markdown"])
    fin = finalize_early_scout(
        scout_input=prep["scout_input"],
        analysis=analysis,
        output_dir=output_dir,
    )
    return {**prep, **fin}
