"""TACTICAL_MARKET_MATCHER (Fase 2 - Tarea C).

Concilia las hipótesis futbolísticas independientes de EARLY_TACTICAL_SCOUT con
el catálogo comercial real de Bwin y la evidencia estadística disponible, sin
limitarse a los mercados modelados por Python ni fabricar explicaciones tácticas.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def _norm(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _families_for_hypothesis(related_family: str, mechanism_text: str) -> tuple[set[str], set[str]]:
    """Mapea familia semántica de hipótesis a familias principales y alternativas próximas en Bwin."""
    raw = _norm(f"{related_family} {mechanism_text}")
    primary: set[str] = set()
    nearby: set[str] = set()

    if "corner" in raw:
        if "local" in raw or "home" in raw or "sao paulo" in raw:
            primary.update({"CORNERS_TEAM"})
            nearby.update({"CORNERS_TOTAL", "CORNERS_TEAM_H1", "CORNERS_TOTAL_H1", "PLAYER_SHOTS"})
        elif "visitante" in raw or "away" in raw or "fluminense" in raw:
            primary.update({"CORNERS_TEAM"})
            nearby.update({"CORNERS_TOTAL", "CORNERS_TEAM_H1", "CORNERS_TOTAL_H1"})
        else:
            primary.update({"CORNERS_TOTAL", "CORNERS_TEAM"})
            nearby.update({"CORNERS_TOTAL_H1", "CORNERS_TEAM_H1"})

    if "remate" in raw or "tiro" in raw or "shot" in raw:
        primary.update({"PLAYER_SHOTS", "PLAYER_ON_TARGET"})
        nearby.update({"GOALS_FT", "GOALS_TEAM", "CORNERS_TEAM", "CORNERS_TOTAL"})

    if "falta" in raw or "tarjeta" in raw or "foul" in raw or "card" in raw:
        if "falta" in raw or "foul" in raw:
            primary.add("PLAYER_FOULS_COMMITTED")
            nearby.update({"CARDS_TOTAL", "CARDS_FT_HOME", "CARDS_FT_AWAY"})
        if "tarjeta" in raw or "card" in raw:
            primary.add("CARDS_TOTAL")
            nearby.update({"PLAYER_FOULS_COMMITTED", "CARDS_FT_HOME", "CARDS_FT_AWAY"})

    if "gol" in raw or "btts" in raw or "ambos" in raw or "produccion_ofensiva" in raw:
        primary.update({"GOALS_FT", "GOALS_TEAM", "BTTS"})
        nearby.update({"GOALS_H1", "PLAYER_SHOTS", "PLAYER_ON_TARGET"})

    return primary, nearby


def _filter_entries(
    entries: list[dict[str, Any]],
    *,
    families: set[str],
    team_players_text: str,
) -> list[dict[str, Any]]:
    tp_norm = _norm(team_players_text)
    matched: list[dict[str, Any]] = []
    for e in entries:
        if e.get("market_family") not in families:
            continue
        if e.get("odd") is None:
            continue
        # If player prop, filter to mentioned players when specific players are named in team_and_players
        if e.get("target_scope") == "PLAYER":
            ent = _norm(e.get("target_entity"))
            ent_tokens = [t for t in ent.split() if len(t) >= 4]
            if ent_tokens and not any(t in tp_norm for t in ent_tokens):
                # Still include if no player tokens matched at all? No: keep player-specific matches focused
                continue
        matched.append(e)
    return matched


def match_hypotheses_to_catalog(
    *,
    scout_report: dict[str, Any],
    bwin_catalog: dict[str, Any],
    evidence_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Concilia cada hipótesis táctica con las líneas reales de Bwin y evidencia disponible."""
    ident = scout_report.get("identity") or {}
    analysis = scout_report.get("analysis") or {}
    hypotheses = analysis.get("hypotheses") or []
    entries = bwin_catalog.get("entries") or []
    contradictions = scout_report.get("contradictions") or []

    links: list[dict[str, Any]] = []
    unmatched_hypotheses: list[int] = []

    for idx, h in enumerate(hypotheses, start=1):
        rel_fam = str(h.get("related_market_family") or "")
        mech = str(h.get("tactical_mechanism") or "")
        tp = str(h.get("team_and_players") or "")
        primary_fams, nearby_fams = _families_for_hypothesis(rel_fam, mech)

        offered = _filter_entries(entries, families=primary_fams, team_players_text=tp)
        nearby = _filter_entries(entries, families=nearby_fams - primary_fams, team_players_text=tp)

        if not offered and not nearby:
            unmatched_hypotheses.append(idx)

        links.append({
            "hypothesis_index": idx,
            "related_market_family": rel_fam,
            "evidence_quality": h.get("evidence_quality"),
            "tactical_mechanism": mech,
            "necessary_conditions": h.get("necessary_conditions"),
            "supporting_stats": h.get("supporting_stats"),
            "contradictory_evidence": h.get("contradictory_evidence"),
            "cross_source_contradictions": contradictions,
            "missing_data": h.get("pending_info"),
            "primary_families_resolved": sorted(primary_fams),
            "nearby_families_resolved": sorted(nearby_fams),
            "offered_bwin_lines_count": len(offered),
            "offered_bwin_lines": offered,
            "nearby_alternatives_count": len(nearby),
            "nearby_alternatives": nearby[:25],
            "match_status": "MATCHED" if offered else ("NEARBY_ONLY" if nearby else "UNSUPPORTED_MARKET"),
            "opponent_contrast_available": bool(evidence_bundle and evidence_bundle.get("opponent_contrast")),
        })

    return {
        "schema_version": "1.0-tactical-market-matcher",
        "identity": ident,
        "bwin_source_sha256": bwin_catalog.get("source_txt_sha256"),
        "bwin_captured_at_utc": bwin_catalog.get("captured_at_utc"),
        "hypotheses_total": len(hypotheses),
        "matched_count": sum(1 for l in links if l["match_status"] == "MATCHED"),
        "unmatched_indices": unmatched_hypotheses,
        "links": links,
    }
