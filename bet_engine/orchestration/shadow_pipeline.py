"""Orquestador en SHADOW MODE (Etapa 5.1): conecta ObservationStore -> PatternRegistry ->
TacticalInvestigator -> CoherentMatchModeler -> BwinMarketEvaluator en paralelo al flujo
operativo actual, con recuperación ante fallos y prohibición de datos live.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.constants import DEEP_HISTORY_DETAIL_LIMIT, DEEP_HISTORY_LIMIT, MICRO_HISTORY_LIMIT
from bet_engine.acquisition import CoveragePolicyEvaluator
from bet_engine.analytics import (
    AnalyticsEngineResult,
    HypothesisRegistry,
    TacticalInvestigator,
     VerifiedHypothesisRecord,
    build_stage3_registry,
    verify_hypotheses,
)
from bet_engine.context import MatchObservationBundle, ObservationStore
from bet_engine.context.observation_store import reconcile_bundle_coach_regime
from bet_engine.contracts import EpistemicContractError, TemporalLeakageError
from bet_engine.contracts.epistemic import parse_utc_timestamp
from bet_engine.markets import (
    FORMAL_DECISION_CLASSIFICATIONS,
    BwinMarketEvaluator,
    EvaluatedMarketOption,
    audit_snapshot_equivalences,
)
from bet_engine.modeling import CoherentMatchModeler, MatchProbabilisticModel
from bet_engine.modeling.distributions import MODEL_VERSION as MODEL_PROBABILISTIC_VERSION


LIVE_OR_POST_STATUSES = frozenset({"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "FT", "AET", "PEN"})
_OU_SELECTION_RE = re.compile(r"^(m[aá]s|menos)\s+de\s+(\d+(?:[.,]\d+)?)$", re.I)


def _norm_team_tokens(text: str) -> str:
    import unicodedata
    raw = unicodedata.normalize("NFKD", str(text or ""))
    clean = "".join(c for c in raw if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", clean).split())


def _is_female_label(text: str) -> bool:
    return bool(re.search(r"(?i)(?:\b(?:w|women|f|femenina|femenino|frauen)\b|\(f\))", str(text or "")))


def _match_team_key(text: str, keys: set[str]) -> bool:
    t = _norm_team_tokens(text)
    if not t:
        return False
    t_fem = _is_female_label(text)
    cat_stop = {"f", "w", "women", "femenina", "femenino", "frauen", "fc", "cf", "sc", "ac"}
    t_tokens = t.split()
    t_core = [tok for tok in t_tokens if tok not in cat_stop]
    for k in keys:
        kn = _norm_team_tokens(k)
        if not kn:
            continue
        k_fem = _is_female_label(k)
        if t_fem != k_fem:
            continue
        if kn == t:
            return True
        if kn in t and re.search(r"(?<!\w)" + re.escape(kn) + r"(?!\w)", t):
            return True
        k_core = [tok for tok in kn.split() if tok not in cat_stop]
        if t_core and k_core and t_core == k_core:
            return True
    return False


def _expand_team_keys(primary: str | None, aliases: tuple[str, ...], *, gender: str | None = None) -> set[str]:
    from bwin_discovery import FEMALE_RE, team_candidate_queries

    raw_names = [x for x in ((primary or ""), *aliases) if x and str(x).strip()]
    out: set[str] = set()
    team_is_female = (str(gender or "").upper() in ("F", "FEMALE", "FEMALE_CLUB")) or any(bool(FEMALE_RE.search(n) or _is_female_label(n)) for n in raw_names)
    for name in raw_names:
        norm_name = _norm_team_tokens(name)
        if norm_name:
            out.add(norm_name)
        for cand in team_candidate_queries(name):
            cn = _norm_team_tokens(cand)
            if not cn:
                continue
            cand_is_female = _is_female_label(cand)
            if team_is_female and not cand_is_female:
                # Mantener marca femenina si el alias curado (ej. OL Lyonnes) aparece en Bwin como "OL Lyonnes (F)"
                out.add(f"{cn} f")
                continue
            if not team_is_female and cand_is_female:
                continue
            out.add(cn)
    return out


def _parse_ou_line(sel: str) -> tuple[str, str] | None:
    m = _OU_SELECTION_RE.match(sel.strip())
    if not m:
        return None
    raw_dir, raw_num = m.groups()
    try:
        val = float(raw_num.replace(",", "."))
    except ValueError:
        return None
    if abs((val * 2) - round(val * 2)) > 1e-6 or round(val * 2) % 2 == 0:
        return None
    direction = "over" if raw_dir.lower().startswith("m") and "n" not in raw_dir.lower() else "under"
    slug = f"{val:.1f}".replace(".", "_")
    return direction, slug


def parse_bwin_odds_txt(
    bwin_txt_path: Path | None,
    *,
    home_name: str | None = None,
    away_name: str | None = None,
    home_aliases: tuple[str, ...] = (),
    away_aliases: tuple[str, ...] = (),
    gender: str | None = None,
) -> dict[str, float]:
    """Reutiliza motor_probabilistico.parse_odds para mapear dinámicamente mercados Bwin con soporte en Shadow."""
    if bwin_txt_path is None or not Path(bwin_txt_path).is_file():
        return {}
    from motor_probabilistico import parse_odds

    parsed = parse_odds(Path(bwin_txt_path))
    quotes: dict[str, set[float]] = {}

    def record(key: str, odd: float) -> None:
        if odd > 1.0:
            quotes.setdefault(key, set()).add(odd)

    home_keys = _expand_team_keys(home_name, home_aliases, gender=gender)
    away_keys = _expand_team_keys(away_name, away_aliases, gender=gender)

    for block in parsed.get("markets", []):
        base, _, period = str(block.get("market") or "").strip().lower().partition("|")
        base, period = base.strip(), period.strip()
        mname = f"{base} | {period}" if period else base
        sels = block.get("selections") or []

        # Excluir props individuales de jugador, goleadores, especiales/combinadas y 2ª mitad
        if mname.startswith(("jugador", "goleadores", "crack bwin", "crea tu")):
            continue
        if re.search(r"\b(?:live|en directo|resto del partido|marcador actual|entre el|minutos?|\d{1,2}:\d{2})\b", mname):
            continue

        is_h1 = period in {"1ª parte", "primera parte"}
        is_reg = period in {"", "tiempo reglamentario"}
        if not (is_reg or is_h1):
            continue
        if is_h1:
            mname = f"{base} | 1ª parte"

        if is_reg and (mname.startswith("resultado del partido | tiempo reglamentario") or mname == "resultado del partido"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                if sel == "x":
                    record("draw", odd)
                elif _match_team_key(sel, home_keys):
                    record("home_win", odd)
                elif _match_team_key(sel, away_keys):
                    record("away_win", odd)
        elif is_reg and (mname.startswith("doble oportunidad | tiempo reglamentario") or mname == "doble oportunidad"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                parts = [p.strip() for p in sel.split(" o ")]
                if len(parts) == 2:
                    has_x = "x" in parts
                    has_h = any(_match_team_key(p, home_keys) for p in parts if p != "x")
                    has_a = any(_match_team_key(p, away_keys) for p in parts if p != "x")
                    if has_h and has_x:
                        record("double_chance_1x", odd)
                    elif has_a and has_x:
                        record("double_chance_x2", odd)
                    elif has_h and has_a:
                        record("double_chance_12", odd)
        elif is_reg and (mname.startswith("ganador sin empate | tiempo reglamentario") or mname == "ganador sin empate"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                if _match_team_key(sel, home_keys):
                    record("dnb_home", odd)
                elif _match_team_key(sel, away_keys):
                    record("dnb_away", odd)
        elif is_reg and (mname.startswith("ambos equipos marcan | tiempo reglamentario") or mname == "ambos equipos marcan"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                if sel in ("sí", "si", "yes"):
                    record("btts_yes", float(s["odd"]))
                elif sel == "no":
                    record("btts_no", float(s["odd"]))
        elif is_reg and (mname.startswith("total de goles | tiempo reglamentario") or mname == "total de goles"):
            for s in sels:
                parsed_line = _parse_ou_line(str(s.get("selection") or ""))
                if parsed_line:
                    d_str, l_slug = parsed_line
                    record(f"{d_str}_{l_slug}_goals", float(s["odd"]))
        elif is_h1 and mname.startswith("total de goles | 1ª parte"):
            for s in sels:
                parsed_line = _parse_ou_line(str(s.get("selection") or ""))
                if parsed_line:
                    d_str, l_slug = parsed_line
                    record(f"h1_{d_str}_{l_slug}_goals", float(s["odd"]))
        elif re.fullmatch(r".+ - total de goles", base):
            prefix_team = mname.split(" - total de goles", 1)[0].strip()
            is_h = _match_team_key(prefix_team, home_keys)
            is_a = _match_team_key(prefix_team, away_keys)
            side = ("home" if is_h else "away") if is_h != is_a else None
            if side:
                h1_pref = "h1_" if is_h1 else ""
                for s in sels:
                    parsed_line = _parse_ou_line(str(s.get("selection") or ""))
                    if parsed_line:
                        d_str, l_slug = parsed_line
                        record(f"{h1_pref}{side}_{d_str}_{l_slug}_goals", float(s["odd"]))
        elif is_reg and (mname.startswith("total de córners | tiempo reglamentario") or mname in ("total de córners", "total de corners")):
            for s in sels:
                parsed_line = _parse_ou_line(str(s.get("selection") or ""))
                if parsed_line:
                    d_str, l_slug = parsed_line
                    record(f"{d_str}_{l_slug}_corners", float(s["odd"]))
        elif is_reg and re.fullmatch(r".+ - total de c[oó]rners", base):
            prefix_team = re.split(r" - total de c[oó]rners", mname, maxsplit=1)[0].strip()
            is_h, is_a = _match_team_key(prefix_team, home_keys), _match_team_key(prefix_team, away_keys)
            side = ("home" if is_h else "away") if is_h != is_a else None
            if side:
                for s in sels:
                    parsed_line = _parse_ou_line(str(s.get("selection") or ""))
                    if parsed_line:
                        d_str, l_slug = parsed_line
                        record(f"{side}_{d_str}_{l_slug}_corners", float(s["odd"]))
        elif is_reg and base == "total de tarjetas":
            for s in sels:
                parsed_line = _parse_ou_line(str(s.get("selection") or ""))
                if parsed_line:
                    d_str, l_slug = parsed_line
                    record(f"{d_str}_{l_slug}_cards", float(s["odd"]))
        elif base == "marcador exacto":
            pref = "h1_" if is_h1 else ""
            for s in sels:
                sel = str(s.get("selection") or "").strip()
                m_sc = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", sel)
                if m_sc:
                    record(f"{pref}exact_score_{int(m_sc.group(1))}_{int(m_sc.group(2))}", float(s["odd"]))
        elif is_h1 and mname.startswith("resultado del partido | 1ª parte"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                if sel == "x":
                    record("h1_draw", odd)
                elif _match_team_key(sel, home_keys):
                    record("h1_home_win", odd)
                elif _match_team_key(sel, away_keys):
                    record("h1_away_win", odd)
        elif is_h1 and mname.startswith("doble oportunidad | 1ª parte"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                parts = [p.strip() for p in sel.split(" o ")]
                if len(parts) == 2:
                    has_x = "x" in parts
                    has_h = any(_match_team_key(p, home_keys) for p in parts if p != "x")
                    has_a = any(_match_team_key(p, away_keys) for p in parts if p != "x")
                    if has_h and has_x:
                        record("h1_double_chance_1x", odd)
                    elif has_a and has_x:
                        record("h1_double_chance_x2", odd)
                    elif has_h and has_a:
                        record("h1_double_chance_12", odd)
        elif is_h1 and mname.startswith("ganador sin empate | 1ª parte"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                if _match_team_key(sel, home_keys):
                    record("h1_dnb_home", odd)
                elif _match_team_key(sel, away_keys):
                    record("h1_dnb_away", odd)
        elif is_h1 and mname.startswith("ambos equipos marcan | 1ª parte"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                if sel in ("sí", "si", "yes"):
                    record("h1_btts_yes", float(s["odd"]))
                elif sel == "no":
                    record("h1_btts_no", float(s["odd"]))
        elif is_reg and mname.startswith("hándicap 1x2"):
            m_h = re.search(r"\(([+-]?\d+)\)", mname)
            if m_h:
                h_val = int(m_h.group(1))
                tag = f"m{abs(h_val)}" if h_val < 0 else f"p{h_val}"
                for s in sels:
                    sel = str(s.get("selection") or "").strip().lower()
                    odd = float(s["odd"])
                    if "empate" in sel or sel == "x":
                        record(f"eh_{tag}_draw", odd)
                    elif _match_team_key(re.sub(r"\s+[+-]?\d+$", "", sel), home_keys):
                        record(f"eh_{tag}_home", odd)
                    elif _match_team_key(re.sub(r"\s+[+-]?\d+$", "", sel), away_keys):
                        record(f"eh_{tag}_away", odd)
        elif is_reg and mname.startswith("resultado (1x2) y ambos equipos marcan"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                if "ningún equipo marca" in sel or "ningun equipo marca" in sel:
                    record("result_none_score", odd)
                elif "empate" in sel and "marcan" in sel:
                    record("result_draw_btts_yes", odd)
                elif "ambos equipos marcan" in sel:
                    if _match_team_key(sel, home_keys):
                        record("result_home_btts_yes", odd)
                    elif _match_team_key(sel, away_keys):
                        record("result_away_btts_yes", odd)
                elif "no marca" in sel:
                    parts = sel.split(" gana y ")
                    if len(parts) == 2:
                        if _match_team_key(parts[0], home_keys):
                            record("result_home_away_no", odd)
                        elif _match_team_key(parts[0], away_keys):
                            record("result_away_home_no", odd)
        elif is_reg and mname.startswith("ambos equipos marcan y cualquier equipo gana"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                if sel in ("sí", "si", "yes"):
                    record("btts_yes_and_either_win", float(s["odd"]))
                elif sel == "no":
                    record("btts_yes_and_either_win_no", float(s["odd"]))
        elif is_reg and mname.startswith("ambos equipos marcan y total de goles 2,5"):
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                odd = float(s["odd"])
                if "más de 2,5" in sel or "mas de 2,5" in sel:
                    record("btts_yes_and_over_2_5" if sel.startswith(("sí", "si")) else "btts_no_and_over_2_5", odd)
                elif "menos de 2,5" in sel:
                    record("btts_yes_and_under_2_5" if sel.startswith(("sí", "si")) else "btts_no_and_under_2_5", odd)
        elif base == "1er equipo en marcar":
            pref = "h1_" if is_h1 else ""
            for s in sels:
                sel = str(s.get("selection") or "").strip().lower()
                if "no hay gol" in sel:
                    record(f"{pref}first_goal_none", float(s["odd"]))
    return {key: next(iter(values)) for key, values in quotes.items() if len(values) == 1}


def audit_bwin_catalog_txt(
    bwin_txt_path: Path | None,
    evaluated_with_odds_count: int,
    *,
    unmodelled_parsed_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Clasifica el catálogo completo del TXT de Bwin entre modelado por Shadow y no modelado."""
    if bwin_txt_path is None or not Path(bwin_txt_path).is_file():
        return {
            "txt_provided": False,
            "total_markets_in_txt": 0,
            "total_selections_in_txt": 0,
            "modelled_selections_with_odds": evaluated_with_odds_count,
            "unmodelled_selections_total": len(unmodelled_parsed_items or ()),
            "unmodelled_by_category": {},
            "parsed_unmodelled_selections": list(unmodelled_parsed_items or ()),
        }
    from motor_probabilistico import parse_odds

    parsed = parse_odds(Path(bwin_txt_path))
    markets = parsed.get("markets", [])
    total_sels = sum(len(b.get("selections") or []) for b in markets)
    unmodelled_cats = {
        "PLAYER_PROPS_REQUIRES_XI_AND_EXPOSURE": 0,
        "UNMODELLED_PERIOD_2H_OR_HALF_DERIVATIVES": 0,
        "UNMODELLED_EXACT_SCORE_HANDICAP_COMBO": 0,
    }
    supported_h1_prefixes = (
        "total de goles | 1ª parte",
        "resultado del partido | 1ª parte",
        "doble oportunidad | 1ª parte",
        "ganador sin empate | 1ª parte",
        "ambos equipos marcan | 1ª parte",
        "marcador exacto | 1ª parte",
    )
    supported_eh_tags = ("(1)", "(-1)", "(2)", "(-2)")
    for block in markets:
        mname = str(block.get("market") or "").strip().lower()
        n_sels = len(block.get("selections") or [])
        is_h1 = "1ª parte" in mname or "primera parte" in mname
        is_2h = "2ª parte" in mname or "segunda parte" in mname
        if mname.startswith(("jugador", "goleadores", "crack bwin")):
            unmodelled_cats["PLAYER_PROPS_REQUIRES_XI_AND_EXPOSURE"] += n_sels
        elif is_2h or (
            is_h1
            and not mname.startswith(supported_h1_prefixes)
            and " - total de goles" not in mname
        ):
            unmodelled_cats["UNMODELLED_PERIOD_2H_OR_HALF_DERIVATIVES"] += n_sels
        elif (
            mname.startswith("hándicap 1x2") and not any(tag in mname for tag in supported_eh_tags)
        ) or (
            not is_h1
            and not is_2h
            and any(
                k in mname
                for k in (
                    "descanso/final",
                    "descanso o final",
                    "1er equipo en marcar",
                    "crea tu",
                    "cuotas mejoradas",
                    "cuotas especiales",
                    "pase y gol",
                )
            )
        ):
            unmodelled_cats["UNMODELLED_EXACT_SCORE_HANDICAP_COMBO"] += n_sels

    unmodelled_total = max(0, total_sels - evaluated_with_odds_count)
    explicit_sum = sum(unmodelled_cats.values())
    if explicit_sum < unmodelled_total:
        unmodelled_cats["UNMODELLED_OTHER_OR_UNMAPPED"] = unmodelled_total - explicit_sum
    elif explicit_sum > unmodelled_total:
        # Ajuste defensivo en caso de variación de cabeceras parciales sin sobrescribir jugadores
        overflow = explicit_sum - unmodelled_total
        for k_adj in ("UNMODELLED_EXACT_SCORE_HANDICAP_COMBO", "UNMODELLED_PERIOD_2H_OR_HALF_DERIVATIVES"):
            take = min(overflow, unmodelled_cats[k_adj])
            unmodelled_cats[k_adj] -= take
            overflow -= take

    return {
        "txt_provided": True,
        "total_markets_in_txt": len(markets),
        "total_selections_in_txt": total_sels,
        "modelled_selections_with_odds": evaluated_with_odds_count,
        "modelled_coverage_of_non_player_pct": round(
            100.0
            * evaluated_with_odds_count
            / max(1, total_sels - unmodelled_cats["PLAYER_PROPS_REQUIRES_XI_AND_EXPOSURE"]),
            2,
        ),
        "unmodelled_selections_total": unmodelled_total,
        "unmodelled_by_category": unmodelled_cats,
        "parsed_unmodelled_selections": list(unmodelled_parsed_items or ()),
    }


