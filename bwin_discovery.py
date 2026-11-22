#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bwin discovery PREMATCH (V0.4).

Discovers *published* Bwin event URLs from Bwin's football calendar using Playwright,
then cross-references them with API-Football fixtures. It NEVER invents event IDs,
and never auto-declares an API-Football <-> Bwin link officially verified unless an
explicit, timezone-aware event start timestamp is available and agrees.

Only reads public pages; no login, bookmaker internal API, bets or bets placing.
Requires: pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlparse, urljoin
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")
BASE = "https://www.bwin.es"
CALENDAR = (
    "/es/sports/f%C3%BAtbol-4/hoy",
    "/es/sports/f%C3%BAtbol-4/ma%C3%B1ana",
    "/es/sports/f%C3%BAtbol-4",
)
EVENT_RE = re.compile(r"(?:^|/)eventos/[^?#]*?(?:-2:|:)(\d{5,12})(?:[/?#]|$)", re.I)
COUNTDOWN_RE = re.compile(r"\b(?:comienza|empieza)\s+en\s+(?:(?P<h>\d{1,2})\s*h(?:oras?)?\s*)?(?P<m>\d{1,3})\s*min(?:utos?)?\b", re.I)
LIVE_RE = re.compile(r"(?:\blive(?:1t|2t|descanso)?\b|\ben\s+vivo\b|\bdescanso\b|\bhalf\s*time\b|\b\d\s*[tT]\s*[•:])", re.I)
FEMALE_RE = re.compile(r"(?:\bwomen(?:'s)?\b|\bwomens\b|\bfemenin[oa]\b|\bfeminin[ea]\b|\bfrauen\b|\bfemminile\b|\bdamen\b|\(w\)|\sw\b|\(f\)|(?<=\s)f(?:hoy|ma[nñ]ana)?(?=\s|$))", re.I)
NON_ADULT_RE = re.compile(r"(?:\b(?:u|sub)\s*[- ]?(?:1[0-9]|2[0-3])\b|\bunder\s*[- ]?(?:1[0-9]|2[0-3])\b|\byouth\b|\bjuvenil(?:es)?\b|\breserves?\b|\breservas?\b)", re.I)
CHROME_ERRORS = ("ERR_CONNECTION", "DNS", "Access Denied", "Forbidden", "403", "CAPTCHA", "captcha")
MAX_VERIFIED_ABSOLUTE_KICKOFF_DIFFERENCE_MINUTES = 90.0
STOP_TOKENS = {
    "fc", "cf", "sc", "ac", "cd", "sd", "afc", "club", "football", "soccer",
    "de", "del", "el", "la", "los", "las", "the", "futbol", "futebol",
    "f", "w", "women", "femenino", "femenil", "fem", "femenina",
}
NATIONAL_TEAM_TRANSLATIONS = {
    "albania": ("albania",),
    "algeria": ("argelia",),
    "andorra": ("andorra",),
    "armenia": ("armenia",),
    "australia": ("australia",),
    "austria": ("austria",),
    "azerbaijan": ("azerbaiyan",),
    "belarus": ("bielorrusia",),
    "belgium": ("belgica",),
    "bosnia herzegovina": ("bosnia y herzegovina", "bosnia-herzegovina"),
    "brazil": ("brasil",),
    "bulgaria": ("bulgaria",),
    "cameroon": ("camerun",),
    "canada": ("canada",),
    "cape verde": ("cabo verde",),
    "chile": ("chile",),
    "china": ("china",),
    "colombia": ("colombia",),
    "costa rica": ("costa rica",),
    "croatia": ("croacia",),
    "cyprus": ("chipre",),
    "czech republic": ("chequia", "republica checa", "czechia"),
    "czechia": ("chequia", "republica checa", "czech republic"),
    "denmark": ("dinamarca",),
    "ecuador": ("ecuador",),
    "egypt": ("egipto",),
    "england": ("inglaterra",),
    "estonia": ("estonia",),
    "faroe islands": ("islas feroe",),
    "finland": ("finlandia",),
    "france": ("francia",),
    "fyr macedonia": ("macedonia del norte", "north macedonia"),
    "georgia": ("georgia",),
    "germany": ("alemania",),
    "ghana": ("ghana",),
    "gibraltar": ("gibraltar",),
    "greece": ("grecia",),
    "hungary": ("hungria",),
    "iceland": ("islandia",),
    "iran": ("iran",),
    "israel": ("israel",),
    "italy": ("italia",),
    "ivory coast": ("costa de marfil", "cote d ivoire"),
    "japan": ("japon",),
    "kazakhstan": ("kazajistan", "kazajstan"),
    "korea republic": ("corea del sur", "south korea"),
    "kosovo": ("kosovo",),
    "latvia": ("letonia",),
    "liechtenstein": ("liechtenstein",),
    "lithuania": ("lituania",),
    "luxembourg": ("luxemburgo",),
    "malta": ("malta",),
    "mexico": ("mexico", "mejico"),
    "moldova": ("moldavia",),
    "montenegro": ("montenegro",),
    "morocco": ("marruecos",),
    "netherlands": ("paises bajos", "holanda"),
    "new zealand": ("nueva zelanda",),
    "north macedonia": ("macedonia del norte", "fyr macedonia"),
    "northern ireland": ("irlanda del norte",),
    "norway": ("noruega",),
    "panama": ("panama",),
    "paraguay": ("paraguay",),
    "peru": ("peru",),
    "poland": ("polonia",),
    "portugal": ("portugal",),
    "qatar": ("catar", "qatar"),
    "rep of ireland": ("irlanda", "republica de irlanda", "republic of ireland"),
    "republic of ireland": ("irlanda", "republica de irlanda", "rep of ireland"),
    "romania": ("rumania",),
    "russia": ("rusia",),
    "san marino": ("san marino",),
    "saudi arabia": ("arabia saudi", "arabia saudita"),
    "scotland": ("escocia",),
    "senegal": ("senegal",),
    "serbia": ("serbia",),
    "slovakia": ("eslovaquia",),
    "slovenia": ("eslovenia",),
    "south africa": ("sudafrica",),
    "south korea": ("corea del sur", "korea republic"),
    "spain": ("espana",),
    "sweden": ("suecia",),
    "switzerland": ("suiza",),
    "tunisia": ("tunez",),
    "turkey": ("turquia", "turkiye"),
    "turkiye": ("turquia", "turkey"),
    "uae": ("emiratos arabes unidos", "united arab emirates"),
    "ukraine": ("ucrania",),
    "united arab emirates": ("emiratos arabes unidos", "uae"),
    "united states": ("estados unidos", "usa", "ee uu"),
    "uruguay": ("uruguay",),
    "usa": ("estados unidos", "united states"),
    "venezuela": ("venezuela",),
    "wales": ("gales",),
}

_NATIONAL_TEAM_EQUIVALENCE: dict[str, set[str]] = {}
for _en_name, _es_variants in NATIONAL_TEAM_TRANSLATIONS.items():
    _group = {_en_name, *_es_variants}
    for _member in _group:
        _NATIONAL_TEAM_EQUIVALENCE.setdefault(_member, set()).update(_group)

_INSTITUTIONAL_CLUB_PREFIXES = (
    ("club deportivo ", "cd "),
    ("c d ", "cd "),
    ("club atletico ", "ca "),
    ("c a ", "ca "),
    ("sociedad deportiva ", "sd "),
    ("union deportiva ", "ud "),
    ("real club deportivo ", "rcd "),
    ("real club ", "rc "),
)
INSTITUTIONAL_CANDIDATE_ALIASES = {
    "sl benfica w": ("benfica f",),
    "austria wien w": ("fk austria viena f",),
}
SENIOR_COMPETITION_IDENTITIES = {
    5: "uefa nations league",
    143: "copa del rey",
}


