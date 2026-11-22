"""TACTICAL_MARKET_MATCHER (Fase 2 - Tarea C).

Concilia las hipótesis futbolísticas independientes de EARLY_TACTICAL_SCOUT con
el catálogo comercial real de Bwin y la evidencia estadística disponible, sin
limitarse a los mercados modelados por Python ni fabricar explicaciones tácticas.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
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


def _corner_target(h, identity):
    """Resuelve el observable declarado, sin extraer el lado de la narrativa causal."""
    target = h.get("corner_target")
    label = _norm(h.get("related_market_family")).replace("_", " ")
    if target is not None:
        if isinstance(target, dict) and target.get("scope") in ("HOME", "AWAY", "MATCH") and target.get("period") in ("FT", "H1", "H2"):
            return (target["scope"], target["period"]), None
        return None, "INVALID_CORNER_TARGET"
    if "corner" not in label:
        return None, None
    if re.search(r"\b(ambas|ambos|mas corners|primer corner|minuto|par|impar|entre)\b", label):
        return None, "UNSUPPORTED_CORNER_CONTRACT_TYPE"
    sides = set()
    for scope, side, tokens in (("HOME", "home", "local|home"), ("AWAY", "away", "visitante|away")):
        name = _norm(identity.get(side))
        if re.search(rf"\b({tokens})\b", label) or (name and re.search(rf"\b{re.escape(name)}\b", label)):
            sides.add(scope)
    if re.search(r"\b(del partido|totales del partido|match|total)\b", label) and not sides:
        sides.add("MATCH")
    if len(sides) != 1:
        return None, "CORNER_SCOPE_UNRESOLVED"
    periods = {
        period for period, pattern in {
            "H1": r"\b(h1|1[ªa]? (?:parte|mitad)|primera (?:parte|mitad))\b",
            "H2": r"\b(h2|2[ªa]? (?:parte|mitad)|segunda (?:parte|mitad))\b",
            "FT": r"\b(ft|tiempo reglamentario|partido completo)\b",
        }.items() if re.search(pattern, label)
    }
    if len(periods) > 1:
        return None, "CORNER_PERIOD_UNRESOLVED"
    return (next(iter(sides)), next(iter(periods), "FT")), None


def _catalog_block(entry):
    verdict = entry.get("verdict")
    if verdict in {"INAPPLICABLE_PREMATCH", "CONFLICTING_CAPTURE_PRICES", "MAPPING_UNRESOLVED"}:
        return verdict
    if entry.get("odd") is None:
        return "PRICE_UNAVAILABLE"
    return None


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
        if _catalog_block(e):
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
    for key in ("event_id", "fixture_id"):
        if ident.get(key) is not None and bwin_catalog.get(key) is not None and str(ident[key]) != str(bwin_catalog[key]):
            raise ValueError(f"TACTICAL_MARKET_IDENTITY_MISMATCH:{key}")
    reverse = defaultdict(lambda: {"direct": [], "indirect": [], "legacy_family": []})

    for idx, h in enumerate(hypotheses, start=1):
        rel_fam = str(h.get("related_market_family") or "")
        mech = str(h.get("tactical_mechanism") or "")
        tp = str(h.get("team_and_players") or "")
        primary_fams, nearby_fams = _families_for_hypothesis(rel_fam, mech)

        target, corner_issue = _corner_target(h, ident)
        direct = []
        offered = _filter_entries(entries, families=primary_fams, team_players_text=tp)
        nearby = _filter_entries(entries, families=nearby_fams - primary_fams, team_players_text=tp)
        if target or corner_issue:
            nearby_fams = {family for family in nearby_fams if not family.startswith("CORNERS")}
            nearby = [entry for entry in nearby if entry.get("market_family") in nearby_fams]
            if target:
                scope, period = target
                family = "CORNERS_TOTAL" if scope == "MATCH" else "CORNERS_TEAM"
                family = family if period == "FT" else f"{family}_{period}"
                primary_fams.add(family)
                direct = [
                    e for e in entries if e.get("market_family") == family
                    and (e.get("target_scope"), e.get("temporal_scope")) == target
                    and e.get("direction") in {"over", "under"} and e.get("line") is not None
                    and not _catalog_block(e)
                ]

        for group, candidates in (("direct", direct), ("legacy_family", offered), ("indirect", nearby)):
            for entry in candidates:
                reverse[entry.get("selection_id")][group].append(idx)
        offered.extend(direct)

        if not offered and not nearby:
            unmatched_hypotheses.append(idx)

        links.append({
            "hypothesis_index": idx,
            "related_market_family": rel_fam,
            "evidence_quality": h.get("evidence_quality"),
            "tactical_mechanism": mech,
            "necessary_conditions": h.get("necessary_conditions"),
            "opponent_coach_response": h.get("opponent_coach_response"),
            "invalidation_scenarios": h.get("invalidation_scenarios"),
            "corner_target": {"scope": target[0], "period": target[1]} if target else None,
            "mapping_issue": corner_issue,
            "supporting_stats": h.get("supporting_stats"),
            "contradictory_evidence": h.get("contradictory_evidence"),
            "cross_source_contradictions": contradictions,
            "missing_data": h.get("pending_info"),
            "primary_families_resolved": sorted(primary_fams),
            "nearby_families_resolved": sorted(nearby_fams),
            "offered_bwin_lines_count": len(offered),
            "offered_bwin_lines": offered,
            "nearby_alternatives_count": len(nearby),
            "nearby_alternatives": nearby,
            "match_status": "MATCHED" if offered else ("NEARBY_ONLY" if nearby else "UNSUPPORTED_MARKET"),
            "opponent_contrast_available": bool(evidence_bundle and evidence_bundle.get("opponent_contrast")),
        })

    selections = []
    markets = defaultdict(Counter)
    for entry in entries:
        sid = entry.get("selection_id")
        refs = reverse[sid]
        status = _catalog_block(entry) or (
            "DIRECT_TACTICAL_LINK" if refs["direct"] else
            "LEGACY_FAMILY_LINK" if refs["legacy_family"] else
            "INDIRECT_ONLY" if refs["indirect"] else "NO_COMPATIBLE_HYPOTHESIS"
        )
        markets[entry.get("market_header_literal")][status] += 1
        selections.append({
            "selection_id": sid, "contract_id": entry.get("contract_id"),
            "market": entry.get("market_header_literal"), "selection": entry.get("selection_literal"),
            "odd": entry.get("odd"), "commercial_verdict": entry.get("verdict", entry.get("commercial_coverage")),
            "tactical_status": status, "hypothesis_indices": refs,
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
        "selection_links": selections,
        "selection_counts_by_tactical_status": dict(Counter(row["tactical_status"] for row in selections)),
        "market_links": [{"market": market, "selection_counts_by_tactical_status": dict(counts)}
                         for market, counts in markets.items()],
    }