@dataclass(frozen=True)
class ShadowRunReport:
    """Reporte completo de una ejecución en Shadow Mode para un Event ID."""

    event_id: str
    fixture_id: int
    mode: str  # SHADOW_NON_INTRUSIVE
    status: str  # COMPLETED_SHADOW | DEGRADED_SHADOW | BLOCKED_PREMATCH_GUARD
    cutoff_utc: str
    kickoff_utc: str
    lead_minutes_before_kickoff: float
    live_data_blocked: bool
    historical_depth_comparison: dict[str, Any]
    coverage_report: dict[str, Any]
    degradations_recovered: tuple[str, ...]
    analytics_summary: dict[str, Any]
    verified_hypotheses: tuple[dict[str, Any], ...]
    model_coherence: dict[str, Any]
    ignored_exploratory_in_p_model: int
    market_comparisons: tuple[dict[str, Any], ...]
    decision: str  # CANDIDATE_IDENTIFIED | NO_BET
    no_bet_reasons: tuple[str, ...]
    differences_vs_legacy: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    decision_taxonomy: str = "NO_BET_POR_EVIDENCIA"  # PICK_ACCIONABLE | CANDIDATO_CONDICIONAL | NO_BET_POR_PRECIO | NO_BET_POR_EVIDENCIA | BLOQUEO_TECNICO
    decision_classification: str = "BLOQUEO_OPERATIVO"  # BET_AUTORIZADO | NO_BET_POR_PRECIO | NO_BET_POR_INCERTIDUMBRE | MODELO_NO_DISPONIBLE | BLOQUEO_OPERATIVO
    decision_breakdown_counts: dict[str, int] = field(default_factory=dict)
    formal_decision_breakdown_counts: dict[str, int] = field(default_factory=dict)
    decision_metrics_summary: dict[str, Any] = field(default_factory=dict)
    top_investigable_candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    catalog_coverage_summary: dict[str, Any] = field(default_factory=dict)
    market_equivalences: dict[str, Any] = field(default_factory=dict)
    lineup_gate: dict[str, Any] = field(default_factory=dict)
    eligible_for_final_action: bool = False
    probabilities_computed_at_utc: str | None = None
    prices_evaluated_at_utc: str | None = None
    timestamp_granularity: str = "IN_PROCESS_SEQUENTIAL_SHADOW_RUN"
    model_version: str = MODEL_PROBABILISTIC_VERSION
    generated_utc: str = ""
    experimental_cand_2b3b_comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShadowPipelineRunner:
    """Ejecuta la cadena modular completa en Shadow Mode sin alterar salidas operativas."""

    def __init__(self, *, min_lead_minutes: int = 45, lineup_cutoff_minutes: int = 15) -> None:
        self.min_lead_minutes = min_lead_minutes
        self.lineup_cutoff_minutes = lineup_cutoff_minutes
        self.coverage_evaluator = CoveragePolicyEvaluator()

    def run_shadow_on_bundle(
        self,
        bundle: MatchObservationBundle,
        *,
        target_status: str = "NS",
        odds_map: dict[str, float] | None = None,
        codex_hypotheses: list[dict[str, Any]] | None = None,
        simulate_codex_failure: bool = False,
        legacy_txt_path: Path | None = None,
        output_dir: Path | None = None,
        bwin_txt_path: Path | None = None,
        lineup_evaluation: dict[str, Any] | None = None,
        lineup_independent_keys: frozenset[str] = frozenset(),
        lineup_cutoff_minutes: int | None = None,
        qualitative_markdown: str | None = None,
        manual_lineup: dict[str, Any] | None = None,
        user_manual_absent: bool = False,
        calibration_accredited_families: set[str] | frozenset[str] | None = None,
        simulate_cand_2b3b_failure: bool = False,
    ) -> ShadowRunReport:
        cut_dt = parse_utc_timestamp(bundle.cutoff_utc)
        ko_dt = parse_utc_timestamp(bundle.kickoff_utc)
        lead_mins = round((ko_dt - cut_dt).total_seconds() / 60.0, 2)

        # 1) Guarda temporal PREMATCH y bloqueo de datos live
        run_generated_utc = datetime.now(timezone.utc).isoformat()
        if target_status.upper() in LIVE_OR_POST_STATUSES or lead_mins < self.min_lead_minutes:
            reason = (
                f"TARGET_STATUS_NOT_PREMATCH({target_status})"
                if target_status.upper() in LIVE_OR_POST_STATUSES
                else f"INSUFFICIENT_PREMATCH_LEAD_TIME({lead_mins}m < {self.min_lead_minutes}m)"
            )
            return ShadowRunReport(
                event_id=bundle.event_id,
                fixture_id=bundle.fixture_id,
                mode="SHADOW_NON_INTRUSIVE",
                status="BLOCKED_PREMATCH_GUARD",
                cutoff_utc=bundle.cutoff_utc,
                kickoff_utc=bundle.kickoff_utc,
                lead_minutes_before_kickoff=lead_mins,
                live_data_blocked=True,
                historical_depth_comparison={},
                coverage_report={},
                degradations_recovered=(reason,),
                analytics_summary={},
                verified_hypotheses=(),
                model_coherence={},
                ignored_exploratory_in_p_model=0,
                market_comparisons=(),
                decision="NO_BET",
                no_bet_reasons=(reason,),
                decision_taxonomy="BLOQUEO_TECNICO",
                decision_classification="BLOQUEO_OPERATIVO",
                formal_decision_breakdown_counts={k: 0 for k in FORMAL_DECISION_CLASSIFICATIONS},
                decision_metrics_summary={
                    "selections_level": {"total_bwin_selections": 0, "modelled_quoted_selections": 0},
                    "independent_candidates_level": {"total_canonical_contracts": 0},
                    "match_decision_level": {
                        "decision": "NO_BET",
                        "decision_taxonomy": "BLOQUEO_TECNICO",
                        "decision_classification": "BLOQUEO_OPERATIVO",
                        "is_statistical_no_bet": False,
                        "is_incomplete_expedient": True,
                        "blocking_reasons": [reason],
                    },
                },
                model_version=MODEL_PROBABILISTIC_VERSION,
                generated_utc=run_generated_utc,
            )

        degradations: list[str] = []

        # 2) Auditoría de cobertura observada
        l20_h = bundle.windows["home"]["L20_all_competitions"]
        l20_a = bundle.windows["away"]["L20_all_competitions"]
        valid_counts_h = {
            k.split(".")[-1]: v["valid_n"]
            for k, v in l20_h.metrics.items()
            if k.startswith("own.")
        }
        cov_audit = self.coverage_evaluator.audit_observations(
            league_id=bundle.league_id,
            season=bundle.season,
            league_coverage=None,
            total_matches=l20_h.total_matches_available,
            detailed_matches_requested=max(1, l20_h.detailed_matches_valid),
            metric_valid_counts=valid_counts_h,
        )
        if l20_h.detailed_matches_valid < 4 or l20_a.detailed_matches_valid < 4:
            degradations.append(
                f"DEGRADED_PARTIAL_API_COVERAGE(home_det={l20_h.detailed_matches_valid},away_det={l20_a.detailed_matches_valid})"
            )

        # 3) Reconciliación contextual de entrenadores (sin tocar observaciones ni p_model) + Núcleo Analítico
        if qualitative_markdown:
            bundle = reconcile_bundle_coach_regime(bundle, qualitative_markdown=qualitative_markdown)
        reg = build_stage3_registry()
        analytics_res: AnalyticsEngineResult = reg.run_all(bundle)

        # 4) Investigador Táctico Codex (con recuperación ante fallo de LLM/formato)
        verified_hyps: list[VerifiedHypothesisRecord] = []
        reg_store = HypothesisRegistry(output_dir / "hypothesis_registry.json") if output_dir else None
        inv = TacticalInvestigator(registry=reg_store)
        try:
            if simulate_codex_failure:
                raise EpistemicContractError("SIMULATED_CODEX_TIMEOUT_OR_MALFORMED_JSON")
            if codex_hypotheses:
                verified_hyps = verify_hypotheses(bundle, codex_hypotheses, registry=reg_store)
            elif analytics_res.tactical_hypotheses:
                verified_hyps = verify_hypotheses(
                    bundle, list(analytics_res.tactical_hypotheses), registry=reg_store
                )
        except Exception as exc:
            degradations.append(f"DEGRADED_CODEX_INVESTIGATOR_RECOVERED({type(exc).__name__}:{exc})")
            verified_hyps = []

        # 5) Modelización Probabilística Coherente (exploratorios aislados de p_model)
        exploratory_all = (
            analytics_res.prematch_patterns
            + analytics_res.internal_mechanisms
            + analytics_res.descriptive_splits
            + analytics_res.tactical_hypotheses
        )
        model: MatchProbabilisticModel = CoherentMatchModeler.build_model(
            bundle, exploratory_artifacts=exploratory_all
        )
        probabilities_computed_at_utc = datetime.now(timezone.utc).isoformat()

        # 5b) Evaluación paralela experimental 2B.3B (no intrusiva y sin capacidad de autorizar BET)
        experimental_cand_2b3b: dict[str, Any] = {}
        try:
            from bet_engine.modeling.prospective_cand_2b3b import evaluate_parallel_bundle_2b3b

            experimental_cand_2b3b = evaluate_parallel_bundle_2b3b(
                bundle,
                history_limit=20,
                simulate_failure=simulate_cand_2b3b_failure,
            )
        except Exception as exc:
            degradations.append(f"DEGRADED_EXPERIMENTAL_CAND_2B3B_RECOVERED({type(exc).__name__}:{exc})")
            experimental_cand_2b3b = {
                "experimental_only": True,
                "candidate_cannot_authorize_bet": True,
                "status": "FAILED_ISOLATED_NON_BLOCKING",
                "error": f"{type(exc).__name__}:{exc}",
            }

        # 6) Evaluación de estado obligatorio de XI confirmados (P0) y Mercados Bwin
        from bet_engine.context.lineup_manager import LineupStatus, evaluate_fixture_lineup_state, is_confirmed_complete

        eff_cutoff = int(lineup_cutoff_minutes if lineup_cutoff_minutes is not None else self.lineup_cutoff_minutes)
        base_lineup_eval = dict(getattr(bundle, "target_lineup_evaluation", None) or {})
        if lineup_evaluation is not None:
            eff_lineup_eval = dict(lineup_evaluation)
        elif is_confirmed_complete(base_lineup_eval.get("status")):
            eff_lineup_eval = base_lineup_eval
        else:
            # Reevaluar con manual_lineup / qualitative_markdown / corte configurable
            eff_lineup_eval = evaluate_fixture_lineup_state(
                api_lineups_response=None,
                manual_lineup=manual_lineup,
                qualitative_markdown=qualitative_markdown,
                fixture_id=bundle.fixture_id,
                event_id=bundle.event_id,
                home_id=bundle.home_id,
                away_id=bundle.away_id,
                home_name=bundle.home_name,
                away_name=bundle.away_name,
                kickoff_utc=bundle.kickoff_utc,
                now_utc=bundle.cutoff_utc,
                lineup_cutoff_minutes=eff_cutoff,
                user_manual_absent=user_manual_absent,
            )
            # Preservar conteo parcial o error API previo si existía en base_lineup_eval y no se resolvió aún
            if not eff_lineup_eval.get("confirmed_complete_22") and base_lineup_eval.get("partial_team_confirmed"):
                eff_lineup_eval["confirmed_sides"] = base_lineup_eval.get("confirmed_sides")
                eff_lineup_eval["partial_team_confirmed"] = True
                if eff_lineup_eval["status"] == LineupStatus.LINEUPS_PENDING.value and base_lineup_eval.get("reason"):
                    eff_lineup_eval["reason"] = base_lineup_eval["reason"]

        lineup_status = str(eff_lineup_eval.get("status") or LineupStatus.LINEUPS_PENDING.value)
        lineups_confirmed_22 = is_confirmed_complete(lineup_status)

        effective_odds = dict(odds_map or {})
        if lineup_independent_keys:
            effective_odds = {key: odd for key, odd in effective_odds.items() if key in lineup_independent_keys}
            eff_lineup_eval["lineup_independent_selection_keys"] = sorted(lineup_independent_keys)
        lineup_gate_satisfied = lineups_confirmed_22 or bool(lineup_independent_keys)
        if not effective_odds:
            degradations.append("DEGRADED_MISSING_BWIN_ODDS_PRICE_PENDING")

        accredited_families = frozenset(calibration_accredited_families or ())
        market_opts: list[EvaluatedMarketOption] = BwinMarketEvaluator.evaluate_standard_markets(
            bundle,
            model,
            odds_map=effective_odds,
            analytics_result=analytics_res,
            lineups_confirmed_complete=lineups_confirmed_22,
            lineup_status=lineup_status,
            lineup_independent_keys=lineup_independent_keys,
            calibration_accredited_families=accredited_families,
        )
        prices_evaluated_at_utc = datetime.now(timezone.utc).isoformat() if effective_odds else None

        # 7) Comparación en paralelo contra el flujo legacy (P-1..P-6)
        legacy_home_n = min(6, len(bundle.home_matches))
        legacy_away_n = min(6, len(bundle.away_matches))
        if legacy_txt_path and Path(legacy_txt_path).is_file():
            from motor_probabilistico import parse_context

            parsed_leg = parse_context(Path(legacy_txt_path))
            legacy_home_n = len(parsed_leg["histories"].get(bundle.home_name.lower(), []))
            legacy_away_n = len(parsed_leg["histories"].get(bundle.away_name.lower(), []))

        window_comparison: dict[str, Any] = {}
        for win_n in (6, 10, 20):
            m_win = (
                model
                if win_n == 20
                else CoherentMatchModeler.build_model(
                    bundle,
                    exploratory_artifacts=exploratory_all,
                    history_limit=win_n,
                )
            )
            gf_win = m_win.families.get("GOALS_FT")
            o25_win = gf_win.prob_over("total", 2.5)["p_over"] if gf_win else None
            window_comparison[f"L{win_n}"] = {
                "history_limit": win_n,
                "home_sample_n": gf_win.home_sample_n if gf_win else 0,
                "away_sample_n": gf_win.away_sample_n if gf_win else 0,
                "home_n_eff": gf_win.home_n_eff if gf_win else None,
                "away_n_eff": gf_win.away_n_eff if gf_win else None,
                "lambda_home": gf_win.lambda_home if gf_win else None,
                "lambda_away": gf_win.lambda_away if gf_win else None,
                "outcomes_1x2": m_win.outcomes_1x2,
                "btts_yes": m_win.btts.get("yes"),
                "over_2_5": o25_win,
            }

        hist_comp = {
            "new_home_matches_n": len(bundle.home_matches),
            "new_away_matches_n": len(bundle.away_matches),
            "new_home_detailed_n": l20_h.detailed_matches_valid,
            "new_away_detailed_n": l20_a.detailed_matches_valid,
            "home_score_only_n": l20_h.partial_or_score_only_count,
            "away_score_only_n": l20_a.partial_or_score_only_count,
            "home_goals_valid_n": (l20_h.metrics.get("own.goals") or {}).get("valid_n", 0),
            "away_goals_valid_n": (l20_a.metrics.get("own.goals") or {}).get("valid_n", 0),
            "requested_deep_history_limit": DEEP_HISTORY_LIMIT,
            "configured_detail_limit": DEEP_HISTORY_DETAIL_LIMIT,
            "configured_micro_history_limit": MICRO_HISTORY_LIMIT,
            "legacy_p6_home_matches_n": legacy_home_n,
            "legacy_p6_away_matches_n": legacy_away_n,
            "extra_matches_preserved_home": max(0, len(bundle.home_matches) - legacy_home_n),
            "extra_matches_preserved_away": max(0, len(bundle.away_matches) - legacy_away_n),
            "window_comparison_l6_l10_l20": window_comparison,
        }

        diffs: list[dict[str, Any]] = []
        for opt in market_opts:
            if opt.delta_vs_legacy_p6 is not None and abs(opt.delta_vs_legacy_p6) >= 0.08:
                diffs.append(
                    {
                        "selection_key": opt.selection_key,
                        "p_model_new": opt.p_model,
                        "legacy_p6_hit_rate": opt.legacy_p6_hit_rate,
                        "delta": opt.delta_vs_legacy_p6,
                        "reason": "EMPIRICAL_BAYES_SHRINKAGE_AND_WINDOW_DEPTH_VS_RAW_P6",
                    }
                )

        # 8) Decisión, clasificación en 5 estados y preservación de hasta 3 candidatos investigables
        no_bet_reasons: list[str] = []
        modelled_quoted_opts = [
            o
            for o in market_opts
            if o.odd is not None and o.odd > 1.0 and o.decision_category != "MERCADO_NO_MODELADO"
        ]
        unmodelled_parsed_opts = [
            o for o in market_opts if o.decision_category == "MERCADO_NO_MODELADO"
        ]
        edges = [o for o in modelled_quoted_opts if o.classification == "QUANTITATIVELY_EVALUABLE_EDGE"]
        investigables = [o for o in modelled_quoted_opts if o.classification == "INVESTIGABLE_CANDIDATE"]
        cond_opts = [o for o in modelled_quoted_opts if o.decision_category == "CANDIDATO_CONDICIONAL"]
        pos_cons_opts = [
            o for o in cond_opts if o.ev_conservative_low is not None and o.ev_conservative_low > 0.0
        ]

        unmodelled_parsed_payload = [
            {
                "market_family": o.market_family,
                "selection_key": o.selection_key,
                "odd": o.odd,
                "canonical_contract_id": o.canonical_contract_id,
                "equivalent_best_selection": o.equivalent_best_selection,
                "equivalent_best_odd": o.equivalent_best_odd,
                "reason": o.pending_conditions[0] if o.pending_conditions else "NO_MODELO_VALIDO",
            }
            for o in unmodelled_parsed_opts
        ]
        catalog_summary = audit_bwin_catalog_txt(
            bwin_txt_path,
            len(modelled_quoted_opts),
            unmodelled_parsed_items=unmodelled_parsed_payload,
        )

        breakdown: dict[str, int] = {
            "PICK_ACCIONABLE": sum(1 for o in modelled_quoted_opts if o.decision_category == "PICK_ACCIONABLE"),
            "CANDIDATO_CONDICIONAL": sum(1 for o in modelled_quoted_opts if o.decision_category == "CANDIDATO_CONDICIONAL"),
            "NO_BET_POR_PRECIO": sum(1 for o in modelled_quoted_opts if o.decision_category == "NO_BET_POR_PRECIO"),
            "NO_BET_POR_EVIDENCIA": sum(1 for o in modelled_quoted_opts if o.decision_category == "NO_BET_POR_EVIDENCIA"),
            "BLOQUEO_TECNICO": sum(1 for o in market_opts if o.decision_category == "BLOQUEO_TECNICO"),
            "MERCADO_NO_MODELADO": (
                int(catalog_summary.get("unmodelled_selections_total", 0))
                if bwin_txt_path and Path(bwin_txt_path).is_file()
                else len(unmodelled_parsed_opts)
            ),
        }

        if not lineup_gate_satisfied:
            no_bet_reasons.append(f"LINEUPS_NOT_CONFIRMED:{lineup_status}")
        if not effective_odds:
            no_bet_reasons.append("PRECIO_PENDIENTE_DE_VERIFICACION_SIN_CUOTAS_BWIN")
        if l20_h.total_matches_available < 4 or l20_a.total_matches_available < 4:
            no_bet_reasons.append("MUESTRA_HISTORICA_INSUFICIENTE")
        if not edges and not investigables and not cond_opts:
            no_bet_reasons.append("SIN_VENTAJA_CUANTITATIVA_DEMOSTRADA_FRENTE_A_CUOTA")
        elif not edges and (investigables or cond_opts):
            if pos_cons_opts:
                no_bet_reasons.append(
                    "CANDIDATO_CON_EV_CONSERVADOR_POSITIVO_BLOQUEADO_POR_CALIBRACION_MUESTRA_O_XI"
                )
                if any((o.raw_sample_n or 0) < 10 for o in pos_cons_opts):
                    no_bet_reasons.append("BLOQUEO_ACCIONABLE_MUESTRA_CORTA(n<10)")
                if all(o.calibration_status != "CALIBRATED_OUT_OF_SAMPLE" for o in pos_cons_opts):
                    no_bet_reasons.append("BLOQUEO_ACCIONABLE_CALIBRACION_NO_ACREDITADA")
            else:
                no_bet_reasons.append("SOLO_CANDIDATO_INVESTIGABLE_EV_CONSERVADOR_NEGATIVO_POR_INCERTIDUMBRE")

        decision = "CANDIDATE_IDENTIFIED" if (edges and lineup_gate_satisfied and not no_bet_reasons) else "NO_BET"
        if not lineup_gate_satisfied and lineup_status == LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value:
            decision_taxonomy = "BLOQUEO_TECNICO"
        elif not effective_odds:
            decision_taxonomy = "BLOQUEO_TECNICO"
        elif l20_h.total_matches_available < 4 or l20_a.total_matches_available < 4:
            decision_taxonomy = "NO_BET_POR_EVIDENCIA"
        elif breakdown["PICK_ACCIONABLE"] > 0 and lineup_gate_satisfied and not no_bet_reasons:
            decision_taxonomy = "PICK_ACCIONABLE"
        elif breakdown["CANDIDATO_CONDICIONAL"] > 0:
            decision_taxonomy = "CANDIDATO_CONDICIONAL"
        elif breakdown["NO_BET_POR_EVIDENCIA"] > 0 and breakdown["NO_BET_POR_PRECIO"] == 0:
            decision_taxonomy = "NO_BET_POR_EVIDENCIA"
        else:
            decision_taxonomy = "NO_BET_POR_PRECIO"

        unmodelled_total_count = breakdown["MERCADO_NO_MODELADO"]
        formal_breakdown: dict[str, int] = {
            "BET_AUTORIZADO": sum(1 for o in modelled_quoted_opts if o.formal_decision_classification == "BET_AUTORIZADO"),
            "NO_BET_POR_PRECIO": sum(1 for o in modelled_quoted_opts if o.formal_decision_classification == "NO_BET_POR_PRECIO"),
            "NO_BET_POR_INCERTIDUMBRE": sum(1 for o in modelled_quoted_opts if o.formal_decision_classification == "NO_BET_POR_INCERTIDUMBRE"),
            "MODELO_NO_DISPONIBLE": unmodelled_total_count,
            "BLOQUEO_OPERATIVO": breakdown["BLOQUEO_TECNICO"],
        }

        if not lineup_gate_satisfied or not effective_odds:
            decision_classification = "BLOQUEO_OPERATIVO"
        elif not modelled_quoted_opts:
            decision_classification = "MODELO_NO_DISPONIBLE"
        elif breakdown["PICK_ACCIONABLE"] > 0 and lineup_gate_satisfied and not no_bet_reasons:
            decision_classification = "BET_AUTORIZADO"
        elif (
            breakdown["CANDIDATO_CONDICIONAL"] > 0
            or breakdown["NO_BET_POR_EVIDENCIA"] > 0
            or any(o.calibration_status != "CALIBRATED_OUT_OF_SAMPLE" for o in modelled_quoted_opts)
        ):
            decision_classification = "NO_BET_POR_INCERTIDUMBRE"
        else:
            decision_classification = "NO_BET_POR_PRECIO"

        def _pct(num: int, den: int) -> float:
            return round((num / den) * 100.0, 2) if den > 0 else 0.0

        total_bwin_selections = (
            int(catalog_summary.get("total_selections_in_txt") or 0)
            if bwin_txt_path and Path(bwin_txt_path).is_file()
            else len(modelled_quoted_opts) + len(unmodelled_parsed_opts)
        )
        all_contract_groups: dict[str, list[EvaluatedMarketOption]] = {}
        for o in modelled_quoted_opts:
            gk = o.canonical_contract_id or f"__unique__:{o.selection_key}"
            all_contract_groups.setdefault(gk, []).append(o)
        n_groups = len(all_contract_groups)
        bet_groups = sum(1 for grp in all_contract_groups.values() if any(x.decision_category == "PICK_ACCIONABLE" for x in grp))
        cond_groups = sum(1 for grp in all_contract_groups.values() if any(x.decision_category == "CANDIDATO_CONDICIONAL" for x in grp) and not any(x.decision_category == "PICK_ACCIONABLE" for x in grp))
        ev_pos_groups = sum(1 for grp in all_contract_groups.values() if any((x.ev_conservative_low or -1.0) > 0.0 for x in grp))
        price_groups = sum(1 for grp in all_contract_groups.values() if all(x.decision_category == "NO_BET_POR_PRECIO" for x in grp))
        evid_groups = sum(1 for grp in all_contract_groups.values() if all(x.decision_category == "NO_BET_POR_EVIDENCIA" for x in grp))

        decision_metrics_summary = {
            "selections_level": {
                "total_bwin_selections": total_bwin_selections,
                "modelled_quoted_selections": len(modelled_quoted_opts),
                "modelled_quoted_pct_of_bwin": _pct(len(modelled_quoted_opts), total_bwin_selections),
                "unmodelled_selections": unmodelled_total_count,
                "unmodelled_pct_of_bwin": _pct(unmodelled_total_count, total_bwin_selections),
                "technical_no_odds_selections": breakdown["BLOQUEO_TECNICO"],
                "over_modelled_quoted_denominator": len(modelled_quoted_opts),
                "over_modelled_quoted_pct": {
                    "BET_AUTORIZADO": _pct(formal_breakdown["BET_AUTORIZADO"], len(modelled_quoted_opts)),
                    "NO_BET_POR_PRECIO": _pct(formal_breakdown["NO_BET_POR_PRECIO"], len(modelled_quoted_opts)),
                    "NO_BET_POR_INCERTIDUMBRE": _pct(formal_breakdown["NO_BET_POR_INCERTIDUMBRE"], len(modelled_quoted_opts)),
                    "CANDIDATO_CONDICIONAL_LEGACY": _pct(breakdown["CANDIDATO_CONDICIONAL"], len(modelled_quoted_opts)),
                    "NO_BET_POR_PRECIO_LEGACY": _pct(breakdown["NO_BET_POR_PRECIO"], len(modelled_quoted_opts)),
                    "EV_CONSERVADOR_POSITIVO": _pct(len(pos_cons_opts), len(modelled_quoted_opts)),
                },
            },
            "independent_candidates_level": {
                "total_canonical_contracts": n_groups,
                "bet_autorizado_groups": bet_groups,
                "bet_autorizado_pct": _pct(bet_groups, n_groups),
                "candidato_condicional_groups": cond_groups,
                "candidato_condicional_pct": _pct(cond_groups, n_groups),
                "ev_conservative_positive_groups": ev_pos_groups,
                "ev_conservative_positive_pct": _pct(ev_pos_groups, n_groups),
                "no_bet_por_precio_groups": price_groups,
                "no_bet_por_precio_pct": _pct(price_groups, n_groups),
                "no_bet_por_evidencia_groups": evid_groups,
                "no_bet_por_evidencia_pct": _pct(evid_groups, n_groups),
            },
            "match_decision_level": {
                "decision": decision,
                "decision_taxonomy": decision_taxonomy,
                "decision_classification": decision_classification,
                "is_statistical_no_bet": decision_classification in ("NO_BET_POR_PRECIO", "NO_BET_POR_INCERTIDUMBRE"),
                "is_incomplete_expedient": decision_classification in ("BLOQUEO_OPERATIVO", "MODELO_NO_DISPONIBLE"),
                "calibration_accredited_families_count": len(accredited_families),
                "calibration_accredited_in_modelled_opts": any(
                    o.calibration_status == "CALIBRATED_OUT_OF_SAMPLE" for o in modelled_quoted_opts
                ),
            },
        }

        # Conservar hasta 3 candidatos investigables/exploratorios deduplicando contratos equivalentes
        # (conservando todas las cuotas originales) y priorizando EV conservador + ausencia de riesgos.
        eligible_for_shortlist = [
            o
            for o in modelled_quoted_opts
            if o.decision_category != "NO_BET_POR_EVIDENCIA"
        ] or modelled_quoted_opts

        contract_groups: dict[str, list[EvaluatedMarketOption]] = {}
        for o in eligible_for_shortlist:
            gk = o.canonical_contract_id or f"__unique__:{o.selection_key}"
            contract_groups.setdefault(gk, []).append(o)

        dedup_representatives: list[tuple[EvaluatedMarketOption, list[dict[str, Any]]]] = []
        for grp in contract_groups.values():
            ordered_grp = sorted(
                grp,
                key=lambda x: (
                    -(x.odd or 0.0),
                    1 if "PRECIO_INFERIOR_A_EQUIVALENTE_EN_SNAPSHOT" in x.risk_flags else 0,
                    x.selection_key,
                ),
            )
            rep_opt = ordered_grp[0]
            eq_quotes = [
                {
                    "selection_key": x.selection_key,
                    "market_family": x.market_family,
                    "odd": x.odd,
                }
                for x in ordered_grp
            ]
            dedup_representatives.append((rep_opt, eq_quotes))

        def _has_material_stat_risk(opt: EvaluatedMarketOption) -> bool:
            return any(
                f.startswith(("COLA_ESTADISTICA_", "RIESGO_HISTORICO_0_DE_", "MUESTRA_INSUFICIENTE"))
                or f == "PRECIO_INFERIOR_A_EQUIVALENTE_EN_SNAPSHOT"
                for f in opt.risk_flags
            )

        ranked_candidates = sorted(
            dedup_representatives,
            key=lambda pair: (
                0 if pair[0].decision_category == "PICK_ACCIONABLE" else 1 if pair[0].decision_category == "CANDIDATO_CONDICIONAL" else 2,
                0 if (pair[0].ev_conservative_low is not None and pair[0].ev_conservative_low > 0.0) else 1,
                1 if _has_material_stat_risk(pair[0]) else 0,
                -(pair[0].ev_conservative_low if pair[0].ev_conservative_low is not None else -999.0),
                -(pair[0].ev_model if pair[0].ev_model is not None else -999.0),
                -(pair[0].effective_sample_n if pair[0].effective_sample_n is not None else float(pair[0].raw_sample_n or 0)),
                pair[0].selection_key,
            ),
        )[:3]

        top_candidates = tuple(
            {
                "market_family": o.market_family,
                "selection_key": o.selection_key,
                "line": o.line,
                "odd": o.odd,
                "break_even_prob": o.break_even_prob,
                "fair_odd": o.fair_odd,
                "p_model": o.p_model,
                "p_low": o.p_low,
                "p_high": o.p_high,
                "ev_model": o.ev_model,
                "ev_conservative_low": o.ev_conservative_low,
                "ev_conservative_positive": bool(o.ev_conservative_low is not None and o.ev_conservative_low > 0.0),
                "raw_sample_n": o.raw_sample_n,
                "effective_sample_n": o.effective_sample_n,
                "calibration_status": o.calibration_status,
                "canonical_contract_id": o.canonical_contract_id,
                "equivalent_best_selection": o.equivalent_best_selection,
                "equivalent_best_odd": o.equivalent_best_odd,
                "equivalent_quotes": eq_quotes,
                "settlement_rule": o.settlement_rule,
                "decision_category": o.decision_category,
                "limitations": list(o.tactical_warnings),
                "risk_flags": list(getattr(o, "risk_flags", ())),
                "pending_conditions": list(o.pending_conditions),
                "actionable_pick": o.decision_category == "PICK_ACCIONABLE" and decision == "CANDIDATE_IDENTIFIED",
            }
            for o, eq_quotes in ranked_candidates
        )

        status_label = "DEGRADED_SHADOW" if degradations else "COMPLETED_SHADOW"

        report = ShadowRunReport(
            event_id=bundle.event_id,
            fixture_id=bundle.fixture_id,
            mode="SHADOW_NON_INTRUSIVE",
            status=status_label,
            cutoff_utc=bundle.cutoff_utc,
            kickoff_utc=bundle.kickoff_utc,
            lead_minutes_before_kickoff=lead_mins,
            live_data_blocked=False,
            historical_depth_comparison=hist_comp,
            coverage_report=cov_audit.to_dict(),
            degradations_recovered=tuple(degradations),
            analytics_summary=analytics_res.to_dict()["counts"],
            verified_hypotheses=tuple(v.to_dict() for v in verified_hyps),
            model_coherence=model.coherence_checks,
            ignored_exploratory_in_p_model=model.ignored_exploratory_count,
            market_comparisons=tuple(o.to_dict() for o in market_opts),
            decision=decision,
            no_bet_reasons=tuple(no_bet_reasons),
            differences_vs_legacy=tuple(diffs),
            decision_taxonomy=decision_taxonomy,
            decision_classification=decision_classification,
            decision_breakdown_counts=breakdown,
            formal_decision_breakdown_counts=formal_breakdown,
            decision_metrics_summary=decision_metrics_summary,
            top_investigable_candidates=top_candidates,
            catalog_coverage_summary=catalog_summary,
            market_equivalences=audit_snapshot_equivalences(effective_odds),
            lineup_gate=eff_lineup_eval,
            eligible_for_final_action=lineup_gate_satisfied,
            probabilities_computed_at_utc=probabilities_computed_at_utc,
            prices_evaluated_at_utc=prices_evaluated_at_utc,
            timestamp_granularity="IN_PROCESS_SEQUENTIAL_SHADOW_RUN",
            model_version=model.model_version,
            generated_utc=run_generated_utc,
            experimental_cand_2b3b_comparison=experimental_cand_2b3b,
        )

        if output_dir is not None:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"shadow_comparison_{bundle.event_id}.json").write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            inv.write_manual_handoff(out, bundle, analytics_res)

        return report