def verified_candidate_aliases() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Load auditable aliases used only to retrieve candidates.

    A registry entry never bypasses order, competition, category or time gates.
    Invalid or unreviewed entries fail closed instead of becoming global aliases.
    """
    path = Path(__file__).resolve().parent / "config" / "bwin_team_aliases.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if (document.get("schema_version") != "1.0-bwin-team-alias-registry" or
            document.get("policy") != "CANDIDATE_RETRIEVAL_ONLY_NEVER_IDENTITY_CONFIRMATION"):
        return ()
    output = []
    for row in document.get("aliases", []):
        variants = row.get("variants")
        if (row.get("status") not in {"VERIFIED_NOMINAL", "VERIFIED_REPOSITORY"} or
                not row.get("canonical") or not row.get("evidence") or not isinstance(variants, list)):
            continue
        output.append((str(row["canonical"]), tuple(str(value) for value in variants if value)))
    return tuple(output)


VERIFIED_CANDIDATE_ALIASES = verified_candidate_aliases()


def verified_identity_candidates(path: Path, fixtures: list[dict]) -> tuple[dict[int, dict], dict]:
    """Load exact frozen bindings for direct, fresh event-page reinspection."""
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = document.get("fixtures")
    if not isinstance(rows, list):
        raise ValueError("VERIFIED_IDENTITY_CATALOG_INVALID")
    original = (document.get("inputs") or {}).get("fixtures")
    expected = (document.get("inputs") or {}).get("fixtures_sha256")
    if original and expected:
        source = Path(original)
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("VERIFIED_IDENTITY_SOURCE_SHA_MISMATCH")
    requested = {int(f["fixture_id"]): f for f in fixtures}
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("fixture_id") is not None:
            grouped.setdefault(int(row["fixture_id"]), []).append(row)
    output = {}
    for fid, fixture in requested.items():
        candidates = grouped.get(fid, [])
        if len(candidates) != 1:
            continue
        row = candidates[0]
        event_id = str(row.get("event_id") or row.get("bwin_event_id") or "")
        url = str(row.get("bwin_url") or "")
        name = row.get("name") or {}
        timing = row.get("time") or {}
        category = row.get("category") or {}
        competition = row.get("competition") or {}
        try:
            persisted_kickoff = datetime.fromisoformat(str(row.get("kickoff_madrid")))
            requested_kickoff = datetime.fromisoformat(str(fixture.get("kickoff_madrid")))
            if persisted_kickoff.utcoffset() is None or requested_kickoff.utcoffset() is None:
                raise ValueError("KICKOFF_WITHOUT_TIMEZONE")
            kickoff_difference = abs(persisted_kickoff.timestamp() - requested_kickoff.timestamp())
        except (TypeError, ValueError):
            kickoff_difference = float("inf")
        match = EVENT_RE.search(url)
        exact_fields = ("home_id", "away_id", "league_id", "season")
        if (not row.get("identity_verified") or row.get("post_status") != "IDENTITY_VERIFIED_OFFLINE"
                or not event_id.isdigit() or not match or match.group(1) != event_id
                or any(str(row.get(k)) != str(fixture.get(k)) for k in exact_fields)
                or kickoff_difference > 180
                or name.get("status") != "CANDIDATE_COMPATIBLE"
                or timing.get("status") != "MATCH"
                or category.get("status") not in ("SENIOR_COMPETITION_CORROBORATED", "CATEGORY_CORROBORATED")
                or competition.get("status") not in ("CORROBORATED_INSPECTION", "CORROBORATED")):
            continue
        frozen = next((c for c in row.get("candidates", []) if str(c.get("event_id")) == event_id), {})
        output[fid] = {
            "bwin_event_id": event_id,
            "bwin_url": url,
            "text": frozen.get("text") or f"{fixture.get('home')} {fixture.get('away')}",
            "text_source": "PERSISTED_VERIFIED_IDENTITY_DIRECT_ROUTE",
            "text_observed_at_utc": None,
            "source_pages": row.get("source_pages", []),
        }
    audit = {"path": str(Path(path).resolve()),
             "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
             "requested": len(requested), "usable_bindings": len(output),
             "freshness": "IDENTITY_ONLY_REQUIRES_DIRECT_REINSPECTION"}
    return output, audit


def norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").replace("ø","o").replace("Ø","O"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " y ")
    s = re.sub(r"\bd\.\s+(?=la\b|serena\b)", "deportivo ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def team_tokens(name: str) -> list[str]:
    result = [t for t in norm(name).split() if t not in STOP_TOKENS]
    return result or norm(name).split()


_CLUB_CITY_TRANSLATIONS = {
    "koln": "colonia",
    "colonia": "koln",
    "munchen": "munich",
    "munich": "munchen",
    "tbilisi": "tiflis",
    "tiflis": "tbilisi",
    "brugge": "brujas",
    "brujas": "brugge",
    "marseille": "marsella",
    "marsella": "marseille",
    "nurnberg": "nuremberg",
    "mainz": "maguncia",
    "gent": "gante",
    "antwerp": "amberes",
    "kobenhavn": "copenhague",
    "basel": "basilea",
}


def identity_name_evidence(event_text: str, slug: str) -> str:
    """Remove catalog clocks before comparing team names.

    Otherwise a club number can accidentally match ``20`` from ``20:00`` or a
    date, which was observed in the 100-fixture retrieval regression.
    """
    clean = re.sub(r"(?i)([a-z0-9])\s*f(?:hoy|ma[nñ]ana)\s*/\s*\d{1,2}:\d{2}\b", r"\1 f ", str(event_text))
    clean = re.sub(r"\b(?:Hoy|Mañana)\s*/\s*\d{1,2}:\d{2}\b", " ", clean, flags=re.I)
    clean = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\b", " ", clean)
    return f"{clean} {slug}"


def _team_similarity_literal(team: str, evidence: str) -> float:
    """Conservative partial-string matching, no automatic acceptance threshold here."""
    words = team_tokens(team)
    hay = norm(evidence)
    # Bwin abrevia New York como NY.
    hay = re.sub(r"\bny\b", "new york", hay)
    hw = set(hay.split())
    if not words or not hay:
        return 0.0
    key = " ".join(words)
    if re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", hay):
        return 1.0
    common = sum(w in hw for w in words)
    coverage = common / len(words)
    if len(words) == 1:
        return 0.94 if coverage == 1 else 0.0
    if coverage == 1:
        return 0.97
    # Many long club names have minor variant endings, but one generic word is not enough.
    longest = max((SequenceMatcher(None, key, " ".join(hay.split()[i:i+len(words)])).ratio()
                   for i in range(max(1, len(hay.split()) - len(words) + 1))), default=0)
    return round(max(coverage * 0.83, longest * 0.75), 4)


def is_national_team_context(fixture: dict) -> bool:
    """Return True only when fixture context indicates senior international teams."""
    if NON_ADULT_RE.search(f"{fixture.get('home', '')} {fixture.get('away', '')}"):
        return False
    try:
        league_id = int(fixture.get("league_id") or 0)
    except (TypeError, ValueError):
        league_id = 0
    return (
        norm(fixture.get("country")) in {"world", "mundo", "international"}
        or "national" in norm(fixture.get("team_kind"))
        or league_id in {5, 10}
    )


def team_candidate_queries(team: str, *, national_context: bool = False) -> set[str]:
    """Return auditable retrieval variants without asserting identity.

    Keeping this separate lets the offline auditor build a cheap lexical
    shortlist before running the more expensive fuzzy comparison.  The result
    remains candidate-only: none of these variants bypasses the independent
    competition, category, order or time gates.
    """
    from config.constants import TEAM_ALIASES
    query=norm(team);queries={str(team)}
    if query=='atletico mg':queries.add('atletico mineiro')
    if national_context:
        queries.update(_NATIONAL_TEAM_EQUIVALENCE.get(query, ()))
    else:
        for long_prefix, short_prefix in _INSTITUTIONAL_CLUB_PREFIXES:
            if query.startswith(long_prefix):
                rest = query[len(long_prefix):].strip()
                if any(len(tok) >= 4 for tok in team_tokens(rest)):
                    queries.add(f"{short_prefix}{rest}")
        # German ordinal prefix like "1. FC Koln" -> candidate variant without "1 "
        if re.match(r"^(?:1|1\.)\s+(?:fc|fsv|sv|sc)\s+", query):
            rest = re.sub(r"^(?:1|1\.)\s+", "", query).strip()
            if any(len(tok) >= 4 for tok in team_tokens(rest)):
                queries.add(rest)
        # Institutional single-word "deportivo <unique_club>" (min 7 chars, avoids short city collisions)
        if query.startswith("deportivo "):
            rest = query[len("deportivo "):].strip()
            if len(rest) >= 7 and " " not in rest:
                queries.add(rest)
        # Safe abbreviation expansion: "st " <-> "saint "
        if re.search(r"\bst\b", query):
            queries.add(re.sub(r"\bst\b", "saint", query))
        elif re.search(r"\bsaint\b", query):
            queries.add(re.sub(r"\bsaint\b", "st", query))
        # Safe city-club lexical translations (candidate retrieval only)
        q_toks = query.split()
        if any(t in _CLUB_CITY_TRANSLATIONS for t in q_toks):
            translated = " ".join(_CLUB_CITY_TRANSLATIONS.get(t, t) for t in q_toks)
            queries.add(translated)
            non_stop = [t for t in translated.split() if t not in STOP_TOKENS and not t.isdigit()]
            if non_stop and any(len(t) >= 5 for t in non_stop):
                queries.add(" ".join(non_stop))
    queries.update(INSTITUTIONAL_CANDIDATE_ALIASES.get(query, ()))
    for canonical,aliases in TEAM_ALIASES.items():
        variants={norm(x) for x in (canonical,*aliases)}
        if query in variants:queries.update(variants)
    for canonical, aliases in VERIFIED_CANDIDATE_ALIASES:
        variants = {norm(x) for x in (canonical, *aliases)}
        if query in variants:
            queries.update(variants)
    # OL Lyonnes is the women's club brand.
    if query in {'lyon w', 'ol lyonnes w', 'ol lyonnes f'}:
        queries.update({'lyon w', 'ol lyonnes w', 'ol lyonnes f'})
    # Equivalencia específica verificada: Inter femenino.
    if query in {'inter milano w', 'inter milan w', 'inter milan f'}:
        queries.update({'inter milano w', 'inter milan w', 'inter milan f'})

    return {q for q in queries if norm(q)}


def resolve_verified_team_side(label: str, fixture: dict, extra_aliases: dict[str, tuple[str, ...]] | None = None) -> str | None:
    """Resuelve 'home' o 'away' desde una cabecera/selección Bwin preservando categoría y evitando colisiones."""
    raw_label = str(label or "").strip()
    p_norm = norm(raw_label)
    if not p_norm:
        return None
    observed_female = bool(FEMALE_RE.search(raw_label) or re.search(r"(?i)(?:\(f\)|\bf\b)", raw_label))
    observed_non_adult = bool(NON_ADULT_RE.search(raw_label))
    fixture_gender = str(fixture.get("gender") or "").upper()
    if fixture_gender not in ("F", "M"):
        h_a_txt = f"{fixture.get('home', '')} {fixture.get('away', '')}"
        if FEMALE_RE.search(h_a_txt):
            fixture_gender = "F"
    if fixture_gender == "F" and not observed_female:
        return None
    if fixture_gender == "M" and observed_female:
        return None

    national_ctx = is_national_team_context(fixture)
    category_stop = STOP_TOKENS | {"f", "w", "women", "femenina", "femenino", "frauen"}
    p_core = [t for t in p_norm.split() if t not in category_stop]
    p_token_set = set(p_core)

    def _side_matches(side: str) -> bool:
        base_names = [fixture.get(side), fixture.get(f"{side}_bwin")]
        if extra_aliases and side in extra_aliases:
            base_names.extend(extra_aliases[side])
        expanded: set[str] = set()
        for cname in base_names:
            if not cname:
                continue
            cname_str = str(cname).strip()
            if not cname_str:
                continue
            if observed_non_adult != bool(NON_ADULT_RE.search(cname_str)):
                continue
            c_fem = bool(FEMALE_RE.search(cname_str) or re.search(r"(?i)(?:\(f\)|\bf\b)", cname_str))
            if observed_female != c_fem and fixture_gender != "F":
                continue
            expanded.add(norm(cname_str))
            for cand in team_candidate_queries(cname_str, national_context=national_ctx):
                if observed_non_adult == bool(NON_ADULT_RE.search(cand)):
                    expanded.add(norm(cand))
        for cand_norm in expanded:
            if not cand_norm:
                continue
            if cand_norm == p_norm:
                return True
            cand_core = [t for t in cand_norm.split() if t not in category_stop]
            if cand_core and p_core and cand_core == p_core:
                return True
            cand_set = set(cand_core)
            # Evita colisiones parciales tipo "paris" vs "paris fc" o "inter" vs "inter miami".
            if len(cand_set) >= 2 and cand_set == p_token_set:
                return True
        return False

    hits = [s for s in ("home", "away") if _side_matches(s)]
    return hits[0] if len(hits) == 1 else None

def team_similarity(team: str, evidence: str, *, national_context: bool = False) -> float:
    # Exact club aliases only generate candidates; temporal/category/ID gates remain.
    queries = team_candidate_queries(team, national_context=national_context)
    query = norm(team)
    if query in {'lyon w','ol lyonnes w','ol lyonnes f'} and re.search(r'\b(?:women|w|f)\b',norm(evidence)):
        queries.update({'lyon','ol lyonnes'})
    return max(_team_similarity_literal(q,evidence) for q in queries)


def team_name_match_method(team: str, evidence: str, *, national_context: bool = False) -> str:
    """Expose whether a passing name match was literal or used a verified alias."""
    literal = _team_similarity_literal(team, evidence)
    if literal >= 0.82:
        return "LITERAL"
    if team_similarity(team, evidence, national_context=national_context) >= 0.82:
        return "VERIFIED_ALIAS"
    return "NO_MATCH"


def absolute_kickoff_status(difference_minutes: float | None) -> str:
    """Classify an absolute API/Bwin kickoff difference without hiding uncertainty."""
    if difference_minutes is None:
        return "TIME_UNVERIFIED"
    if difference_minutes <= MAX_VERIFIED_ABSOLUTE_KICKOFF_DIFFERENCE_MINUTES:
        return "TIME_VERIFIED"
    return "TIME_REVIEW"


def team_order_status(home: str, away: str, evidence: str, *, national_context: bool = False) -> str:
    """Check home/away order only from observed Bwin text or URL slug."""
    observed = norm(evidence)
    h, a = norm(home), norm(away)
    hp, ap = observed.find(h), observed.find(a)
    if hp >= 0 and ap >= 0 and hp != ap:
        return "HOME_AWAY" if hp < ap else "AWAY_HOME_CONFLICT"
    ht = [t for t in team_tokens(home) if len(t) >= 3]
    at = [t for t in team_tokens(away) if len(t) >= 3]
    hpos = [observed.find(t) for t in ht if observed.find(t) >= 0]
    apos = [observed.find(t) for t in at if observed.find(t) >= 0]
    if hpos and apos and min(hpos) != min(apos):
        return "HOME_AWAY_TOKEN" if min(hpos) < min(apos) else "AWAY_HOME_TOKEN_CONFLICT"
    # Multilingual/verified aliases may have no literal token in common with
    # the API name (Moldova/Moldavia, Türkiye/Turquía). Resolve their positions
    # only for order corroboration; the remaining hard gates still apply.
    def variant_position(team: str):
        positions = []
        for variant in team_candidate_queries(team, national_context=national_context):
            variant_norm = norm(variant)
            phrase_position = observed.find(variant_norm)
            if variant_norm and phrase_position >= 0:
                positions.append(phrase_position)
                continue
            words = team_tokens(variant)
            if not words:
                continue
            word_positions = [observed.find(word) for word in words]
            if all(position >= 0 for position in word_positions):
                positions.append(min(word_positions))
        return min(positions) if positions else None
    hp, ap = variant_position(home), variant_position(away)
    if hp is not None and ap is not None and hp != ap:
        return "HOME_AWAY_ALIAS" if hp < ap else "AWAY_HOME_ALIAS_CONFLICT"
    return "UNKNOWN"


def category_compatibility(fixture: dict, evidence: str) -> str:
    observed_female = bool(FEMALE_RE.search(evidence))
    observed_non_adult = bool(NON_ADULT_RE.search(evidence))
    expected = str(fixture.get("gender") or "UNKNOWN").upper()
    if observed_non_adult:
        return "NON_ADULT_CONFLICT"
    if expected == "M" and observed_female:
        return "GENDER_CONFLICT"
    if expected == "F":
        return "FEMALE_CORROBORATED" if observed_female else "FEMALE_NOT_VISIBLE"
    if expected == "UNKNOWN":
        if observed_female:return "OBSERVED_FEMALE_TARGET_UNKNOWN"
        return "CATEGORY_UNCONFIRMED"
    return "COMPATIBLE_OR_UNKNOWN"


def senior_category_corroborated(fixture: dict, candidate_category: str,
                                 competition_score: object,
                                 page_category: str | None = None) -> bool:
    """Close UNKNOWN only from a known senior competition corroborated by Bwin."""
    try:league_id=int(fixture.get('league_id'))
    except (TypeError,ValueError):return False
    from filter_major_leagues import explicit_competition_category
    explicit=explicit_competition_category(fixture)
    provenance=fixture.get('category_provenance') or {}
    positive_category=bool(explicit and fixture.get('gender')==explicit['gender']=='M'
        and str(fixture.get('category_level') or '').upper()=='SENIOR'
        and str(fixture.get('category_evidence_status') or '').upper()=='CORROBORATED'
        and provenance==explicit['category_provenance'])
    return bool(candidate_category=='CATEGORY_UNCONFIRMED'
        and page_category in (None,'CATEGORY_UNCONFIRMED','COMPATIBLE_OR_UNKNOWN')
        and str(fixture.get('category_status') or '').upper() in {'ELIGIBLE','ADULT'}
        and positive_category
        and isinstance(competition_score,(int,float)) and not isinstance(competition_score,bool)
        and competition_score>=.95)


def competition_similarity(competition: str, evidence: str, *, league_id=None,
                           country: str | None = None, season=None) -> float:
    """Supporting competition evidence; missing evidence never becomes a match."""
    expected, observed = norm(competition), norm(evidence)
    if not expected or not observed:
        return 0.0
    # API 10 identifies international friendlies, not club friendlies (667).
    # This is competition evidence only; it supplies no gender/age evidence.
    if league_id==10 and norm(country) in {'world','international','mundo'} and expected=='friendlies':
        return 1.0 if re.search(r'(?:\bamistosos\s+int(?:ernacional(?:es)?)?\b|\binternational\s+friendl(?:y|ies)\b|\bfriendlies\s+international\b)',observed) else 0.0
    from filter_major_leagues import verified_bwin_competition_alias
    if verified_bwin_competition_alias({'league_id':league_id,'league':competition,
            'country':country,'season':season},evidence):
        return 1.0
    if expected == 'wsl cup':
        return 1.0 if re.search(r'\bfa league cup f\b',observed) else 0.0
    if expected == 'uefa europa cup women':
        # Observed Spanish Bwin competition label, distinct from Champions
        # League and the men's Europa League. Team/time checks stay separate.
        return 1.0 if re.search(r'\buefa (?:copa europa f|europa cup women)\b', observed) else 0.0
    compact_expected = expected.replace(" ", "")
    compact_observed = observed.replace(" ", "")
    compact = (1.0 if compact_expected in compact_observed else
               SequenceMatcher(None, compact_expected, compact_observed).ratio())
    return round(max(team_similarity(competition, evidence), compact), 4)


def extract_event_id(url: str) -> str | None:
    try:
        path = unquote(urlparse(url).path)
    except (ValueError, TypeError):
        return None
    m = EVENT_RE.search(path)
    return m.group(1) if m else None


def local_kickoff(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    except ValueError:
        return None



def countdown_corrob(result: dict, fixture_kickoff: datetime | None) -> dict:
    """Corroborate a fixture using a catalog countdown *and its own capture instant*.

    Countdown is supporting evidence only; unlike JSON-LD it is not a verified
    absolute time. Never use the report-generation time as the capture instant.
    """
    out = {"countdown_minutes": None, "countdown_observed_at_utc": None,
           "event_start_from_countdown": None, "countdown_difference_minutes": None}
    text = str(result.get("text") or "")
    snapshot = result.get("text_observed_at_utc")
    match = COUNTDOWN_RE.search(text)
    if not match or not snapshot or fixture_kickoff is None:
        return out
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    countdown = hours * 60 + minutes
    if countdown < 0 or countdown > 24 * 60:
        return out
    try:
        seen_at = datetime.fromisoformat(str(snapshot).replace("Z", "+00:00"))
    except ValueError:
        return out
    if seen_at.tzinfo is None:
        return out
    start_estimate = (seen_at + timedelta(minutes=countdown)).astimezone(TZ)
    out.update({
        "countdown_minutes": countdown,
        "countdown_observed_at_utc": seen_at.astimezone(timezone.utc).isoformat(),
        "event_start_from_countdown": start_estimate.isoformat(),
        "countdown_difference_minutes": round(abs((fixture_kickoff - start_estimate).total_seconds()) / 60, 2),
    })
    return out

def displayed_header_kickoff(evidence: dict) -> datetime | None:
    """Resolve only an event-scoped PREMATCH clock with an observed date anchor."""
    if evidence.get('browser_timezone') != 'Europe/Madrid' or evidence.get('prematch_header') is not True:
        return None
    try:
        start = datetime.fromisoformat(evidence['capture_started_at_utc'].replace('Z','+00:00'))
        end = datetime.fromisoformat(evidence['captured_at_utc'].replace('Z','+00:00'))
        if not start.tzinfo or not end.tzinfo or not 0 <= (end-start).total_seconds() <= 120:
            return None
        start = start.astimezone(TZ); end = end.astimezone(TZ)
        if start.date() != end.date():return None
        raw_label = str(evidence.get('date_label', '')).strip()
        label = norm(raw_label)
        clock = evidence.get('time_label','').strip()
        if not re.fullmatch(r'\d{1,2}:\d{2}',clock):return None
        hour,minute = map(int,clock.split(':'))
        if hour>23 or minute>59:return None
        if label in ('hoy', 'manana'):
            date = end.date() + timedelta(days=int(label=='manana'))
        else:
            m_date = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', raw_label)
            if not m_date:
                return None
            d_val, m_val, y_val = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
            if y_val < 100:
                y_val += 2000
            date = datetime(y_val, m_val, d_val).date()
        kickoff = datetime(date.year,date.month,date.day,hour,minute,tzinfo=TZ)
        # A displayed clock without an offset cannot disambiguate a DST fold.
        if kickoff.replace(fold=0).utcoffset() != kickoff.replace(fold=1).utcoffset():return None
        if kickoff <= end:return None
        return kickoff
    except (KeyError,ValueError,TypeError,AttributeError):
        return None


def catalog_relative_kickoff(event: dict) -> datetime | None:
    """Resolve an event-anchor Hoy/Mañana clock from its retained observation."""
    text=str(event.get('text') or '')
    observed=event.get('text_observed_at_utc')
    match=re.search(r'\b(Hoy|Mañana)\s*/\s*(\d{1,2}):(\d{2})\b',text,re.I)
    if not match or not observed:return None
    try:seen=datetime.fromisoformat(str(observed).replace('Z','+00:00')).astimezone(TZ)
    except (ValueError,TypeError):return None
    hour,minute=int(match.group(2)),int(match.group(3))
    if hour>23 or minute>59:return None
    date=seen.date()+timedelta(days=int(norm(match.group(1))=='manana'))
    result=datetime(date.year,date.month,date.day,hour,minute,tzinfo=TZ)
    if result.replace(fold=0).utcoffset()!=result.replace(fold=1).utcoffset():return None
    return result if result>seen else None


def event_absolute_kickoff(raw_html: str) -> datetime | None:
    """Parse a single SportsEvent JSON-LD startDate, never unrelated sidebar dates."""
    event_starts = set()
    for script in re.findall(r'<script\b[^>]*type=[\'\"]application/ld\+json[\'\"][^>]*>(.*?)</script>',
                             raw_html, re.I | re.S):
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, ValueError):
            continue
        def walk(value):
            if isinstance(value, list):
                for v in value: walk(v)
            elif isinstance(value, dict):
                types = value.get('@type', [])
                if isinstance(types, str): types = [types]
                if any(str(t).lower() == 'sportsevent' for t in types):
                    val = value.get('startDate')
                    if isinstance(val, str):
                        try:
                            dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
                            if dt.tzinfo is not None:
                                event_starts.add(dt.astimezone(TZ).isoformat())
                        except ValueError:
                            pass
                for v in value.values(): walk(v)
        walk(data)
    if len(event_starts) != 1:
        return None
    return datetime.fromisoformat(next(iter(event_starts)))


def read_candidates(path: Path, fixture_ids: list[int], top: int, include_past: bool,
                    now: datetime) -> tuple[list[dict], list[dict]]:
    with path.open("r", encoding="utf-8-sig") as fh:
        if path.suffix.lower() == ".json":
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                # Phase 1 consumes the lightweight discovery contract directly
                # (fixtures[]), while legacy preselection keeps matches[].
                data = loaded.get("matches")
                if data is None:
                    data = loaded.get("fixtures", loaded)
            else:
                data = loaded
        elif path.suffix.lower() == ".csv":
            data = list(csv.DictReader(fh))
        else:
            raise ValueError("--input debe ser un .json de preselection o un .csv de ranking")
    if not isinstance(data, list):
        raise ValueError("El archivo no contiene matches[]")
    gathered = []
    for item in data:
        # Raw discovery snapshots may predate category enrichment. Reuse the
        # canonical classifier here without rewriting the source snapshot.
        from filter_major_leagues import explicit_competition_category, gender_for, matching_eligibility
        item=dict(item)
        explicit=explicit_competition_category(item)
        if explicit and str(item.get('gender') or 'UNKNOWN').upper() in {'UNKNOWN',explicit['gender']}:
            item.update(explicit)
        from competition_registry import category
        item.update(category(item))
        if not item.get('gender') or item.get('gender')=='UNKNOWN':
            item['gender']=gender_for(item)
        try:
            fid = int(item.get("fixture_id") or item.get("fixture_id_api_football"))
        except (TypeError, ValueError):
            continue
        kickoff = local_kickoff(item.get("kickoff_madrid") or item.get("kickoff_europe_madrid"))
        score = item.get("research_priority_score")
        if score is None:
            score = item.get("research_priority_score_heuristic")
        try:
            score = float(score) if score not in (None, "") else None
        except (ValueError, TypeError):
            score = None
        gathered.append({
            "fixture_id": fid, "home": item.get("home", ""), "away": item.get("away", ""),
            "league": item.get("league") or item.get("competition") or "",
            "league_id": item.get("league_id"), "season": item.get("season"),
            "league_type":item.get('league_type'),
            "round": item.get("round"), "home_id": item.get("home_id"),
            "away_id": item.get("away_id"), "kickoff_utc": item.get("kickoff_utc"),
            "country": item.get("country", ""), "gender": item.get("gender"),
            "category_status": item.get("category_status") or item.get("filter_status"),
            "category_evidence_status": item.get("category_evidence_status"),
            "category_level": item.get("category_level"),
            "category_provenance": item.get("category_provenance"),
            "competition_identity_status":item.get('competition_identity_status'),
            "competition_eligibility":item.get('competition_eligibility'),
            "competition_eligibility_reason":item.get('competition_eligibility_reason'),
            "participant_eligibility":item.get('participant_eligibility'),
            "participant_eligibility_reason":item.get('participant_eligibility_reason'),
            "filter_status":item.get('filter_status'),"filter_reason":item.get('filter_reason'),
            "national_team_evidence":item.get('national_team_evidence'),
            "cup_eligibility_evidence":item.get('cup_eligibility_evidence'),
            "kickoff_madrid": kickoff.isoformat() if kickoff else None,
            "research_priority_score": score,
            "details_checked": item.get("details_checked", False),
        })
    mapping = {i["fixture_id"]: i for i in gathered}
    if fixture_ids:
        missing = [i for i in fixture_ids if i not in mapping]
        if missing:
            raise ValueError(f"Fixture ID(s) no están en el input: {missing}")
        requested = [mapping[i] for i in dict.fromkeys(fixture_ids)]
    else:
        priority_order = sorted(mapping.values(), key=lambda i: (
            i["research_priority_score"] is None,
            -(i["research_priority_score"] or 0),
            i["kickoff_madrid"] or "9999",
        ))
        # Every inventory fixture enters candidate discovery. An old category
        # filter is evidence for review, never a reason to hide the fixture.
        # Category/gender conflicts remain hard gates in match_catalog().
        eligible = priority_order
        # Choose the best N *upcoming* fixtures; don't fill a finite quota with
        # started matches. Shared-catalog mode keeps all rows so the skipped
        # temporal state is auditable in its output.
        upcoming = [i for i in eligible if include_past or (
                (k := local_kickoff(i["kickoff_madrid"])) is not None and k > now
        )]
        # ``--top 0`` is the explicit shared-catalog mode: every lightweight
        # fixture is compared against the same catalog, without statistical
        # ranking or batching.
        requested = (sorted(eligible, key=lambda i: (i["kickoff_madrid"] or "9999", i["fixture_id"]))
                     if top == 0 else upcoming[:top])
    ready, skipped = [], []
    for i in requested:
        eligibility,reason=matching_eligibility(i)
        resource_policy=('FULL_DISCOVERY' if eligibility=='ELIGIBLE' else
                         'FULL_DISCOVERY_CLASSIFICATION_WARNING' if eligibility=='PENDING_CLASSIFICATION' else
                         'NO_SEARCH')
        i.update(matching_eligibility=eligibility,matching_eligibility_reason=reason,
                 discovery_resource_policy=resource_policy)
        if eligibility=='EXCLUDED':
            skipped.append({**i,'status':'SKIPPED_CATEGORY_EXCLUDED','bwin_event_id':None})
            continue
        kickoff = local_kickoff(i["kickoff_madrid"])
        if not include_past and kickoff and kickoff <= now:
            skipped.append({**i, "status": "SKIPPED_ALREADY_KICKED_OFF", "bwin_event_id": None})
        elif not include_past and kickoff is None:
            skipped.append({**i, "status": "SKIPPED_NO_KICKOFF", "bwin_event_id": None})
        else:
            ready.append(i)
    return ready, skipped


def browser_collect_anchors(page, max_scrolls: int = 8, trace: dict | None = None) -> list[dict]:
    """Read public event links while scrolling; stop after 3 *truly* stable samples.

    V0.3 compared the cardinality *after* collection against itself; this meant
    the scraper quit after 3 scrolls even when new events were still appearing.
    """
    snippet = r"""() => Array.from(document.querySelectorAll('a[href*="/eventos/"]')).map(a => {
      const tile = a.closest('ms-tree-event, .event-item');
      const ids = tile ? new Set(Array.from(tile.querySelectorAll('a[href*="/eventos/"]')).map(x => (x.href.match(/:(\d{5,12})(?:[/?#]|$)/)||[])[1]).filter(Boolean)) : new Set();
      const ownId = (a.href.match(/:(\d{5,12})(?:[/?#]|$)/)||[])[1];
      const scoped = tile && ids.size === 1 && ids.has(ownId);
      return {url:a.href, text:((scoped ? tile.innerText : a.innerText) || a.textContent || '').trim().replace(/\s+/g,' ').slice(0,600),
      title:a.title || '', aria:a.getAttribute('aria-label') || '', text_source:scoped ? 'SINGLE_EVENT_TILE' : 'EVENT_ANCHOR'};
    })"""
    seen = {}; samples=[];load_more_clicks=0
    stable = 0
    try:page.evaluate("window.scrollTo(0,0)")
    except Exception:pass
    for scroll_number in range(max(0, max_scrolls) + 1):
        previous_count = len(seen)
        anchors = page.evaluate(snippet)
        captured_at_utc = datetime.now(timezone.utc).isoformat()
        for a in anchors:
            event_id = extract_event_id(a.get("url", ""))
            if event_id:
                text = " ".join(str(a.get(k, "")) for k in ("text", "title", "aria")).strip()
                if event_id not in seen or len(text) > len(seen[event_id]["text"]):
                    seen[event_id] = {"bwin_event_id": event_id, "bwin_url": a["url"],
                                      "text": text, "text_source": a.get("text_source","EVENT_ANCHOR"), "text_observed_at_utc": captured_at_utc}
        try:
            geometry=page.evaluate(r"""() => {
              const boxes=Array.from(document.querySelectorAll('body *')).filter(e=>{
                const s=getComputedStyle(e);return e.scrollHeight>e.clientHeight+8 && /(auto|scroll)/.test(s.overflowY)
              });
              return {y:window.scrollY,h:window.innerHeight,full:Math.max(document.body.scrollHeight,document.documentElement.scrollHeight),
                scrollable_containers:boxes.length,container_positions:boxes.slice(0,8).map(e=>({top:e.scrollTop,h:e.clientHeight,full:e.scrollHeight}))};
            }""")
        except Exception:geometry={}
        at_bottom=bool(geometry and geometry.get('y',0)+geometry.get('h',0)>=geometry.get('full',1)-4)
        samples.append({'iteration':scroll_number,'dom_anchors':len(anchors),'unique_events':len(seen),'at_bottom':at_bottom,**geometry})
        stable = stable + 1 if len(seen) == previous_count else 0
        no_scroll_surface=bool(geometry and geometry.get('full',0)<=geometry.get('h',0)+4 and not geometry.get('scrollable_containers',0))
        if stable >= 5 and (at_bottom or no_scroll_surface) or scroll_number == max_scrolls:
            break
        clicked=False
        try:
            clicked=bool(page.evaluate(r"""() => {
              const labels=/^(mostrar|ver|cargar)\s+m[aá]s$/i;
              const x=Array.from(document.querySelectorAll('button,a')).find(e=>labels.test((e.innerText||e.textContent||'').trim()) && e.offsetParent!==null);
              if(x){x.click();return true} return false;
            }"""))
        except Exception:pass
        if clicked:load_more_clicks+=1
        try:
            page.evaluate("""() => {window.scrollTo(0,Math.max(document.body.scrollHeight,document.documentElement.scrollHeight));
              for(const e of document.querySelectorAll('body *')) {const s=getComputedStyle(e);
                if(e.scrollHeight>e.clientHeight+8 && /(auto|scroll)/.test(s.overflowY)) e.scrollTop=e.scrollHeight;}}""")
        except Exception:
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 1.6, 800))")
        page.wait_for_timeout(550)
    if trace is not None:trace.update({'scroll_samples':samples,'load_more_clicks':load_more_clicks,
        'max_scrolls':max_scrolls,'termination':'MAX_SCROLLS' if samples and samples[-1]['iteration']==max_scrolls else
        'STABLE_NO_SCROLL_SURFACE' if samples and samples[-1].get('full',0)<=samples[-1].get('h',0)+4 and not samples[-1].get('scrollable_containers',0) else 'STABLE_AT_BOTTOM'})
    return list(seen.values())


def fetch_catalog(browser, base_url: str, page_paths: list[str], max_scrolls: int,
                  navigation_timeout: int) -> tuple[list[dict], list[dict]]:
    catalog, diagnostics = {}, []
    page = browser.new_page(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1440, "height": 900})
    request_count=0;failed_requests=[]
    if hasattr(page,'on'):
        def requested(_request):
            nonlocal request_count;request_count+=1
        page.on('request',requested)
        page.on('requestfailed',lambda request:failed_requests.append(str(request.url)[:300]))
    # One public page per discovery cycle.  Repeated paths from CLI, fallback
    # routing or country resolution must not trigger repeated navigation.
    for path in dict.fromkeys(page_paths):
        url = urljoin(base_url, path)
        d = {"catalog_page": url, "status": "NOT_ATTEMPTED", "events_found": 0, "error": None,
             'started_at_utc':datetime.now(timezone.utc).isoformat()}
        before_requests=request_count
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout)
            page.wait_for_timeout(1300)
            if response and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            # Do not click sportsbook controls: this stage is read-only.
            trace={};found = browser_collect_anchors(page, max_scrolls=max_scrolls,trace=trace)
            d["events_found"] = len(found)
            d['collection_trace']=trace
            d["status"] = "OK" if found else "EMPTY_OR_BLOCKED"
            if not found:
                d["error"] = f"Sin enlaces de eventos en DOM; título={page.title()[:120]!r}"
            for event in found:
                eid = event["bwin_event_id"]
                if urlparse(event["bwin_url"]).hostname != urlparse(base_url).hostname:
                    continue  # No seguir enlaces que salgan del dominio aprobado
                event.setdefault("source_pages", [])
                event["source_pages"].append(url)
                if eid in catalog:
                    catalog[eid]["source_pages"].append(url)
                    if len(event["text"]) > len(catalog[eid]["text"]):
                        catalog[eid]["text"] = event["text"]
                        catalog[eid]["text_observed_at_utc"] = event.get("text_observed_at_utc")
                else:
                    catalog[eid] = event
        except Exception as exc:
            d["status"] = "ERROR"
            d["error"] = f"{type(exc).__name__}: {exc}"[:500]
        d['completed_at_utc']=datetime.now(timezone.utc).isoformat();d['requests_observed']=request_count-before_requests
        d['failed_requests_observed']=failed_requests[-20:]
        diagnostics.append(d)
    page.close()
    return list(catalog.values()), diagnostics


# Traduce países API-Football a nombres del menú de Bwin. Las URLs se LEEN del DOM.
COUNTRY_ES = {
    "usa": "estados unidos", "us": "estados unidos", "united states": "estados unidos",
    "united states of america": "estados unidos", "canada": "canada",
    "spain": "espana", "italy": "italia", "england": "inglaterra",
    "france": "francia", "germany": "alemania", "norway": "noruega",
    "sweden": "suecia", "denmark": "dinamarca", "finland": "finlandia",
    "switzerland": "suiza", "belgium": "belgica", "austria": "austria",
    "netherlands": "paises bajos", "portugal": "portugal", "scotland": "escocia",
    "poland": "polonia", "greece": "grecia", "turkey": "turquia",
    "czech republic": "republica checa", "croatia": "croacia",
    "serbia": "serbia", "romania": "rumania", "brazil": "brasil",
    "argentina": "argentina", "chile": "chile", "colombia": "colombia",
    "ecuador": "ecuador", "uruguay": "uruguay", "paraguay": "paraguay",
    "bolivia": "bolivia", "peru": "peru", "mexico": "mexico",
    "japan": "japon", "south korea": "corea", "saudi arabia": "arabia saudi",
    "morocco": "marruecos", "egypt": "egipto", "south africa": "sudafrica",
}


def country_label(api_country: str) -> str:
    key = norm(api_country)
    return COUNTRY_ES.get(key, key)


def is_country_page_url(url: str, base_url: str) -> bool:
    """Solo ruta pública /fútbol-4/apuestas/<país>-<id> (excluye liga y externos)."""
    parsed = urlparse(url)
    if parsed.hostname != urlparse(base_url).hostname:
        return False
    path = unquote(parsed.path).rstrip("/")
    return bool(re.fullmatch(r"/es/sports/fútbol-4/apuestas/[^/]+-\d+", path, flags=re.I))


def is_betting_navigation_url(url: str, base_url: str) -> bool:
    """Accept real country or nested competition routes from Bwin's DOM."""
    parsed=urlparse(url)
    if parsed.hostname!=urlparse(base_url).hostname:return False
    path=unquote(parsed.path).rstrip('/')
    segment=r'[^/]+-\d+(?::\d+)?'
    return bool(re.fullmatch(rf'/es/sports/fútbol-4/apuestas/{segment}(?:/{segment})?',path,re.I))


def parse_country_nav(anchors: list[dict], base_url: str) -> dict[str, str]:
    """Extract country URLs from actual DOM <a href>, never construct country IDs.

    Uses the country slug as a fallback label if Bwin renders the visible text in a
    nested control or abbreviates "Todos <country>" in the anchor itself.
    """
    found = {}
    for item in anchors:
        url = str(item.get("url") or "")
        if not is_betting_navigation_url(url, base_url):
            continue
        country_slug = unquote(urlparse(url).path).rstrip('/').rsplit('/', 1)[-1]
        country_slug = re.sub(r"-\d+$", "", country_slug)
        country_from_path = norm(country_slug.replace('-', ' '))
        label = norm(item.get("text") or "")
        from_text = re.sub(r"\s+\d+$", "", label.removeprefix("todos ")).strip()
        # URL is a real anchor.  When there is no recognizable country label,
        # use the slug extracted FROM that real URL, not a guessed URL.
        nested='/apuestas/' in unquote(urlparse(url).path) and len(
            unquote(urlparse(url).path).split('/apuestas/',1)[1].split('/'))>1
        country = (label if nested and label else
                   from_text if label.startswith('todos ') and from_text else country_from_path)
        if country:
            found.setdefault(country, url)
    return found


# These are previously verified PUBLIC *country page* URLs, not bookmaker event IDs.
# Only used when Bwin's dynamic navigation does not expose the href in Playwright.
# They are visited and validated during each run; existence is never assumed.
VERIFIED_PUBLIC_COUNTRY_PAGES = {
    'estados unidos': 'https://www.bwin.es/es/sports/f%C3%BAtbol-4/apuestas/estados-unidos-39',
    'canada': 'https://www.bwin.es/es/sports/f%C3%BAtbol-4/apuestas/canad%C3%A1-94',
}

# Public competition routes observed and verified previously. They are a
# fallback route only: every visited page is still parsed and every event must
# pass team, order, competition, category and time gates independently.
VERIFIED_PUBLIC_COMPETITION_PAGES = {
    5: 'https://www.bwin.es/es/sports/f%C3%BAtbol-4/apuestas/europa-7/uefa-nations-league-0:4',
}


def competition_navigation_url(fixture: dict, navigation: dict[str,str], base_url: str) -> tuple[str|None,str]:
    """Resolve a real competition link, anchored by API league identity."""
    from competition_registry import lookup
    if fixture.get('league_id') != 5 and lookup(fixture) is None:
        return None,'NO_CURATED_COMPETITION_ROUTE'
    scored=[]
    for label,url in navigation.items():
        score=competition_similarity(fixture.get('league',''),label,league_id=fixture.get('league_id'),
            country=fixture.get('country'),season=fixture.get('season'))
        if score>=.95:scored.append((score,url,label))
    scored.sort(reverse=True)
    if scored and (len(scored)==1 or scored[0][0]-scored[1][0]>=.05 or scored[0][1]==scored[1][1]):
        return scored[0][1],'BWIN_DOM_COMPETITION_LINK'
    fallback=VERIFIED_PUBLIC_COMPETITION_PAGES.get(fixture.get('league_id'))
    if fallback and urlparse(fallback).hostname==urlparse(base_url).hostname:
        return fallback,'VERIFIED_PUBLIC_COMPETITION_FALLBACK'
    return None,'COMPETITION_ROUTE_UNRESOLVED'


def read_country_navigation(browser, base_url: str, nav_page_url: str, timeout: int) -> tuple[dict[str, str], dict]:
    """Read navigation anchors, scrolling to activate lazy-loaded country lists."""
    page = browser.new_page(locale="es-ES", timezone_id="Europe/Madrid")
    diag = {"catalog_page": nav_page_url, "kind": "COUNTRY_NAV_DISCOVERY",
            "status": "NOT_ATTEMPTED", "countries_found": 0, "events_found": 0, "error": None}
    try:
        response = page.goto(nav_page_url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_timeout(900)
        if response and response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        js = r"""() => Array.from(document.querySelectorAll('a[href*="/apuestas/"]')).map(a=>({
            url:a.href, text:(a.innerText || a.textContent || a.getAttribute('aria-label') || '')
            .trim().replace(/\s+/g,' ').slice(0,150)
        }))"""
        all_anchors = {}
        stable = 0
        for i in range(11):
            count_before = len(all_anchors)
            for a in page.evaluate(js):
                if is_betting_navigation_url(str(a.get('url') or ''), base_url):
                    all_anchors[str(a['url'])] = a
            stable = stable + 1 if len(all_anchors) == count_before else 0
            if stable >= 3 or i >= 10:
                break
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 1.8, 800))")
            page.wait_for_timeout(250)
        nav = parse_country_nav(list(all_anchors.values()), base_url)
        diag.update({"status": "OK" if nav else "EMPTY_NAV_DOM", "countries_found": len(nav)})
        if not nav:
            diag['error'] = 'Sin enlaces de países en el DOM visible; no prueba que no haya países disponibles'
        return nav, diag
    except Exception as exc:
        diag.update({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:400]})
        return {}, diag
    finally:
        page.close()


def merge_catalog_events(catalog: list[dict], additions: list[dict]) -> list[dict]:
    by_id={event['bwin_event_id']:event for event in catalog}
    for event in additions:
        key=event['bwin_event_id']
        if key not in by_id:by_id[key]=event;continue
        current=by_id[key]
        current['source_pages']=list(dict.fromkeys(current.get('source_pages',[])+event.get('source_pages',[])))
        if len(event.get('text',''))>len(current.get('text','')):
            for field in ('text','text_source','text_observed_at_utc'):current[field]=event.get(field)
    return list(by_id.values())


def extend_catalog_bwin_first(browser,base_url,catalog,max_pages,scrolls,timeout):
    """Visit actual navigation links without using API fixtures to choose them."""
    if max_pages<=0:return catalog,[],{'navigation_links':0,'pages_selected':[]}
    nav={};diagnostics=[]
    for path in ('/es/sports/f%C3%BAtbol-4/apuestas/competiciones','/es/sports/f%C3%BAtbol-4/apuestas'):
        links,diag=read_country_navigation(browser,base_url,urljoin(base_url,path),timeout);diagnostics.append(diag)
        for label,url in links.items():nav.setdefault(label,url)
    selected=[]
    for label,url in sorted(nav.items()):
        if url not in {x['url'] for x in selected}:selected.append({'label':label,'url':url})
        if len(selected)>=max_pages:break
    if selected:
        additions,page_diags=fetch_catalog(browser,base_url,[x['url'] for x in selected],scrolls,timeout)
        for d in page_diags:d['kind']='BWIN_FIRST_NAVIGATION_CATALOG'
        diagnostics.extend(page_diags);catalog=merge_catalog_events(catalog,additions)
    return catalog,diagnostics,{'navigation_links':len(nav),'pages_selected':selected}


def extend_catalog_for_missing(browser, base_url: str, catalog: list[dict],
                               selected: list[dict], max_countries: int,
                               scrolls: int, timeout: int) -> tuple[list[dict], list[dict], dict]:
    """Visit only country pages needed by unmatched fixtures.

    First read public Bwin competition navigation; then use only explicitly
    verified known public country URLs if the dynamic navigation is incomplete.
    """
    started=time.perf_counter()
    # Classification is not event identity. Unknown tiers may create bounded
    # navigation demand; only positively excluded fixtures are suppressed.
    from filter_major_leagues import matching_eligibility
    missing = [f for f in selected
               if (f.get('matching_eligibility') or matching_eligibility(f)[0])!='EXCLUDED'
               and not match_catalog(f, catalog)]
    if not missing or max_countries <= 0:
        return catalog, [], {"requested_countries": [], "country_urls_found": 0,
                             "country_url_sources": {}, "country_pages": {}}
    wanted=[]
    for fixture in missing:
        for label in (country_label(fixture.get('country','')),norm(fixture.get('league',''))):
            if label and label not in {'world','mundo','international'} and label not in wanted:wanted.append(label)
    nav = {}
    diagnostics = []
    for path in ('/es/sports/f%C3%BAtbol-4/apuestas/competiciones',
                 '/es/sports/f%C3%BAtbol-4/apuestas'):
        if all(k in nav for k in wanted):
            break
        links, diag = read_country_navigation(browser, base_url, urljoin(base_url, path), timeout)
        diagnostics.append(diag)
        for k, url in links.items():
            nav.setdefault(k, url)
    competition_pages={};competition_sources={}
    for fixture in missing:
        url,source=competition_navigation_url(fixture,nav,base_url)
        if url:
            competition_pages[str(fixture.get('league_id'))]=url
            competition_sources[str(fixture.get('league_id'))]=source
    competition_urls=list(dict.fromkeys(competition_pages.values()))[:max_countries]
    if competition_urls:
        additions,page_diags=fetch_catalog(browser,base_url,competition_urls,scrolls,timeout)
        for d in page_diags:d['kind']='TARGETED_COMPETITION_CATALOG'
        diagnostics.extend(page_diags);catalog=merge_catalog_events(catalog,additions)
        missing=[f for f in missing if not match_catalog(f,catalog)]
    # International competitions are resolved by competition, never by the
    # synthetic API country "World" or arbitrary national menu pages.
    wanted=[]
    for fixture in missing:
        if fixture.get('league_id')==5:continue
        for label in (country_label(fixture.get('country','')),norm(fixture.get('league',''))):
            if label and label not in {'world','mundo','international'} and label not in wanted:wanted.append(label)
    resolved, sources = {}, {}
    for k in wanted:
        exact=nav.get(k)
        if not exact:
            scored=sorted(((competition_similarity(k,label),url,label) for label,url in nav.items()),reverse=True)
            if scored and scored[0][0]>=.92 and (len(scored)==1 or scored[0][0]-scored[1][0]>=.05):exact=scored[0][1]
        if exact:
            resolved[k], sources[k] = exact, 'BWIN_DOM_CONTEXT_LINK'
        else:
            fallback = VERIFIED_PUBLIC_COUNTRY_PAGES.get(k)
            if fallback and is_country_page_url(fallback, base_url):
                resolved[k], sources[k] = fallback, 'VERIFIED_PUBLIC_PAGE_FALLBACK'
            else:
                resolved[k], sources[k] = None, 'UNRESOLVED'
    urls = list(dict.fromkeys(u for u in resolved.values() if u and u not in competition_urls))[:max(0,max_countries-len(competition_urls))]
    if urls:
        extra_catalog, extra_diagnostics = fetch_catalog(browser, base_url, urls, scrolls, timeout)
        for d in extra_diagnostics:
            d["kind"] = "TARGETED_COUNTRY_CATALOG"
        diagnostics.extend(extra_diagnostics)
        catalog=merge_catalog_events(catalog,extra_catalog)
    return catalog, diagnostics, {
        "requested_countries": [country_label(f.get('country','')) for f in missing if f.get('league_id')!=5],
        "requested_contexts": wanted, "country_urls_found": len(urls),
        "country_pages": resolved, "country_url_sources": sources,
        "competition_pages":competition_pages,"competition_url_sources":competition_sources,
        "navigation_urls_deduplicated":len(competition_pages)+sum(bool(u) for u in resolved.values())-len(set(competition_pages.values())|set(u for u in resolved.values() if u)),
        "unresolved_contexts":[k for k,v in resolved.items() if not v],
        "deferred_navigation_urls":[u for u in dict.fromkeys(resolved.values()) if u and u not in urls and u not in competition_urls],
        "duration_seconds":round(time.perf_counter()-started,6),
    }


def match_catalog(fixture: dict, catalog: list[dict]) -> list[dict]:
    from filter_major_leagues import matching_eligibility
    if matching_eligibility(fixture)[0]=='EXCLUDED':return []
    candidates = []
    national_context = is_national_team_context(fixture)
    for e in catalog:
        if LIVE_RE.search(e.get("text", "")):
            continue
        parsed = unquote(urlparse(e["bwin_url"]).path)
        slug = parsed.split("/eventos/")[-1].rsplit(":", 1)[0]
        # ``-2`` is Bwin's event-route marker, not part of either team name.
        # Keeping it polluted numeric club names (e.g. synthetic/academy names)
        # and could retrieve unrelated events merely because both contained 2.
        slug = re.sub(r"-2$", "", slug)
        evidence = f'{e["text"]} {slug}'
        name_evidence = identity_name_evidence(e.get("text", ""), slug)
        home = team_similarity(fixture["home"], name_evidence,national_context=national_context)
        away = team_similarity(fixture["away"], name_evidence,national_context=national_context)
        if min(home, away) < 0.82:
            continue
        order_status = team_order_status(fixture["home"], fixture["away"], name_evidence,
                                         national_context=national_context)
        category_status = category_compatibility(fixture, evidence)
        if order_status in {"AWAY_HOME_CONFLICT", "AWAY_HOME_TOKEN_CONFLICT", "AWAY_HOME_ALIAS_CONFLICT"}:
            continue
        if category_status in {"GENDER_CONFLICT", "NON_ADULT_CONFLICT"}:
            continue
        score = round((home + away) / 2, 4)
        competition_score = competition_similarity(str(fixture.get("league") or ""), evidence,
            league_id=fixture.get('league_id'),country=fixture.get('country'),season=fixture.get('season')) if fixture.get("league") else None
        candidates.append({**e, "name_score": score, "home_similarity": home, "away_similarity": away,
                           "home_name_match_method": team_name_match_method(
                               fixture["home"], name_evidence, national_context=national_context),
                           "away_name_match_method": team_name_match_method(
                               fixture["away"], name_evidence, national_context=national_context),
                           "name_match_status": (
                               "VERIFIED_ALIAS_USED"
                               if "VERIFIED_ALIAS" in {
                                   team_name_match_method(fixture["home"], name_evidence, national_context=national_context),
                                   team_name_match_method(fixture["away"], name_evidence, national_context=national_context),
                               }
                               else "LITERAL_NAMES"
                           ),
                           "team_order_status": order_status,
                           "category_compatibility": category_status,
                           "competition_similarity": competition_score})
    return sorted(candidates, key=lambda i: i["name_score"], reverse=True)


def candidate_possibilities(fixture: dict, catalog: list[dict], direct_bindings: dict[int, dict]) -> list[dict]:
    """Apply the normal semantic gates to a persisted direct route as well."""
    binding = direct_bindings.get(int(fixture['fixture_id']))
    return match_catalog(fixture, [binding] if binding else catalog)


def load_local_catalog_snapshots(paths: list[Path]) -> list[dict]:
    """Read frozen catalogs without opening Bwin; later files win by Event ID."""
    merged={}
    for path in paths:
        document=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if isinstance(document,list):rows=document
        elif isinstance(document,dict):rows=document.get('catalog_observed',document.get('events'))
        else:rows=None
        if not isinstance(rows,list):raise ValueError(f'CATALOG_SNAPSHOT_INVALID:{path}')
        for row in rows:
            event_id=str(row.get('bwin_event_id') or row.get('event_id') or '')
            if not event_id.isdigit() or not row.get('bwin_url'):continue
            merged[event_id]={**row,'bwin_event_id':event_id}
    return list(merged.values())


def pending_local_result(fixture: dict, possible: list[dict]) -> dict:
    """Retain local evidence for a pending fixture without inspection/navigation."""
    out={**fixture,'status':'NOT_SEARCHED_PENDING_CLASSIFICATION','identity_status':'NOT_VERIFIED',
         'bwin_event_id':None,'bwin_url':None,'candidates_found':len(possible),'alternatives':[],
         'markets':None,'prematch_authorization':'NOT_GRANTED','actionable_bet':False,
         'inspection':None,'discovery_diagnostic':'PENDING_CLASSIFICATION_LOCAL_CATALOG_ONLY'}
    if not possible:return out
    out['alternatives']=[{'event_id':row['bwin_event_id'],'url':row['bwin_url'],
                          'score':row['name_score'],'text':row.get('text','')[:160],
                          'name_match_status':row.get('name_match_status'),
                          'home_name_match_method':row.get('home_name_match_method'),
                          'away_name_match_method':row.get('away_name_match_method')}
                         for row in possible[:5]]
    if len(possible)>1 and possible[0]['name_score']-possible[1]['name_score']<.05:
        out['status']='PENDING_CLASSIFICATION_AMBIGUOUS_LOCAL_CANDIDATES'
        return out
    winner=possible[0]
    if winner['name_score']<.94:
        out['status']='PENDING_CLASSIFICATION_LOW_CONFIDENCE_LOCAL_CANDIDATE'
        return out
    out.update(status='EVENT_VISIBLE_PENDING_CLASSIFICATION',
               bwin_event_id=winner['bwin_event_id'],bwin_url=winner['bwin_url'],
               name_match_score=winner['name_score'],team_order_status=winner.get('team_order_status'),
               category_compatibility=winner.get('category_compatibility'),
               competition_similarity=winner.get('competition_similarity'),
               event_identity_status='PROVISIONAL_COMPATIBLE',
               category_corroboration_status='PENDING',
               discovery_diagnostic='EVENT_MATCHED_IN_LOCAL_CATALOG_INSPECTION_DEFERRED')
    return out


def visible_without_matching(fixture: dict, catalog: list[dict]) -> list[dict]:
    """Diagnostic shortlist only; it never supplies a candidate identity."""
    national_context = is_national_team_context(fixture)
    rows=[];fixture_start=local_kickoff(fixture.get('kickoff_madrid'))
    for event in catalog:
        if LIVE_RE.search(str(event.get('text') or '')):continue
        parsed=unquote(urlparse(event.get('bwin_url','')).path)
        slug=re.sub(r'-2$','',parsed.split('/eventos/')[-1].rsplit(':',1)[0])
        evidence=identity_name_evidence(event.get('text',''),slug)
        home=team_similarity(fixture.get('home',''),evidence,national_context=national_context)
        away=team_similarity(fixture.get('away',''),evidence,national_context=national_context)
        if min(home,away)<.60 or (home+away)/2<.80:continue
        order=team_order_status(fixture.get('home',''),fixture.get('away',''),evidence,
                                national_context=national_context)
        category=category_compatibility(fixture,evidence)
        if order.startswith('AWAY_HOME') or category in {'GENDER_CONFLICT','NON_ADULT_CONFLICT'}:continue
        event_start=catalog_relative_kickoff(event)
        difference=(abs((fixture_start-event_start).total_seconds())/60
                    if fixture_start is not None and event_start is not None else None)
        if (difference is not None and
                difference > MAX_VERIFIED_ABSOLUTE_KICKOFF_DIFFERENCE_MINUTES):
            continue
        rows.append({'event_id':event.get('bwin_event_id'),'url':event.get('bwin_url'),
                     'home_similarity':home,'away_similarity':away,
                     'average_similarity':round((home+away)/2,4),'team_order_status':order,
                     'kickoff_difference_minutes':round(difference,2) if difference is not None else None})
    return sorted(rows,key=lambda row:row['average_similarity'],reverse=True)[:5]


def coverage_for_fixture(fixture: dict, pages: list[dict], extension_info: dict,
                         *, candidate_visible: bool, unmatched_visible: list[dict] | None = None) -> dict:
    """Fixture-scoped coverage evidence; MAX_SCROLLS is always partial."""
    if candidate_visible:
        return {'status':'EVENT_VISIBLE_IN_LOCAL_CATALOG','pages':[],'exhaustive':False}
    if unmatched_visible:
        return {'status':'EVENT_VISIBLE_WITHOUT_MATCHING','pages':[],'exhaustive':False,
                'unmatched_visible_events':unmatched_visible}
    country=country_label(fixture.get('country',''))
    league_id=str(fixture.get('league_id'))
    urls=[]
    competition_url=extension_info.get('competition_pages',{}).get(league_id)
    country_url=extension_info.get('country_pages',{}).get(country)
    for value in (competition_url,country_url):
        if value and value not in urls:urls.append(value)
    unresolved=(country in extension_info.get('unresolved_contexts',[]) and not urls)
    scoped=[row for row in pages if row.get('catalog_page') in urls]
    terminal={'STABLE','NO_NEW_EVENTS','AT_BOTTOM','EXHAUSTED','STABLE_AT_BOTTOM'}
    exhaustive=bool(scoped and all(row.get('status')=='OK' and
        (row.get('collection_trace') or {}).get('termination') in terminal for row in scoped))
    if unresolved:status='NAVIGATION_UNRESOLVED'
    elif exhaustive:status='EVENT_NOT_OBSERVED_IN_COMPLETE_SCOPE'
    else:status='CATALOG_PARTIAL'
    return {'status':status,'pages':[row.get('catalog_page') for row in scoped],
            'page_terminations':[(row.get('collection_trace') or {}).get('termination') for row in scoped],
            'exhaustive':exhaustive}


# Market headings are *provisional indicators*, not verified selectable bets.
MARKET_PATTERNS = {
    "result": r"^(resultado del partido|resultado 1x2|ganador del partido)(?:\s*[-(].*)?$",
    "goals": r"^(total de goles|ambos equipos marcan|.+ - total de goles|total de goles - .+)$",
    "corners": r"^(total de c[oó]rners|c[oó]rners - .+|.+ - total de c[oó]rners|h[aá]ndicap de c[oó]rners.*)$",
    "cards": r"^(total de tarjetas|tarjetas - .+|recibe una tarjeta|.+ - total de tarjetas|tarjetas amarillas.*)$",
    "shots": r"^(jugador - total de tiros(?: a porter[ií]a)?|.+ - total de tiros(?: a porter[ií]a)?|equipo - total de tiros.*)$",
    "fouls": r"^(jugador - total de faltas|.+ - total de faltas|total de faltas.*)$",
    "offsides": r"^(total de fueras? de juego|.+ - fueras? de juego|total de offsides)$",
}


def market_indicators(body_text: str) -> dict:
    """Exact-looking market heading lines, not broad tab names or promos."""
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    found = {}
    for key, pat in MARKET_PATTERNS.items():
        matches = []
        for line in lines:
            if len(line) > 115:
                continue
            if re.match(pat, line, re.I):
                if line not in matches:
                    matches.append(line)
        found[key] = {"status": "HEADING_OBSERVED_NOT_PRICE_VERIFIED" if matches else "NOT_OBSERVED",
                      "headings": matches[:12]}
    return found


def inspect_event(browser, result: dict, fixture: dict, navigation_timeout: int,
                  load_method: str = 'DOMCONTENTLOADED') -> dict:
    started=time.perf_counter()
    page = browser.new_page(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1440, "height": 900})
    observation = {"status": "NOT_INSPECTED", "page_title": None, "event_id_from_url": None,
                   "event_start_machine_readable": None, "kickoff_difference_minutes": None,
                   "market_headings": {}, "market_heading_families": 0,
                   "is_live": None, "identity_status": "NOT_VERIFIED", "time_evidence_type": "NONE",
                   "competition_match_score": None,'load_method':load_method}
    try:
        capture_started = datetime.now(timezone.utc).isoformat()
        wait_until='commit' if load_method=='COMMIT_THEN_VALIDATE' else 'domcontentloaded'
        method_timeout=min(navigation_timeout,6000) if load_method=='COMMIT_THEN_VALIDATE' else navigation_timeout
        navigation_error=None
        try:
            response = page.goto(result["bwin_url"], wait_until=wait_until, timeout=method_timeout)
        except Exception as exc:
            if 'Timeout' not in type(exc).__name__:raise
            navigation_error=exc
            observation['navigation_warning']=str(exc)[:300]
            # Hydrated DOM may already exist after a navigation timeout.
            # All ID/title/clock/category gates below still apply.
            if extract_event_id(page.url)!=result['bwin_event_id']:raise
            response=None
        # Do not immediately discard a target URL whose DOM is still hydrating.
        # This is recovery on the same navigation, not an identical second goto.
        for _ in range(8):
            if norm(page.title()) not in ('','bwin'):break
            page.wait_for_timeout(350)
        if navigation_error is not None and norm(page.title()) in ('','bwin'):
            raise navigation_error
        page.wait_for_timeout(700)
        if response and response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        observation["status"] = "OPENED"
        # The event shell may finish navigation before the real title is hydrated.
        # Wait only for a generic shell; a different team title still fails identity.
        for _ in range(12):
            title = page.title()
            if norm(title) not in ('', 'bwin'):
                break
            page.wait_for_timeout(500)
        # Wait briefly for Angular scoreboard header (.event-time) or JSON-LD to hydrate
        header = page.locator('ms-event-details-main ms-prematch-scoreboard .event-time')
        for _ in range(8):
            if header.count() == 1 and header.locator('.date').count() == 1 and header.locator('.time').count() == 1:
                break
            if event_absolute_kickoff(page.content()) is not None:
                break
            page.wait_for_timeout(400)
        observation["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        observation["page_title"] = page.title()
        observation["event_id_from_url"] = extract_event_id(page.url)
        body = page.locator("body").inner_text(timeout=10000)
        markup = page.content()
        header = page.locator('ms-event-details-main ms-prematch-scoreboard .event-time')
        header_evidence = None
        if header.count() == 1 and header.locator('.date').count() == 1 and header.locator('.time').count() == 1:
            header_evidence = {'prematch_header': True,
                'browser_timezone':page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone'),
                'date_label':header.locator('.date').inner_text(),
                'time_label':header.locator('.time').inner_text(),
                'capture_started_at_utc':capture_started,
                'captured_at_utc':datetime.now(timezone.utc).isoformat(),
                'selector':'ms-event-details-main ms-prematch-scoreboard .event-time'}
        observation['displayed_time_evidence'] = header_evidence
        observation["observed_catalog"] = browser_collect_anchors(page,0)
        observation["market_headings"] = market_indicators(body)
        observation["market_heading_families"] = sum(x["status"].startswith("HEADING_")
                                                     for x in observation["market_headings"].values())
        if fixture.get("league"):
            observation["competition_match_score"] = competition_similarity(fixture["league"], body,
                league_id=fixture.get('league_id'),country=fixture.get('country'),season=fixture.get('season'))
        # The global header often advertises 'Live'. Only inspect close to event header.
        # The event page can display OTHER ongoing fixtures in sidebars.
        # Never infer the current event is live from global body text.
        observation["is_live"] = None
        observed_start = event_absolute_kickoff(markup)
        displayed_start = displayed_header_kickoff(header_evidence or {})
        clock_conflict = bool(observed_start and displayed_start and observed_start != displayed_start)
        time_source = 'ABSOLUTE_JSON_LD' if observed_start else 'SCOPED_PREMATCH_HEADER'
        if not observed_start:observed_start = displayed_start
        if observed_start:
            observation["event_start_machine_readable"] = observed_start.isoformat()
        candidate_start = local_kickoff(fixture["kickoff_madrid"])
        observation["api_kickoff_madrid"] = candidate_start.isoformat() if candidate_start else None
        observation["api_kickoff_utc"] = candidate_start.astimezone(timezone.utc).isoformat() if candidate_start else None
        observation["bwin_observed_time"] = {
            "header_evidence": header_evidence,
            "catalog_text": result.get("text"),
            "catalog_observed_at_utc": result.get("text_observed_at_utc"),
        }
        observation["accredited_kickoff_utc"] = (
            observed_start.astimezone(timezone.utc).isoformat()
            if (observed_start is not None and not clock_conflict)
            else None
        )
        observation.update(countdown_corrob(result, candidate_start))
        if observed_start and candidate_start:
            observation["kickoff_difference_minutes"] = round(abs((candidate_start - observed_start).total_seconds()) / 60, 1)
            observation["time_evidence_type"] = time_source
        elif observation["countdown_minutes"] is not None:
            observation["time_evidence_type"] = "RELATIVE_CATALOG_COUNTDOWN"
        title = norm(observation["page_title"])
        national_context=(norm(fixture.get('country')) in {'world','mundo','international'}
                          and not NON_ADULT_RE.search(str(fixture.get('home',''))+' '+str(fixture.get('away',''))))
        # Title is a supporting check, not an absolute identity test.
        title_ok = (team_similarity(fixture["home"], title,national_context=national_context) >= 0.82
                    and team_similarity(fixture["away"], title,national_context=national_context) >= 0.82)
        observation["title_teams_match"] = title_ok
        observation['page_category_compatibility']=category_compatibility(fixture,observation['page_title'])
        if clock_conflict:
            observation['identity_status'] = 'CONFLICTING_TIME_SOURCES_REVIEW'
            observation['time_verification_status'] = 'TIME_CONFLICT'
        elif observation["event_id_from_url"] != result["bwin_event_id"]:
            observation["identity_status"] = "EVENT_ID_CHANGED_REVIEW"
            observation['time_verification_status'] = 'TIME_UNVERIFIED_EXPLORATORY_ONLY'
        elif not title_ok:
            observation["identity_status"] = "TITLE_MISMATCH_REVIEW"
            observation['time_verification_status'] = 'TIME_UNVERIFIED_EXPLORATORY_ONLY'
        elif observed_start is not None:
            if absolute_kickoff_status(observation["kickoff_difference_minutes"]) == "TIME_VERIFIED":
                observation["identity_status"] = "NAME_AND_ABSOLUTE_TIME_MATCH"
                observation['time_verification_status'] = 'TIME_VERIFIED'
            else:
                observation["identity_status"] = "KICKOFF_MISMATCH_REVIEW"
                observation['time_verification_status'] = 'TIME_MISMATCH'
        elif observation["countdown_difference_minutes"] is not None:
            # Rounded and occasionally delayed countdown. Keep confidence lower than absolute timestamp.
            if observation["countdown_difference_minutes"] <= 4.0:
                observation["identity_status"] = "NAME_AND_RELATIVE_TIME_CORROBORATED"
                observation['time_verification_status'] = 'TIME_CORROBORATED_RELATIVE'
            else:
                observation["identity_status"] = "KICKOFF_COUNTDOWN_MISMATCH_REVIEW"
                observation['time_verification_status'] = 'TIME_MISMATCH'
        else:
            observation["identity_status"] = "NAME_MATCH_TIME_NOT_MACHINE_VERIFIED"
            observation['time_verification_status'] = 'TIME_UNVERIFIED_EXPLORATORY_ONLY'
    except Exception as exc:
        observation["status"] = "ERROR"
        observation["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        observation['duration_seconds']=round(time.perf_counter()-started,6)
        page.close()
    return observation


def summarize(fixture: dict, possible: list[dict], browser, timeout: int, inspect: bool,
              inspection_retries: int = 0, inspection_control: dict | None = None) -> dict:
    out = {**fixture, "status": "NOT_FOUND_IN_SCANNED_PAGES", "bwin_event_id": None,
           "bwin_url": None, "candidates_found": len(possible), "alternatives": [],
           "identity_status": "NOT_VERIFIED", "markets": None}
    if not possible:
        return out
    out["alternatives"] = [{"event_id": i["bwin_event_id"], "url": i["bwin_url"],
                            "score": i["name_score"], "text": i["text"][:160],
                            "name_match_status": i.get("name_match_status"),
                            "home_name_match_method": i.get("home_name_match_method"),
                            "away_name_match_method": i.get("away_name_match_method")}
                           for i in possible[:5]]
    if len(possible) > 1 and possible[0]["name_score"] - possible[1]["name_score"] < 0.05:
        out["status"] = "AMBIGUOUS_MULTIPLE_EVENTS"
        return out
    winner = possible[0]
    if winner["name_score"] < 0.94:
        out["status"] = "LOW_CONFIDENCE_REVIEW"
        return out
    out.update({"status": "POSSIBLE_EVENT_FOUND", "bwin_event_id": winner["bwin_event_id"],
                "bwin_url": winner["bwin_url"], "name_match_score": winner["name_score"],
                "name_match_status": winner.get("name_match_status"),
                "home_name_match_method": winner.get("home_name_match_method"),
                "away_name_match_method": winner.get("away_name_match_method"),
                "team_order_status": winner.get("team_order_status"),
                "category_compatibility": winner.get("category_compatibility"),
                "competition_similarity": winner.get("competition_similarity")})
    if inspect:
        control=inspection_control if inspection_control is not None else {}
        if control.get('open'):
            control['deferred_since_open']=int(control.get('deferred_since_open',0))+1
            probe_interval=int(control.get('probe_interval',20))
            if control['deferred_since_open']>=probe_interval:
                control.update(open=False,consecutive_timeouts=0,deferred_since_open=0)
            else:
                observed={'status':'ERROR','error':'INSPECTION_CIRCUIT_OPEN',
                          'identity_status':'NOT_VERIFIED','market_headings':{},
                          'load_method':'CIRCUIT_OPEN','duration_seconds':0.0,'attempts':[]}
                out['inspection']=observed;out['identity_status']='NOT_VERIFIED'
                out['status']='EVENT_REQUIRES_REVIEW';out['markets']={}
                out['discovery_diagnostic']='INSPECTION_DEFERRED_CIRCUIT_OPEN'
                out.update(event_identity_status='PENDING_OR_CONFLICT',category_corroboration_status='PENDING',
                           prematch_authorization='NOT_GRANTED',actionable_bet=False)
                return out
        attempts=[]
        for attempt in range(inspection_retries+1):
            method='DOMCONTENTLOADED' if attempt==0 else 'COMMIT_THEN_VALIDATE'
            observed = inspect_event(browser, winner, fixture, timeout,method)
            attempts.append({'attempt':attempt+1,'method':method,'status':observed.get('status'),
                             'duration_seconds':observed.get('duration_seconds'),'error':observed.get('error')})
            if observed.get('status')!='ERROR':break
        observed['attempts']=attempts
        observed['final_error_cause']=observed.get('error') if observed.get('status')=='ERROR' else None
        timeout_failure=bool(observed.get('status')=='ERROR' and 'Timeout' in str(observed.get('error')))
        if timeout_failure:
            control['consecutive_timeouts']=int(control.get('consecutive_timeouts',0))+1
            threshold=int(control.get('threshold',3))
            if control['consecutive_timeouts']>=threshold:
                control['open']=True;control['deferred_since_open']=0
        else:
            control['consecutive_timeouts']=0
        expected_gender = str(fixture.get("gender") or "UNKNOWN").upper()
        competition_score = observed.get("competition_match_score")
        competition_ok = (isinstance(competition_score, (int, float)) and
                          not isinstance(competition_score, bool) and competition_score >= 0.82)
        senior_inferred=senior_category_corroborated(fixture,winner.get('category_compatibility'),competition_score,
            observed.get('page_category_compatibility'))
        candidate_category=winner.get("category_compatibility")
        page_category=observed.get('page_category_compatibility')
        category_conflict=(candidate_category in {'GENDER_CONFLICT','NON_ADULT_CONFLICT'} or
                           page_category in {'GENDER_CONFLICT','NON_ADULT_CONFLICT'})
        # Missing registry/category metadata is a warning. Identity only needs
        # the observed event not to contradict the known fixture category.
        category_ok=not category_conflict
        classification_pending=(fixture.get('matching_eligibility')=='PENDING_CLASSIFICATION' or
                                fixture.get('category_evidence_status')!='CORROBORATED')
        raw_identity=observed.get('identity_status')
        provisional=bool(competition_ok and not category_conflict and
            winner.get('team_order_status') in {'HOME_AWAY','HOME_AWAY_TOKEN','HOME_AWAY_ALIAS'} and
            raw_identity in {'NAME_AND_ABSOLUTE_TIME_MATCH','NAME_AND_RELATIVE_TIME_CORROBORATED',
                             'NAME_MATCH_TIME_NOT_MACHINE_VERIFIED'})
        out.update(event_identity_status='PROVISIONAL_COMPATIBLE' if provisional else 'PENDING_OR_CONFLICT',
                   category_corroboration_status='CONFLICT' if category_conflict else 'CORROBORATED' if category_ok else 'PENDING',
                   prematch_authorization='NOT_GRANTED',actionable_bet=False)
        if senior_inferred:observed['category_evidence']='SENIOR_COMPETITION_CORROBORATED'
        observed["supporting_identity_checks"] = {
            "category_compatible": category_ok,
            "competition_compatible": competition_ok,
            "classification_pending": classification_pending,
        }
        warnings=[]
        if classification_pending:warnings.append(fixture.get('matching_eligibility_reason') or 'CLASSIFICATION_PENDING')
        if not competition_ok:warnings.append('COMPETITION_METADATA_NOT_CORROBORATED')
        out['identity_warnings']=warnings
        out["inspection"] = observed
        out['catalog_competition_similarity']=out.get('competition_similarity')
        out['competition_similarity']=competition_score
        out["identity_status"] = observed["identity_status"]
        out["markets"] = observed["market_headings"]
        if category_conflict:
            observed['identity_status']='CATEGORY_CONFLICT'
            out['identity_status']='CATEGORY_CONFLICT'
            out['status']='EVENT_REQUIRES_REVIEW'
        elif observed["identity_status"] == "NAME_AND_ABSOLUTE_TIME_MATCH":
            out["status"] = "IDENTITY_VERIFIED_AUTOMATICALLY"
        elif observed["identity_status"] == "NAME_AND_RELATIVE_TIME_CORROBORATED":
            out["status"] = "EVENT_FOUND_TIME_CORROBORATED"
        elif observed["identity_status"] == "NAME_MATCH_TIME_NOT_MACHINE_VERIFIED":
            out["status"] = "EVENT_FOUND_NEEDS_TIME_CONFIRMATION"
        else:
            out["status"] = "EVENT_REQUIRES_REVIEW"
        if out['status']=='IDENTITY_VERIFIED_AUTOMATICALLY':out['event_identity_status']='VERIFIED'
        if observed.get('status')=='ERROR':out['discovery_diagnostic']='INSPECTION_FAILED'
    return out


def reconcile_catalog_clocks(matches,catalog):
    """Rebuild countdowns from the exact retained observation, not report time."""
    for row in matches:
        inspection=row.get('inspection')
        if not inspection:continue
        samples=[e for e in catalog if str(e.get('bwin_event_id'))==str(row.get('bwin_event_id'))]
        if len(samples)!=1:continue
        sample=samples[0]
        if LIVE_RE.search(sample.get('text','')):
            inspection['is_live']=True
            continue
        if inspection.get('time_evidence_type') in ('ABSOLUTE_JSON_LD','SCOPED_PREMATCH_HEADER'):
            continue  # A weaker catalog countdown cannot replace the event clock.
        clock=countdown_corrob(sample,local_kickoff(row.get('kickoff_madrid')))
        if clock.get('countdown_minutes') is not None:
            inspection.update(clock)
            inspection['time_evidence_type']='RELATIVE_CATALOG_COUNTDOWN'
            if inspection.get('title_teams_match') and clock['countdown_difference_minutes']<=4:
                inspection['identity_status']='NAME_AND_RELATIVE_TIME_CORROBORATED'
                row['identity_status']=inspection['identity_status'];row['status']='EVENT_FOUND_TIME_CORROBORATED'
    return matches


def write_reports(folder: Path, payload: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bwin_discovery.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (folder / "bwin_discovery.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["fixture_id", "home", "away", "kickoff_madrid",
                                                    "league", "status", "identity_status", "bwin_event_id", "bwin_url", "candidates_found",
                                                    "name_match_score", "market_heading_families", "notes"])
        writer.writeheader()
        for row in payload["matches"]:
            obs = row.get("inspection") or {}
            writer.writerow({k: ({
                                     "market_heading_families": obs.get("market_heading_families"),
                                     "notes": obs.get("error") or "REVISAR HORA Y MERCADOS; no son cuotas verificadas"
                                 }.get(k, row.get(k))) for k in writer.fieldnames})
    (folder / "catalog_observed.json").write_text(json.dumps(payload.get("catalog_observed", []), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Bwin discovery PREMATCH — resultado", "",
             f"Fecha de ejecución UTC: {payload['generated_at_utc']}",
             f"Enlaces de eventos observados: {len(payload.get('catalog_observed', []))}", "",
             "| Fixture API | Encuentro | Estado | Event ID Bwin | Mercado (familias de encabezados) |",
             "|---|---|---|---|---:|"]
    for row in payload["matches"]:
        obs = row.get("inspection") or {}
        lines.append(f"| {row['fixture_id']} | {row['home']} – {row['away']} | {row['status']} | {row.get('bwin_event_id') or '—'} | {obs.get('market_heading_families', '—')} |")
    lines += ["", "El enlace se obtiene del catálogo de Bwin. Un encabezado de mercado observado NO prueba que exista una cuota seleccionable.",
              "IDENTITY_VERIFIED_AUTOMATICALLY exige timestamp absoluto; una cuenta atrás solo permite EVENT_FOUND_TIME_CORROBORATED.",
              "Si no aparece el encuentro, significa 'no encontrado en las páginas consultadas', NO 'Bwin no lo ofrece'.", ""]
    (folder / "bwin_discovery.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descubrimiento verificable de partidos PREMATCH en Bwin.es")
    parser.add_argument("--catalog-snapshot",type=Path,action="append",default=[],help="Catálogo archivado: pista de identidad, nunca horario vigente sin revalidación")
    parser.add_argument("--verified-identity-catalog", type=Path,
                        help="Reconciliación congelada Fixture ID/Event ID; solo habilita reinspección directa")
    parser.add_argument("--direct-verified-only", action="store_true",
                        help="No recorre catálogos: abre URLs exactas y vuelve a validar identidad/hora")
    parser.add_argument("--input", type=Path, required=True, help="fixtures_discovered/fixtures.json, preselection.json V1.3 o CSV")
    parser.add_argument("--fixture-id", type=int, action="append", default=[], help="Fixture API-Football a buscar (repetible)")
    parser.add_argument("--top", type=int, default=1, help="Sin fixture-id: top N futuros; 0=todos, sin ranking estadístico")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/bwin_discovery/test"))
    parser.add_argument("--headless", action="store_true", help="Usar Chromium sin ventana visible")
    parser.add_argument('--confirm-external',action='store_true')
    parser.add_argument("--browser-executable", help="Ruta a Chrome/Chromium instalada (opcional)")
    parser.add_argument("--catalog-page", action="append", default=[], help="Ruta/URL de listado a recorrer; repetible")
    parser.add_argument("--max-scrolls", type=int, default=12)
    parser.add_argument("--no-country-discovery", action="store_true", help="Desactivar el segundo barrido por país")
    parser.add_argument("--max-country-pages", type=int, default=8, help="Máximo de páginas nacionales adicionales (default: 8)")
    parser.add_argument('--bwin-first-max-pages',type=int,default=0,help='Máximo de páginas reales de navegación para catálogo Bwin-first')
    parser.add_argument("--timeout", type=int, default=25000, help="Timeout navegación en milisegundos")
    parser.add_argument("--no-inspect", action="store_true", help="Solo identificar links, sin abrir eventos")
    parser.add_argument("--directed-pilot", action="store_true", help="Piloto dirigido: exige 1-10 --fixture-id y limita páginas nacionales")
    parser.add_argument("--inspection-retries",type=int,default=0,
                        help="Recuperación alternativa por evento: 0=solo DOM existente; 1=commit+validación")
    parser.add_argument("--include-past-for-debug", action="store_true", help="No omitir encuentros ya iniciados (solo depuración; nunca PREMATCH)")
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args(argv)
    from prematch_execution_control import require_external_confirmation
    try:require_external_confirmation(args.confirm_external)
    except ValueError as error:parser.error(str(error))
    if args.top < 0:
        parser.error('--top debe ser >=0; 0 significa todos los fixtures')
    if args.directed_pilot and (not args.fixture_id or len(set(args.fixture_id))>10):
        parser.error('--directed-pilot exige entre 1 y 10 --fixture-id distintos')
    if args.directed_pilot and args.max_country_pages>10:
        parser.error('--directed-pilot limita --max-country-pages a 10')
    if args.bwin_first_max_pages<0 or (args.directed_pilot and args.bwin_first_max_pages>10):
        parser.error('--bwin-first-max-pages inválido; en piloto se permiten 0-10')
    if args.directed_pilot and args.bwin_first_max_pages+args.max_country_pages>10:
        parser.error('el presupuesto combinado de páginas Bwin-first y dirigidas no puede superar 10')
    if args.inspection_retries not in (0,1):
        parser.error('--inspection-retries inválido; se permiten 0-1 y nunca dos navegaciones idénticas')
    if args.direct_verified_only and not args.verified_identity_catalog:
        parser.error('--direct-verified-only requiere --verified-identity-catalog')
    now = datetime.now(timezone.utc).astimezone(TZ)
    try:
        chosen, skipped = read_candidates(args.input, args.fixture_id, args.top, args.include_past_for_debug, now)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    eligible_external=[row for row in chosen if row.get('matching_eligibility')=='ELIGIBLE']
    pending_external=[row for row in chosen if row.get('matching_eligibility')=='PENDING_CLASSIFICATION']
    discovery_external=eligible_external+pending_external
    print(f"Selección: {len(eligible_external)} elegibles · {len(pending_external)} clasificación pendiente con descubrimiento · {len(skipped)} excluidos/omitidos")
    if not chosen:
        print("Sin encuentros futuros a buscar. Usa otro fixture o ventana; no se abrirá Bwin.")
        write_reports(args.output_dir, {"schema_version":"0.2-bwin-discovery", "generated_at_utc":datetime.now(timezone.utc).isoformat(),
                                        "catalog_observed": [], "pages": [], "matches": skipped})
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Falta Playwright: pip install playwright && python -m playwright install chromium", file=sys.stderr)
        return 3
    paths = args.catalog_page or list(CALENDAR)
    direct_bindings = {}; identity_audit = None
    if args.verified_identity_catalog:
        try:
            direct_bindings, identity_audit = verified_identity_candidates(args.verified_identity_catalog, chosen)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        if args.direct_verified_only and len(direct_bindings) != len(chosen):
            missing = sorted(set(int(f['fixture_id']) for f in chosen) - set(direct_bindings))
            parser.error(f'VERIFIED_DIRECT_BINDING_MISSING:{missing}')
    with sync_playwright() as p:
        kwargs = {"headless": args.headless, "args": ["--disable-blink-features=AutomationControlled"]}
        if args.browser_executable:
            kwargs["executable_path"] = args.browser_executable
        browser = p.chromium.launch(**kwargs)
        try:
            catalog, pages = (list(direct_bindings.values()), []) if args.direct_verified_only else fetch_catalog(browser, args.base_url, paths, args.max_scrolls, args.timeout)
            if args.catalog_snapshot:
                archived=load_local_catalog_snapshots(args.catalog_snapshot)
                byid={e['bwin_event_id']:e for e in archived}
                byid.update({e['bwin_event_id']:e for e in catalog});catalog=list(byid.values())
            competition_targeted=any(f.get('league_id')==5 for f in discovery_external)
            if args.direct_verified_only:
                bwin_first_pages,bwin_first_info=[],{'status':'SKIPPED_DIRECT_VERIFIED_ROUTE'}
            elif competition_targeted:
                bwin_first_pages,bwin_first_info=[],{'status':'SKIPPED_COMPETITION_TARGETING','pages_selected':[]}
            else:
                catalog,bwin_first_pages,bwin_first_info=extend_catalog_bwin_first(browser,args.base_url,catalog,
                    args.bwin_first_max_pages,args.max_scrolls,args.timeout)
            pages.extend(bwin_first_pages)
            extension_info = {"requested_countries": [], "country_urls_found": 0,
                              'bwin_first':bwin_first_info}
            if not args.no_country_discovery and not args.direct_verified_only:
                catalog, extended_pages, extension_info = extend_catalog_for_missing(
                    browser, args.base_url, catalog, discovery_external, args.max_country_pages,
                    args.max_scrolls, args.timeout)
                pages.extend(extended_pages)
                extension_info['bwin_first']=bwin_first_info
            print(f"Eventos observados en el catálogo: {len(catalog)}")
            if not args.no_country_discovery and not args.direct_verified_only:
                print(f"Búsqueda por países: {extension_info.get('requested_countries', [])} | enlaces nacionales: {extension_info.get('country_urls_found', 0)}")
            for d in pages:
                if d.get('kind') == 'COUNTRY_NAV_DISCOVERY':
                    print(f"  {d.get('status', '?')}: {d.get('catalog_page', '?')} ({d.get('countries_found', 0)} países en menú)")
                else:
                    print(f"  {d.get('status', '?')}: {d.get('catalog_page', '?')} ({d.get('events_found', 0)} eventos)")
                if d.get("error"):
                    print(f"    {d['error'][:160]}")
            results = [];inspection_control={'consecutive_timeouts':0,'threshold':3,'open':False,
                                             'deferred_since_open':0,'probe_interval':20}
            for fixture in chosen:
                possibilities = candidate_possibilities(fixture, catalog, direct_bindings)
                result = summarize(fixture, possibilities, browser, args.timeout,
                                   not args.no_inspect,args.inspection_retries,inspection_control)
                print(f"[{fixture['fixture_id']}] {fixture['home']} - {fixture['away']}: {result['status']}" +
                      (f" | bwin={result['bwin_event_id']}" if result['bwin_event_id'] else ""))
                results.append(result)
                refreshed=(result.get('inspection') or {}).get('observed_catalog',[])
                byid={e['bwin_event_id']:e for e in catalog};byid.update({e['bwin_event_id']:e for e in refreshed});catalog=list(byid.values())
        finally:
            browser.close()
    # Recheck identities not found before the shared catalog grew. The browser
    # is closed, so recovered candidates stay pending inspection.
    for index,result in enumerate(results):
        if result.get('status')!='NOT_FOUND_IN_SCANNED_PAGES':continue
        fixture=next(f for f in chosen if f['fixture_id']==result['fixture_id'])
        possible=candidate_possibilities(fixture,catalog,direct_bindings)
        if possible:
            # Browser has closed: persist recovered candidates for later
            # inspection rather than claiming a verified identity.
            results[index]=summarize(fixture,possible,None,args.timeout,False)
            results[index]['discovery_diagnostic']='EVENT_PRESENT_LOCAL_REMATCH'
        near=[] if possible else visible_without_matching(fixture,catalog)
        coverage=coverage_for_fixture(fixture,pages,extension_info,candidate_visible=bool(possible),
                                      unmatched_visible=near)
        results[index]['coverage']=coverage
        if not possible:
            results[index]['discovery_diagnostic']=coverage['status']
    for result in results:
        if result.get('coverage') is not None:continue
        fixture=next(f for f in chosen if f['fixture_id']==result['fixture_id'])
        visible=bool(result.get('bwin_event_id') or result.get('candidates_found'))
        result['coverage']=coverage_for_fixture(fixture,pages,extension_info,candidate_visible=visible)
    reconcile_catalog_clocks(results,catalog)
    payload = {"schema_version": "0.5-bwin-discovery-v3", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "source_input": str(args.input), "user_time_reference": "Europe/Madrid", "prematch_only": not args.include_past_for_debug,
               "verified_identity_catalog": identity_audit,
               "methodology": "Public DOM event links + targeted country pages discovered from DOM or previously verified; time identity checks",
               "pages": pages, "country_discovery": extension_info, "catalog_observed": catalog, "matches": results + skipped}
    write_reports(args.output_dir, payload)
    print(f"Archivos: {args.output_dir / 'bwin_discovery.json'} | .csv | .md | catalog_observed.json")
    if not catalog:
        print("AVISO: catálogo vacío; no equivale a ausencia de mercados. Revisa pages[].error en JSON.", file=sys.stderr)
        return 4
    return 0

from prematch_execution_control import confirmed_cli
main = confirmed_cli(main)

if __name__ == "__main__":
    raise SystemExit(main())
