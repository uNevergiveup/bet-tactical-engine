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
FEMALE_RE = re.compile(r"(?:\bwomen(?:'s)?\b|\bwomens\b|\bfemenin[oa]\b|\bfeminin[ea]\b|\bfrauen\b|\bfemminile\b|\bdamen\b|\(w\)|\sw\b)", re.I)
NON_ADULT_RE = re.compile(r"(?:\bu\s*[- ]?(?:1[0-9]|2[0-3])\b|\bunder\s*[- ]?(?:1[0-9]|2[0-3])\b|\byouth\b|\bjuvenil(?:es)?\b|\breserves?\b|\breservas?\b)", re.I)
CHROME_ERRORS = ("ERR_CONNECTION", "DNS", "Access Denied", "Forbidden", "403", "CAPTCHA", "captcha")
STOP_TOKENS = {
    "fc", "cf", "sc", "ac", "cd", "sd", "afc", "club", "football", "soccer",
    "de", "del", "el", "la", "los", "las", "the", "futbol", "futebol",
    "f", "w", "women", "femenino", "femenil", "fem", "femenina",
}


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


def team_similarity(team: str, evidence: str) -> float:
    # Exact club aliases only generate candidates; temporal/category/ID gates remain.
    from config.constants import TEAM_ALIASES
    query=norm(team);queries={str(team)}
    for canonical,aliases in TEAM_ALIASES.items():
        variants={norm(x) for x in (canonical,*aliases)}
        if query in variants:queries.update(variants)
    # OL Lyonnes is the women's club brand, never a global "Lyon" replacement.
    if query in {'lyon w','ol lyonnes w','ol lyonnes f'} and re.search(r'\b(?:women|w|f)\b',norm(evidence)):
        queries.update({'lyon','ol lyonnes'})
    return max(_team_similarity_literal(q,evidence) for q in queries)


def team_order_status(home: str, away: str, evidence: str) -> str:
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
    return "UNKNOWN"


def category_compatibility(fixture: dict, evidence: str) -> str:
    observed_female = bool(FEMALE_RE.search(evidence) or re.search(r'(?i)(?:\(f\)|\bf\b)',evidence))
    observed_non_adult = bool(NON_ADULT_RE.search(evidence))
    expected = str(fixture.get("gender") or "UNKNOWN").upper()
    if observed_non_adult and fixture.get("category_status") in {"ELIGIBLE", "ADULT", None}:
        return "NON_ADULT_CONFLICT"
    if expected == "M" and observed_female:
        return "GENDER_CONFLICT"
    if expected == "F":
        return "FEMALE_CORROBORATED" if observed_female else "FEMALE_NOT_VISIBLE"
    if expected == "UNKNOWN":
        return "CATEGORY_UNCONFIRMED"
    return "COMPATIBLE_OR_UNKNOWN"


def competition_similarity(competition: str, evidence: str) -> float:
    """Supporting competition evidence; missing evidence never becomes a match."""
    expected, observed = norm(competition), norm(evidence)
    if not expected or not observed:
        return 0.0
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
            "round": item.get("round"), "home_id": item.get("home_id"),
            "away_id": item.get("away_id"), "kickoff_utc": item.get("kickoff_utc"),
            "country": item.get("country", ""), "gender": item.get("gender"),
            "category_status": item.get("category_status") or item.get("filter_status"),
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
        eligible = [i for i in priority_order if i.get("category_status") != "EXCLUDED"]
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
        kickoff = local_kickoff(i["kickoff_madrid"])
        if not include_past and kickoff and kickoff <= now:
            skipped.append({**i, "status": "SKIPPED_ALREADY_KICKED_OFF", "bwin_event_id": None})
        elif not include_past and kickoff is None:
            skipped.append({**i, "status": "SKIPPED_NO_KICKOFF", "bwin_event_id": None})
        else:
            ready.append(i)
    return ready, skipped


def browser_collect_anchors(page, max_scrolls: int = 8) -> list[dict]:
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
    seen = {}
    stable = 0
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
        stable = stable + 1 if len(seen) == previous_count else 0
        if stable >= 3 or scroll_number == max_scrolls:
            break
        page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 1.6, 800))")
        page.wait_for_timeout(550)
    return list(seen.values())


