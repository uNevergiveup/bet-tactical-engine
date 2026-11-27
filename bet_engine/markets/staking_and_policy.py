"""Política cuantitativa anti-fugas, devigging de mercado, estimador de props por 90',
desduplicación por correlación intra-partido y dimensionamiento de Stake (Kelly Fraccional).

Principios de diseño:
1. Sin llamadas de red ni dependencias externas (Python >= 3.10 stdlib).
2. Elimina las 3 fugas empíricas identificadas (Córners UNDER con muestra corta,
   1ª Parte sin desglose suficiente y 1X2/DNB/DC sin control de salto de liga/Elo).
3. Destraba las 2 familias con ventaja real (GOALS_FT con n>=12 y PLAYER_PROPS
   titulares confirmados con tasa por 90' regularizada).
4. Desduplica contratos correlacionados del mismo partido (ej. Equipo +1.5 goles vs
   Total +3.5 goles) permitiendo hasta 2 selecciones por partido solo en ejes
   independientes y con tope máximo de exposición por encuentro.
"""

from __future__ import annotations

import math
import re
from typing import Any

DEFAULT_BANKROLL: float = 1000.0
DEFAULT_CURRENCY: str = "EUR"
DEFAULT_KELLY_FRACTION: float = 0.15
MIN_SINGLE_STAKE_PCT: float = 0.50
MAX_SINGLE_STAKE_PCT: float = 2.00
MAX_MATCH_EXPOSURE_PCT: float = 3.00
MAX_PICKS_PER_MATCH: int = 2
MAX_ACCEPTABLE_OVERROUND: float = 0.105

