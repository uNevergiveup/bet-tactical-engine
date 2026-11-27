"""Capa de equivalencia contractual y detección de mejor cuota Bwin (Fase E).

Canonicaliza selecciones mediante contrato exacto:
`(family, period, scope, predicate, settlement_rule, push_condition)`.
Nunca infiere equivalencia por similitud nominal y bloquea explícitamente falsos equivalentes
(Hándicap Europeo vs Hándicap Asiático/DNB, 1ª Parte vs FT, Descanso/Final vs 1X2).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CanonicalContract:
    """Identidad matemática y contractual de una selección de mercado."""

    contract_id: str
    period: str  # FT | H1
    scope: str  # match | home | away
    predicate: str
    settlement_rule: str
    push_condition: str | None = None

    def is_equivalent_to(self, other: "CanonicalContract") -> bool:
        return (
            self.period == other.period
            and self.scope == other.scope
            and self.predicate == other.predicate
            and self.settlement_rule == other.settlement_rule
            and self.push_condition == other.push_condition
        )


def are_contracts_equivalent(sel_a: str, sel_b: str) -> bool:
    """Devuelve True solo si dos selecciones poseen contrato matemático y liquidación idénticos."""
    ca = canonicalize_selection_key(sel_a)
    cb = canonicalize_selection_key(sel_b)
    if ca is None or cb is None:
        return False
    return ca.is_equivalent_to(cb)


_EXACT_SCORE_KEY_RE = re.compile(r"^(?:(h1)_)?exact_score_(\d+)_(\d+)$")
_EH_KEY_RE = re.compile(r"^eh_([mp]\d+)_(home|draw|away)$")
_AH_HALF_KEY_RE = re.compile(r"^ah_(home|away)_([mp])(\d+)_5$")


def canonicalize_selection_key(selection_key: str) -> CanonicalContract | None:
    """Mapea un selection_key a su contrato canónico verificable, o None si no aplica."""
    k = str(selection_key or "").strip()
    if not k:
        return None

    # 1) Cero goles en Tiempo Reglamentario (FT): Under 0.5 == 0-0 exacto == 1er gol Ninguno == Resultado/BTTS Ninguno
    if k in {"under_0_5_goals", "exact_score_0_0", "first_goal_none", "result_none_score"}:
        return CanonicalContract(
            contract_id="FT_ZERO_GOALS_REGULATION",
            period="FT",
            scope="match",
            predicate="H==0 and A==0",
            settlement_rule="BINARY_REGULATION_TIME",
            push_condition=None,
        )

    # 2) Marcador exacto 1-1 FT == BTTS Sí y Menos de 2,5 goles FT
    if k in {"exact_score_1_1", "btts_yes_and_under_2_5"}:
        return CanonicalContract(
            contract_id="FT_EXACT_1_1_REGULATION",
            period="FT",
            scope="match",
            predicate="H==1 and A==1",
            settlement_rule="BINARY_REGULATION_TIME",
            push_condition=None,
        )

    # 3) Doble oportunidad 1X FT == Hándicap Europeo Home +1 == Hándicap 1-2 Home +0.5 (H >= A)
    if k in {"double_chance_1x", "eh_p1_home", "ah_home_p0_5"}:
        return CanonicalContract(
            contract_id="FT_HOME_OR_DRAW",
            period="FT",
            scope="match",
            predicate="H>=A",
            settlement_rule="BINARY_REGULATION_TIME",
            push_condition=None,
        )

    # 4) Doble oportunidad X2 FT == Hándicap Europeo Away +1 desde (-1) == Hándicap 1-2 Away +0.5 (H <= A)
    if k in {"double_chance_x2", "eh_m1_away", "ah_away_p0_5"}:
        return CanonicalContract(
            contract_id="FT_DRAW_OR_AWAY",
            period="FT",
            scope="match",
            predicate="H<=A",
            settlement_rule="BINARY_REGULATION_TIME",
            push_condition=None,
        )

    # 5) Cero goles en 1ª parte: 1H Under 0.5 == 1H 0-0 exacto == 1H 1er gol Ninguno
    if k in {"h1_under_0_5_goals", "h1_exact_score_0_0", "h1_first_goal_none"}:
        return CanonicalContract(
            contract_id="H1_ZERO_GOALS",
            period="H1",
            scope="match",
            predicate="H1_H==0 and H1_A==0",
            settlement_rule="BINARY_FIRST_HALF",
            push_condition=None,
        )

    # 6) Contratos diferenciados y equivalencias exactas Hándicap 1X2 <-> Hándicap 1-2 (.5)
    if k == "dnb_home":
        return CanonicalContract("FT_DNB_HOME", "FT", "match", "H>A (else push if H==A)", "DRAW_NO_BET", push_condition="DRAW_REFUND")
    if k == "dnb_away":
        return CanonicalContract("FT_DNB_AWAY", "FT", "match", "A>H (else push if H==A)", "DRAW_NO_BET", push_condition="DRAW_REFUND")
    if k == "h1_dnb_home":
        return CanonicalContract("H1_DNB_HOME", "H1", "match", "H1_H>H1_A (else push if H1_H==H1_A)", "DRAW_NO_BET_FIRST_HALF", push_condition="DRAW_REFUND")
    if k == "h1_dnb_away":
        return CanonicalContract("H1_DNB_AWAY", "H1", "match", "H1_A>H1_H (else push if H1_H==H1_A)", "DRAW_NO_BET_FIRST_HALF", push_condition="DRAW_REFUND")
    if k in {"home_win", "ah_home_m0_5"}:
        return CanonicalContract("FT_1X2_HOME", "FT", "match", "H>A", "BINARY_REGULATION_TIME", None)
    if k == "draw":
        return CanonicalContract("FT_1X2_DRAW", "FT", "match", "H==A", "THREE_WAY_REGULATION_TIME", None)
    if k in {"away_win", "ah_away_m0_5"}:
        return CanonicalContract("FT_1X2_AWAY", "FT", "match", "A>H", "BINARY_REGULATION_TIME", None)
    if k in {"eh_m1_home", "ah_home_m1_5"}:
        return CanonicalContract("FT_EH_M1_HOME", "FT", "match", "H-1>A", "BINARY_REGULATION_TIME", None)
    if k in {"eh_p1_away", "ah_away_m1_5"}:
        return CanonicalContract("FT_EH_P1_AWAY", "FT", "match", "A-1>H", "BINARY_REGULATION_TIME", None)
    if k in {"eh_p2_home", "ah_home_p1_5"}:
        return CanonicalContract("FT_HOME_PLUS_1_5", "FT", "match", "H+2>A", "BINARY_REGULATION_TIME", None)
    if k in {"eh_m2_away", "ah_away_p1_5"}:
        return CanonicalContract("FT_AWAY_PLUS_1_5", "FT", "match", "A+2>H", "BINARY_REGULATION_TIME", None)
    if k in {"eh_m2_home", "ah_home_m2_5"}:
        return CanonicalContract("FT_HOME_BY_3_PLUS", "FT", "match", "H-2>A", "BINARY_REGULATION_TIME", None)
    if k in {"eh_p2_away", "ah_away_m2_5"}:
        return CanonicalContract("FT_AWAY_BY_3_PLUS", "FT", "match", "A-2>H", "BINARY_REGULATION_TIME", None)
    if k in {"eh_p3_home", "ah_home_p2_5"}:
        return CanonicalContract("FT_HOME_PLUS_2_5", "FT", "match", "H+3>A", "BINARY_REGULATION_TIME", None)
    if k in {"eh_m3_away", "ah_away_p2_5"}:
        return CanonicalContract("FT_AWAY_PLUS_2_5", "FT", "match", "A+3>H", "BINARY_REGULATION_TIME", None)
    if k == "eh_m1_draw":
        return CanonicalContract("FT_EH_M1_DRAW", "FT", "match", "H-1==A", "THREE_WAY_EUROPEAN_HANDICAP", None)

    m_es = _EXACT_SCORE_KEY_RE.match(k)
    if m_es:
        per = "H1" if m_es.group(1) else "FT"
        rule = "BINARY_FIRST_HALF" if per == "H1" else "BINARY_REGULATION_TIME"
        return CanonicalContract(
            contract_id=f"{per}_EXACT_{m_es.group(2)}_{m_es.group(3)}",
            period=per,
            scope="match",
            predicate=f"{per}_H=={m_es.group(2)} and {per}_A=={m_es.group(3)}",
            settlement_rule=rule,
            push_condition=None,
        )

    m_ah = _AH_HALF_KEY_RE.match(k)
    if m_ah:
        side, sign_ch, whole_str = m_ah.groups()
        val = (int(whole_str) + 0.5) * (-1.0 if sign_ch == "m" else 1.0)
        pred = f"H+({val})>A" if side == "home" else f"A+({val})>H"
        return CanonicalContract(f"FT_AH_{side.upper()}_{sign_ch.upper()}{whole_str}_5", "FT", "match", pred, "BINARY_REGULATION_TIME", None)

    m_eh = _EH_KEY_RE.match(k)
    if m_eh:
        tag, side = m_eh.groups()
        val = int(tag[1:]) * (-1 if tag.startswith("m") else 1)
        pred = f"H+({val})>A" if side == "home" else f"H+({val})==A" if side == "draw" else f"H+({val})<A"
        rule = "THREE_WAY_EUROPEAN_HANDICAP" if side == "draw" else "BINARY_REGULATION_TIME"
        return CanonicalContract(f"FT_EH_{tag.upper()}_{side.upper()}", "FT", "match", pred, rule, None)

    return None


@dataclass(frozen=True)
class EquivalentPriceGroup:
    contract_id: str
    period: str
    predicate: str
    settlement_rule: str
    quotes: tuple[dict[str, Any], ...]
    best_selection_key: str
    best_odd: float
    worst_selection_key: str
    worst_odd: float
    price_spread_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "period": self.period,
            "predicate": self.predicate,
            "settlement_rule": self.settlement_rule,
            "quotes": list(self.quotes),
            "best_selection_key": self.best_selection_key,
            "best_odd": self.best_odd,
            "worst_selection_key": self.worst_selection_key,
            "worst_odd": self.worst_odd,
            "price_spread_pct": self.price_spread_pct,
        }


def audit_snapshot_equivalences(odds_map: dict[str, float]) -> dict[str, Any]:
    """Identifica contratos equivalentes cotizados simultáneamente y la mejor cuota disponible."""
    grouped: dict[tuple[str, str, str, str, str | None], list[tuple[str, float, CanonicalContract]]] = {}
    for skey, raw_odd in (odds_map or {}).items():
        if raw_odd is None or raw_odd <= 1.0:
            continue
        contract = canonicalize_selection_key(skey)
        if contract is None:
            continue
        sig = (contract.period, contract.scope, contract.predicate, contract.settlement_rule, contract.push_condition)
        grouped.setdefault(sig, []).append((skey, float(raw_odd), contract))

    groups: list[EquivalentPriceGroup] = []
    suboptimal_map: dict[str, dict[str, Any]] = {}
    for sig, entries in grouped.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries, key=lambda x: (-x[1], x[0]))
        best_k, best_o, c0 = ordered[0]
        worst_k, worst_o, _ = ordered[-1]
        spread = round(((best_o / worst_o) - 1.0) * 100.0, 2) if worst_o > 0 else 0.0
        grp = EquivalentPriceGroup(
            contract_id=c0.contract_id,
            period=c0.period,
            predicate=c0.predicate,
            settlement_rule=c0.settlement_rule,
            quotes=tuple({"selection_key": k, "odd": o} for k, o, _ in ordered),
            best_selection_key=best_k,
            best_odd=best_o,
            worst_selection_key=worst_k,
            worst_odd=worst_o,
            price_spread_pct=spread,
        )
        groups.append(grp)
        for k, o, _ in ordered:
            if o < best_o:
                suboptimal_map[k] = {
                    "contract_id": c0.contract_id,
                    "better_selection_key": best_k,
                    "better_odd": best_o,
                    "price_gain_pct": round(((best_o / o) - 1.0) * 100.0, 2),
                }

    groups.sort(key=lambda g: (-g.price_spread_pct, g.contract_id))
    return {
        "equivalent_groups_count": len(groups),
        "equivalent_groups": [g.to_dict() for g in groups],
        "suboptimal_selections": suboptimal_map,
    }