def fetch_catalog(browser, base_url: str, page_paths: list[str], max_scrolls: int,
                  navigation_timeout: int) -> tuple[list[dict], list[dict]]:
    catalog, diagnostics = {}, []
    page = browser.new_page(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1440, "height": 900})
    # One public page per discovery cycle.  Repeated paths from CLI, fallback
    # routing or country resolution must not trigger repeated navigation.
    for path in dict.fromkeys(page_paths):
        url = urljoin(base_url, path)
        d = {"catalog_page": url, "status": "NOT_ATTEMPTED", "events_found": 0, "error": None}
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout)
            page.wait_for_timeout(1300)
            if response and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            # Do not click sportsbook controls: this stage is read-only.
            found = browser_collect_anchors(page, max_scrolls=max_scrolls)
            d["events_found"] = len(found)
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


def parse_country_nav(anchors: list[dict], base_url: str) -> dict[str, str]:
    """Extract country URLs from actual DOM <a href>, never construct country IDs.

    Uses the country slug as a fallback label if Bwin renders the visible text in a
    nested control or abbreviates "Todos <country>" in the anchor itself.
    """
    found = {}
    for item in anchors:
        url = str(item.get("url") or "")
        if not is_country_page_url(url, base_url):
            continue
        country_slug = unquote(urlparse(url).path).rstrip('/').rsplit('/', 1)[-1]
        country_slug = re.sub(r"-\d+$", "", country_slug)
        country_from_path = norm(country_slug.replace('-', ' '))
        label = norm(item.get("text") or "")
        from_text = re.sub(r"\s+\d+$", "", label.removeprefix("todos ")).strip()
        # URL is a real anchor.  When there is no recognizable country label,
        # use the slug extracted FROM that real URL, not a guessed URL.
        country = from_text if label.startswith('todos ') and from_text else country_from_path
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
                if is_country_page_url(str(a.get('url') or ''), base_url):
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


def extend_catalog_for_missing(browser, base_url: str, catalog: list[dict],
                               selected: list[dict], max_countries: int,
                               scrolls: int, timeout: int) -> tuple[list[dict], list[dict], dict]:
    """Visit only country pages needed by unmatched fixtures.

    First read public Bwin competition navigation; then use only explicitly
    verified known public country URLs if the dynamic navigation is incomplete.
    """
    missing = [f for f in selected if not match_catalog(f, catalog)]
    if not missing or max_countries <= 0:
        return catalog, [], {"requested_countries": [], "country_urls_found": 0,
                             "country_url_sources": {}, "country_pages": {}}
    wanted = list(dict.fromkeys(country_label(f.get("country", "")) for f in missing))
    wanted = [x for x in wanted if x][:max_countries]
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
    resolved, sources = {}, {}
    for k in wanted:
        if k in nav:
            resolved[k], sources[k] = nav[k], 'BWIN_DOM_COUNTRY_LINK'
        else:
            fallback = VERIFIED_PUBLIC_COUNTRY_PAGES.get(k)
            if fallback and is_country_page_url(fallback, base_url):
                resolved[k], sources[k] = fallback, 'VERIFIED_PUBLIC_PAGE_FALLBACK'
            else:
                resolved[k], sources[k] = None, 'UNRESOLVED'
    urls = list(dict.fromkeys(u for u in resolved.values() if u))
    if urls:
        extra_catalog, extra_diagnostics = fetch_catalog(browser, base_url, urls, scrolls, timeout)
        for d in extra_diagnostics:
            d["kind"] = "TARGETED_COUNTRY_CATALOG"
        diagnostics.extend(extra_diagnostics)
        by_id = {v["bwin_event_id"]: v for v in catalog}
        for event in extra_catalog:
            key = event["bwin_event_id"]
            if key in by_id:
                existing = by_id[key]
                existing["source_pages"] = list(dict.fromkeys(existing.get("source_pages", []) + event.get("source_pages", [])))
                if len(event.get("text", "")) > len(existing.get("text", "")):
                    for field in ("text", "text_observed_at_utc"):
                        existing[field] = event.get(field)
            else:
                by_id[key] = event
        catalog = list(by_id.values())
    return catalog, diagnostics, {
        "requested_countries": wanted, "country_urls_found": len(urls),
        "country_pages": resolved, "country_url_sources": sources,
    }