_OU_PAIR_RE = re.compile(
    r"^((?:h1_)?(?:(?:home|away)_)?)(over|under)_(\d+_\d+_(?:goals|corners|cards|shots|on_target|fouls))$"
)
_AH_PAIR_RE = re.compile(r"^ah_(home|away)_(m|p)(\d+_5)$")
_MIN_ODD_TEXT_RE = re.compile(
    r"(?:debajo\s+de|inferior\s+a|menor\s+a|bajo\s+el\s+piso(?:\s+de)?|piso\s+de|ca[ií]da[^.;]{0,35}?(?:a|bajo)\s+)\s*(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)

_CROSS_LEAGUE_KEYWORDS = (
    "champions league",
    "europa league",
    "conference league",
    "libertadores",
    "sudamericana",
    "club world cup",
    "mundial de clubes",
    "concacaf champions",
    "afc champions",
    "caf champions",
    "super cup",
    "recopa",
)

# Priors Empirical Bayes por 90 minutos según familia de prop y posición API-Football (F/M/D/G)
_PLAYER_PRIOR_PER90: dict[str, dict[str, float]] = {
    "PLAYER_SHOTS": {"F": 2.35, "M": 1.45, "D": 0.65, "G": 0.05, "DEFAULT": 1.55},
    "PLAYER_ON_TARGET": {"F": 0.88, "M": 0.48, "D": 0.20, "G": 0.02, "DEFAULT": 0.55},
    "PLAYER_FOULS_COMMITTED": {"F": 1.25, "M": 1.55, "D": 1.35, "G": 0.10, "DEFAULT": 1.40},
    "PLAYER_GOAL_OR_ASSIST": {"F": 0.46, "M": 0.27, "D": 0.13, "G": 0.01, "DEFAULT": 0.28},
    "PLAYER_ASSISTS": {"F": 0.18, "M": 0.16, "D": 0.08, "G": 0.01, "DEFAULT": 0.14},
}
_PLAYER_SHRINK_MINUTES: float = 180.0  # Equivalente a 2 partidos completos de 90'
_PLAYER_NB_DISPERSION: float = 9.0


def is_cross_league_club_competition(league_name: str | None, country: str | None = None) -> bool:
    """Detecta torneos internacionales de clubes donde las tasas domésticas no son comparables sin Elo."""
    txt = f"{league_name or ''} {country or ''}".strip().lower()
    if not txt:
        return False
    return any(kw in txt for kw in _CROSS_LEAGUE_KEYWORDS)


def extract_min_acceptable_odd(candidate_item: dict[str, Any] | None) -> float | None:
    """Obtiene el piso mínimo de cuota explícito (`min_odd`) o declarado en `lineup_condition`."""
    if not isinstance(candidate_item, dict):
        return None
    raw_explicit = candidate_item.get("min_odd")
    if isinstance(raw_explicit, (int, float)) and not isinstance(raw_explicit, bool) and raw_explicit > 1.0:
        return round(float(raw_explicit), 3)
    for field in ("lineup_condition", "case_for"):
        text = str(candidate_item.get(field) or "")
        matches = _MIN_ODD_TEXT_RE.findall(text)
        for m in matches:
            try:
                val = float(m.replace(",", "."))
                if 1.05 <= val <= 10.0:
                    return round(val, 3)
            except ValueError:
                continue
    return None


def compute_market_devig_map(odds_map: dict[str, float] | None) -> dict[str, dict[str, Any]]:
    """Calcula margen real (overround) y probabilidad implícita sin margen (devig proporcional)
    para todos los mercados completos de 2 y 3 vías presentes en `odds_map`.
    """
    if not odds_map:
        return {}
    clean: dict[str, float] = {
        k: float(v)
        for k, v in odds_map.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 1.0
    }
    out: dict[str, dict[str, Any]] = {}

    def _register_group(keys: tuple[str, ...]) -> None:
        if not all(k in clean for k in keys):
            return
        inv = [1.0 / clean[k] for k in keys]
        total_inv = sum(inv)
        if total_inv <= 0.98:
            return
        overround = round(total_inv - 1.0, 4)
        n_way = len(keys)
        for idx, k in enumerate(keys):
            opp = clean[keys[1 - idx]] if n_way == 2 else None
            out[k] = {
                "p_market_fair": round(inv[idx] / total_inv, 4),
                "overround": overround,
                "opposite_odd": round(opp, 3) if opp is not None else None,
                "market_way": n_way,
            }

    # Mercados fijos 3 vías y 2 vías
    _register_group(("home_win", "draw", "away_win"))
    _register_group(("h1_home_win", "h1_draw", "h1_away_win"))
    _register_group(("btts_yes", "btts_no"))
    _register_group(("h1_btts_yes", "h1_btts_no"))
    _register_group(("dnb_home", "dnb_away"))
    _register_group(("h1_dnb_home", "h1_dnb_away"))

    # Pares dinámicos Over/Under (.5) y Hándicap 1-2 (.5)
    visited: set[str] = set(out.keys())
    for key in sorted(clean.keys()):
        if key in visited:
            continue
        m_ou = _OU_PAIR_RE.match(key)
        if m_ou:
            pref, side_dir, suffix = m_ou.groups()
            opp_dir = "under" if side_dir == "over" else "over"
            opp_key = f"{pref}{opp_dir}_{suffix}"
            if opp_key in clean:
                _register_group((key, opp_key))
                visited.update((key, opp_key))
            continue
        m_ah = _AH_PAIR_RE.match(key)
        if m_ah:
            side, sign, line_slug = m_ah.groups()
            opp_side = "away" if side == "home" else "home"
            opp_sign = "p" if sign == "m" else "m"
            opp_key = f"ah_{opp_side}_{opp_sign}{line_slug}"
            if opp_key in clean:
                _register_group((key, opp_key))
                visited.update((key, opp_key))

    return out


def correlation_axis_for_family(market_family: str | None) -> str:
    """Agrupa familias en ejes de riesgo intra-partido para impedir doble exposición correlacionada."""
    fam = str(market_family or "").upper()
    if any(tok in fam for tok in ("GOALS", "BTTS", "MATCH_RESULT", "DOUBLE_CHANCE", "DRAW_NO_BET", "HANDICAP", "GOAL_OR_ASSIST", "ASSISTS")):
        return "GOALS_AXIS"
    if "CORNER" in fam:
        return "CORNERS_AXIS"
    if any(tok in fam for tok in ("SHOT", "ON_TARGET")):
        return "PLAYER_SHOTS_AXIS"
    if "FOUL" in fam:
        return "PLAYER_FOULS_AXIS"
    if "CARD" in fam or "YELLOW" in fam:
        return "CARDS_AXIS"
    return f"OTHER_AXIS:{fam or 'UNKNOWN'}"


def _nb_prob_at_least(rate: float, threshold: int, dispersion: float = _PLAYER_NB_DISPERSION) -> float:
    if threshold <= 0:
        return 1.0
    if rate <= 0.0:
        return 0.0
    p = dispersion / (dispersion + rate)
    cdf = 0.0
    for k in range(threshold):
        log_pmf = (
            math.lgamma(k + dispersion)
            - math.lgamma(dispersion)
            - math.lgamma(k + 1)
            + dispersion * math.log(p)
            + k * math.log1p(-p)
        )
        cdf += math.exp(log_pmf)
    return max(0.001, min(0.995, 1.0 - cdf))


def evaluate_player_prop_quant(
    player_evidence: dict[str, Any] | None,
    *,
    odd: float | None,
    market_family: str,
    lineup_role: str | None = None,
    lineup_confirmed: bool = False,
    min_acceptable_odd: float | None = None,
) -> dict[str, Any]:
    """Modela tasa por 90' con regularización Empirical Bayes y distribución Binomial Negativa
    para props de jugador (Tiros, SOT, Faltas, G+A), exigiendo titularidad oficial al cierre.
    """
    fam = str(market_family or "").upper()
    if not player_evidence or odd is None or odd <= 1.0:
        return {
            "status": "SIN_EVIDENCIA_SUFICIENTE_JUGADOR",
            "bet_authorized": False,
            "correlation_axis": correlation_axis_for_family(fam),
            "p_model": None,
            "ev_model": None,
        }

    if fam == "PLAYER_CARDS":
        return {
            "status": "NO_ACCIONABLE_TARJETAS_SOLO_PROXY_AMARILLAS",
            "bet_authorized": False,
            "correlation_axis": correlation_axis_for_family(fam),
            "p_model": None,
            "ev_model": None,
        }

    sample = list(player_evidence.get("sample") or [])
    threshold = int(player_evidence.get("threshold_at_least") or 1)
    starters = [g for g in sample if g.get("role") == "STARTER" and (g.get("minutes") or 0) >= 45]
    usable = starters if len(starters) >= 4 else [g for g in sample if (g.get("minutes") or 0) >= 25]
    total_mins = float(sum(float(g.get("minutes") or 0) for g in usable))
    total_events = float(sum(float(g.get("value") or 0) for g in usable))
    starter_n = len(starters)
    n_valid = int(player_evidence.get("n_valid") or len(sample))
    n_missing = int(player_evidence.get("n_missing_metric_in_observed_appearances") or 0)

    if len(usable) < 4 or total_mins < 240.0:
        return {
            "status": "MUESTRA_MINUTOS_INSUFICIENTE_JUGADOR",
            "bet_authorized": False,
            "correlation_axis": correlation_axis_for_family(fam),
            "starter_sample_n": starter_n,
            "usable_minutes": round(total_mins, 1),
            "p_model": None,
            "ev_model": None,
        }

    positions = [str(g.get("position") or "").strip().upper()[:1] for g in usable if g.get("position")]
    dom_pos = max(set(positions), key=positions.count) if positions else "DEFAULT"
    fam_priors = _PLAYER_PRIOR_PER90.get(fam, _PLAYER_PRIOR_PER90["PLAYER_SHOTS"])
    prior_per90 = fam_priors.get(dom_pos, fam_priors["DEFAULT"])

    raw_per90 = (total_events / total_mins) * 90.0
    shrunk_per90 = (
        (total_mins * raw_per90 + _PLAYER_SHRINK_MINUTES * prior_per90)
        / (total_mins + _PLAYER_SHRINK_MINUTES)
    )
    avg_starter_mins = (
        sum(float(g["minutes"]) for g in starters) / len(starters)
        if starters
        else (total_mins / len(usable))
    )
    expected_mins = max(65.0, min(90.0, avg_starter_mins))
    expected_rate = shrunk_per90 * (expected_mins / 90.0)

    # Intervalo aproximado Gamma sobre la tasa posterior (penalizando ausencias en el borde inferior)
    conservative_mins = total_mins + (n_missing * 60.0 if n_missing > 0 else 0.0)
    conservative_shrunk_per90 = (
        (total_events * 90.0 + _PLAYER_SHRINK_MINUTES * prior_per90)
        / (conservative_mins + _PLAYER_SHRINK_MINUTES)
    )
    conservative_expected_rate = conservative_shrunk_per90 * (expected_mins / 90.0)
    eff_matches_90 = (total_mins + _PLAYER_SHRINK_MINUTES) / 90.0
    alpha_post = max(1.0, expected_rate * eff_matches_90)
    se_rate = expected_rate / math.sqrt(alpha_post)
    rate_lo = max(0.05, min(expected_rate, conservative_expected_rate) - 1.28 * se_rate)
    rate_hi = expected_rate + 1.28 * se_rate

    p_mod = round(_nb_prob_at_least(expected_rate, threshold), 4)
    p_lo = round(_nb_prob_at_least(rate_lo, threshold), 4)
    p_hi = round(_nb_prob_at_least(rate_hi, threshold), 4)
    fair_odd = round(1.0 / p_mod, 3) if p_mod > 1e-6 else None
    be_prob = round(1.0 / float(odd), 4)
    ev_mod = round(p_mod * float(odd) - 1.0, 4)
    ev_lo = round(p_lo * float(odd) - 1.0, 4)

    # Reglas anti-fuga específicas por subtipo de prop y control de sesgo por datos ausentes
    blocks: list[str] = []
    total_appearances = n_valid + max(0, n_missing)
    missing_ratio = (n_missing / total_appearances) if total_appearances > 0 else 0.0
    has_zero_observed = any(float(g.get("value") or 0) == 0.0 for g in usable)
    if n_missing > 0 and (missing_ratio > 0.20 or (n_missing >= 2 and not has_zero_observed)):
        blocks.append(f"SESGO_SELECCION_METRICA_INCOMPLETA(miss={n_missing}/{total_appearances})")
    if min_acceptable_odd is not None and float(odd) < min_acceptable_odd:
        blocks.append(f"CUOTA_BAJO_PISO_DECLARADO({odd:.2f}<{min_acceptable_odd:.2f})")
    if n_valid < 6 or total_mins < 360.0:
        blocks.append(f"MUESTRA_CORTA_PROP(n={n_valid},min={int(total_mins)})")
    if fam == "PLAYER_ON_TARGET":
        # En 1+ SOT evitar laterales/defensas de cuota alta y baja frecuencia (ej. Renan Lodi @2.40)
        if dom_pos == "D" and float(odd) > 2.05:
            blocks.append("FUGA_SOT_DEFENSA_CUOTA_ALTA")
        if float(odd) > 2.20:
            blocks.append(f"FUGA_SOT_BAJA_PROBABILIDAD(odd={odd:.2f}>2.20)")
        if ev_mod < 0.08:
            blocks.append(f"EV_PROP_SOT_INSUFICIENTE({ev_mod:+.3f}<+0.080)")
    else:
        if float(odd) > 3.25:
            blocks.append(f"CUOTA_PROP_COLA_ALTA({odd:.2f}>3.25)")
        if ev_mod < 0.06:
            blocks.append(f"EV_PROP_INSUFICIENTE({ev_mod:+.3f}<+0.060)")

    eff_n = round(total_mins / 90.0, 2)
    potential_stake = compute_kelly_stake_pct(
        ev_model=ev_mod,
        odd=float(odd),
        effective_n=eff_n,
        p_low=p_lo,
        p_high=p_hi,
    )

    if not lineup_confirmed:
        status = "CANDIDATO_PROP_PENDIENTE_XI" if not blocks else f"DESCARTADO_PROP:{blocks[0]}"
        return {
            "status": status,
            "bet_authorized": False,
            "requires_confirmed_starter": True,
            "correlation_axis": correlation_axis_for_family(fam),
            "position": dom_pos,
            "starter_sample_n": starter_n,
            "usable_minutes": round(total_mins, 1),
            "expected_starter_minutes": round(expected_mins, 1),
            "raw_rate_per90": round(raw_per90, 3),
            "shrunk_rate_per90": round(shrunk_per90, 3),
            "expected_match_rate": round(expected_rate, 3),
            "p_model": p_mod,
            "p_low": p_lo,
            "p_high": p_hi,
            "break_even_prob": be_prob,
            "fair_odd": fair_odd,
            "ev_model": ev_mod,
            "ev_conservative_low": ev_lo,
            "projected_stake_if_starter_pct": potential_stake if not blocks else 0.0,
            "blocking_reasons": blocks,
        }

    role_norm = str(lineup_role or "").strip().upper()
    if role_norm != "TITULAR":
        blocks.insert(0, f"VETO_JUGADOR_NO_TITULAR({role_norm or 'NO_CONVOCADO'})")

    authorized = len(blocks) == 0 and potential_stake >= MIN_SINGLE_STAKE_PCT
    if not blocks and potential_stake < MIN_SINGLE_STAKE_PCT:
        blocks.append("EV_INSUFICIENTE_PARA_STAKE_MINIMO")

    return {
        "status": "BET_PROP_TITULAR_AUTORIZADO" if authorized else f"NO_BET_PROP:{blocks[0]}",
        "bet_authorized": authorized,
        "lineup_role_verified": role_norm or "DESCONOCIDO",
        "correlation_axis": correlation_axis_for_family(fam),
        "position": dom_pos,
        "starter_sample_n": starter_n,
        "usable_minutes": round(total_mins, 1),
        "expected_starter_minutes": round(expected_mins, 1),
        "raw_rate_per90": round(raw_per90, 3),
        "shrunk_rate_per90": round(shrunk_per90, 3),
        "expected_match_rate": round(expected_rate, 3),
        "p_model": p_mod,
        "p_low": p_lo,
        "p_high": p_hi,
        "break_even_prob": be_prob,
        "fair_odd": fair_odd,
        "ev_model": ev_mod,
        "ev_conservative_low": ev_lo,
        "raw_kelly_stake_pct": potential_stake if authorized else 0.0,
        "blocking_reasons": blocks,
    }


def evaluate_collective_quant_policy(
    *,
    market_family: str,
    direction: str | None,
    temporal_scope: str | None,
    odd: float | None,
    p_model: float | None,
    p_low: float | None,
    p_high: float | None,
    raw_sample_n: int | None,
    effective_sample_n: float | None = None,
    devig_info: dict[str, Any] | None = None,
    is_cross_league: bool = False,
    tactical_warnings: tuple[str, ...] | list[str] = (),
    min_acceptable_odd: float | None = None,
    settlement_verified: bool = True,
) -> dict[str, Any]:
    """Aplica reglas anti-fuga, control de margen (devigging) y autorización cuantitativa
    sobre mercados colectivos de equipo/partido.
    """
    fam = str(market_family or "").upper()
    dir_up = str(direction or "").upper()
    scope_t = str(temporal_scope or "FT").upper()
    axis = correlation_axis_for_family(fam)

    if odd is None or odd <= 1.0 or p_model is None:
        return {
            "status": "SIN_MODELO_O_PRECIO",
            "bet_authorized": False,
            "leak_guard_blocked": False,
            "correlation_axis": axis,
            "p_model": p_model,
            "ev_model": None,
            "blocking_reasons": ["SIN_MODELO_PROBABILISTICO_O_CUOTA"],
        }

    odd_f = float(odd)
    be_prob = round(1.0 / odd_f, 4)
    fair_odd = round(1.0 / p_model, 3) if p_model > 1e-6 else None
    ev_mod = round(p_model * odd_f - 1.0, 4)
    ev_lo = round(p_low * odd_f - 1.0, 4) if p_low is not None else None
    n_raw = int(raw_sample_n or 0)
    n_eff = float(effective_sample_n if effective_sample_n is not None else n_raw)

    p_mkt_fair = (devig_info or {}).get("p_market_fair")
    overround = (devig_info or {}).get("overround")
    opp_odd = (devig_info or {}).get("opposite_odd")
    mkt_diff = round(p_model - float(p_mkt_fair), 4) if p_mkt_fair is not None else None

    # Anclaje Bayesiano al mercado (Market-Prior Shrinkage) en cruces inter-liga o divergencias altas
    # para descontar inflación de ligas menores (ej. Georgia vs LaLiga) sin destruir ventajas reales.
    has_comp_mix = any("COMPETITION_MIX" in str(w) for w in tactical_warnings)
    p_eff = float(p_model)
    p_lo_eff = float(p_low) if p_low is not None else None
    if p_mkt_fair is not None:
        if is_cross_league:
            p_eff = round(0.55 * float(p_model) + 0.45 * float(p_mkt_fair), 4)
            if p_lo_eff is not None:
                p_lo_eff = round(0.55 * p_lo_eff + 0.45 * float(p_mkt_fair), 4)
        elif mkt_diff is not None and mkt_diff > 0.18:
            p_eff = round(0.75 * float(p_model) + 0.25 * float(p_mkt_fair), 4)

    fair_odd = round(1.0 / p_eff, 3) if p_eff > 1e-6 else None
    ev_mod = round(p_eff * odd_f - 1.0, 4)
    ev_lo = round(p_lo_eff * odd_f - 1.0, 4) if p_lo_eff is not None else None

    blocks: list[str] = []
    leak_blocked = False

    if not settlement_verified:
        blocks.append("LIQUIDACION_COMERCIAL_NO_VERIFICADA")
    if min_acceptable_odd is not None and odd_f < min_acceptable_odd:
        blocks.append(f"CUOTA_BAJO_PISO_DECLARADO({odd_f:.2f}<{min_acceptable_odd:.2f})")

    # 1. Frenos de las 3 fugas estructurales demostradas en las 60 muestras
    if scope_t != "FT" or "H1" in fam or "H2" in fam:
        if n_raw < 14 or (ev_lo is None or ev_lo < 0.03):
            leak_blocked = True
            blocks.append(f"FUGA_PERIODO_PARCIAL_ALTA_VARIANZA(n={n_raw})")

    if "CORNER" in fam:
        if dir_up == "UNDER":
            if n_raw < 14 or (ev_lo is None or ev_lo < 0.03):
                leak_blocked = True
                blocks.append(f"FUGA_CORNERS_UNDER_COLA_DERECHA(n={n_raw}<14)")
        else:
            if n_raw < 10:
                leak_blocked = True
                blocks.append(f"MUESTRA_DETALLADA_CORTA_CORNERS(n={n_raw}<10)")
            elif ev_mod < 0.06:
                blocks.append(f"EV_CORNERS_INSUFICIENTE({ev_mod:+.3f}<+0.060)")

    if any(tok in fam for tok in ("MATCH_RESULT", "DOUBLE_CHANCE", "DRAW_NO_BET", "HANDICAP")):
        if is_cross_league:
            leak_blocked = True
            blocks.append("FUGA_1X2_INTERLIGA_SIN_AJUSTE_ELO")
        elif n_raw < 14 or (ev_lo is None or ev_lo < 0.02):
            leak_blocked = True
            blocks.append(f"FUGA_1X2_MERCADO_EFICIENTE_SIN_MARGEN_CONSERVADOR(n={n_raw})")

    if any(tok in fam for tok in ("CARD", "YELLOW", "SHOT", "ON_TARGET", "FOUL")) and n_raw < 12:
        leak_blocked = True
        blocks.append(f"MUESTRA_DETALLADA_INSUFICIENTE_FAMILIA_SECUNDARIA(n={n_raw}<12)")

    # 2. Control de margen de casa (Devigging) y freno anti-cola extrema
    if overround is not None and overround > MAX_ACCEPTABLE_OVERROUND:
        blocks.append(f"MARGEN_CASA_EXCESIVO({overround * 100:.1f}%>{MAX_ACCEPTABLE_OVERROUND * 100:.1f}%)")

    if odd_f > 4.20 or (mkt_diff is not None and mkt_diff > 0.34 and odd_f > 2.80):
        leak_blocked = True
        blocks.append(f"COLA_EXTREMA_NO_ACCIONABLE(odd={odd_f:.2f})")

    # 3. Regla de autorización principal para GOALS_FT (la familia más sólida) y colectivos limpios
    is_goals_ft = fam in {"GOALS_FT", "GOALS_TOTAL", "GOALS_TEAM"} and scope_t == "FT"
    if is_goals_ft:
        min_n_goals = 10 if not is_cross_league else 12
        if n_raw < min_n_goals:
            blocks.append(f"MUESTRA_GOLES_INSUFICIENTE(n={n_raw}<{min_n_goals})")
        if ev_mod < 0.05:
            blocks.append(f"EV_CENTRAL_INSUFICIENTE({ev_mod:+.3f}<+0.050)")
        if ev_lo is not None and ev_lo < -0.15:
            blocks.append(f"INCERTIDUMBRE_INFERIOR_EXCESIVA(ev_low={ev_lo:+.3f}<-0.150)")
    else:
        if ev_mod < 0.06 and not blocks:
            blocks.append(f"EV_CENTRAL_INSUFICIENTE({ev_mod:+.3f}<+0.060)")

    stake_pct = compute_kelly_stake_pct(
        ev_model=ev_mod,
        odd=odd_f,
        effective_n=n_eff,
        p_low=p_lo_eff,
        p_high=p_high,
    )
    if not blocks and stake_pct < MIN_SINGLE_STAKE_PCT:
        blocks.append("EV_INSUFICIENTE_PARA_STAKE_MINIMO")

    authorized = len(blocks) == 0 and stake_pct >= MIN_SINGLE_STAKE_PCT
    status = (
        "BET_COLECTIVO_AUTORIZADO"
        if authorized
        else ("BLOQUEADO_FUGA_ESTRUCTURAL" if leak_blocked else f"NO_BET:{blocks[0]}")
    )

    return {
        "status": status,
        "bet_authorized": authorized,
        "leak_guard_blocked": leak_blocked,
        "correlation_axis": axis,
        "p_model_raw": round(p_model, 4),
        "p_model": round(p_eff, 4),
        "p_low": round(p_lo_eff, 4) if p_lo_eff is not None else None,
        "p_high": round(p_high, 4) if p_high is not None else None,
        "p_market_fair": p_mkt_fair,
        "market_overround": overround,
        "opposite_odd": opp_odd,
        "model_minus_market_fair": mkt_diff,
        "break_even_prob": be_prob,
        "fair_odd": fair_odd,
        "ev_model": ev_mod,
        "ev_conservative_low": ev_lo,
        "raw_sample_n": n_raw,
        "effective_sample_n": round(n_eff, 2),
        "raw_kelly_stake_pct": stake_pct if authorized else 0.0,
        "blocking_reasons": blocks,
    }


def compute_kelly_stake_pct(
    *,
    ev_model: float | None,
    odd: float | None,
    effective_n: float | None,
    p_low: float | None = None,
    p_high: float | None = None,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    max_single_pct: float = MAX_SINGLE_STAKE_PCT,
) -> float:
    """Calcula el % de bankroll sugerido mediante Kelly Fraccional modulado por tamaño muestral
    y anchura del intervalo de incertidumbre. Devuelve 0.0 si EV <= 0.
    """
    if ev_model is None or odd is None or odd <= 1.02 or ev_model <= 0.0:
        return 0.0
    full_kelly = ev_model / (odd - 1.0)
    n_val = max(1.0, float(effective_n or 6.0))
    conf_n = min(1.0, max(0.45, n_val / 14.0))
    width = (float(p_high) - float(p_low)) if (p_low is not None and p_high is not None and p_high >= p_low) else 0.20
    conf_width = max(0.55, min(1.0, 1.0 - 1.4 * max(0.0, width - 0.16)))
    pct = 100.0 * kelly_fraction * full_kelly * conf_n * conf_width
    if pct < MIN_SINGLE_STAKE_PCT:
        return 0.0
    return round(min(max_single_pct, pct), 2)


def allocate_match_portfolio_stakes(
    candidates: list[dict[str, Any]],
    *,
    bankroll: float = DEFAULT_BANKROLL,
    currency: str = DEFAULT_CURRENCY,
    max_match_exposure_pct: float = MAX_MATCH_EXPOSURE_PCT,
    max_picks_per_match: int = MAX_PICKS_PER_MATCH,
) -> dict[str, Any]:
    """Resuelve múltiples candidatos de un mismo partido sin sesgo subjetivo:
    1. Descarta duplicados en el mismo eje de correlación (ej. Equipo +1.5 goles y Total +3.5 goles),
       conservando únicamente el contrato con mejor score cuantitativo (EV * confianza).
    2. Permite hasta `max_picks_per_match` apuestas del mismo partido SOLO si pertenecen a ejes
       independientes (ej. Goles FT + Faltas de jugador, o Goles FT + Córners OVER).
    3. Aplica recorte por correlación parcial (0.80x) si coinciden Goles y Tiros de jugador,
       y reescala proporcionalmente para no superar nunca `max_match_exposure_pct` (ej. 3.0% del bank).
    """
    authorized_pool: list[tuple[int, dict[str, Any], float]] = []
    allocations: list[dict[str, Any]] = []

    for idx, cand in enumerate(candidates):
        q = cand.get("quant_decision") or {}
        if q.get("bet_authorized") and (q.get("raw_kelly_stake_pct") or 0.0) >= MIN_SINGLE_STAKE_PCT:
            ev = float(q.get("ev_model") or 0.0)
            stake_raw = float(q.get("raw_kelly_stake_pct") or 0.0)
            score = round(ev * stake_raw, 6)
            authorized_pool.append((idx, cand, score))
        allocations.append({
            "selection_id": cand.get("selection_id"),
            "market_header_literal": cand.get("market_header_literal"),
            "selection_literal": cand.get("selection_literal"),
            "odd": cand.get("odd"),
            "correlation_axis": q.get("correlation_axis"),
            "decision": "NO_BET",
            "reason": q.get("status") or "NO_AUTORIZADO",
            "ev_model": q.get("ev_model"),
            "p_model": q.get("p_model"),
            "fair_odd": q.get("fair_odd"),
            "stake_pct_bank": 0.0,
            "stake_units": 0.0,
            "stake_amount": 0.0,
            "currency": currency,
        })

    if not authorized_pool:
        return {
            "match_decision": "NO_BET",
            "authorized_picks_count": 0,
            "total_match_stake_pct": 0.0,
            "total_match_units": 0.0,
            "total_match_amount": 0.0,
            "bankroll_reference": bankroll,
            "currency": currency,
            "allocations": allocations,
        }

    # Ordenar por mejor score cuantitativo y quedarnos con máximo 1 contrato por eje de correlación
    ordered = sorted(authorized_pool, key=lambda item: (-item[2], -(item[1]["quant_decision"].get("ev_model") or 0.0)))
    chosen_by_axis: dict[str, tuple[int, dict[str, Any], float]] = {}
    for idx, cand, score in ordered:
        axis = str((cand.get("quant_decision") or {}).get("correlation_axis") or "OTHER")
        if axis in chosen_by_axis:
            winner = chosen_by_axis[axis][1]
            allocations[idx]["decision"] = "NO_BET_CORRELACIONADO"
            allocations[idx]["reason"] = (
                f"DESCARTADO_POR_CORRELACION_MISMO_EJE({axis}: superado por "
                f"{winner.get('market_header_literal')} -> {winner.get('selection_literal')})"
            )
            continue
        if len(chosen_by_axis) >= max_picks_per_match:
            allocations[idx]["decision"] = "NO_BET_TOPE_PICKS_PARTIDO"
            allocations[idx]["reason"] = f"TOPE_MAXIMO_{max_picks_per_match}_EJES_POR_PARTIDO_ALCANZADO"
            continue
        chosen_by_axis[axis] = (idx, cand, score)

    selected = list(chosen_by_axis.values())
    axes_present = set(chosen_by_axis.keys())

    # Recorte de 0.80x en tiros si también llevamos goles en el mismo encuentro (correlación parcial moderada)
    prelim_stakes: dict[int, float] = {}
    for idx, cand, _ in selected:
        raw_s = float(cand["quant_decision"]["raw_kelly_stake_pct"])
        axis = str(cand["quant_decision"].get("correlation_axis"))
        if axis == "PLAYER_SHOTS_AXIS" and "GOALS_AXIS" in axes_present:
            raw_s = round(raw_s * 0.80, 2)
        prelim_stakes[idx] = raw_s

    total_prelim = sum(prelim_stakes.values())
    scale = (max_match_exposure_pct / total_prelim) if total_prelim > max_match_exposure_pct else 1.0

    total_pct = 0.0
    total_amt = 0.0
    bet_count = 0
    for idx, cand, _ in selected:
        final_pct = round(prelim_stakes[idx] * scale, 2)
        if final_pct < MIN_SINGLE_STAKE_PCT:
            allocations[idx]["decision"] = "NO_BET"
            allocations[idx]["reason"] = "STAKE_POST_ESCALADO_BAJO_MINIMO"
            continue
        amt = round(bankroll * (final_pct / 100.0), 2)
        allocations[idx].update({
            "decision": "BET",
            "reason": cand["quant_decision"]["status"],
            "stake_pct_bank": final_pct,
            "stake_units": final_pct,
            "stake_amount": amt,
            "scaled_by_match_cap": scale < 1.0,
        })
        total_pct = round(total_pct + final_pct, 2)
        total_amt = round(total_amt + amt, 2)
        bet_count += 1

    return {
        "match_decision": "BET" if bet_count > 0 else "NO_BET",
        "authorized_picks_count": bet_count,
        "total_match_stake_pct": total_pct,
        "total_match_units": total_pct,
        "total_match_amount": total_amt,
        "bankroll_reference": bankroll,
        "currency": currency,
        "allocations": allocations,
    }
