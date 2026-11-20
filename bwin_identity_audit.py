#!/usr/bin/env python3
"""Offline, bidirectional API-Football <-> Bwin identity audit.

Consumes frozen fixture and catalog snapshots.  It never opens a browser, calls
an API, scrapes Bwin, creates odds, or promotes ambiguous candidates.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from bwin_discovery import (LIVE_RE, category_compatibility, catalog_relative_kickoff,
                            competition_similarity, is_national_team_context, norm,
                            team_order_status, team_similarity, identity_name_evidence,
                            team_candidate_queries, team_tokens)

SCHEMA = "1.0-bwin-identity-audit"
FINAL_STATES = {
    "VERIFIED_LINK", "AMBIGUOUS_LINK", "NAME_MISMATCH", "TIME_CONFLICT",
    "CATEGORY_CONFLICT", "NOT_FOUND_IN_CATALOG", "SEARCH_INCOMPLETE",
    "NOT_OFFERED_VERIFIED", "SOURCE_UNAVAILABLE", "NOT_SEARCHED_BUDGET", "CANDIDATE", "EXCLUDED",
}


def catalog_scope_complete(pages: list[dict]) -> bool:
    """Return true only when every scoped catalog traversal proves completion."""
    scoped = [row for row in pages if row.get("kind") != "COUNTRY_NAV_DISCOVERY"]
    if not scoped:
        return False
    terminal = {"STABLE", "NO_NEW_EVENTS", "AT_BOTTOM", "EXHAUSTED"}
    return all(row.get("status") == "OK" and
               (row.get("collection_trace") or {}).get("termination") in terminal
               for row in scoped)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fixture_rows(document) -> list[dict]:
    if isinstance(document, list): rows = document
    else: rows = document.get("matches", document.get("fixtures", document.get("fixture_audit", [])))
    if not isinstance(rows, list): raise ValueError("FIXTURE_INVENTORY_INVALID")
    output = []
    for row in rows:
        if row.get("fixture_id") is None: continue
        item = dict(row)
        item.setdefault("kickoff_madrid", item.get("kickoff_utc"))
        output.append(item)
    return output


def _event_rows(document) -> tuple[list[dict], list[dict], list[dict], str | None]:
    if isinstance(document, list): return document, [], [], None
    return (document.get("catalog_observed", []), document.get("pages", []),
            document.get("matches", []), document.get("generated_at_utc"))


def merge_catalog(paths: list[Path]) -> tuple[list[dict], list[dict], list[dict]]:
    events: dict[str, dict] = {}; pages: dict[str, dict] = {}; sources = []
    for path in paths:
        document = read(path); rows, diagnostics, matches, generated = _event_rows(document)
        sources.append({"path": str(path.resolve()), "sha256": sha(path),
                        "generated_at_utc": generated, "rows": len(rows)})
        for row in rows:
            event_id = str(row.get("bwin_event_id") or row.get("event_id") or "")
            url = str(row.get("bwin_url") or row.get("url") or "")
            if not event_id.isdigit() or not url: continue
            candidate = {**row, "bwin_event_id": event_id, "bwin_url": url}
            candidate.setdefault("source_snapshots", []).append(str(path.resolve()))
            current = events.get(event_id)
            if current is None:
                events[event_id] = candidate
            else:
                current["source_pages"] = list(dict.fromkeys((current.get("source_pages") or []) + (candidate.get("source_pages") or [])))
                current["source_snapshots"] = list(dict.fromkeys((current.get("source_snapshots") or []) + candidate["source_snapshots"]))
                if (str(candidate.get("text_observed_at_utc") or ""), len(str(candidate.get("text") or ""))) > (
                        str(current.get("text_observed_at_utc") or ""), len(str(current.get("text") or ""))):
                    for key in ("text", "text_source", "text_observed_at_utc", "bwin_url"):
                        current[key] = candidate.get(key)
        for row in diagnostics:
            url = str(row.get("catalog_page") or "")
            if url and (url not in pages or pages[url].get("status") != "OK"):
                pages[url] = {**row, "source_snapshot": str(path.resolve())}
        # Candidate pages were inspected after catalog acquisition.  Preserve
        # their machine-readable kickoff and page title as stronger evidence
        # on the corresponding catalog event; do not manufacture fields for
        # events that were never opened.
        for match in matches:
            event_id = str(match.get("bwin_event_id") or "")
            current = events.get(event_id)
            inspection = match.get("inspection") or {}
            if current is None or not event_id.isdigit() or not isinstance(inspection, dict):
                continue
            current["inspection"] = inspection
            current["inspected_fixture_id"] = match.get("fixture_id")
            if inspection.get("page_title"):
                current["page_title"] = inspection["page_title"]
            if inspection.get("event_start_machine_readable"):
                current["event_start_machine_readable"] = inspection["event_start_machine_readable"]
    return sorted(events.values(), key=lambda row: int(row["bwin_event_id"])), list(pages.values()), sources


def _kickoff(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError): return None


def event_features(fixture: dict, event: dict) -> dict:
    parsed = unquote(urlparse(event["bwin_url"]).path)
    slug = parsed.split("/eventos/")[-1].rsplit(":", 1)[0]
    slug = re.sub(r"-2$", "", slug)
    page_title = str(event.get("page_title") or "")
    evidence = str(event.get("_identity_evidence") or f"{event.get('text', '')} {slug} {page_title}")
    name_evidence = str(event.get("_identity_name_evidence") or
                        identity_name_evidence(str(event.get('text') or ''), slug))
    national = is_national_team_context(fixture)
    home = team_similarity(str(fixture.get("home") or ""), name_evidence, national_context=national)
    away = team_similarity(str(fixture.get("away") or ""), name_evidence, national_context=national)
    order = team_order_status(str(fixture.get("home") or ""), str(fixture.get("away") or ""), name_evidence,
                              national_context=national)
    category = category_compatibility(fixture, evidence)
    competition = competition_similarity(str(fixture.get("league") or ""), evidence,
        league_id=fixture.get('league_id'),country=fixture.get('country'),season=fixture.get('season')) if fixture.get("league") else None
    expected = _kickoff(fixture.get("kickoff_madrid") or fixture.get("kickoff_utc"))
    observed = _kickoff(event.get("event_start_machine_readable")) or catalog_relative_kickoff(event)
    difference = (round(abs((expected - observed).total_seconds()) / 60, 3)
                  if expected is not None and observed is not None else None)
    return {"home_similarity": home, "away_similarity": away,
            "name_score": round((home + away) / 2, 4), "team_order": order,
            "category": category, "competition_similarity": competition,
            "fixture_kickoff": expected.isoformat() if expected else None,
            "catalog_kickoff": observed.isoformat() if observed else None,
            "kickoff_difference_minutes": difference, "evidence": evidence[:700]}


def prepare_event(event: dict) -> dict:
    """Precompute immutable local matching evidence for a catalog row."""
    item = dict(event)
    parsed = unquote(urlparse(item["bwin_url"]).path)
    slug = re.sub(r"-2$", "", parsed.split("/eventos/")[-1].rsplit(":", 1)[0])
    title = str(item.get("page_title") or "")
    item["_identity_name_evidence"] = identity_name_evidence(str(item.get("text") or ""), slug)
    item["_identity_evidence"] = f"{item.get('text', '')} {slug} {title}"
    item["_identity_norm"] = norm(item["_identity_name_evidence"])
    return item


def catalog_identity_fields(event: dict) -> dict:
    """Project only identity fields explicitly observable in the catalog row."""
    text = str(event.get("text") or "")
    after_clock = re.split(r"\b(?:Hoy|Mañana)\s*/\s*\d{1,2}:\d{2}\b", text, maxsplit=1, flags=re.I)
    home = away = None
    if len(after_clock) == 2 and " - " in after_clock[1]:
        pair = re.sub(r"\s+CA\s*$", "", after_clock[1]).strip()
        parts = pair.split(" - ", 1)
        if len(parts) == 2 and all(parts):
            home, away = parts
    observed = _kickoff(event.get("event_start_machine_readable")) or catalog_relative_kickoff(event)
    evidence = f"{text} {event.get('page_title', '')}"
    category = ("FEMALE" if re.search(r"(?i)(?:women|femenin|frauen|\(f\)|\bf\b)", evidence)
                else "YOUTH_OR_RESERVE" if re.search(r"(?i)(?:\bu\s*[- ]?\d{2}\b|youth|juvenil|reserve)", evidence)
                else "NOT_EXPLICIT")
    return {"bwin_home_observed": home, "bwin_away_observed": away,
            "competition_evidence": event.get("page_title") or text or None,
            "country_source_pages": event.get("source_pages", []),
            "kickoff_observed_madrid": observed.isoformat() if observed else None,
            "category_observed": category}


def _lexical_shortlist(team: str, event: dict, national: bool) -> bool:
    """Fast, recall-oriented gate before fuzzy scoring.

    At least one meaningful token from any registered retrieval variant must
    occur in the event name.  Time and date text have already been removed.
    This gate never confirms an identity and therefore cannot bypass the hard
    validation signals.
    """
    evidence = str(event.get("_identity_norm") or norm(event.get("_identity_name_evidence") or ""))
    for query in team_candidate_queries(team, national_context=national):
        tokens = [token for token in team_tokens(query) if len(token) >= 2]
        # A complete token cover for *one* registered variant is a broad but
        # useful retrieval condition. Requiring only one generic token (Home,
        # United, FC) caused quadratic fuzzy scoring over whole catalogs.
        if tokens and all(re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", evidence)
                          for token in tokens):
            return True
    return False


def resolve_fixture(fixture: dict, catalog: list[dict], *, catalog_complete: bool) -> dict:
    from filter_major_leagues import matching_eligibility
    eligibility,eligibility_reason=matching_eligibility(fixture)
    fixture={**fixture,'matching_eligibility':eligibility,'matching_eligibility_reason':eligibility_reason}
    if eligibility=='EXCLUDED':
        return {**fixture,'link_status':'EXCLUDED','bwin_event_id':None,
                'reason_codes':[eligibility_reason],'candidates':[]}
    if fixture.get("discovery_deferred") or fixture.get("not_searched_budget"):
        return {**fixture, "link_status": "NOT_SEARCHED_BUDGET", "bwin_event_id": None,
                "reason_codes": ["DISCOVERY_NOT_EXECUTED_BY_BUDGET"], "candidates": []}
    scored = []
    national = is_national_team_context(fixture)
    for raw_event in catalog:
        event = raw_event if raw_event.get("_identity_name_evidence") else prepare_event(raw_event)
        if LIVE_RE.search(str(event.get("text") or "")): continue
        if not (_lexical_shortlist(str(fixture.get("home") or ""), event, national) and
                _lexical_shortlist(str(fixture.get("away") or ""), event, national)):
            continue
        features = event_features(fixture, event)
        # Candidate retrieval is deliberately wider than final verification.
        if min(features["home_similarity"], features["away_similarity"]) >= .72:
            scored.append({"event": event, "features": features})
    scored.sort(key=lambda row: row["features"]["name_score"], reverse=True)
    candidates = [{"event_id": row["event"]["bwin_event_id"], "url": row["event"]["bwin_url"], **row["features"]}
                  for row in scored[:10]]
    if not scored:
        state = "NOT_FOUND_IN_CATALOG" if catalog_complete else "SEARCH_INCOMPLETE"
        return {**fixture, "link_status": state, "bwin_event_id": None,
                "reason_codes": ["NO_TWO_TEAM_CANDIDATE_IN_OBSERVED_CATALOG"], "candidates": []}
    valid = []
    rejected = []
    for row in scored:
        f = row["features"]
        reasons = []
        if f["team_order"] in {"AWAY_HOME_CONFLICT", "AWAY_HOME_TOKEN_CONFLICT", "AWAY_HOME_ALIAS_CONFLICT"}: reasons.append("HOME_AWAY_REVERSED")
        if f["team_order"] == "UNKNOWN": reasons.append("HOME_AWAY_ORDER_NOT_CORROBORATED")
        if f["category"] in {"GENDER_CONFLICT", "NON_ADULT_CONFLICT", "OBSERVED_FEMALE_TARGET_UNKNOWN", "FEMALE_NOT_VISIBLE"}: reasons.append("CATEGORY_CONTRADICTION")
        if min(f["home_similarity"], f["away_similarity"]) < .82: reasons.append("TEAM_NAME_BELOW_CANDIDATE_FLOOR")
        if reasons: rejected.append((row, reasons))
        else: valid.append(row)
    if not valid:
        category_conflict = any("CATEGORY_CONTRADICTION" in reasons for _, reasons in rejected)
        return {**fixture, "link_status": "CATEGORY_CONFLICT" if category_conflict else "NAME_MISMATCH",
                "bwin_event_id": None, "reason_codes": sorted({reason for _, reasons in rejected for reason in reasons}),
                "candidates": candidates}
    category_unconfirmed = [row for row in valid if row["features"]["category"] == "CATEGORY_UNCONFIRMED"]
    valid = [row for row in valid if row["features"]["category"] != "CATEGORY_UNCONFIRMED"]
    if not valid:
        return {**fixture, "link_status": "CANDIDATE", "bwin_event_id": None,
                "reason_codes": ["CATEGORY_NOT_CORROBORATED"], "candidates": candidates,
                "category_unconfirmed_candidates": len(category_unconfirmed)}
    winner = valid[0]; features = winner["features"]; event = winner["event"]
    if len(valid) > 1 and features["name_score"] - valid[1]["features"]["name_score"] < .05:
        return {**fixture, "link_status": "AMBIGUOUS_LINK", "bwin_event_id": None,
                "reason_codes": ["MULTIPLE_PLAUSIBLE_EVENTS_WITHIN_SCORE_MARGIN"], "candidates": candidates}
    if min(features["home_similarity"], features["away_similarity"]) < .94:
        return {**fixture, "link_status": "NAME_MISMATCH", "bwin_event_id": None,
                "reason_codes": ["NAME_CANDIDATE_NOT_STRONG_ENOUGH_TO_VERIFY"], "candidates": candidates}
    competition = features["competition_similarity"]
    if fixture.get("league") and (not isinstance(competition, (int, float)) or competition < .82):
        return {**fixture, "link_status": "NAME_MISMATCH", "bwin_event_id": None,
                "reason_codes": ["COMPETITION_NOT_CORROBORATED"], "candidates": candidates}
    difference = features["kickoff_difference_minutes"]
    if difference is None:
        return {**fixture, "link_status": "SEARCH_INCOMPLETE", "bwin_event_id": None,
                "reason_codes": ["BWIN_KICKOFF_NOT_MACHINE_VERIFIED"], "candidates": candidates}
    if difference > 5:
        return {**fixture, "link_status": "TIME_CONFLICT", "bwin_event_id": None,
                "reason_codes": ["KICKOFF_DIFFERENCE_EXCEEDS_5_MINUTES"], "candidates": candidates}
    if (eligibility=='PENDING_CLASSIFICATION' or fixture.get('category_evidence_status')!='CORROBORATED'
            or fixture.get('category_level')!='SENIOR' or not fixture.get('category_provenance')):
        return {**fixture,'link_status':'CANDIDATE','bwin_event_id':None,
                'reason_codes':[eligibility_reason],'candidates':candidates}
    return {**fixture, "link_status": "VERIFIED_LINK", "bwin_event_id": event["bwin_event_id"],
            "bwin_url": event["bwin_url"], "reason_codes": ["TEAMS_COMPETITION_CATEGORY_TIME_CORROBORATED"],
            "verified_features": features, "catalog_event_sources": event.get("source_snapshots", []),
            "candidates": candidates}


def metrics(rows: list[dict], catalog: list[dict], pages: list[dict], elapsed: float,
            truth: dict | None = None) -> dict:
    counts = Counter(row["link_status"] for row in rows)
    truth = truth or {}; expected = {int(row["fixture_id"]): str(row["bwin_event_id"]) for row in truth.get("positive_cases", [])}
    evaluated = [row for row in rows if int(row["fixture_id"]) in expected]
    tp = sum(row["link_status"] == "VERIFIED_LINK" and str(row.get("bwin_event_id")) == expected[int(row["fixture_id"])] for row in evaluated)
    # Precision/recall are undefined without an independently verified truth
    # set.  A verified production link is not a "false positive" merely
    # because no truth file was supplied.
    fp = (sum(row["link_status"] == "VERIFIED_LINK" and
              (int(row["fixture_id"]) not in expected or
               str(row.get("bwin_event_id")) != expected.get(int(row["fixture_id"])))
              for row in rows) if truth else None)
    verified = counts["VERIFIED_LINK"]
    return {"fixtures_total": len(rows),
            "fixtures_explicitly_adult": sum(
                str(row.get("category_evidence_status", "")).upper() == "CORROBORATED" and
                str(row.get("category_level", "")).upper() == "SENIOR" for row in rows),
            "fixtures_category_unknown": sum(
                str(row.get("category_evidence_status", "")).upper() != "CORROBORATED" for row in rows),
            "bwin_events_observed": len(catalog), "verified_links": verified,
            "candidate_links": counts['CANDIDATE'], "excluded": counts['EXCLUDED'],
            "ambiguous_links": counts["AMBIGUOUS_LINK"], "not_found": counts["NOT_FOUND_IN_CATALOG"],
            "search_incomplete": counts["SEARCH_INCOMPLETE"], "time_conflicts": counts["TIME_CONFLICT"],
            "category_conflicts": counts["CATEGORY_CONFLICT"], "not_searched_budget": counts["NOT_SEARCHED_BUDGET"],
            "name_mismatches": counts["NAME_MISMATCH"], "source_unavailable": counts["SOURCE_UNAVAILABLE"],
            "not_offered_verified": counts["NOT_OFFERED_VERIFIED"],
            "known_presence_denominator": len(evaluated), "true_positives": tp if truth else None,
            "false_positives": fp, "false_negatives": len(evaluated) - tp if truth else None,
            "recall_verified_presence": round(tp / len(evaluated), 6) if evaluated else None,
            "precision_automatic_links": round(tp / (tp + fp), 6) if truth and tp + fp else None,
            "matching_local_seconds": round(elapsed, 6),
            "catalog_page_navigations_recorded": len({p.get("catalog_page") for p in pages if p.get("catalog_page")}),
            "supplemental_navigations_recorded": sum(p.get("kind") != "COUNTRY_NAV_DISCOVERY" for p in pages),
            "seconds_per_verified_link_local": round(elapsed / verified, 6) if verified else None}


def audit(fixtures: list[dict], catalog: list[dict], pages: list[dict], sources: list[dict],
          *, catalog_complete: bool, truth: dict | None = None) -> dict:
    from filter_major_leagues import explicit_competition_category
    from competition_registry import category
    enriched=[]
    for fixture in fixtures:
        explicit=explicit_competition_category(fixture) or {}
        if str(fixture.get('gender') or 'UNKNOWN').upper() not in ('UNKNOWN',explicit.get('gender')):
            explicit={}
        enriched.append({**fixture,**explicit,**category(fixture)})
    fixtures=enriched
    started = time.perf_counter(); prepared = [prepare_event(event) for event in catalog]
    rows = [resolve_fixture(fixture, prepared, catalog_complete=catalog_complete) for fixture in fixtures]
    linked = {str(row.get("bwin_event_id")) for row in rows if row.get("link_status") == "VERIFIED_LINK"}
    reverse_candidates: dict[str, list[dict]] = {}
    for row in rows:
        for candidate in row.get("candidates", []):
            reverse_candidates.setdefault(str(candidate["event_id"]), []).append({
                "fixture_id": row["fixture_id"], "home": row.get("home"), "away": row.get("away"),
                "fixture_link_status": row["link_status"], "name_score": candidate.get("name_score"),
                "team_order": candidate.get("team_order"), "category": candidate.get("category"),
                "kickoff_difference_minutes": candidate.get("kickoff_difference_minutes"),
            })
    reverse = [{"bwin_event_id": event["bwin_event_id"], "bwin_url": event["bwin_url"],
                "text": event.get("text"),
                "status": "POSSIBLE_API_CANDIDATES" if reverse_candidates.get(str(event["bwin_event_id"])) else "UNMATCHED_BWIN_EVENT",
                "api_candidates": reverse_candidates.get(str(event["bwin_event_id"]), []),
                "source_pages": event.get("source_pages", [])} for event in catalog if event["bwin_event_id"] not in linked]
    elapsed = time.perf_counter() - started
    return {"schema_version": SCHEMA, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "catalog_complete_claimed": catalog_complete, "network_used": False,
            "thresholds": {"candidate_team_min": .72, "semantic_team_min": .82,
                           "verification_team_min": .94, "ambiguity_margin": .05,
                           "competition_min": .82, "kickoff_tolerance_minutes": 5},
            "catalog_sources": sources, "pages": pages, "fixtures": rows,
            "unmatched_bwin_events": reverse, "metrics": metrics(rows, catalog, pages, elapsed, truth)}


def write_reports(output: Path, document: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "identity_audit.json", document)
    fields = ["fixture_id", "home", "away", "kickoff_madrid", "league", "link_status",
              "bwin_event_id", "bwin_url", "reason_codes", "candidate_count"]
    with (output / "identity_audit.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row in document["fixtures"]:
            writer.writerow({**{key: row.get(key) for key in fields},
                             "reason_codes": ",".join(row.get("reason_codes", [])),
                             "candidate_count": len(row.get("candidates", []))})
    lines = ["# Auditoría API-Football ↔ Bwin", "", "## Métricas", ""]
    lines += [f"- {key}: {value}" for key, value in document["metrics"].items()]
    lines += ["", "## Fixtures", "", "| Fixture | Partido | Estado | Event ID | Motivo |", "|---:|---|---|---:|---|"]
    for row in document["fixtures"]:
        lines.append(f"| {row['fixture_id']} | {row.get('home')}–{row.get('away')} | {row['link_status']} | {row.get('bwin_event_id') or ''} | {', '.join(row.get('reason_codes', []))} |")
    lines += ["", f"Eventos Bwin sin enlace: {len(document['unmatched_bwin_events'])}.",
              "NOT_FOUND_IN_CATALOG no significa que Bwin no ofrezca el encuentro.", ""]
    (output / "identity_audit.md").write_text("\n".join(lines), encoding="utf-8")
    with (output / "unmatched_bwin_events.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["bwin_event_id", "bwin_url", "text", "status", "api_candidate_count", "api_candidate_fixture_ids"]); writer.writeheader()
        for row in document["unmatched_bwin_events"]:
            candidates = row.get("api_candidates", [])
            writer.writerow({"bwin_event_id": row.get("bwin_event_id"), "bwin_url": row.get("bwin_url"),
                             "text": row.get("text"), "status": row.get("status"),
                             "api_candidate_count": len(candidates),
                             "api_candidate_fixture_ids": ",".join(str(item["fixture_id"]) for item in candidates)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog-complete", action="store_true",
                        help="Only use when the captured page scope completed successfully")
    parser.add_argument("--truth", type=Path, help="Verified regression dataset; optional metrics denominator")
    args = parser.parse_args(argv)
    try:
        fixtures = fixture_rows(read(args.fixtures)); catalog, pages, sources = merge_catalog(args.catalog)
        truth = read(args.truth) if args.truth else None
        if args.catalog_complete and not catalog_scope_complete(pages):
            raise ValueError("CATALOG_COMPLETE_NOT_PROVEN_BY_PAGE_TERMINATION")
        document = audit(fixtures, catalog, pages, sources, catalog_complete=args.catalog_complete, truth=truth)
        write_reports(args.output_dir, document)
        print(json.dumps({"output": str(args.output_dir.resolve()), **document["metrics"]}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"BLOQUEADO: {exc}", file=__import__("sys").stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