def match_catalog(fixture: dict, catalog: list[dict]) -> list[dict]:
    candidates = []
    for e in catalog:
        if LIVE_RE.search(e.get("text", "")):
            continue
        parsed = unquote(urlparse(e["bwin_url"]).path)
        slug = parsed.split("/eventos/")[-1].rsplit(":", 1)[0]
        evidence = f'{e["text"]} {slug}'
        home = team_similarity(fixture["home"], evidence)
        away = team_similarity(fixture["away"], evidence)
        if min(home, away) < 0.82:
            continue
        order_status = team_order_status(fixture["home"], fixture["away"], evidence)
        category_status = category_compatibility(fixture, evidence)
        if order_status in {"AWAY_HOME_CONFLICT", "AWAY_HOME_TOKEN_CONFLICT"}:
            continue
        if category_status in {"GENDER_CONFLICT", "NON_ADULT_CONFLICT"}:
            continue
        score = round((home + away) / 2, 4)
        competition_score = competition_similarity(str(fixture.get("league") or ""), evidence) if fixture.get("league") else None
        candidates.append({**e, "name_score": score, "home_similarity": home, "away_similarity": away,
                           "team_order_status": order_status,
                           "category_compatibility": category_status,
                           "competition_similarity": competition_score})
    return sorted(candidates, key=lambda i: i["name_score"], reverse=True)


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


def inspect_event(browser, result: dict, fixture: dict, navigation_timeout: int) -> dict:
    page = browser.new_page(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1440, "height": 900})
    observation = {"status": "NOT_INSPECTED", "page_title": None, "event_id_from_url": None,
                   "event_start_machine_readable": None, "kickoff_difference_minutes": None,
                   "market_headings": {}, "market_heading_families": 0,
                   "is_live": None, "identity_status": "NOT_VERIFIED", "time_evidence_type": "NONE",
                   "competition_match_score": None}
    try:
        response = page.goto(result["bwin_url"], wait_until="domcontentloaded", timeout=navigation_timeout)
        page.wait_for_timeout(1700)
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
        observation["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        observation["page_title"] = page.title()
        observation["event_id_from_url"] = extract_event_id(page.url)
        body = page.locator("body").inner_text(timeout=10000)
        markup = page.content()
        observation["observed_catalog"] = browser_collect_anchors(page,0)
        observation["market_headings"] = market_indicators(body)
        observation["market_heading_families"] = sum(x["status"].startswith("HEADING_")
                                                     for x in observation["market_headings"].values())
        if fixture.get("league"):
            observation["competition_match_score"] = competition_similarity(fixture["league"], body)
        # The global header often advertises 'Live'. Only inspect close to event header.
        # The event page can display OTHER ongoing fixtures in sidebars.
        # Never infer the current event is live from global body text.
        observation["is_live"] = None
        observed_start = event_absolute_kickoff(markup)
        if observed_start:
            observation["event_start_machine_readable"] = observed_start.isoformat()
        candidate_start = local_kickoff(fixture["kickoff_madrid"])
        observation.update(countdown_corrob(result, candidate_start))
        if observed_start and candidate_start:
            observation["kickoff_difference_minutes"] = round(abs((candidate_start - observed_start).total_seconds()) / 60, 1)
            observation["time_evidence_type"] = "ABSOLUTE_JSON_LD"
        elif observation["countdown_minutes"] is not None:
            observation["time_evidence_type"] = "RELATIVE_CATALOG_COUNTDOWN"
        title = norm(observation["page_title"])
        # Title is a supporting check, not an absolute identity test.
        title_ok = team_similarity(fixture["home"], title) >= 0.82 and team_similarity(fixture["away"], title) >= 0.82
        observation["title_teams_match"] = title_ok
        if observation["event_id_from_url"] != result["bwin_event_id"]:
            observation["identity_status"] = "EVENT_ID_CHANGED_REVIEW"
        elif not title_ok:
            observation["identity_status"] = "TITLE_MISMATCH_REVIEW"
        elif observed_start is not None:
            if observation["kickoff_difference_minutes"] is not None and observation["kickoff_difference_minutes"] <= 15:
                observation["identity_status"] = "NAME_AND_ABSOLUTE_TIME_MATCH"
            else:
                observation["identity_status"] = "KICKOFF_MISMATCH_REVIEW"
        elif observation["countdown_difference_minutes"] is not None:
            # Rounded and occasionally delayed countdown. Keep confidence lower than absolute timestamp.
            if observation["countdown_difference_minutes"] <= 4.0:
                observation["identity_status"] = "NAME_AND_RELATIVE_TIME_CORROBORATED"
            else:
                observation["identity_status"] = "KICKOFF_COUNTDOWN_MISMATCH_REVIEW"
        else:
            observation["identity_status"] = "NAME_MATCH_TIME_NOT_MACHINE_VERIFIED"
    except Exception as exc:
        observation["status"] = "ERROR"
        observation["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        page.close()
    return observation


def summarize(fixture: dict, possible: list[dict], browser, timeout: int, inspect: bool) -> dict:
    out = {**fixture, "status": "NOT_FOUND_IN_SCANNED_PAGES", "bwin_event_id": None,
           "bwin_url": None, "candidates_found": len(possible), "alternatives": [],
           "identity_status": "NOT_VERIFIED", "markets": None}
    if not possible:
        return out
    out["alternatives"] = [{"event_id": i["bwin_event_id"], "url": i["bwin_url"],
                            "score": i["name_score"], "text": i["text"][:160]}
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
                "team_order_status": winner.get("team_order_status"),
                "category_compatibility": winner.get("category_compatibility"),
                "competition_similarity": winner.get("competition_similarity")})
    if inspect:
        observed = inspect_event(browser, winner, fixture, timeout)
        expected_gender = str(fixture.get("gender") or "UNKNOWN").upper()
        category_ok = ((expected_gender == "M" and winner.get("category_compatibility") == "COMPATIBLE_OR_UNKNOWN") or
                       (expected_gender == "F" and winner.get("category_compatibility") == "FEMALE_CORROBORATED"))
        competition_score = observed.get("competition_match_score")
        competition_ok = (isinstance(competition_score, (int, float)) and
                          not isinstance(competition_score, bool) and competition_score >= 0.82)
        if observed.get("identity_status") == "NAME_AND_ABSOLUTE_TIME_MATCH" and not (category_ok and competition_ok):
            observed["identity_status"] = "CATEGORY_OR_COMPETITION_UNVERIFIED"
            observed["supporting_identity_checks"] = {
                "category_compatible": category_ok,
                "competition_compatible": competition_ok,
            }
        out["inspection"] = observed
        out["identity_status"] = observed["identity_status"]
        out["markets"] = observed["market_headings"]
        if observed["identity_status"] == "NAME_AND_ABSOLUTE_TIME_MATCH":
            out["status"] = "IDENTITY_VERIFIED_AUTOMATICALLY"
        elif observed["identity_status"] == "NAME_AND_RELATIVE_TIME_CORROBORATED":
            out["status"] = "EVENT_FOUND_TIME_CORROBORATED"
        elif observed["identity_status"] == "NAME_MATCH_TIME_NOT_MACHINE_VERIFIED":
            out["status"] = "EVENT_FOUND_NEEDS_TIME_CONFIRMATION"
        else:
            out["status"] = "EVENT_REQUIRES_REVIEW"
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
    parser.add_argument("--input", type=Path, required=True, help="fixtures_discovered/fixtures.json, preselection.json V1.3 o CSV")
    parser.add_argument("--fixture-id", type=int, action="append", default=[], help="Fixture API-Football a buscar (repetible)")
    parser.add_argument("--top", type=int, default=1, help="Sin fixture-id: top N futuros; 0=todos, sin ranking estadístico")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/bwin_discovery/test"))
    parser.add_argument("--headless", action="store_true", help="Usar Chromium sin ventana visible")
    parser.add_argument("--browser-executable", help="Ruta a Chrome/Chromium instalada (opcional)")
    parser.add_argument("--catalog-page", action="append", default=[], help="Ruta/URL de listado a recorrer; repetible")
    parser.add_argument("--max-scrolls", type=int, default=12)
    parser.add_argument("--no-country-discovery", action="store_true", help="Desactivar el segundo barrido por país")
    parser.add_argument("--max-country-pages", type=int, default=8, help="Máximo de páginas nacionales adicionales (default: 8)")
    parser.add_argument("--timeout", type=int, default=25000, help="Timeout navegación en milisegundos")
    parser.add_argument("--no-inspect", action="store_true", help="Solo identificar links, sin abrir eventos")
    parser.add_argument("--include-past-for-debug", action="store_true", help="No omitir encuentros ya iniciados (solo depuración; nunca PREMATCH)")
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args(argv)
    if args.top < 0:
        parser.error('--top debe ser >=0; 0 significa todos los fixtures')
    now = datetime.now(timezone.utc).astimezone(TZ)
    try:
        chosen, skipped = read_candidates(args.input, args.fixture_id, args.top, args.include_past_for_debug, now)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"Selección: {len(chosen)} para buscar · {len(skipped)} omitidos (ya iniciados/sin hora)")
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
    with sync_playwright() as p:
        kwargs = {"headless": args.headless, "args": ["--disable-blink-features=AutomationControlled"]}
        if args.browser_executable:
            kwargs["executable_path"] = args.browser_executable
        browser = p.chromium.launch(**kwargs)
        try:
            catalog, pages = fetch_catalog(browser, args.base_url, paths, args.max_scrolls, args.timeout)
            if args.catalog_snapshot:
                from prematch_phase1 import merge_bwin_snapshots
                archived=merge_bwin_snapshots(args.catalog_snapshot)
                byid={e['bwin_event_id']:e for e in archived['events']}
                byid.update({e['bwin_event_id']:e for e in catalog});catalog=list(byid.values())
            extension_info = {"requested_countries": [], "country_urls_found": 0}
            if not args.no_country_discovery:
                catalog, extended_pages, extension_info = extend_catalog_for_missing(
                    browser, args.base_url, catalog, chosen, args.max_country_pages,
                    args.max_scrolls, args.timeout)
                pages.extend(extended_pages)
            print(f"Eventos observados en el catálogo: {len(catalog)}")
            if not args.no_country_discovery:
                print(f"Búsqueda por países: {extension_info.get('requested_countries', [])} | enlaces nacionales: {extension_info.get('country_urls_found', 0)}")
            for d in pages:
                if d.get('kind') == 'COUNTRY_NAV_DISCOVERY':
                    print(f"  {d.get('status', '?')}: {d.get('catalog_page', '?')} ({d.get('countries_found', 0)} países en menú)")
                else:
                    print(f"  {d.get('status', '?')}: {d.get('catalog_page', '?')} ({d.get('events_found', 0)} eventos)")
                if d.get("error"):
                    print(f"    {d['error'][:160]}")
            results = []
            for fixture in chosen:
                possibilities = match_catalog(fixture, catalog)
                result = summarize(fixture, possibilities, browser, args.timeout, not args.no_inspect)
                print(f"[{fixture['fixture_id']}] {fixture['home']} - {fixture['away']}: {result['status']}" +
                      (f" | bwin={result['bwin_event_id']}" if result['bwin_event_id'] else ""))
                results.append(result)
                refreshed=(result.get('inspection') or {}).get('observed_catalog',[])
                byid={e['bwin_event_id']:e for e in catalog};byid.update({e['bwin_event_id']:e for e in refreshed});catalog=list(byid.values())
        finally:
            browser.close()
    reconcile_catalog_clocks(results,catalog)
    payload = {"schema_version": "0.4-bwin-discovery", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "source_input": str(args.input), "user_time_reference": "Europe/Madrid", "prematch_only": not args.include_past_for_debug,
               "methodology": "Public DOM event links + targeted country pages discovered from DOM or previously verified; time identity checks",
               "pages": pages, "country_discovery": extension_info, "catalog_observed": catalog, "matches": results + skipped}
    write_reports(args.output_dir, payload)
    print(f"Archivos: {args.output_dir / 'bwin_discovery.json'} | .csv | .md | catalog_observed.json")
    if not catalog:
        print("AVISO: catálogo vacío; no equivale a ausencia de mercados. Revisa pages[].error en JSON.", file=sys.stderr)
        return 4
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
