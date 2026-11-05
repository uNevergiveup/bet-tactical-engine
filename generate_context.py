from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from html import unescape
from datetime import datetime, timedelta, timezone
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse
from playwright.sync_api import sync_playwright

import requests

try:
    import fcntl  # Bloqueo entre procesos (macOS/Linux); API compartida del lote.
except ImportError:
    fcntl = None

try:
    from deep_translator import GoogleTranslator
except ImportError:  # el traductor es solo un fallback opcional
    GoogleTranslator = None

from config.constants import (
    API_KEY,
    FOOTBALL_BASE_URL,
    BWIN_EVENT_URL,
    BWIN_USER_AGENT,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RECENT_FIXTURES_LIMIT,
    MAX_COMPETITION_FIXTURES,
    CACHE_ENABLED,
    CACHE_DIR,
    OUTPUT_DIR_DATA,
    GET_LAST_MATCHES,
    LAST_MATCHES_LIMIT,
    GET_NEXT_MATCHES,
    NEXT_MATCHES_LIMIT,
    GET_H2H,
    GET_STANDINGS,
    GET_PREDICTIONS,
    GET_INJURIES,
    GET_LINEUPS,
    GET_STATISTICS,
    SECTION_SEPARATOR,
    SUBSECTION_SEPARATOR,
    PERMANENT_API_ERROR_MARKERS,
    VERSION,
    GET_DEEP_HISTORY,
    MICRO_HISTORY_LIMIT,
    DEEP_HISTORY_LIMIT,
    MICRO_RECENT_WINDOW,
    MICRO_CONTEXT_WINDOW,
    MICRO_HISTORY_WINDOWS,
    FIXTURE_IDS_BATCH_SIZE,
    COMPLETED_STATUSES,
    FUTURE_STATUSES,
    LIVE_STATUSES,
    GET_SQUADS,
    GET_COACHS,
    GET_API_ODDS,
    INCLUDE_RAW_CONTEXT_JSON,
    CONTEXT_FIXTURE_LOOKAHEAD,
    CONTEXT_FIXTURE_HISTORY,
    GET_REFEREE_HISTORY,
    REFEREE_HISTORY_LIMIT,
    REFEREE_HISTORY_DAYS_BACK,
    REFEREE_HISTORY_COMPETITION_PAGES,
    REFEREE_HISTORY_GLOBAL_DAYS,
    MAX_WORKERS,
    CACHE_TTL_DEFAULT,
    CACHE_TTL_EMPTY,
    CACHE_TTL_BY_ENDPOINT,
    CACHE_TTL_FIXTURE_IDS,
    API_USER_AGENT,
    TEAM_ALIASES,
    COUNTRY_ES_EN,
    FEMALE_SUFFIXES,
    FEMALE_QUERY_SUFFIXES,
    YOUTH_PATTERNS,
    BRAZIL_STATE_SUFFIXES,
    CLUB_PREFIXES,
    CUSTOM_TEAM_IDS,
    PLAYER_STAT_MAPPING,
)


# Identificador independiente de VERSION en config/constants.py.
RESOLVER_PATCH_VERSION = '5.7.9-verified-pair-institutional-brazilian-fixture'


# =============================================================================
# LOGGER
# =============================================================================


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def _write(self, prefix: str, message: str, console: bool = True) -> None:
        line = f"{prefix} {message}"
        with self._lock:
            if console:
                print(line)
            self.lines.append(line)

    def info(self, message: str) -> None:
        self._write("[INFO]", message)

    def debug(self, message: str) -> None:
        self._write("[DEBUG]", message, console=False)

    def ok(self, message: str) -> None:
        self._write("[OK]  ", message)

    def warn(self, message: str) -> None:
        self._write("[WARN]", message)

    def error(self, message: str) -> None:
        self._write("[ERROR]", message)


LOG = Logger()


# =============================================================================
# DATACLASSES
# =============================================================================


@dataclass
class BwinMatch:
    url: str
    event_id: Optional[int]
    home_name_raw: str
    away_name_raw: str
    home_name: str
    away_name: str
    home_gender: str
    away_gender: str
    tournament_id: Optional[int] = None
    tournament_name: Optional[str] = None
    competition_display: Optional[str] = None
    stage: Optional[str] = None
    round_name: Optional[str] = None
    event_date: Optional[str] = None
    # Solo se rellena cuando un fixture concreto de API-Football acreditó
    # ambos clubes + orden + ventana horaria durante el rescate.
    verified_fixture_id: Optional[int] = None


@dataclass
class APITeam:
    id: int
    name: str
    code: Optional[str]
    country: Optional[str]
    founded: Optional[int]
    national: bool
    gender: str
    logo: Optional[str]
    venue_name: Optional[str]


@dataclass
class CompetitionInfo:
    league_id: int
    league_name: str
    season: int
    country: Optional[str]
    latest_date: Optional[str]
    appearances: int


@dataclass
class ResolvedFixture:
    fixture_id: int
    date: str
    status: str
    status_long: str
    league_id: int
    league_name: str
    season: int
    round_name: Optional[str]
    home_id: int
    home_name: str
    away_id: int
    away_name: str
    referee: Optional[str]
    venue_name: Optional[str]
    venue_city: Optional[str]


# =============================================================================
# UTILIDADES
# =============================================================================


def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = unquote(value)
    normalized = unicodedata.normalize("NFD", value)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    normalized = normalized.lower()
    normalized = normalized.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", normalized).strip()


# Bwin concatena a veces la competición al nombre del equipo visitante:
#   "Juventus (F) UEFA Champions League (F) Fútbol".
# Recortar exclusivamente colas de torneo TERMINADAS en «Fútbol/Futebol»,
# preservando denominaciones del club y cualquier categoría explícita del club.
# Una expresión central de torneos, no un alias por cada equipo. Solo se recorta
# cuando el título de Bwin termina en COMPETICIÓN + Fútbol/Futebol/Football.
# NO se elimina "liga", "fútbol" ni sufijos de clubes arbitrariamente.
_BWIN_COMPETITION_LABELS = (
    r"(?:uefa|afc|conmebol|concacaf)?\s*(?:women(?:'s)?\s*)?"
    r"(?:champions\s+league|europa\s+league|conference\s+league|"
    r"nations\s+league|super\s+cup)",
    r"(?:[123]\.?\s*)?bundesliga", r"(?:[123]\.?\s*)?liga",
    r"(?:primera|segunda|tercera)\s+(?:[abc]|divisi[oó]n)",
    r"(?:serie|série)\s+[abcd]", r"(?:ligue)\s+[12]",
    r"(?:la\s*liga|laliga)(?:\s+ea\s+sports|\s+hypermotion)?",
    r"(?:premier\s+league|championship|league\s+(?:one|two))",
    r"(?:jupiler\s+pro\s+league)",
    r"(?:eredivisie|eerste\s+divisie|primeira\s+liga)",
    r"(?:(?:saudi|uae|emirates|qatar|egyptian|english|scottish|belgian|turkish|french|german|brazilian|colombian|portuguese|spanish|italian)\s+)?"
    r"(?:pro\s+league|professional\s+league|premier\s+league|super\s+league|superliga|liga\s+pro)",
    r"(?:brasileiro|brasileir[aã]o|campeonato\s+brasileiro)"
    r"(?:\s+(?:serie|série)\s+[abcd])?",
    r"(?:liga\s+(?:profesional|profissional|betplay|mx|1|2|3))",
    r"(?:primera\s+(?:a|b|nacional))",
    r"(?:copa\s+(?:del\s+rey|libertadores|sudamericana|america|argentina|do\s+brasil))",
    r"(?:world\s+cup|club\s+world\s+cup|euro|africa\s+cup\s+of\s+nations)",
)
BWIN_TRAILING_COMPETITION = re.compile(
    r"\s+(?:" + "|".join(_BWIN_COMPETITION_LABELS) +
    r")(?:\s*\(\s*[fmw]\s*\))?\s+f[uú]t(?:bol|ebol)\s*$",
    flags=re.IGNORECASE,
    )


# Señales de género en el título de Bwin. «(F)» y «(W)» indican femenino.
# «(M)» = masculino. La competición (F) NO forma parte del nombre del club.
BWIN_GENDER_TAG = re.compile(r"\(\s*([fmw])\s*\)", re.IGNORECASE)
BWIN_FEMALE_WORD = re.compile(r"\b(?:femenin[ao]|feminino|feminina|women|woman|frauen|damen)\b", re.I)


def clean_team_name(value: str) -> tuple[str, str]:
    raw = unquote(value).strip()
    # Hacer el corte antes de mirar el género del club, de modo que una cola
    # «UEFA Champions League (F) Fútbol» no feminice a un club masculino.
    club = BWIN_TRAILING_COMPETITION.sub("", raw).strip()
    tags = [tag.lower() for tag in BWIN_GENDER_TAG.findall(club)]
    female = ('f' in tags or 'w' in tags or bool(BWIN_FEMALE_WORD.search(club)))
    cleaned = BWIN_GENDER_TAG.sub("", club)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, ("female" if female else "male")


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_date(value: Optional[str]) -> Optional[str]:
    dt = parse_date(value)
    return dt.date().isoformat() if dt else None


def same_date(date_a: Optional[str], date_b: Optional[str]) -> bool:
    if not date_a or not date_b:
        return False
    return iso_date(date_a) == iso_date(date_b)


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", normalize_text(value)).strip("_")
    return value or "match"


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def to_number(value: Any) -> Optional[int | float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        found = re.search(r"-?\d+(?:\.\d+)?", value)
        if found:
            number = float(found.group(0))
            return int(number) if number.is_integer() else number
    return None


def average_list(values: list[Any]) -> Optional[float]:
    numeric = [v for v in values if isinstance(v, (int, float))]
    if not numeric:
        return None
    return round(sum(numeric) / len(numeric), 2)


def fmt(value: Any) -> str:
    if value is None:
        return "N/D"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.2f}"
    return str(value)


# API-Football acostumbra añadir W/Women a los equipos femeninos.
# «F» también puede aparecer como sufijo en algunas ligas/idiomas.
FEMALE_CATEGORY_TOKENS = set(FEMALE_SUFFIXES) | {"f", "fem", "feminino", "feminina", "frauen", "damen"}


def detect_gender(api_name: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", normalize_text(api_name))
    words = set(tokens)
    return ("female" if ((words & (FEMALE_CATEGORY_TOKENS - {'f', 'w'}))
                         or (tokens and tokens[-1] in {'f', 'w'})) else "male")


def base_name(name: str) -> str:
    tokens = [t for t in re.findall(r"[a-z0-9]+", normalize_text(name))
              if t not in FEMALE_CATEGORY_TOKENS]
    return " ".join(tokens).strip()


# Los sufijos de categoría son parte de la identidad, no abreviaturas.
# `Chapecoense-SC` puede ser el club principal de Santa Catarina;
# `Chapecoense B` NO lo es. Las coincidencias de estado son solo PISTAS;
# el fixture del evento debe validar los IDs y la localía.
RESERVE_CATEGORY_TOKENS = {"b", "ii", "iii", "women", "woman", "w", "f", "femenino", "femenina", "fem", "feminino", "feminina", "frauen", "damen", "reserves", "reserve"}


def _club_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize_text(value))


def _club_category(value: str) -> Optional[str]:
    tokens = _club_tokens(value)
    for index, token in enumerate(tokens):
        if token in FEMALE_CATEGORY_TOKENS and (token not in {'f', 'w'} or index == len(tokens)-1):
            return 'female'
        if token in RESERVE_CATEGORY_TOKENS and token not in {'f', 'w'} or re.fullmatch(r"u(?:1[5-9]|2[0-3])", token):
            return token
    return None


def club_category_compatible(target: str, candidate: str,
                             expected_gender: Optional[str] = None) -> bool:
    """Preserva W/B/U19; acepta W si Bwin ya acreditó género female.

    Un nombre Bwin como «Juventus» (tras limpiar «(F)») no incluye W.
    Sin `expected_gender='female'`, NUNCA permite saltar de un club
    masculino sin marca al filial femenino solo por similitud.
    """
    a, b = _club_category(target), _club_category(candidate)
    if expected_gender == 'female' and a is None and b == 'female':
        return True
    return a == b


def _club_identity_tokens(value: str) -> list[str]:
    tokens = _club_tokens(value)
    # Solo la COLA, p.ej. `Chapecoense-SC`, representa un estado opcional.
    # No alterar categorías juveniles / reservas ni prefijos de clubes.
    if len(tokens) > 1 and tokens[-1] in BRAZIL_STATE_SUFFIXES:
        tokens = tokens[:-1]
    return tokens


# Los prefijos institucionales son pistas de comparación, NO IDs de clubes.
# Se aplican solo al PRINCIPIO del nombre, y conservan números sustantivos:
# Mainz 05 != Mainz 07; Elversberg B != Elversberg.
_OPTIONAL_CLUB_PREFIXES = {
    'sv', 'fsv', 'kv', 'fc', 'sc', 'cf', 'cd', 'afc', 'ac', 'as',
    'fk', 'bk', 'bsc', 'vfb', 'tsg', 'ca', 'rc', 'ssc', 'ud', 'sd',
}


def institutional_club_key(name: str) -> str:
    """Núcleo nominal conservador, solo para GENERAR/COMPARAR candidatos.

    Quita siglas institucionales del principio/final y estados brasileños al
    final; nunca elimina categorías B/II/U19/W ni numerales significativos.
    La igualdad de núcleos NO acredita un ID: siempre exige fixture verificable.
    """
    tokens = _club_tokens(name)
    if not tokens:
        return ''
    # Estados brasileños solo en cola, p. ej. Vila Nova FC GO / Atletico-MG.
    if len(tokens) >= 3 and tokens[-1] in BRAZIL_STATE_SUFFIXES:
        tokens = tokens[:-1]
    prefixes = _OPTIONAL_CLUB_PREFIXES | {'ec', 'esporte', 'esportivo', 'ss', 'ssc', 'cd', 'ce'}
    suffixes = {'fc', 'cf', 'sc', 'ec', 'ac', 'afc', 'sk'}
    if len(tokens) >= 3 and tokens[0] == '1' and tokens[1] in prefixes:
        tokens = tokens[1:]
    # Preservar 07 de Paderborn 07 / Mainz 05; no borrar numerales al azar.
    if len(tokens) >= 3 and tokens[0] in prefixes and tokens[1] in {'07', '09'}:
        tokens = [tokens[0]] + tokens[2:]
    if len(tokens) >= 2 and tokens[0] in prefixes:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in suffixes:
        tokens = tokens[:-1]
    return ' '.join(tokens)


def institutional_name_match(name_a: str, name_b: str, expected_gender: Optional[str] = None) -> bool:
    if not club_category_compatible(name_a, name_b, expected_gender):
        return False
    a, b = institutional_club_key(name_a), institutional_club_key(name_b)
    # No equiparar una abreviatura de ciudad de un solo carácter.
    return bool(a and b and a == b and len(a) >= 4)


def team_name_similarity(name_a: str, name_b: str,
                         expected_gender: Optional[str] = None) -> int:
    """Comparación de PISTAS de identidad, con guiones/estados y categorías.

    Importante: 100 NO autoriza un team ID sin validar fixture/competición.
    """
    if not name_a or not name_b or not club_category_compatible(name_a, name_b, expected_gender):
        return 0
    # Quitar W/F SOLO de la similitud textual, nunca de la validación de género.
    if expected_gender == 'female':
        a, b = _club_identity_tokens(base_name(name_a)), _club_identity_tokens(base_name(name_b))
    else:
        a, b = _club_identity_tokens(name_a), _club_identity_tokens(name_b)
    if not a or not b:
        return 0
    if a == b or institutional_name_match(name_a, name_b, expected_gender):
        return 100
    common = (set(a) & set(b)) - CLUB_PREFIXES
    if not common:
        return 0
    score = len(common) * 20
    # Separar tokens antes del containment: `chapecoense-sc` es `chapecoense sc`.
    joined_a, joined_b = " ".join(a), " ".join(b)
    if joined_a in joined_b or joined_b in joined_a:
        score += 30
    return min(score, 90)


@lru_cache(maxsize=512)
def translate_es_en(text: str) -> Optional[str]:
    """Traducción ES->EN solo como último recurso. Ahora los fallos se registran."""
    if GoogleTranslator is None:
        return None
    try:
        out = GoogleTranslator(source="es", target="en").translate(text)
    except Exception as exc:
        LOG.warn(f"Traducción falló para '{text}': {exc}")
        return None
    return out.lower().strip() if out else None


def name_variants(normalized: str, use_translation: bool = False) -> list[str]:
    """Variantes de nombre: original, alias manuales, país en inglés, traducción."""
    variants = [normalized]
    variants.extend(normalize_text(a) for a in TEAM_ALIASES.get(normalized, ()))
    # Acrónimo institucional Bwin: sin estas variantes, la búsqueda de PSG
    # no encuentra necesariamente la denominación completa de API-Football.
    # Son candidatos textuales; se sigue exigiendo validación del fixture.
    if normalized == "psg":
        variants.extend(("paris saint germain", "paris saint-germain", "paris sg", "paris st germain"))
    variants.extend(COUNTRY_ES_EN.get(normalized, ()))
    # Exónimos en Bwin frente a denominaciones de equipos femeninos API.
    # Son pistas de búsqueda, no autorizan el ID sin género y fixture verificados.
    if normalized in {'fk austria viena', 'austria viena'}:
        variants.extend(('austria vienna', 'austria vienna w', 'austria wien', 'austria wien women'))
    elif normalized in {'bk hacken', 'bk häcken'}:
        variants.extend(('bk hacken w', 'hacken w', 'hacken women'))
    elif normalized == 'paris fc':
        variants.extend(('paris fc w', 'paris fc women'))
    elif normalized == 'juventus':
        variants.extend(('juventus w', 'juventus women'))

    # Prefijos institucionales opcionales en casas de apuestas (p.ej. CA Huracán).
    # Solo generamos variantes: no identificamos clubes basándonos únicamente
    # en haber eliminado el prefijo; la identidad se valida después contra el fixture.
    prefix_patterns = (
        r"^(?:ca|c a|c\.a\.|club atletico|club atlético)\s+",
        r"^(?:ac|a c|a\.c\.)\s+",
    )
    for prefix_pattern in prefix_patterns:
        stripped = re.sub(prefix_pattern, "", normalized, count=1).strip()
        if stripped != normalized and len(stripped) >= 4:
            variants.append(stripped)
            variants.extend(normalize_text(a) for a in TEAM_ALIASES.get(stripped, ()))

    # Sufijos institucionales opcionales: pistas para búsqueda, nunca identidad.
    # Ejemplo: "lille osc" -> "lille". No quitar "ii", "b" ni categorías.
    non_identity_suffixes = {"osc", "losc", "fc", "cf", "sc", "afc", "ac", "fk", "sk"}
    suffix_tokens = normalized.split()
    if len(suffix_tokens) > 1 and suffix_tokens[-1] in non_identity_suffixes:
        shorter = " ".join(suffix_tokens[:-1])
        if len(shorter) >= 4:
            variants.append(shorter)
            variants.extend(normalize_text(a) for a in TEAM_ALIASES.get(shorter, ()))

    # NUEVO: quitar sufijo de estado brasileño (sao paulo sp -> sao paulo)
    tokens = normalized.split()
    if len(tokens) > 1 and tokens[-1] in BRAZIL_STATE_SUFFIXES:
        variants.append(" ".join(tokens[:-1]))

    # Variantes institucionales no destructivas: Vila Nova FC GO -> Vila Nova;
    # EC Juventude -> Juventude; SS Lazio -> Lazio. Solo se genera candidato.
    # La categoría B/II/U19/W sigue siendo parte de la identidad del club.
    core = institutional_club_key(normalized)
    if core and core != normalized and len(core) >= 4:
        original_category = _club_category(normalized)
        if original_category is None or _club_category(core) == original_category:
            variants.append(core)
            variants.extend(normalize_text(a) for a in TEAM_ALIASES.get(core, ()))
    # Retirar únicamente estado final si permite conservar un nombre significativo.
    # E.g. Vila Nova FC GO -> Vila Nova FC, de ahí el núcleo anterior.
    state_tokens = _club_tokens(normalized)
    if len(state_tokens) >= 3 and state_tokens[-1] in BRAZIL_STATE_SUFFIXES:
        variants.append(' '.join(state_tokens[:-1]))

    if use_translation and normalized not in COUNTRY_ES_EN:
        translated = translate_es_en(normalized)
        if translated and translated != normalized:
            variants.append(translated)
            if translated.startswith("the "):
                variants.append(translated[4:].strip())

    result: list[str] = []
    for v in variants:
        if v and v not in result:
            result.append(v)
    return result


def weak_team_search_terms(name: str) -> list[str]:
    """Genera búsquedas de RESCATE, no equivalencias de identidad.

    /teams?search= puede indexar «Chico» cuando Bwin escribe «Boyacá Chicó»,
    o «Junior» cuando Bwin escribe «CD Junior FC Barranquilla».
    Solo se usa en candidates_for_pair, y el fixture debe verificar orden,
    IDs y ventana temporal antes de aceptar una pareja.
    """
    tokens = re.findall(r"[a-z0-9]+", normalize_text(name))
    # Nunca borrar categorías: W, Women, U19, II, B... La comprobación de
    # género/categoría sigue siendo obligatoria para los candidatos.
    protected = {"w", "women", "woman", "femenino", "femenina", "fem", "ii", "iii", "b"}
    protected |= {t for t in tokens if re.fullmatch(r"u\d{2}", t)}
    removable = {"cd", "fc", "cf", "sc", "ac", "afc", "club", "de", "del", "the"}
    core = [t for t in tokens if t not in removable]
    if not core:
        return []
    if protected & set(core):
        # No producir búsqueda de una sola palabra que pierda la categoría.
        return [" ".join(core)] if core != tokens else []
    terms = []
    compact = " ".join(core)
    if compact != " ".join(tokens) and len(compact) >= 4:
        terms.append(compact)
    if len(core) >= 2:
        # Primero las formas abreviadas conservadoras. Después, tokens
        # distintivos como 'chico' y 'junior' para generar candidatos.
        for t in core:
            if len(t) >= 4 and t not in {"real", "united", "city", "sporting", "atletico", "athletic", "deportivo"}:
                terms.append(t)
    return list(dict.fromkeys(terms))


def parse_api_team(item: dict[str, Any], gender: Optional[str] = None) -> Optional[APITeam]:
    team = item.get("team", {}) or {}
    venue = item.get("venue", {}) or {}
    team_id, team_name = team.get("id"), team.get("name")
    if not team_id or not team_name:
        return None
    return APITeam(
        id=int(team_id),
        name=team_name,
        code=team.get("code"),
        country=team.get("country"),
        founded=team.get("founded"),
        national=bool(team.get("national")),
        gender=gender or detect_gender(team_name),
        logo=team.get("logo"),
        venue_name=venue.get("name"),
    )


# =============================================================================
# CACHE (con TTL)
# =============================================================================


class JSONCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, endpoint: str, params: dict[str, Any]) -> Path:
        raw = json.dumps({"endpoint": endpoint, "params": params}, ensure_ascii=False, sort_keys=True)
        return self.directory / f"{hashlib.sha256(raw.encode('utf-8')).hexdigest()}.json"

    @staticmethod
    def ttl_for(endpoint: str, params: dict[str, Any]) -> int:
        if endpoint == "fixtures" and "date" in params:
            try:
                d = datetime.fromisoformat(params["date"]).date()
                if d < datetime.now(timezone.utc).date() - timedelta(days=2):
                    return 30 * 86400   # días pasados: inmutables
            except ValueError:
                pass
        if endpoint in CACHE_TTL_BY_ENDPOINT:
            return CACHE_TTL_BY_ENDPOINT[endpoint]
        if endpoint == "fixtures" and "ids" in params:
            return CACHE_TTL_FIXTURE_IDS
        return CACHE_TTL_DEFAULT

    def get(self, endpoint: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        path = self._path(endpoint, params)
        if not path.exists():
            return None
        try:
            age = time.time() - path.stat().st_mtime
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return None

        ttl = self.ttl_for(endpoint, params)
        if isinstance(data, dict) and data.get("response") == []:
            ttl = min(ttl, CACHE_TTL_EMPTY)  # las respuestas vacías caducan rápido
        return data if age <= ttl else None

    def set(self, endpoint: str, params: dict[str, Any], data: dict[str, Any]) -> None:
        path = self._path(endpoint, params)
        tmp_path = path.with_suffix(f".{threading.get_ident()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        tmp_path.replace(path)


CACHE = JSONCache(CACHE_DIR)


# =============================================================================
# API CLIENT
# =============================================================================


class SharedAPIRateLimiter:
    """Límite global por proceso para TODOS los hilos y reintentos.

    Dos ventanas móviles (1 s / 60 s) y cooldown ante 429, Retry-After o
    errors.rateLimit. No garantiza cuotas compartidas con otros procesos.
    """

    def __init__(self, per_second: int = 4, per_minute: int = 240) -> None:
        from collections import deque
        self.per_second = per_second
        self.per_minute = per_minute
        self._calls = deque()
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._shared_path = os.getenv('PREMATCH_SHARED_BUDGET_FILE')
        if self._shared_path:
            if fcntl is None:
                raise RuntimeError('El presupuesto API compartido requiere fcntl (macOS/Linux).')
            self.per_second = min(self.per_second, int(os.getenv('PREMATCH_API_MAX_PER_SECOND', '3')))
            self.per_minute = min(self.per_minute, int(os.getenv('PREMATCH_API_MAX_PER_MINUTE', '180')))
            Path(self._shared_path).parent.mkdir(parents=True, exist_ok=True)

    def _shared_update(self, cooldown_seconds: float = 0.0) -> None:
        """Una sola reserva por llamada, segura entre procesos del mismo lote."""
        assert self._shared_path and fcntl is not None
        while True:
            with open(self._shared_path, 'a+', encoding='utf-8') as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    stream.seek(0)
                    try:
                        data = json.load(stream)
                    except (json.JSONDecodeError, ValueError):
                        data = {}
                    now = time.time()
                    calls = sorted(float(x) for x in data.get('calls', [])
                                   if isinstance(x, (int, float)) and now - 60 < x <= now + 2)
                    until = float(data.get('cooldown_until') or 0)
                    if cooldown_seconds:
                        until = max(until, now + cooldown_seconds)
                    delay = max(0.0, until - now)
                    in_second = [t for t in calls if t > now - 1]
                    if len(in_second) >= self.per_second:
                        delay = max(delay, in_second[0] + 1.02 - now)
                    if len(calls) >= self.per_minute:
                        delay = max(delay, calls[0] + 60.02 - now)
                    if delay <= 0 and not cooldown_seconds:
                        calls.append(now)
                    stream.seek(0)
                    stream.truncate()
                    json.dump({'version': 1, 'calls': calls,
                               'cooldown_until': until, 'per_second': self.per_second,
                               'per_minute': self.per_minute}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            if cooldown_seconds or delay <= 0:
                return
            time.sleep(max(0.03, delay))

    def wait_turn(self) -> None:
        if self._shared_path:
            self._shared_update()
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and self._calls[0] <= now - 60:
                    self._calls.popleft()
                last_second = sum(t > now - 1 for t in self._calls)
                delay = max(0.0, self._cooldown_until - now)
                if last_second >= self.per_second:
                    recent = [t for t in self._calls if t > now - 1]
                    delay = max(delay, recent[0] + 1.02 - now)
                if len(self._calls) >= self.per_minute:
                    delay = max(delay, self._calls[0] + 60.02 - now)
                if delay <= 0:
                    self._calls.append(now)
                    return
            time.sleep(max(0.02, delay))

    def cooldown(self, seconds: float) -> None:
        if self._shared_path:
            self._shared_update(cooldown_seconds=max(0.0, seconds))
            return
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + max(0.0, seconds),
                )


# Una instancia por proceso: todas las fases comparten la misma cuota.
API_RATE_LIMITER = SharedAPIRateLimiter()


class APIFootballClient:
    def __init__(self, api_key: str, cache_enabled: bool = True) -> None:
        if not api_key:
            raise RuntimeError("No se encontró FOOTBALL_API_KEY.")
        self.api_key = api_key
        self.cache_enabled = cache_enabled
        self.session = requests.Session()
        self.session.headers.update({
            "x-apisports-key": self.api_key,
            "User-Agent": API_USER_AGENT,
            "Accept": "application/json",
        })
        self.api_calls = 0
        self.cache_hits = 0
        self._lock = threading.Lock()

    def _is_permanent_api_error(self, errors: Any) -> bool:
        text = normalize_text(str(errors))
        return any(marker in text for marker in PERMANENT_API_ERROR_MARKERS)

    @staticmethod
    def _retry_after_seconds(response: Any, default: float = 65.0) -> float:
        from email.utils import parsedate_to_datetime
        raw = response.headers.get("Retry-After") if response is not None else None
        if raw:
            try:
                return max(1.0, min(180.0, float(raw)))
            except (ValueError, TypeError):
                try:
                    dt = parsedate_to_datetime(raw)
                    return max(1.0, min(180.0, (dt - datetime.now(timezone.utc)).total_seconds()))
                except (ValueError, TypeError, OverflowError):
                    pass
        return default

    @staticmethod
    def _is_rate_error(errors: Any) -> bool:
        if isinstance(errors, dict):
            if any("rate" in str(k).lower() or "limit" in str(k).lower() for k in errors):
                return True
        message = str(errors).lower()
        return any(part in message for part in (
            "too many requests", "rate limit", "ratelimit", "requests per minute",
        ))

    def get(
            self,
            endpoint: str,
            params: Optional[dict[str, Any]] = None,
            use_cache: Optional[bool] = None,
    ) -> dict[str, Any]:
        params = params or {}
        if use_cache is None:
            use_cache = self.cache_enabled

        if use_cache:
            cached = CACHE.get(endpoint, params)
            if cached is not None:
                with self._lock:
                    self.cache_hits += 1
                LOG.debug(f"CACHE HIT /{endpoint} {params}")
                return cached

        url = f"{FOOTBALL_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
        last_error: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Importante: la reserva es GLOBAL incluso cuando la llamada es un retry.
            API_RATE_LIMITER.wait_turn()
            try:
                LOG.debug(f"API /{endpoint} {params} (intento {attempt}/{MAX_RETRIES})")
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                with self._lock:
                    self.api_calls += 1

                if response.status_code == 429:
                    last_error = "HTTP 429: cuota por intervalo agotada"
                    cooldown = self._retry_after_seconds(response)
                    LOG.warn(f"{last_error}; pausa global de {cooldown:.0f} s")
                    API_RATE_LIMITER.cooldown(cooldown)
                    continue
                if response.status_code in (401, 403):
                    raise RuntimeError(f"API-Football rechazó la credencial/permisos (HTTP {response.status_code}).")
                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                    LOG.warn(last_error)
                    if response.status_code in (400, 404, 422):
                        raise RuntimeError(last_error)
                    if attempt < MAX_RETRIES:
                        time.sleep(min(10, attempt * 2))
                    continue

                data = response.json()
                errors = data.get("errors")
                if errors:
                    last_error = str(errors)
                    if self._is_rate_error(errors):
                        cooldown = self._retry_after_seconds(response)
                        LOG.warn(f"API-Football rateLimit; pausa global de {cooldown:.0f} s")
                        API_RATE_LIMITER.cooldown(cooldown)
                        continue
                    LOG.warn(f"API-Football devolvió errors={errors}")
                    if self._is_permanent_api_error(errors):
                        raise RuntimeError(f"Error permanente de API-Football: {errors}")
                    if attempt < MAX_RETRIES:
                        time.sleep(min(10, attempt * 2))
                    continue

                if use_cache:
                    CACHE.set(endpoint, params, data)
                return data

            except requests.RequestException as exc:
                last_error = str(exc)
                LOG.warn(f"Error HTTP: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(min(10, attempt * 2))
            except ValueError as exc:
                last_error = f"JSON inválido: {exc}"
                if attempt < MAX_RETRIES:
                    time.sleep(min(10, attempt * 2))

        raise RuntimeError(f"Falló API-Football /{endpoint}: {last_error}")


# =============================================================================
# PARSER BWIN
# =============================================================================

def title_club_from_slug(home_raw: str, away_raw: str, slug: str) -> Optional[tuple[str, str]]:
    """Recorta un torneo no catalogado SOLO con corroboración textual del slug.

    No descompone títulos incompatibles ni borra categorías por intuición.
    Devuelve (nombre_visitante, sufijo_competición) o None.
    """
    if not re.search(r"\bf[uú]t(?:bol|ebol)\s*$", away_raw, re.I):
        return None
    home_tokens = _club_tokens(home_raw)
    slug_tokens = _club_tokens(slug)
    if not home_tokens or slug_tokens[:len(home_tokens)] != home_tokens:
        return None
    visitor_slug = slug_tokens[len(home_tokens):]
    if not visitor_slug:
        return None
    # Tokens en texto original para conservar mayúsculas, guiones, acentos y (F).
    words = list(re.finditer(r"[^\W_]+", away_raw, flags=re.UNICODE))
    actual = [_club_tokens(w.group(0)) for w in words]
    flat = [xs[0] if len(xs) == 1 else '' for xs in actual]
    if flat[:len(visitor_slug)] != visitor_slug or len(words) <= len(visitor_slug):
        return None
    split_end = words[len(visitor_slug)-1].end()
    club, tail = away_raw[:split_end].strip(), away_raw[split_end:].strip()
    if not club or not tail or not re.search(r"\bf[uú]t(?:bol|ebol)\s*$", tail, re.I):
        return None
    return club, tail



def female_pair_from_slug(slug: str) -> Optional[tuple[str, str]]:
    """Dos categorías explícitas -f/-w permiten separar URLs Bwin sin HTML.

    No se utiliza para URLs masculinas ni slugs sin DOS sufijos inequívocos.
    """
    pieces = re.fullmatch(r'(.+?)-(f|w)-(.+)-(f|w)', slug, re.I)
    if not pieces or pieces.group(2).lower() != pieces.group(4).lower():
        return None
    home, away = pieces.group(1), pieces.group(3)
    if not home or not away or any(not x for x in (home, away)):
        return None
    suffix = pieces.group(2).upper()
    return f"{home.replace('-', ' ')} ({suffix})", f"{away.replace('-', ' ')} ({suffix})"


class BwinParser:
    # Captura el slug y el ID de URLs como: /eventos/valencia-villarreal-2:7893914
    EVENT_REGEX = re.compile(r"/eventos/([^/?#]+)-2:(\d+)", flags=re.IGNORECASE)

    def parse(self, url: str, html: Optional[str] = None) -> BwinMatch:
        path = unquote(urlparse(url).path)
        match = self.EVENT_REGEX.search(path)
        if not match:
            raise ValueError("No se pudo extraer event_id desde la URL de Bwin.")

        slug = match.group(1).strip("/")
        event_id = int(match.group(2))

        # 1. Intentar sacar los nombres exactos del HTML (Bwin los pone en el <title>)
        home_raw, away_raw = self._extract_teams_from_html(html, slug)

        home_clean, home_gender = clean_team_name(home_raw)
        suffix = BWIN_TRAILING_COMPETITION.search(away_raw)
        fallback = (title_club_from_slug(home_raw, away_raw, slug)
                    if suffix is None and html else None)
        away_display_name = fallback[0] if fallback else away_raw
        away_clean, away_gender = clean_team_name(away_display_name)
        competition_from_title = (re.sub(r"\s+f[uú]t(?:bol|ebol)$", "", suffix.group().strip(), flags=re.I)
                                  if suffix else re.sub(r"\s+f[uú]t(?:bol|ebol)$", "", fallback[1], flags=re.I).strip()
        if fallback else None)
        if fallback:
            LOG.info(f"Competición no catalogada separada por coincidencia completa con slug: {fallback[1]!r}")
        # Sólo reforzar el género desde el slug si los DOS nombres aparecen
        # con sufijo inequívoco «-f» o «-w» (nunca uno solo / exónimos).
        if (re.search(r"-(?:f|w)-(?:[^/]+)-(?:f|w)$", slug, re.I)
                and not BWIN_GENDER_TAG.search(home_raw)
                and not BWIN_GENDER_TAG.search(away_raw)):
            home_gender = away_gender = 'female'

        return BwinMatch(
            url=url,
            event_id=event_id,
            home_name_raw=home_raw,
            away_name_raw=away_raw,
            home_name=home_clean,
            away_name=away_clean,
            home_gender=home_gender,
            away_gender=away_gender,
            # Bwin no expone estos metadatos fácilmente en el HTML raw, se dejan en None
            tournament_id=None,
            tournament_name=None,
            competition_display=competition_from_title,
            stage=None,
            round_name=None,
        )

    def _extract_teams_from_html(self, html: Optional[str], slug: str) -> tuple[str, str]:
        if html:
            # Ejemplo: "<title>Valencia - Villarreal | Cuotas | Bwin</title>"
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html, flags=re.IGNORECASE)
            if title_match:
                title_text = title_match.group(1).split('|')[0].strip()
                parts = title_text.split(' - ')
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip()

        female_split = female_pair_from_slug(slug)
        if female_split:
            return female_split

        # Los slugs sin separador inequívoco se confirman mediante API en main.
        # El corte que sigue es provisional y NUNCA se acepta como identidad.
        # Si la URL no trae "-vs-", se puntúan todos los límites posibles y se
        # favorecen los nombres/alias ya conocidos. Esto evita partir marcas
        # compuestas, por ejemplo Palmeiras / Red Bull Bragantino.
        if "-vs-" in slug:
            parts = slug.split("-vs-", maxsplit=1)
            return parts[0].replace("-", " "), parts[1].replace("-", " ")

        tokens = [token for token in slug.split("-") if token]
        if len(tokens) == 2:
            return tokens[0], tokens[1]
        if len(tokens) > 2:
            known_names = {
                normalize_text(name)
                for canonical, aliases in TEAM_ALIASES.items()
                for name in (canonical, *aliases)
            }

            def boundary_score(index: int) -> tuple[int, int]:
                home = " ".join(tokens[:index])
                away = " ".join(tokens[index:])
                known_score = 100 * int(home in known_names) + 100 * int(away in known_names)
                balance = -abs(len(tokens[:index]) - len(tokens[index:]))
                return known_score, balance

            split_at = max(range(1, len(tokens)), key=boundary_score)
            return " ".join(tokens[:split_at]), " ".join(tokens[split_at:])

        return slug.replace("-", " "), "Rival"


def fetch_bwin_html(url: str) -> str:
    LOG.info("Descargando HTML de Bwin con Playwright para extraer nombres reales...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # Navegamos a la URL y esperamos a que el DOM esté listo
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            # Pausa de 2.5s para que el JavaScript de Bwin inyecte el <title> completo
            page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
            # No modificar HTML globalmente: las colas de competición se
            # recortan solo del nombre extraído en clean_team_name().
            LOG.ok(f"HTML Bwin recibido vía Playwright: {len(html):,} caracteres")
            return html
    except Exception as exc:
        LOG.warn(f"Fallo al descargar HTML con Playwright: {exc}. Se retorna vacío.")
        return ""


# =============================================================================
# RESOLVER DE EQUIPOS
# =============================================================================


class TeamResolver:

    def __init__(self, api: APIFootballClient) -> None:
        self.api = api
        # IDs resueltos con evidencia fuerte (ID manual validado o selección exacta).
        self.confident_ids: set[int] = set()

    # ------------------------------------------------------------------ resolve
    def resolve(self, bwin_name: str, expected_gender: str) -> APITeam:
        normalized = normalize_text(bwin_name)
        country_names = COUNTRY_ES_EN.get(normalized)
        expected_national = bool(country_names)

        LOG.info(
            f"Resolviendo equipo: '{bwin_name}' -> '{normalized}' "
            f"(gender={expected_gender}, selección={'sí' if expected_national else 'no'})"
        )

        direct = self._resolve_direct(normalized, expected_gender, country_names)
        if direct:
            self.confident_ids.add(direct.id)
            return direct

        last_error = "sin candidatos"
        for use_translation in (False, True):
            # Translate sólo cuando el texto representa una selección/país.
            # Los nombres propios de clubes NO deben pasar por Google Translate.
            if use_translation and (GoogleTranslator is None or not expected_national):
                break

            variants = name_variants(normalized, use_translation)
            terms = self._search_terms(normalized, expected_gender, variants)
            LOG.debug(f"Términos de búsqueda generados: {terms}")

            candidates: dict[int, APITeam] = {}
            for term in terms:
                try:
                    data = self.api.get("teams", {"search": term})
                except Exception as exc:
                    LOG.warn(f"Búsqueda '{term}' falló: {exc}")
                    last_error = f"error consultando API ({term}): {exc}"
                    continue

                for item in data.get("response", []) or []:
                    parsed = parse_api_team(item)
                    if parsed:
                        candidates[parsed.id] = parsed

                if not candidates:
                    continue

                ranked = self._rank(bwin_name, expected_gender, expected_national,
                                    variants, list(candidates.values()))
                team, reason = self._accept(ranked, expected_gender, expected_national)
                if team:
                    self._log_ranking(bwin_name, ranked)
                    LOG.ok(f"Equipo resuelto: {bwin_name} -> {team.name} (API ID {team.id})")
                    if expected_national and team.national:
                        self.confident_ids.add(team.id)
                    return team
                last_error = reason

            if candidates:
                self._log_ranking(
                    bwin_name,
                    self._rank(bwin_name, expected_gender, expected_national,
                               variants, list(candidates.values())),
                )

        raise RuntimeError(
            f"No se pudo resolver '{bwin_name}' ({expected_gender}): {last_error}"
        )

    def candidates_for_pair(self, bwin_name: str, expected_gender: str, max_candidates: int = 5) -> list[APITeam]:
        """Candidatos amplios, pero identidad aprobada SOLO tras cruzar fixture.

        El primer intento consulta variantes fuertes; si no obtiene candidatos
        del género correcto, consulta un máximo de 3 variantes de rescate.
        Nunca convierte la búsqueda parcial en un ID verificado.
        """
        normalized = normalize_text(bwin_name)
        variants = name_variants(normalized)
        expected_national = normalized in COUNTRY_ES_EN
        strong_terms = self._search_terms(normalized, expected_gender, variants)[:8]
        weak_terms = [t for t in weak_team_search_terms(normalized)
                      if t not in strong_terms][:3]
        candidates: dict[int, APITeam] = {}

        def search(terms: list[str]) -> None:
            for term in terms:
                try:
                    response = self.api.get("teams", {"search": term}).get("response", []) or []
                except Exception as exc:
                    LOG.warn(f"Rescate: error buscando '{term}': {exc}")
                    continue
                for item in response:
                    parsed = parse_api_team(item)
                    if parsed and parsed.gender == expected_gender and parsed.national == expected_national:
                        candidates[parsed.id] = parsed

        search(strong_terms)
        # No ampliar a términos débiles como 'vila' cuando ya existe una
        # coincidencia de núcleo institucional fuerte ('vila nova').
        if not any(institutional_name_match(normalized, t.name, expected_gender)
                   or normalize_text(t.name) in variants for t in candidates.values()):
            search(weak_terms)

        # Las variantes débiles sirven para ordenar CANDIDATOS, no para declarar
        # identidades: solo se aceptarán si existe el fixture real entre ambas.
        candidate_variants = list(dict.fromkeys([*variants, *weak_terms]))
        ranked = self._rank(bwin_name, expected_gender, expected_national,
                            candidate_variants, list(candidates.values()))
        acceptable = []
        for score, team, _ in ranked:
            if score < 200 or not club_category_compatible(bwin_name, team.name, expected_gender):
                continue
            similarity = max(team_name_similarity(v, team.name, expected_gender) for v in variants)
            if similarity == 0 and not any(base_name(v) == base_name(team.name) for v in variants):
                continue
            acceptable.append(team)
        if acceptable:
            LOG.info(f"Candidatos de rescate para {bwin_name!r}: " +
                     ", ".join(f"{t.name} ({t.id})" for t in acceptable[:max_candidates]) +
                     "; todavía NO verificados por fixture")
        return acceptable[:max_candidates]

    def _resolve_direct(
            self,
            normalized: str,
            expected_gender: str,
            country_names: Optional[tuple[str, ...]],
    ) -> Optional[APITeam]:
        direct_id = CUSTOM_TEAM_IDS.get((normalized, expected_gender))
        if not direct_id:
            return None

        LOG.info(f"ID manual para '{normalized}': {direct_id}")
        try:
            data = self.api.get("teams", {"id": direct_id})
            response = data.get("response", []) or []
            if not response:
                LOG.warn(f"El ID manual {direct_id} no existe en la API.")
                return None
            team = parse_api_team(response[0], gender=expected_gender)
        except Exception as exc:
            LOG.warn(f"Fallo al buscar el ID directo {direct_id} en API: {exc}")
            return None

        if not team:
            return None

        # Validación: un ID manual erróneo no puede envenenar la resolución.
        expected_names = {
            base_name(name)
            for name in name_variants(normalized, use_translation=False)
            if name
        }
        valid_name = (
            base_name(team.name) in country_names
            if country_names
            else base_name(team.name) in expected_names
        )
        if not valid_name:
            LOG.warn(
                f"El ID manual {direct_id} es '{team.name}', no coincide con "
                f"los nombres esperados {sorted(expected_names)}. "
                "Se ignora y se usa búsqueda normal."
            )
            return None

        LOG.ok(f"Equipo resuelto por ID manual: {team.name} (API ID {team.id})")
        return team

    # ----------------------------------------------------------------- búsqueda
    @staticmethod
    def _search_terms(normalized: str, expected_gender: str, variants: list[str]) -> list[str]:
        def clean(value: str) -> str:
            value = re.sub(r"[^a-z0-9 ]+", " ", normalize_text(value))
            return re.sub(r"\s+", " ", value).strip()

        if normalized in COUNTRY_ES_EN:
            terms: list[str] = list(variants[1:])  # solo inglés: 'grecia' devuelve clubes homónimos
        else:
            terms = list(variants)
            terms.append(clean(normalized))

        tokens = normalized.split()
        if len(tokens) > 1 and tokens[0] in CLUB_PREFIXES:
            terms.append(" ".join(tokens[1:]))
        if len(tokens) > 1 and tokens[-1] in {"montevideo", "city"}:
            terms.append(" ".join(tokens[:-1]))
        if len(tokens) > 1 and tokens[-1] in BRAZIL_STATE_SUFFIXES:
            terms.append(" ".join(tokens[:-1]))
        if normalized == "oud heverlee leuven":
            terms.extend(["oh leuven", "leuven"])
        if normalized in {"paris s.g.", "paris sg"}:
            terms.extend(["paris sg", "psg", "paris saint germain"])

        if expected_gender == "female":
            base_terms = list(terms)
            for term in base_terms:
                terms.extend(f"{term} {suffix}" for suffix in FEMALE_QUERY_SUFFIXES)

        result: list[str] = []
        for term in terms:
            cleaned = clean(term)
            if len(cleaned) >= 3 and cleaned not in result:  # la API exige >= 3 caracteres
                result.append(cleaned)
        return result

    # ----------------------------------------------------------------- ranking
    def _rank(
            self,
            bwin_name: str,
            expected_gender: str,
            expected_national: bool,
            variants: list[str],
            candidates: list[APITeam],
    ) -> list[tuple[int, APITeam, list[str]]]:
        target = normalize_text(bwin_name)
        target_base = base_name(bwin_name)
        target_bases = {base_name(v) for v in variants if v}

        scored: list[tuple[int, APITeam, list[str]]] = []
        # /teams?search= puede devolver varias filas con el MISMO team.id.
        # Solo se debe puntuar una entidad por ID.
        seen_candidate_ids: set[int] = set()

        for candidate in candidates:
            if candidate.id in seen_candidate_ids:
                continue
            seen_candidate_ids.add(candidate.id)
            cand_name = normalize_text(candidate.name)
            cand_base = base_name(candidate.name)
            score = 0
            reasons: list[str] = []

            if candidate.gender == expected_gender:
                score += 250
                reasons.append("género correcto")
            else:
                score -= 500
                reasons.append("género incorrecto")

            if cand_name == target:
                score += 300
                reasons.append("nombre exacto")
            if cand_base == target_base:
                score += 250
                reasons.append("nombre base exacto")
            elif (not candidate.national and
                  institutional_name_match(bwin_name, candidate.name, expected_gender)):
                # No se identifica por prefijo: solo mejora el ranking de un
                # candidato que aún debe cuadrar con el fixture del encuentro.
                score += 350
                reasons.append("mismo núcleo de club con prefijo institucional")

            for alias in variants[1:]:
                alias_base = base_name(alias)
                if cand_name == alias:
                    score += 500
                    reasons.append(f"alias exacto: {alias}")
                    break
                if alias_base and cand_base == alias_base:
                    score += 450
                    reasons.append(f"alias base exacto: {alias}")
                    break

            if expected_gender == "female":
                if candidate.gender == "female":
                    score += 150
                    reasons.append("entidad femenina")
                if cand_base in target_bases:
                    score += 100
                    reasons.append("variante femenina")

            common = set(target_base.split()) & set(cand_base.split())
            score += len(common) * 20

            alias_common: set[str] = set()
            for vb in target_bases:
                alias_common |= set(vb.split()) & set(cand_base.split())
            score += len(alias_common) * 15

            if cand_base != target_base and cand_base not in target_bases:
                if target_base in cand_base:
                    score -= 100
                    reasons.append("nombre contenido en otra entidad")
                elif cand_base in target_base:
                    score -= 50
                    reasons.append("nombre candidato parcial")

            if any(p in cand_name for p in YOUTH_PATTERNS):
                score -= 150
                reasons.append("juvenil/reserva")

            # CAMBIO CLAVE: antes una selección siempre restaba 50, justo cuando
            # Bwin pide una selección. Ahora se premia/castiga según lo esperado.
            if expected_national:
                if candidate.national:
                    score += 400
                    reasons.append("selección nacional esperada")
                else:
                    score -= 300
                    reasons.append("club (se esperaba selección)")
            elif candidate.national:
                score -= 50
                reasons.append("selección nacional")

            scored.append((score, candidate, reasons))

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    @staticmethod
    def _log_ranking(bwin_name: str, scored: list[tuple[int, APITeam, list[str]]]) -> None:
        LOG.debug(f"Ranking de candidatos para '{bwin_name}':")
        for score, c, reasons in scored[:5]:
            LOG.debug(
                f"  score={score:4d} | ID={c.id} | {c.name} | country={c.country} | "
                f"gender={c.gender} | national={c.national} | motivos={', '.join(reasons)}"
            )

    @staticmethod
    def _accept(
            scored: list[tuple[int, APITeam, list[str]]],
            expected_gender: str,
            expected_national: bool = False,
    ) -> tuple[Optional[APITeam], str]:
        if not scored:
            return None, "sin candidatos"

        best_score, best, _ = scored[0]
        if expected_national and not best.national:
            return None, f"se esperaba una selección y el mejor candidato '{best.name}' es un club"
        if best.gender != expected_gender:
            return None, f"mejor candidato '{best.name}' tiene género '{best.gender}'"
        if best_score < 300:
            return None, f"score insuficiente ({best_score}) para '{best.name}'"

        # Una duplicación del mismo ID jamás constituye ambigüedad.
        # La ambigüedad solo existe entre entidades de IDs distintos.
        for second_score, second, _ in scored[1:]:
            if second.id == best.id:
                continue
            if second.gender == expected_gender and second_score >= best_score - 40:
                return None, (
                    f"resolución ambigua: {best.name} ({best_score}, ID {best.id}) vs "
                    f"{second.name} ({second_score}, ID {second.id})"
                )
            break
        return best, "ok"


# =============================================================================
# RESOLUCIÓN CONTEXTUAL DE EQUIPOS
# =============================================================================


class ContextualTeamResolver:
    """
    Corrige errores de /teams?search= usando al otro equipo como ancla:
    busca en sus fixtures un rival que se parezca al nombre buscado.

    Parches respecto a V5.6:
      - Exige similitud de nombre > 0 (antes todos los rivales puntuaban 300
        solo por 'género correcto' y salía siempre 'ambiguo').
      - Deduplica por ID de rival (el mismo equipo repetido ya no se considera
        un empate contra sí mismo).
      - Usa variantes en inglés para selecciones nacionales.
    """

    def __init__(self, api: APIFootballClient) -> None:
        self.api = api

    def _anchor_fixtures(self, anchor: APITeam) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for label, key, limit in (
                ("next", "next", CONTEXT_FIXTURE_LOOKAHEAD),
                ("last", "last", CONTEXT_FIXTURE_HISTORY),
        ):
            try:
                data = self.api.get("fixtures", {"team": anchor.id, key: limit})
                groups.append((label, data.get("response", []) or []))
            except Exception as exc:
                LOG.warn(f"No se pudieron consultar fixtures '{label}' de {anchor.name}: {exc}")
        return groups

    def resolve_from_opponent_fixtures(
            self,
            target_name: str,
            expected_gender: str,
            known_opponent: APITeam,
    ) -> Optional[APITeam]:

        LOG.info(
            f"Resolución contextual de '{target_name}' usando "
            f"'{known_opponent.name}' (ID {known_opponent.id}) como ancla."
        )

        normalized = normalize_text(target_name)
        variants = name_variants(normalized, use_translation=False)
        variant_bases = {base_name(v) for v in variants}
        expected_national = normalized in COUNTRY_ES_EN

        best_by_id: dict[int, tuple[int, APITeam, dict[str, Any]]] = {}

        for source, fixtures in self._anchor_fixtures(known_opponent):
            for item in fixtures:
                teams = item.get("teams", {}) or {}
                home = teams.get("home", {}) or {}
                away = teams.get("away", {}) or {}
                if known_opponent.id not in (home.get("id"), away.get("id")):
                    continue

                opponent = away if home.get("id") == known_opponent.id else home
                opp_id, opp_name = opponent.get("id"), opponent.get("name")
                if not opp_id or not opp_name or opp_id == known_opponent.id:
                    continue

                if not club_category_compatible(target_name, opp_name, expected_gender):
                    continue
                name_score = max(team_name_similarity(v, opp_name, expected_gender) for v in variants)
                opp_norm = normalize_text(opp_name)
                opp_base = base_name(opp_name)
                exact_name = opp_norm in variants
                exact_base = opp_base in variant_bases

                # PARCHE: sin ninguna similitud de nombre no es candidato.
                if name_score == 0 and not exact_name and not exact_base:
                    continue

                api_gender = detect_gender(opp_name)
                score = name_score
                reasons: list[str] = []

                if api_gender == expected_gender:
                    score += 300
                    reasons.append("género correcto")
                else:
                    score -= 500
                    reasons.append("género incorrecto")

                if exact_base:
                    score += 350
                    reasons.append("nombre base exacto")
                if exact_name:
                    score += 300
                    reasons.append("nombre exacto")

                status = (item.get("fixture", {}).get("status", {}) or {}).get("short")
                if status in FUTURE_STATUSES:
                    score += 100
                    reasons.append("fixture futuro")

                league_name = normalize_text((item.get("league", {}) or {}).get("name", ""))
                if expected_gender == "female" and any(t in league_name for t in ("women", "femin")):
                    score += 100
                    reasons.append("competición femenina")

                team = APITeam(
                    id=int(opp_id), name=opp_name, code=None,
                    country=opponent.get("country"), founded=None,
                    national=expected_national, gender=api_gender,
                    logo=opponent.get("logo"), venue_name=None,
                )

                LOG.debug(
                    f"  Contexto {source}: {known_opponent.name} vs {opp_name} | "
                    f"score={score} | motivos={', '.join(reasons)}"
                )

                current = best_by_id.get(int(opp_id))
                if current is None or score > current[0]:
                    best_by_id[int(opp_id)] = (score, team, item)

        if not best_by_id:
            LOG.warn(f"No se encontraron candidatos contextuales para '{target_name}'.")
            return None

        candidates = sorted(best_by_id.values(), key=lambda x: x[0], reverse=True)
        best_score, best_team, best_fixture = candidates[0]

        LOG.info(f"Mejor candidato contextual: {best_team.name} (ID {best_team.id}) score={best_score}")

        if best_team.gender != expected_gender:
            LOG.warn("El candidato contextual tiene género incorrecto. Se descarta.")
            return None
        if best_score < 300:
            LOG.warn(f"Score contextual insuficiente: {best_score}")
            return None
        if len(candidates) > 1 and candidates[1][0] >= best_score - 40:
            LOG.warn(
                f"Resolución contextual ambigua: {best_team.name} ({best_score}) vs "
                f"{candidates[1][1].name} ({candidates[1][0]})"
            )
            return None

        LOG.ok(f"Equipo resuelto contextualmente: {target_name} -> {best_team.name} (API ID {best_team.id})")
        fixture_id = best_fixture.get("fixture", {}).get("id")
        if fixture_id:
            LOG.ok(f"La evidencia contextual proviene del fixture {fixture_id}.")
        return best_team


# =============================================================================
# DESCUBRIMIENTO DE COMPETICIONES
# =============================================================================


class CompetitionDiscovery:
    def __init__(self, api: APIFootballClient) -> None:
        self.api = api

    def recent_fixtures(self, team_id: int, limit: Optional[int] = None) -> list[dict[str, Any]]:
        limit = limit or RECENT_FIXTURES_LIMIT
        data = self.api.get("fixtures", {"team": team_id, "last": limit})
        fixtures = data.get("response", []) or []
        LOG.ok(f"Equipo {team_id}: {len(fixtures)} fixtures recientes obtenidos.")
        return fixtures

    def discover(self, team: APITeam) -> list[CompetitionInfo]:
        competitions: dict[tuple[int, int], CompetitionInfo] = {}

        for item in self.recent_fixtures(team.id):
            league = item.get("league", {}) or {}
            date = (item.get("fixture", {}) or {}).get("date")
            league_id, season = league.get("id"), league.get("season")
            if league_id is None or season is None:
                continue

            key = (int(league_id), int(season))
            if key not in competitions:
                competitions[key] = CompetitionInfo(
                    league_id=int(league_id),
                    league_name=league.get("name", ""),
                    season=int(season),
                    country=league.get("country"),
                    latest_date=date,
                    appearances=1,
                )
            else:
                info = competitions[key]
                info.appearances += 1
                if not info.latest_date or (date and date > info.latest_date):
                    info.latest_date = date

        result = sorted(
            competitions.values(),
            key=lambda c: (c.latest_date or "", c.appearances),
            reverse=True,
        )

        LOG.ok(f"{team.name}: {len(result)} competiciones detectadas.")
        for comp in result[:10]:
            LOG.debug(
                f"  {comp.league_id} | {comp.league_name} | season={comp.season} | "
                f"apariciones={comp.appearances} | último={comp.latest_date}"
            )
        return result

    @staticmethod
    def common_competitions(
            home_competitions: list[CompetitionInfo],
            away_competitions: list[CompetitionInfo],
    ) -> list[tuple[int, int]]:
        home_keys = {(c.league_id, c.season) for c in home_competitions}
        away_keys = {(c.league_id, c.season) for c in away_competitions}
        return list(home_keys & away_keys)


# =============================================================================
# RESOLVER DEL FIXTURE
# =============================================================================


class FixtureResolver:
    """
    Cambio principal: como ya conocemos los IDs de ambos equipos, el fixture se
    busca por pareja de IDs exacta (no por parecido de nombres), y entre varios
    enfrentamientos se elige el activo/próximo más cercano.
    """

    def __init__(self, api: APIFootballClient) -> None:
        self.api = api

    def resolve(
            self,
            match: BwinMatch,
            home_team: APITeam,
            away_team: APITeam,
            home_competitions: list[CompetitionInfo],
            away_competitions: list[CompetitionInfo],
    ) -> ResolvedFixture:

        # El rescate de identidad por calendario puede obtener el fixture objetivo
        # aunque /fixtures?team=...next no lo devuelva. Conservar su ID y verificarlo.
        verified_id = getattr(match, "verified_fixture_id", None)
        if verified_id:
            try:
                rows = self.api.get("fixtures", {"id": verified_id}).get("response", []) or []
                exact = [row for row in rows if
                         (row.get("teams", {}).get("home", {}) or {}).get("id") == home_team.id and
                         (row.get("teams", {}).get("away", {}) or {}).get("id") == away_team.id and
                         (row.get("fixture", {}) or {}).get("id") == verified_id]
                if len(exact) == 1:
                    LOG.ok(f"FIXTURE confirmado por ID de calendario: {verified_id}")
                    return self._to_resolved_fixture(exact[0])
                LOG.warn(f"El fixture {verified_id} no reproduce ambos IDs y localía; se rechaza.")
            except Exception as exc:
                LOG.warn(f"No se pudo consultar fixture ID {verified_id}: {exc}")

        for label, anchor, other in (
                ("HOME", home_team, away_team),
                ("AWAY", away_team, home_team),
        ):
            fixture = self._search_from_team_fixtures(match, anchor, other)
            if fixture:
                LOG.ok(f"FIXTURE ENCONTRADO POR CONTEXTO {label}: {fixture.fixture_id}")
                return fixture

        common = CompetitionDiscovery.common_competitions(home_competitions, away_competitions)
        LOG.info(f"Competiciones comunes detectadas: {len(common)}")

        for league_id, season in common:
            LOG.info(f"Probando competición común: league={league_id}, season={season}")
            fixture = self._search_in_competition(match, home_team, away_team, league_id, season)
            if fixture:
                LOG.ok(f"FIXTURE ENCONTRADO: {fixture.fixture_id}")
                return fixture

        tried = set(common)
        for label, comps in (("HOME", home_competitions), ("AWAY", away_competitions)):
            LOG.warn(f"Sin resultados aún. Probando competiciones {label}.")
            for comp in comps:
                key = (comp.league_id, comp.season)
                if key in tried:
                    continue
                tried.add(key)
                fixture = self._search_in_competition(match, home_team, away_team, comp.league_id, comp.season)
                if fixture:
                    return fixture

        raise RuntimeError(
            f"No se pudo encontrar el fixture exacto {home_team.name} vs {away_team.name}."
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _pair_matches(item: dict[str, Any], id_a: int, id_b: int) -> bool:
        teams = item.get("teams", {}) or {}
        ids = {(teams.get("home", {}) or {}).get("id"), (teams.get("away", {}) or {}).get("id")}
        return ids == {id_a, id_b} and bool((item.get("fixture", {}) or {}).get("id"))

    @staticmethod
    def _candidate_score(match: BwinMatch, fixture: dict[str, Any]) -> float:
        status = (fixture.get("status", {}) or {}).get("short")
        dt = parse_date(fixture.get("date"))
        now = datetime.now(timezone.utc)
        score = 0.0

        if status in LIVE_STATUSES:
            score += 1200
        elif status in FUTURE_STATUSES:
            days = max(((dt - now).total_seconds() / 86400) if dt else 0, 0)
            score += 1000 - min(days, 900)
        else:  # ya jugado: el más reciente primero
            days = max(((now - dt).total_seconds() / 86400) if dt else 900, 0)
            score += 400 - min(days, 390)

        if same_date(match.event_date, fixture.get("date")):
            score += 5000
        return score

    def _pick_best(self, match: BwinMatch, items: list[dict[str, Any]]) -> dict[str, Any]:
        return max(items, key=lambda it: self._candidate_score(match, it.get("fixture", {}) or {}))

    def _search_from_team_fixtures(
            self,
            match: BwinMatch,
            anchor: APITeam,
            opponent: APITeam,
    ) -> Optional[ResolvedFixture]:

        LOG.info(f"Buscando fixture contextual: ancla={anchor.name} vs objetivo={opponent.name}")

        found: list[dict[str, Any]] = []
        for key, limit in (("next", CONTEXT_FIXTURE_LOOKAHEAD), ("last", CONTEXT_FIXTURE_HISTORY)):
            try:
                data = self.api.get("fixtures", {"team": anchor.id, key: limit})
            except Exception as exc:
                LOG.warn(f"No se pudo consultar {key} de {anchor.name}: {exc}")
                continue
            for item in data.get("response", []) or []:
                if self._pair_matches(item, anchor.id, opponent.id):
                    found.append(item)
                    LOG.debug(
                        f"  candidato {key}: fixture={item['fixture']['id']} | "
                        f"{item['fixture'].get('date')} | "
                        f"status={(item['fixture'].get('status') or {}).get('short')}"
                    )

        if not found:
            return None

        best = self._pick_best(match, found)
        LOG.ok(f"Fixture contextual seleccionado: ID={best['fixture']['id']}")
        return self._to_resolved_fixture(best)

    def _search_in_competition(
            self,
            match: BwinMatch,
            home_team: APITeam,
            away_team: APITeam,
            league_id: int,
            season: int,
    ) -> Optional[ResolvedFixture]:

        data = self.api.get("fixtures", {"league": league_id, "season": season, "team": home_team.id})
        fixtures = (data.get("response", []) or [])[:MAX_COMPETITION_FIXTURES]
        found = [it for it in fixtures if self._pair_matches(it, home_team.id, away_team.id)]
        if not found:
            return None

        best = self._pick_best(match, found)
        LOG.debug(f"Candidato fixture: id={best['fixture']['id']}")
        return self._to_resolved_fixture(best)

    @staticmethod
    def _to_resolved_fixture(item: dict[str, Any]) -> ResolvedFixture:
        fixture = item.get("fixture", {}) or {}
        league = item.get("league", {}) or {}
        teams = item.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        venue = fixture.get("venue", {}) or {}
        status = fixture.get("status", {}) or {}

        return ResolvedFixture(
            fixture_id=int(fixture["id"]),
            date=fixture.get("date", ""),
            status=status.get("short", ""),
            status_long=status.get("long", ""),
            league_id=int(league["id"]),
            league_name=league.get("name", ""),
            season=int(league["season"]),
            round_name=league.get("round"),
            home_id=int(home["id"]),
            home_name=home.get("name", ""),
            away_id=int(away["id"]),
            away_name=away.get("name", ""),
            referee=fixture.get("referee"),
            venue_name=venue.get("name"),
            venue_city=venue.get("city"),
        )


# =============================================================================
# HISTORIAL PROFUNDO
# =============================================================================


class DeepHistoryCollector:
    """Historial de un equipo + detalle completo vía /fixtures?ids=... (máx. 20 por consulta)."""

    def __init__(self, api: APIFootballClient) -> None:
        self.api = api

    def collect_team_history(self, team: APITeam, limit: int) -> dict[str, Any]:
        LOG.info(f"Construyendo historial profundo de {team.name}...")

        basic_data = self.api.get("fixtures", {"team": team.id, "last": limit})
        basic_fixtures = basic_data.get("response", []) or []

        def fid(item: dict[str, Any]) -> Any:
            return (item.get("fixture", {}) or {}).get("id")

        historical = [
            it for it in basic_fixtures
            if fid(it) and (it["fixture"].get("status", {}) or {}).get("short") in COMPLETED_STATUSES
        ]
        if not historical:
            historical = [it for it in basic_fixtures if fid(it)]

        fixture_ids = list(dict.fromkeys(int(fid(it)) for it in historical))
        LOG.ok(f"{team.name}: {len(fixture_ids)} IDs históricos seleccionados.")

        detailed: list[dict[str, Any]] = []
        batches = chunked(fixture_ids, FIXTURE_IDS_BATCH_SIZE)

        for index, batch in enumerate(batches, start=1):
            LOG.debug(f"{team.name}: detalle histórico batch {index}/{len(batches)} ({len(batch)} fixtures)")
            data = self.api.get("fixtures", {"ids": "-".join(str(v) for v in batch)})
            response = data.get("response", []) or []
            detailed.extend(response)
            LOG.debug(f"{team.name}: batch {index} -> {len(response)} fixtures detallados.")

        detailed.sort(key=lambda it: (it.get("fixture", {}) or {}).get("date") or "", reverse=True)

        return {
            "team": asdict(team),
            "requested_limit": limit,
            "basic_fixtures_count": len(basic_fixtures),
            "completed_fixtures_count": len(historical),
            "fixture_ids": fixture_ids,
            "batches": len(batches),
            "detailed_fixtures": detailed,
        }


# =============================================================================
# HISTORIAL DEL ÁRBITRO
# =============================================================================

def referee_card_profile(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Perfil de tarjetas del árbitro. Solo usa partidos que tienen el dato (no cuenta faltantes como 0)."""

    def pct_over(values: list[float], line: float) -> Optional[float]:
        return round(100 * sum(v > line for v in values) / len(values), 1) if values else None

    # Partidos con stats o eventos: un partido con 0 tarjetas SÍ cuenta como dato válido.
    card_ms = [m for m in matches
               if m["data_quality"]["statistics"] or m["data_quality"]["events"]]
    foul_ms = [m for m in matches if m["data_quality"]["fouls_available"]]
    both = [m for m in card_ms if m["data_quality"]["fouls_available"]]

    yellows = [m["yellow_cards_total"] for m in card_ms]
    reds = [m["red_cards_total"] for m in card_ms]
    cards = [y + r for y, r in zip(yellows, reds)]
    fouls = [m["fouls_total"] for m in foul_ms]

    total_f = sum(m["fouls_total"] for m in both)
    total_c = sum(m["yellow_cards_total"] + m["red_cards_total"] for m in both)

    return {
        "sample_cards": len(card_ms),
        "sample_fouls": len(foul_ms),
        "yellow_avg": average_list(yellows),
        "red_avg": average_list(reds),
        "cards_avg": average_list(cards),
        "fouls_avg": average_list(fouls),
        "fouls_per_card": round(total_f / total_c, 2) if total_c else None,
        "penalty_avg": average_list([m["penalties"] for m in matches]),
        "over_cards_pct": {str(l): pct_over(cards, l) for l in (2.5, 3.5, 4.5, 5.5, 6.5)},
        "over_fouls_pct": {str(l): pct_over(fouls, l) for l in (20.5, 24.5, 28.5)},
        "last_5_cards": cards[:5],  # los matches ya vienen del más reciente al más antiguo
    }

class RefereeHistoryCollector:
    """
    Reconstruye el historial reciente del árbitro filtrando localmente
    fixture.referee (API-Football no permite filtrar /fixtures por referee).
    """

    def __init__(self, api: APIFootballClient) -> None:
        self.api = api

    @staticmethod
    def _referee_base_name(value: Optional[str]) -> str:
        if not value:
            return ""
        return normalize_text(str(value).split(",", 1)[0])

    @classmethod
    def _same_referee(cls, target: Optional[str], candidate: Optional[str]) -> bool:
        t, c = cls._referee_base_name(target), cls._referee_base_name(candidate)
        if not t or not c:
            return False
        if t == c:
            return True
        # "Nick Walsh" vs "N. Walsh": mismo apellido y misma inicial del nombre
        tt, ct = t.replace(".", " ").split(), c.replace(".", " ").split()
        if len(tt) < 2 or len(ct) < 2:
            return False
        if tt[-1] != ct[-1] or tt[0][0] != ct[0][0]:
            return False
        # Si ambos traen país ("..., Scotland"), también debe coincidir
        t_country = normalize_text(str(target).split(",", 1)[1]) if "," in str(target) else ""
        c_country = normalize_text(str(candidate).split(",", 1)[1]) if "," in str(candidate) else ""
        return not (t_country and c_country and t_country != c_country)

    @staticmethod
    def _date_window(fixture_date: Optional[str], days_back: int) -> tuple[str, str]:
        target = parse_date(fixture_date) or datetime.now(timezone.utc)
        return (target - timedelta(days=days_back)).date().isoformat(), target.date().isoformat()

    def _scan_pages(
            self,
            params_base: dict[str, Any],
            target_referee: str,
            limit: int,
            max_pages: int,   # se conserva por compatibilidad; /fixtures no pagina
            label: str,
    ) -> list[dict[str, Any]]:
        try:
            data = self.api.get("fixtures", dict(params_base))
        except Exception as exc:
            LOG.warn(f"Escaneo árbitro {label} no disponible: {exc}")
            return []

        response = data.get("response", []) or []
        LOG.info(f"Historial árbitro {label}: {len(response)} fixtures")

        return self._filter_referee(response, target_referee, limit)

    def _filter_referee(
            self,
            response: list[dict[str, Any]],
            target_referee: str,
            limit: int,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for item in response:
            fixture = item.get("fixture", {}) or {}
            fixture_id = safe_int(fixture.get("id"))
            status = (fixture.get("status", {}) or {}).get("short")
            if not fixture_id or fixture_id in seen_ids or status not in COMPLETED_STATUSES:
                continue
            if self._same_referee(target_referee, fixture.get("referee")):
                seen_ids.add(fixture_id)
                matches.append(item)
                LOG.ok(f"Árbitro encontrado: {fixture.get('referee')} | fixture={fixture_id}")
        # /fixtures viene en orden ascendente: nos quedamos con los más recientes
        matches.sort(key=lambda x: x["fixture"].get("date") or "", reverse=True)
        return matches[:limit]

    def _scan_by_dates(
            self,
            target_referee: str,
            limit: int,
            end_date: str,
            max_days: int,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        seen: set[int] = set()
        day = datetime.fromisoformat(end_date).date()

        for _ in range(max_days):
            day -= timedelta(days=1)   # hoy no sirve: aún no está completo
            try:
                data = self.api.get("fixtures", {"date": day.isoformat()})
                resp = data.get("response", []) or []
                con_arbitro = sum(1 for i in resp if (i.get("fixture") or {}).get("referee"))
                apellido = self._referee_base_name(target_referee).replace(".", " ").split()[-1]
                for i in resp:
                    r = (i.get("fixture") or {}).get("referee") or ""
                    if apellido in normalize_text(r):
                        LOG.debug(f"    posible: '{r}' | fixture={i['fixture'].get('id')} | "
                                  f"{(i.get('league') or {}).get('name')}")
                LOG.debug(f"  {day}: {len(resp)} fixtures, {con_arbitro} con árbitro")
            except Exception as exc:
                LOG.warn(f"Escaneo por fecha {day}: {exc}")
                continue
            for item in self._filter_referee(data.get("response", []) or [], target_referee, limit):
                fid = safe_int(item["fixture"].get("id"))
                if fid and fid not in seen:
                    seen.add(fid)
                    matches.append(item)
            if len(matches) >= limit:
                break

        LOG.info(f"Escaneo por fecha: {len(matches)} partidos del árbitro encontrados.")
        matches.sort(key=lambda x: x["fixture"].get("date") or "", reverse=True)
        return matches[:limit]

    @staticmethod
    def _match_metrics(item: dict[str, Any]) -> dict[str, Any]:
        teams = item.get("teams", {}) or {}
        events = item.get("events", []) or []
        stats = item.get("statistics", []) or []

        total_yellow = total_red = total_fouls = 0
        for team_block in stats:
            for st in team_block.get("statistics", []) or []:
                typ, val = st.get("type"), to_number(st.get("value"))
                if val is None:
                    continue
                if typ == "Yellow Cards":
                    total_yellow += val
                elif typ == "Red Cards":
                    total_red += val
                elif typ == "Fouls":
                    total_fouls += val

        yellow_details = {"yellow card", "second yellow card", "yellow-red card"}
        event_yellows = sum(
            1 for e in events
            if e.get("type") == "Card" and str(e.get("detail", "")).lower() in yellow_details
        )
        event_reds = sum(
            1 for e in events
            if e.get("type") == "Card" and "red" in str(e.get("detail", "")).lower()
        )
        event_penalties = sum(
            1 for e in events
            if e.get("type") == "Goal" and "penalty" in str(e.get("detail", "")).lower()
        )

        yellow_value = total_yellow if total_yellow else event_yellows
        red_value = total_red if total_red else event_reds
        fixture = item.get("fixture", {}) or {}

        return {
            "fixture_id": fixture.get("id"),
            "date": fixture.get("date"),
            "referee": fixture.get("referee"),
            "league": item.get("league", {}),
            "home": teams.get("home", {}),
            "away": teams.get("away", {}),
            "score": item.get("goals", {}),
            "yellow_cards_total": yellow_value,
            "red_cards_total": red_value,
            "fouls_total": total_fouls if total_fouls else None,
            "penalties": event_penalties,
            "data_quality": {
                "statistics": bool(stats),
                "events": bool(events),
                "fouls_available": total_fouls > 0,
                "cards_available": bool(total_yellow or total_red or event_yellows or event_reds),
            },
        }

    def collect(
            self,
            referee: Optional[str],
            fixture: Optional[ResolvedFixture] = None,
            limit: int = REFEREE_HISTORY_LIMIT,
    ) -> dict[str, Any]:

        if not referee:
            return {"available": False, "reason": "El fixture no proporciona nombre de árbitro.", "matches": []}

        LOG.info(f"Reconstruyendo historial reciente del árbitro: {referee}")
        from_date, to_date = self._date_window(fixture.date if fixture else None, REFEREE_HISTORY_DAYS_BACK)

        collected: list[dict[str, Any]] = []

        if fixture:
            LOG.info(
                f"Escaneando competición objetivo del árbitro: {fixture.league_name} "
                f"(league={fixture.league_id}, season={fixture.season})"
            )
            collected = self._scan_pages(
                {"league": fixture.league_id, "season": fixture.season, "from": from_date, "to": to_date},
                referee, limit, REFEREE_HISTORY_COMPETITION_PAGES, "competición",
            )

        # La búsqueda diaria global es costosa: limitarla a 10 días como máximo
        # incluso cuando REFEREE_HISTORY_GLOBAL_DAYS sea 180 en constants.py.
        global_scan_days = min(max(0, REFEREE_HISTORY_GLOBAL_DAYS), 10)
        if len(collected) < limit and global_scan_days:
            LOG.info(
                f"Historial arbitral incompleto ({len(collected)}/{limit}). "
                f"Búsqueda global acotada a {global_scan_days} días (no {REFEREE_HISTORY_GLOBAL_DAYS})."
            )
            extra = self._scan_by_dates(
                referee, limit - len(collected), to_date, global_scan_days)
            existing = {safe_int(it.get("fixture", {}).get("id")) for it in collected}
            for item in extra:
                fid = safe_int(item.get("fixture", {}).get("id"))
                if fid and fid not in existing:
                    collected.append(item)
                    existing.add(fid)
                if len(collected) >= limit:
                    break

        if not collected:
            return {
                "available": False,
                "reason": "No se encontraron fixtures cuyo campo fixture.referee coincida con el árbitro.",
                "referee": referee,
                "requested_limit": limit,
                "search_window": {"from": from_date, "to": to_date},
                "matches": [],
            }

        completed = [
            it for it in collected
            if it.get("fixture", {}).get("status", {}).get("short") in COMPLETED_STATUSES
        ]
        if completed:
            collected = completed

        collected.sort(key=lambda x: x.get("fixture", {}).get("date") or "", reverse=True)

        ids = list(dict.fromkeys(
            safe_int(it.get("fixture", {}).get("id"))
            for it in collected if safe_int(it.get("fixture", {}).get("id"))
        ))[:limit]

        detailed: list[dict[str, Any]] = []
        for batch in chunked(ids, FIXTURE_IDS_BATCH_SIZE):
            try:
                data = self.api.get("fixtures", {"ids": "-".join(str(x) for x in batch)})
                detailed.extend(data.get("response", []) or [])
            except Exception as exc:
                LOG.warn(f"No se pudo ampliar historial del árbitro: {exc}")

        detailed.sort(key=lambda x: x.get("fixture", {}).get("date") or "", reverse=True)
        detailed = [
            it for it in detailed
            if self._same_referee(referee, it.get("fixture", {}).get("referee"))
        ][:limit]

        matches = [self._match_metrics(it) for it in detailed]

        return {
            "available": bool(matches),
            "referee": referee,
            "requested_limit": limit,
            "search_window": {"from": from_date, "to": to_date},
            "matches": matches,
            "averages": {
                "cards_total": average_list([m["yellow_cards_total"] + m["red_cards_total"] for m in matches]),
                "red_cards": average_list([m["red_cards_total"] for m in matches]),
                "fouls_total": average_list([m["fouls_total"] or 0 for m in matches]),
                "penalties": average_list([m["penalties"] for m in matches]),
            },
            "coverage": {
                "matches": len(matches),
                "cards_matches": sum(1 for m in matches if m["data_quality"]["cards_available"]),
                "fouls_matches": sum(1 for m in matches if m["data_quality"]["fouls_available"]),
            },
            "profile": referee_card_profile(matches),
        }


# =============================================================================
# CONTEXTO PRINCIPAL (llamadas independientes en paralelo)
# =============================================================================


class ContextCollector:

    def __init__(self, api: APIFootballClient) -> None:
        self.api = api
        self.history = DeepHistoryCollector(api)
        self.referee_history = RefereeHistoryCollector(api)

    def _safe_get(self, label: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.api.get(endpoint, params)
        except Exception as exc:
            LOG.warn(f"{label} no disponible: {exc}")
            return {"error": str(exc)}

    @staticmethod
    def _guard(label: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            LOG.warn(f"{label} no disponible: {exc}")
            return {"error": str(exc)}

    def _run(self, tasks: dict[str, Callable[[], Any]]) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {key: pool.submit(self._guard, key, fn) for key, fn in tasks.items()}
            return {key: future.result() for key, future in futures.items()}  # orden estable

    def collect(self, fixture: ResolvedFixture, home_team: APITeam, away_team: APITeam) -> dict[str, Any]:
        fixture_id = fixture.fixture_id
        LOG.info(f"Recolectando contexto profundo del fixture {fixture_id}...")

        sides = (("home", home_team), ("away", away_team))
        get = self._safe_get

        tasks: dict[str, Callable[[], Any]] = {
            "target_fixture_full": partial(self.api.get, "fixtures", {"id": fixture_id}),
        }

        if GET_LAST_MATCHES:
            if GET_DEEP_HISTORY:
                for side, team in sides:
                    tasks[f"{side}_history_deep"] = partial(
                        self.history.collect_team_history, team, DEEP_HISTORY_LIMIT)
            else:
                for side, team in sides:
                    tasks[f"{side}_last_10"] = partial(
                        self.api.get, "fixtures", {"team": team.id, "last": LAST_MATCHES_LIMIT})

        if GET_REFEREE_HISTORY:
            tasks["referee_history"] = partial(
                self.referee_history.collect, fixture.referee, fixture, REFEREE_HISTORY_LIMIT)

        if GET_NEXT_MATCHES:
            for side, team in sides:
                tasks[f"{side}_next_5"] = partial(
                    get, f"Next matches {side.upper()}", "fixtures",
                    {"team": team.id, "next": NEXT_MATCHES_LIMIT})

        if GET_H2H:
            tasks["h2h"] = partial(
                get, "H2H", "fixtures/headtohead",
                {"h2h": f"{home_team.id}-{away_team.id}", "last": 10})
        if GET_STANDINGS:
            tasks["standings"] = partial(
                get, "Standings", "standings",
                {"league": fixture.league_id, "season": fixture.season})
        if GET_PREDICTIONS:
            tasks["predictions"] = partial(get, "Predictions", "predictions", {"fixture": fixture_id})
        if GET_INJURIES:
            tasks["injuries"] = partial(get, "Injuries", "injuries", {"fixture": fixture_id})
        if GET_LINEUPS:
            # El XI objetivo cambia cerca del inicio. Una respuesta cacheada 20 min
            # antes puede estar obsoleta. Refrescar SOLO este endpoint en ventana
            # prepartido, sin realizar consultas live/post-kickoff automáticamente.
            kickoff = parse_date(fixture.date)
            if kickoff and kickoff.tzinfo is not None:
                seconds = (kickoff - datetime.now(timezone.utc)).total_seconds()
            else:
                seconds = None
            if seconds is not None and 0 < seconds <= 7200:
                LOG.info('XI objetivo: consulta directa sin caché dentro de las 2 h previas al kickoff.')
                tasks['lineups'] = partial(self.api.get, 'fixtures/lineups',
                                           {'fixture': fixture_id}, use_cache=False)
            else:
                tasks['lineups'] = partial(get, 'Lineups', 'fixtures/lineups', {'fixture': fixture_id})
        if GET_STATISTICS:
            tasks["statistics"] = partial(
                get, "Statistics objetivo", "fixtures/statistics", {"fixture": fixture_id})

        if GET_SQUADS:
            tasks["squads"] = lambda: {
                side: get(f"Squad {side.upper()}", "players/squads", {"team": team.id})
                for side, team in sides
            }
        if GET_COACHS:
            tasks["coachs"] = lambda: {
                side: get(f"Coach {side.upper()}", "coachs", {"team": team.id})
                for side, team in sides
            }
        if GET_API_ODDS:
            tasks["api_odds"] = partial(get, "Odds API-Football", "odds", {"fixture": fixture_id})

        results = self._run(tasks)

        context: dict[str, Any] = {
            "meta": {
                "generator_version": VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "deep_history_enabled": GET_DEEP_HISTORY,
                "history_limit": DEEP_HISTORY_LIMIT,
                "fixture_ids_batch_size": FIXTURE_IDS_BATCH_SIZE,
            },
            "fixture": asdict(fixture),
            "teams": {"home": asdict(home_team), "away": asdict(away_team)},
        }
        context.update(results)

        if "target_fixture_full" in results and not results["target_fixture_full"].get("error"):
            LOG.ok("Fixture objetivo completo obtenido.")
        return context


# =============================================================================
# EXTRACTORES
# =============================================================================


class DataExtractor:

    @staticmethod
    def extract_statistics(item: dict[str, Any]) -> list[dict[str, Any]]:
        return item.get("statistics", []) or []

    @staticmethod
    def extract_players(item: dict[str, Any]) -> list[dict[str, Any]]:
        return item.get("players", []) or []

    @staticmethod
    def extract_events(item: dict[str, Any]) -> list[dict[str, Any]]:
        return item.get("events", []) or []

    @staticmethod
    def extract_lineups(item: dict[str, Any]) -> list[dict[str, Any]]:
        return item.get("lineups", []) or []

    @staticmethod
    def extract_score(item: dict[str, Any], team_id: int) -> dict[str, Any]:
        teams = item.get("teams", {}) or {}
        goals = item.get("goals", {}) or {}
        score = item.get("score", {}) or {}

        if team_id == (teams.get("home", {}) or {}).get("id"):
            side = "home"
        elif team_id == (teams.get("away", {}) or {}).get("id"):
            side = "away"
        else:
            return {}

        def part(name: str) -> Any:
            return (score.get(name, {}) or {}).get(side)

        return {
            "team_id": team_id,
            "is_home": side == "home",
            "goals": goals.get(side),
            "halftime": part("halftime"),
            "fulltime": part("fulltime"),
            "extratime": part("extratime"),
            "penalty": part("penalty"),
        }

    @staticmethod
    def extract_player_rows_for_team(item: dict[str, Any], team_id: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for team_block in DataExtractor.extract_players(item):
            if (team_block.get("team", {}) or {}).get("id") != team_id:
                continue
            for player_block in team_block.get("players", []) or []:
                stats_list = player_block.get("statistics", []) or []
                result.append({
                    "player": player_block.get("player", {}),
                    "statistics": stats_list[0] if stats_list else {},
                })
        return result


# =============================================================================
# MICROCONTEXTO HISTÓRICO
# =============================================================================

class MicroHistoryFormatter:
    """Genera contexto fixture-by-fixture para razonamiento de micro-mercados."""

    @staticmethod
    def _stats_for_team(item: dict[str, Any], team_id: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for block in DataExtractor.extract_statistics(item):
            if (block.get("team", {}) or {}).get("id") != team_id:
                continue
            for stat in block.get("statistics", []) or []:
                if stat.get("type"):
                    result[stat["type"]] = stat.get("value")
        return result

    @staticmethod
    def _lineup_for_team(item: dict[str, Any], team_id: int) -> dict[str, Any]:
        for block in DataExtractor.extract_lineups(item):
            if (block.get("team", {}) or {}).get("id") == team_id:
                return block
        return {}

    @staticmethod
    def _event_bucket(minute: Any) -> str:
        try:
            m = int(minute)
        except (TypeError, ValueError):
            return "minuto_no_disponible"
        for limit, label in ((15, "00-15"), (30, "16-30"), (45, "31-45+"), (60, "46-60"), (75, "61-75")):
            if m <= limit:
                return label
        return "76-90+"

    @classmethod
    def _event_periods(cls, events: list[dict[str, Any]], team_id: int) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for event in events:
            bucket = cls._event_bucket((event.get("time", {}) or {}).get("elapsed"))
            side = "team" if (event.get("team", {}) or {}).get("id") == team_id else "opponent"
            typ = str(event.get("type", "unknown")).lower().replace(" ", "_")
            key = f"{side}_{typ}"
            slot = result.setdefault(bucket, {})
            slot[key] = slot.get(key, 0) + 1
        return result

    @staticmethod
    def _starting(lineup: dict[str, Any]) -> list[dict[str, Any]]:
        return [p for p in (lineup.get("startXI", []) or []) if p.get("player")]

    @staticmethod
    def _bench(lineup: dict[str, Any]) -> list[dict[str, Any]]:
        return [p for p in (lineup.get("substitutes", []) or []) if p.get("player")]

    @classmethod
    def _player_aggregate(cls, fixtures: list[dict[str, Any]], team_id: int) -> list[dict[str, Any]]:
        """Agregación por fixture; titularidad derivada del XI, no de games.substitute.

        Las estadísticas no disponibles permanecen N/D y se informa cobertura por métrica.
        Un registro con games.minutes=0 y sin participación confirmada no cuenta como aparición.
        """
        agg: dict[int, dict[str, Any]] = {}
        for item in fixtures:
            lineup = cls._lineup_for_team(item, team_id)
            starting_ids = {slot.get("player", {}).get("id") for slot in cls._starting(lineup)}
            bench_ids = {slot.get("player", {}).get("id") for slot in cls._bench(lineup)}
            lineup_known = bool(starting_ids)
            seen: set[int] = set()
            for row in DataExtractor.extract_player_rows_for_team(item, team_id):
                player = row.get("player", {}) or {}
                pid = player.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                stats = row.get("statistics", {}) or {}
                games = stats.get("games", {}) or {}
                minutes = to_number(games.get("minutes"))
                if minutes is None or minutes <= 0:
                    # Aparecer en el banquillo o en el endpoint no implica haber jugado.
                    continue
                entry = agg.setdefault(pid, {
                    "id": pid, "name": player.get("name"), "position": games.get("position"),
                    "position_observed": set(),
                    "appearances": 0, "starts": 0, "starts_unknown": 0, "minutes": 0,
                    **{k: 0 for k in PLAYER_STAT_MAPPING},
                    "metric_coverage": {k: 0 for k in PLAYER_STAT_MAPPING},
                    "ratings": [],
                })
                entry["appearances"] += 1
                if games.get("position"):
                    entry["position_observed"].add(str(games["position"]))
                if lineup_known:
                    entry["starts"] += int(pid in starting_ids)
                elif games.get("substitute") is False:
                    entry["starts"] += 1
                    entry["starts_unknown"] += 1
                elif games.get("substitute") is None:
                    entry["starts_unknown"] += 1
                entry["minutes"] += minutes
                for out, (block, key) in PLAYER_STAT_MAPPING.items():
                    value = to_number((stats.get(block, {}) or {}).get(key))
                    if value is not None:
                        entry[out] += value
                        entry["metric_coverage"][out] += 1
                rating = to_number(games.get("rating"))
                if rating is not None:
                    entry["ratings"].append(rating)
        for entry in agg.values():
            observed = sorted(entry.pop("position_observed", set()))
            entry["position_observed"] = observed
            entry["position_conflict"] = len(observed) > 1
            if len(observed) > 1:
                entry["position"] = '/'.join(observed) + ' (CONFLICTO_EN_API)'
            entry["average_rating"] = average_list(entry.pop("ratings", []))
            for name, count in entry["metric_coverage"].items():
                if count == 0:
                    entry[name] = None
        return sorted(agg.values(), key=lambda x: x.get("minutes", 0), reverse=True)

    @classmethod
    def _averages(cls, fixtures: list[dict[str, Any]], team_id: int) -> dict[str, Any]:
        out: dict[str, list[float]] = {}
        for item in fixtures:
            for key, value in cls._stats_for_team(item, team_id).items():
                n = to_number(value)
                if n is not None:
                    out.setdefault(key, []).append(n)
            goals = to_number(DataExtractor.extract_score(item, team_id).get("goals"))
            if goals is not None:
                out.setdefault("Goals", []).append(goals)
        return {k: average_list(v) for k, v in sorted(out.items())}

    @classmethod
    def _formation_distribution(cls, fixtures: list[dict[str, Any]], team_id: int) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in fixtures:
            formation = cls._lineup_for_team(item, team_id).get("formation") or "N/D"
            result[formation] = result.get(formation, 0) + 1
        return result

    @classmethod
    def _xi_stability(cls, fixtures: list[dict[str, Any]], team_id: int) -> list[dict[str, Any]]:
        counts: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(fixtures, start=1):
            lineup = cls._lineup_for_team(item, team_id)
            starters = cls._starting(lineup)
            bench = cls._bench(lineup)
            for is_start, slot in [(True, v) for v in starters] + [(False, v) for v in bench]:
                player = slot.get("player", {}) or {}
                pid = player.get("id")
                if not pid:
                    continue
                row = counts.setdefault(pid, {
                    "id": pid, "name": player.get("name"), "starts": 0,
                    "matches_present": 0, "recent_start_indices": [],
                    "_seen_fixtures": set(),
                })
                if index not in row["_seen_fixtures"]:
                    row["matches_present"] += 1
                    row["_seen_fixtures"].add(index)
                if is_start:
                    row["starts"] += 1
                    row["recent_start_indices"].append(index)
        result = list(counts.values())
        for row in result:
            row.pop("_seen_fixtures", None)
        result.sort(key=lambda x: (x["starts"], x["matches_present"]), reverse=True)
        return result

    @classmethod
    def _match_record(cls, item: dict[str, Any], team: APITeam, index: int) -> dict[str, Any]:
        fixture = item.get("fixture", {}) or {}
        teams = item.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        is_home = home.get("id") == team.id
        opponent = away if is_home else home
        opp_id = opponent.get("id")
        opp_key = "away" if is_home else "home"

        own_lineup = cls._lineup_for_team(item, team.id)
        opp_lineup = cls._lineup_for_team(item, opp_id) if opp_id else {}
        events = DataExtractor.extract_events(item)
        score = DataExtractor.extract_score(item, team.id)
        raw_score = item.get("score", {}) or {}

        def opp_part(name: str) -> Any:
            return (raw_score.get(name, {}) or {}).get(opp_key)

        return {
            "history_index": index,
            "fixture_id": fixture.get("id"),
            "date": fixture.get("date"),
            "competition": item.get("league", {}),
            "round": (item.get("league", {}) or {}).get("round"),
            "team": {"id": team.id, "name": team.name},
            "opponent": {"id": opp_id, "name": opponent.get("name")},
            "venue": "home" if is_home else "away",
            "result": {
                "goals_for": score.get("goals"),
                "goals_against": (item.get("goals", {}) or {}).get(opp_key),
                "halftime_for": score.get("halftime"),
                "halftime_against": opp_part("halftime"),
                "fulltime_for": score.get("fulltime"),
                "fulltime_against": opp_part("fulltime"),
            },
            "status": (fixture.get("status", {}) or {}).get("short"),
            "referee": fixture.get("referee"),
            "venue_info": fixture.get("venue", {}),
            "weather": fixture.get("weather"),
            "team_statistics": cls._stats_for_team(item, team.id),
            "opponent_statistics": cls._stats_for_team(item, opp_id) if opp_id else {},
            "lineup": {
                "formation": own_lineup.get("formation"),
                "coach": own_lineup.get("coach", {}),
                "starting_xi": cls._starting(own_lineup),
                "substitutes": cls._bench(own_lineup),
            },
            "opponent_lineup": {
                "formation": opp_lineup.get("formation"),
                "coach": opp_lineup.get("coach", {}),
                "starting_xi": cls._starting(opp_lineup),
                "substitutes": cls._bench(opp_lineup),
            },
            "events": events,
            "event_periods": cls._event_periods(events, team.id),
            "players": {
                "team": DataExtractor.extract_player_rows_for_team(item, team.id),
                "opponent": DataExtractor.extract_player_rows_for_team(item, opp_id) if opp_id else [],
            },
            "data_quality": {
                # Un contenedor no vacío no acredita estadísticas reales de ambos equipos.
                # El proveedor puede devolver bloques de equipo con lista vacía.
                "statistics": (any(to_number(v) is not None for v in cls._stats_for_team(item, team.id).values())
                               and bool(opp_id)
                               and any(to_number(v) is not None for v in cls._stats_for_team(item, opp_id).values())),
                "lineups": len(cls._starting(own_lineup)) == 11 and len(cls._starting(opp_lineup)) == 11,
                "players": (bool(DataExtractor.extract_player_rows_for_team(item, team.id))
                            and bool(opp_id)
                            and bool(DataExtractor.extract_player_rows_for_team(item, opp_id))),
                "events": bool(events),
                "referee": bool(fixture.get("referee")),
                "weather": bool(fixture.get("weather")),
            },
        }

    @staticmethod
    def _kv(lines: list[str], data: dict[str, Any], indent: str = "") -> None:
        if data:
            lines.extend(f"{indent}{k}: {fmt(v)}" for k, v in data.items())
        else:
            lines.append(f"{indent}N/D")

    @staticmethod
    def _slots(lines: list[str], slots: list[dict[str, Any]], with_grid: bool) -> None:
        for slot in slots:
            p = slot.get("player", {}) or {}
            grid = f" | grid={p.get('grid')}" if with_grid else ""
            lines.append(f"    {p.get('name')} | pos={p.get('pos')}{grid} | id={p.get('id')}")
        if not slots:
            lines.append("    N/D")

    @classmethod
    def format_team_history(
            cls,
            lines: list[str],
            history: dict[str, Any],
            team: APITeam,
    ) -> None:
        fixtures = [
            item for item in history.get("detailed_fixtures", []) or []
            if team.id in {
                                 (item.get("teams", {}).get("home", {}) or {}).get("id"),
                                 (item.get("teams", {}).get("away", {}) or {}).get("id"),
                             }
        ]
        fixtures.sort(key=lambda x: x.get("fixture", {}).get("date") or "", reverse=True)
        fixtures = fixtures[:MICRO_HISTORY_LIMIT]
        records = [cls._match_record(item, team, i) for i, item in enumerate(fixtures, 1)]

        lines.extend([
            "",
            "#" * 100,
            f"MICROCONTEXTO HISTÓRICO — {team.name}",
            "#" * 100,
            f"Partidos utilizados: {len(records)}",
            "Regla: P-1 = último partido; P-2/P-3 = contexto reciente; P-4..P-6 = contexto evolutivo.",
            "Todos los jugadores disponibles en la API se conservan por fixture.",
            "",
            ])

        for window in MICRO_HISTORY_WINDOWS:
            lines.extend([SUBSECTION_SEPARATOR, f"PROMEDIOS VENTANA ÚLTIMOS {window} — {team.name}", SUBSECTION_SEPARATOR])
            cls._kv(lines, cls._averages(fixtures[:window], team.id))
            lines.append("")

        home_subset = [x for x in fixtures if (x.get("teams", {}).get("home", {}) or {}).get("id") == team.id]
        away_subset = [x for x in fixtures if (x.get("teams", {}).get("away", {}) or {}).get("id") == team.id]
        lines.extend([SUBSECTION_SEPARATOR, "PROMEDIOS POR CONDICIÓN", SUBSECTION_SEPARATOR, f"Local — muestra {len(home_subset)}"])
        cls._kv(lines, cls._averages(home_subset, team.id), "  ")
        lines.append(f"Visitante — muestra {len(away_subset)}")
        cls._kv(lines, cls._averages(away_subset, team.id), "  ")
        lines.append("")

        formations = cls._formation_distribution(fixtures, team.id)
        lines.extend([SUBSECTION_SEPARATOR, "FORMACIONES OBSERVADAS", SUBSECTION_SEPARATOR])
        for formation, count in sorted(formations.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"{formation}: {count}/{len(fixtures)}")
        if not formations:
            lines.append("N/D")

        # Reconstrucción explícita de mercados de marcador sobre la ventana mostrada.
        completed_scores = []
        for item in fixtures:
            own = DataExtractor.extract_score(item, team.id).get("goals")
            teams = item.get("teams", {}) or {}
            other_id = (teams.get("away", {}) or {}).get("id") if (teams.get("home", {}) or {}).get("id") == team.id else (teams.get("home", {}) or {}).get("id")
            against = DataExtractor.extract_score(item, other_id).get("goals") if other_id else None
            if isinstance(own, int) and isinstance(against, int):
                completed_scores.append((own, against))
        lines.extend(["", "MERCADOS DERIVADOS DE MARCADOR — VENTANA MICRO", SUBSECTION_SEPARATOR])
        n = len(completed_scores)
        lines.append(f"Partidos con marcador válido: {n}/{len(fixtures)}")
        if n:
            counters = {
                "BTTS_SI": sum(a > 0 and b > 0 for a, b in completed_scores),
                "BTTS_NO": sum(a == 0 or b == 0 for a, b in completed_scores),
                "OVER_1_5": sum(a + b >= 2 for a, b in completed_scores),
                "OVER_2_5": sum(a + b >= 3 for a, b in completed_scores),
                "OVER_3_5": sum(a + b >= 4 for a, b in completed_scores),
                "CLEAN_SHEETS": sum(b == 0 for a, b in completed_scores),
                "MARCO_AL_MENOS_UNO": sum(a > 0 for a, b in completed_scores),
            }
            for key, value in counters.items():
                lines.append(f"{key}: {value}/{n} (frecuencia descriptiva, NO p_model)")
        else:
            lines.append("N/D")

        lines.extend(["", "ESTABILIDAD DEL XI", SUBSECTION_SEPARATOR])
        stability = cls._xi_stability(fixtures, team.id)
        if stability:
            for player in stability:
                lines.append(
                    f"{player['name']} | id={player['id']} | titularidades={player['starts']}/{len(fixtures)} | "
                    f"presencias_lineup={player['matches_present']}/{len(fixtures)}"
                )
        else:
            lines.append("N/D")
        lines.append("")

        lines.extend(["AGREGADO DE JUGADORES — VENTANA DE 6", SUBSECTION_SEPARATOR])
        players = cls._player_aggregate(fixtures, team.id)
        if players:
            for p in players:
                lines.append(
                    f"{p['name']} | id={p['id']} | pos={p['position']} | apps={p['appearances']} | starts={p['starts']} | "
                    f"min={fmt(p['minutes'])} | shots={fmt(p['shots_total'])} | SOT={fmt(p['shots_on_target'])} | "
                    f"G={fmt(p['goals'])} | A={fmt(p['assists'])} | key_passes={fmt(p['key_passes'])} | "
                    f"tackles={fmt(p['tackles'])} | interceptions={fmt(p['interceptions'])} | "
                    f"duels={fmt(p['duels_total'])}/{fmt(p['duels_won'])} | "
                    f"fouls={fmt(p['fouls_committed'])}/{fmt(p['fouls_drawn'])} | "
                    f"YC={fmt(p['yellow_cards'])} | RC={fmt(p['red_cards'])} | rating={fmt(p['average_rating'])}"
                )
        else:
            lines.append("N/D")
        lines.append("")

        for record in records:
            cls._format_record(lines, record, team)

        lines.extend([
            "",
            SUBSECTION_SEPARATOR,
            "LECTURA PARA LA IA — SOLO DATOS DERIVADOS, NO PICK",
            SUBSECTION_SEPARATOR,
            "P-1 tiene prioridad temporal porque es el partido inmediatamente anterior.",
            "P-2/P-3 sirven para detectar continuidad reciente.",
            "P-4..P-6 sirven para distinguir tendencia de ruido reciente.",
            "Los números de micro-mercados deben ignorar partidos donde la estadística correspondiente no exista.",
            "Los eventos por minuto son evidencia temporal; no equivalen a estadísticas de posesión/remates por minuto.",
            "La táctica funcional no se afirma si la API solo proporciona formación/XI: "
            "cualquier interpretación debe ser hecha por la IA como INFERIDA.",
            "",
        ])

    @classmethod
    def _format_record(cls, lines: list[str], record: dict[str, Any], team: APITeam) -> None:
        comp = record.get("competition", {}) or {}
        result = record["result"]
        venue_info = record.get("venue_info")
        venue_dict = venue_info if isinstance(venue_info, dict) else {}

        lines.extend([
            "",
            SECTION_SEPARATOR,
            f"PARTIDO HISTÓRICO P-{record['history_index']} — {team.name}",
            SECTION_SEPARATOR,
            f"Fixture ID: {record['fixture_id']}",
            f"Fecha: {record['date']}",
            f"Competición: {comp.get('name')}",
            f"País competición: {comp.get('country')}",
            f"Temporada: {comp.get('season')}",
            f"Ronda: {record.get('round')}",
            f"Rival: {record['opponent'].get('name')} ({record['opponent'].get('id')})",
            f"Condición: {record['venue']}",
            f"Resultado: {fmt(result.get('goals_for'))}-{fmt(result.get('goals_against'))}",
            f"Descanso: {fmt(result.get('halftime_for'))}-{fmt(result.get('halftime_against'))}",
            f"Árbitro: {record.get('referee')}",
            f"Estadio: {venue_dict.get('name') if venue_dict else venue_info}",
            f"Ciudad: {venue_dict.get('city') if venue_dict else None}",
            f"Clima disponible en API: {'SÍ' if record.get('weather') else 'NO'}",
            "",
            "CALIDAD DE DATOS",
        ])
        for key, value in record["data_quality"].items():
            lines.append(f"  {key}: {'OK' if value else 'N/D'}")

        lines.extend(["", "ESTADÍSTICAS COLECTIVAS — EQUIPO"])
        cls._kv(lines, record["team_statistics"], "  ")
        lines.extend(["", "ESTADÍSTICAS COLECTIVAS — RIVAL"])
        cls._kv(lines, record["opponent_statistics"], "  ")

        lineup, opp_lineup = record["lineup"], record["opponent_lineup"]
        lines.extend(["", "ALINEACIÓN Y ESTRATEGIA OBSERVABLE"])
        lines.append(f"  Formación propia: {lineup.get('formation')}")
        lines.append(f"  Entrenador propio registrado en lineup: {(lineup.get('coach') or {}).get('name')}")
        lines.append(f"  Formación rival: {opp_lineup.get('formation')}")
        lines.append(f"  Entrenador rival registrado en lineup: {(opp_lineup.get('coach') or {}).get('name')}")
        lines.append("  XI titular propio:")
        cls._slots(lines, lineup.get("starting_xi", []), True)
        lines.append("  Banquillo propio:")
        cls._slots(lines, lineup.get("substitutes", []), False)
        lines.append("  XI titular rival:")
        cls._slots(lines, opp_lineup.get("starting_xi", []), True)

        lines.extend(["", "EVENTOS Y CRONOLOGÍA"])
        if record["events"]:
            for event in record["events"]:
                lines.append("  " + json.dumps(event, ensure_ascii=False, separators=(", ", ": ")))
        else:
            lines.append("  N/D")
        lines.append("EVENTOS POR PERÍODO")
        if record["event_periods"]:
            for period, data in record["event_periods"].items():
                lines.append(f"  {period}: {json.dumps(data, ensure_ascii=False, sort_keys=True)}")
        else:
            lines.append("  N/D")

        lines.extend(["", "ESTADÍSTICAS DE TODOS LOS JUGADORES DEL PARTIDO"])
        for side_label, key in (("EQUIPO", "team"), ("RIVAL", "opponent")):
            rows = record["players"].get(key, [])
            lines.append(f"  --- {side_label} ({len(rows)}) ---")
            if not rows:
                lines.append("    N/D")
            for row in rows:
                lines.append("    " + json.dumps(
                    {"player": row.get("player", {}), "statistics": row.get("statistics", {})},
                    ensure_ascii=False, separators=(", ", ": "),
                ))

        if record.get("weather"):
            lines.extend(["", "CLIMA RAW DISPONIBLE", json.dumps(record["weather"], ensure_ascii=False, indent=2)])


# =============================================================================
# FORMATEADOR TXT
# =============================================================================


class ContextFormatter:
    @classmethod
    def _section(cls, lines: list[str], title: str) -> None:
        lines.extend(["", SECTION_SEPARATOR, title, SECTION_SEPARATOR, ""])

    @staticmethod
    def _team_block(lines: list[str], label: str, team: APITeam) -> None:
        lines.extend([
            f"{label}: {team.name}",
            f"  ID: {team.id}",
            f"  Country: {team.country}",
            f"  Code: {team.code}",
            f"  Founded: {team.founded}",
            f"  National: {team.national}",
            f"  Gender: {team.gender}",
            f"  Venue: {team.venue_name}",
            "",
        ])

    @staticmethod
    def _json_block(lines: list[str], data: Any) -> None:
        if data is None:
            lines.append("N/D")
        elif isinstance(data, dict) and data.get("error"):
            lines.append(f"ERROR: {data.get('error')}")
        else:
            lines.append(json.dumps(data, ensure_ascii=False, indent=2))

    @classmethod
    def generate(
            cls,
            match: BwinMatch,
            home_team: APITeam,
            away_team: APITeam,
            fixture: ResolvedFixture,
            context: dict[str, Any],
            api: APIFootballClient,
    ) -> str:
        lines: list[str] = []

        cls._section(lines, f"API-FOOTBALL PRO - GENERADOR DE CONTEXTO {VERSION}")
        lines.extend([
            f"Generado: {datetime.now(timezone.utc).isoformat()}",
            "",
            "IMPORTANTE:",
            "Este contexto prioriza microdetalle por fixture: último partido (P-1), contexto reciente "
            "(P-2/P-3) y contexto evolutivo (P-4..P-6). Se conservan estadísticas colectivas, rival, XI, "
            "formación, eventos, jugadores y datos temporales cuando API-Football los proporciona.",
            "La ausencia de lineups/statistics en el fixture objetivo antes del partido NO implica "
            "ausencia de esos datos en partidos históricos.",
            "Los datos faltantes por fixture se etiquetan como N/D y no se usan para fabricar promedios.",
            "",
        ])

        cls._section(lines, "CONFIGURACIÓN")
        config_rows = [
            ("API Base", FOOTBALL_BASE_URL),
            ("Bwin URL", match.url),
            ("Cache enabled", api.cache_enabled),
            ("Cache dir", CACHE_DIR),
            ("Last matches", f"{GET_LAST_MATCHES} (limit={LAST_MATCHES_LIMIT})"),
            ("Deep history", GET_DEEP_HISTORY),
            ("Deep history limit", DEEP_HISTORY_LIMIT),
            ("Micro recent window", MICRO_RECENT_WINDOW),
            ("Micro context window", MICRO_CONTEXT_WINDOW),
            ("Referee history", f"{GET_REFEREE_HISTORY} (limit={REFEREE_HISTORY_LIMIT})"),
            ("IDs batch size", FIXTURE_IDS_BATCH_SIZE),
            ("Next matches", f"{GET_NEXT_MATCHES} (limit={NEXT_MATCHES_LIMIT})"),
            ("H2H", GET_H2H),
            ("Standings", GET_STANDINGS),
            ("Predictions", GET_PREDICTIONS),
            ("Injuries", GET_INJURIES),
            ("Lineups", GET_LINEUPS),
            ("Statistics", GET_STATISTICS),
            ("Squads", GET_SQUADS),
            ("Coachs", GET_COACHS),
            ("API Odds", GET_API_ODDS),
            ("Context lookahead", CONTEXT_FIXTURE_LOOKAHEAD),
            ("Context history", CONTEXT_FIXTURE_HISTORY),
        ]
        lines.extend(f"{key}: {fmt(value)}" for key, value in config_rows)
        lines.append("")

        cls._section(lines, "IDENTIFICACIÓN BWIN")
        lines.extend([
            f"URL: {match.url}",
            f"Event ID: {match.event_id}",
            f"Home Bwin: {match.home_name_raw}",
            f"Away Bwin: {match.away_name_raw}",
            f"Gender Home: {match.home_gender}",
            f"Gender Away: {match.away_gender}",
            f"Tournament ID: {match.tournament_id}",
            f"Tournament: {match.tournament_name}",
            f"Competition display: {match.competition_display}",
            f"Stage: {match.stage}",
            f"Round: {match.round_name}",
            "",
        ])

        cls._section(lines, "EQUIPOS API-FOOTBALL")
        cls._team_block(lines, "HOME", home_team)
        cls._team_block(lines, "AWAY", away_team)

        cls._section(lines, "FIXTURE RESUELTO")
        lines.extend([
            f"Fixture ID: {fixture.fixture_id}",
            f"Date UTC: {fixture.date}",
            f"Status: {fixture.status}",
            f"Status long: {fixture.status_long}",
            f"League ID: {fixture.league_id}",
            f"League: {fixture.league_name}",
            f"Season: {fixture.season}",
            f"Round: {fixture.round_name}",
            f"Home: {fixture.home_name} ({fixture.home_id})",
            f"Away: {fixture.away_name} ({fixture.away_id})",
            f"Referee: {fixture.referee}",
            f"Venue: {fixture.venue_name}",
            f"City: {fixture.venue_city}",
            "",
        ])

        cls._section(lines, "VALIDACIÓN")
        lines.extend([
            "Bwin event -> API fixture: OK",
            "Home team resolution: OK",
            "Away team resolution: OK",
            "Competition resolution: OK",
            "Season resolution: OK",
            "Exact fixture resolution: OK",
            "",
        ])

        for side, team in (("home", home_team), ("away", away_team)):
            deep = context.get(f"{side}_history_deep")
            if isinstance(deep, dict):
                MicroHistoryFormatter.format_team_history(lines, deep, team)

        cls._format_next_matches(lines, context, home_team, away_team)

        cls._section(lines, "ÁRBITRO — HISTORIAL RECIENTE")
        referee_history = context.get("referee_history")
        if isinstance(referee_history, dict):
            if referee_history.get("referee"):
                lines.append(f"Árbitro objetivo: {referee_history.get('referee')}")
            lines.append(f"Disponible: {fmt(referee_history.get('available'))}")
            averages = referee_history.get("averages", {})
            if averages:
                lines.append("Promedios observados:")
                lines.extend(f"  {k}: {fmt(v)}" for k, v in averages.items())
            coverage = referee_history.get("coverage", {})
            if coverage:
                lines.append(f"Cobertura: {json.dumps(coverage, ensure_ascii=False)}")
                profile = referee_history.get("profile")
                if profile:
                    lines.append("Perfil de tarjetas (solo partidos con dato disponible):")
                    for key in ("sample_cards", "sample_fouls", "yellow_avg", "red_avg",
                                "cards_avg", "fouls_avg", "fouls_per_card", "penalty_avg",
                                "last_5_cards"):
                        lines.append(f"  {key}: {fmt(profile.get(key))}")
                    lines.append(f"  over_cards_pct: {json.dumps(profile.get('over_cards_pct'))}")
                    lines.append(f"  over_fouls_pct: {json.dumps(profile.get('over_fouls_pct'))}")
            for item in referee_history.get("matches", []):
                lines.append(
                    f"  {item.get('fixture_id')} | {item.get('date')} | "
                    f"{item.get('league', {}).get('name')} | "
                    f"YC+RC={fmt(item.get('yellow_cards_total'))} | "
                    f"RC={fmt(item.get('red_cards_total'))} | "
                    f"Faltas={fmt(item.get('fouls_total'))} | "
                    f"Penales={fmt(item.get('penalties'))}"
                )
        else:
            lines.append("N/D")
        lines.append("")

        cls._section(lines, "H2H")
        cls._json_block(lines, context.get("h2h"))

        cls._section(lines, "STANDINGS")
        cls._json_block(lines, context.get("standings"))

        cls._section(lines, "PREDICTIONS API-FOOTBALL")
        lines.extend([
            "NOTA: estas predicciones son una fuente externa/modelizada.",
            "No deben convertirse automáticamente en picks.",
            "",
        ])
        cls._json_block(lines, context.get("predictions"))

        cls._section(lines, "INJURIES / SITUACIÓN DE DISPONIBILIDAD")
        cls._json_block(lines, context.get("injuries"))

        cls._section(lines, "AUDITORÍA DEL XI DEL FIXTURE OBJETIVO")
        lineup_payload = context.get("lineups", {}) or {}
        lineups = lineup_payload.get("response", []) if isinstance(lineup_payload, dict) else []
        for side, expected_id in (("HOME", home_team.id), ("AWAY", away_team.id)):
            matches = [block for block in (lineups or []) if (block.get("team", {}) or {}).get("id") == expected_id]
            if not matches:
                lines.append(f"{side}: XI NO DISPONIBLE (no confundir con no confirmado en otras fuentes)")
                continue
            block = matches[0]
            starters = [(slot.get("player", {}) or {}).get("id") for slot in block.get("startXI", []) or []]
            bench = {(slot.get("player", {}) or {}).get("id") for slot in block.get("substitutes", []) or []}
            valid = len(starters) == 11 and None not in starters and len(set(starters)) == 11 and not (set(starters) & bench)
            lines.append(f"{side}: titulares={len(starters)}, únicos={len(set(starters))}, estado={'XI VÁLIDO' if valid else 'XI INCONSISTENTE; REVISAR'}")
        lines.append("Los XI históricos NO son el XI del fixture objetivo; XI de 11 filas con ID faltante es NOMINALMENTE disponible, no ausente.")
        lines.append("Las posiciones/dorsales/IDs son datos del proveedor, no una certificación oficial. Contrastar con SQUADS y prensa; no inventar IDs.")
        lines.append("ATENCIÓN: 11 titulares con IDs completos NO implica que el grid/pos sea veraz.")
        squad_data=context.get('squads') or {}
        for side, expected in (('home', home_team.id), ('away', away_team.id)):
            candidate_blocks=((squad_data.get(side) or {}).get('response') or [])
            listed={str(x.get('id')): x for block in candidate_blocks
                    if (block.get('team') or {}).get('id') == expected
                    for x in (block.get('players') or []) if x.get('id') is not None}
            current=next((b for b in (lineups or []) if (b.get('team') or {}).get('id') == expected), {})
            for row in current.get('startXI') or []:
                player=row.get('player') or {}
                pid=player.get('id'); number=player.get('number')
                found=listed.get(str(pid)) if pid is not None else None
                if found and number is not None and found.get('number') is not None and str(found['number'])!=str(number):
                    lines.append(f"REVIEW {side.upper()} ID/DORSAL: XI {player.get('name')} id={pid} dorsal={number}; SQUAD dorsal={found['number']} — NO CORREGIR AUTOMÁTICAMENTE")
                if pid is None and number is not None:
                    by_jersey=[{'id':r.get('id'),'name':r.get('name')} for r in listed.values() if str(r.get('number'))==str(number)]
                    if by_jersey:
                        lines.append(f"REVIEW {side.upper()} ID AUSENTE: XI {player.get('name')} dorsal={number}; posibles en SQUAD={by_jersey} — NO ASIGNAR SIN VALIDAR")
                if found:
                    lineup_pos=str(player.get('pos') or '').upper()
                    roster_position=normalize_text(str(found.get('position') or ''))
                    roster_group=({'goalkeeper':'G','defender':'D','midfielder':'M','attacker':'F','forward':'F'}).get(roster_position)
                    if roster_group and lineup_pos and (((lineup_pos=='G') != (roster_group=='G')) or
                                                        (lineup_pos,roster_group) in {('D','F'),('F','D')}):
                        lines.append(f"REVIEW {side.upper()} CONFLICTO POSICION: XI {player.get('name')} id={pid} pos={lineup_pos} grid={player.get('grid')}; SQUAD position={found.get('position')} — CONTRASTAR PRENSA; NO SOBRESCRIBIR")
        lines.append("")
        cls._section(lines, "LINEUPS DEL FIXTURE OBJETIVO")
        cls._json_block(lines, context.get("lineups"))

        cls._format_squads(lines, context.get("squads"))
        cls._format_coachs(lines, context.get("coachs"))

        cls._section(lines, "ODDS API-FOOTBALL — REFERENCIA SECUNDARIA")
        lines.extend([
            "La fuente operativa de mercado del proyecto continúa siendo Bwin.",
            "Estas odds de API-Football se incluyen solo como referencia adicional.",
            "",
        ])
        cls._json_block(lines, context.get("api_odds"))

        cls._section(lines, "STATISTICS DEL FIXTURE OBJETIVO")
        if fixture.status in FUTURE_STATUSES:
            lines.extend([
                "El partido todavía no ha comenzado o no tiene estadísticas disponibles.",
                "Esto es NORMAL. No se debe interpretar como fallo del recolector.",
                "",
            ])
        cls._json_block(lines, context.get("statistics"))

        cls._section(lines, "RAW FIXTURE COMPLETO DEL OBJETIVO")
        cls._json_block(lines, context.get("target_fixture_full"))

        cls._section(lines, "ESTADÍSTICAS DEL PROCESO")
        lines.extend([f"API calls realizadas: {api.api_calls}", f"Cache hits: {api.cache_hits}", ""])

        if INCLUDE_RAW_CONTEXT_JSON:
            cls._section(lines, "RAW CONTEXT JSON")
            raw_context = {k: v for k, v in context.items() if not k.startswith("_")}
            lines.append(json.dumps(raw_context, ensure_ascii=False, indent=2))

        lines.append(SECTION_SEPARATOR)
        return "\n".join(lines)

    @classmethod
    def _format_next_matches(
            cls,
            lines: list[str],
            context: dict[str, Any],
            home_team: APITeam,
            away_team: APITeam,
    ) -> None:
        cls._section(lines, "PRÓXIMOS PARTIDOS")

        for label, team, key in (
                ("HOME", home_team, "home_next_5"),
                ("AWAY", away_team, "away_next_5"),
        ):
            lines.append(f"{label} — {team.name}")
            data = context.get(key)

            if not data:
                lines.extend(["  N/D", ""])
                continue

            response = data.get("response", []) if isinstance(data, dict) else []
            if not response:
                lines.append("  N/D")
            for item in response:
                fx = item.get("fixture", {}) or {}
                teams = item.get("teams", {}) or {}
                lines.append(
                    f"  ID={fx.get('id')} | {fx.get('date')} | "
                    f"{(teams.get('home', {}) or {}).get('name')} vs "
                    f"{(teams.get('away', {}) or {}).get('name')} | "
                    f"status={(fx.get('status', {}) or {}).get('short')}"
                )
            lines.append("")

    @classmethod
    def _format_squads(cls, lines: list[str], squads: Any) -> None:
        cls._section(lines, "PLANTILLAS ACTUALES — SQUADS")
        if not isinstance(squads, dict):
            lines.append("N/D")
            return

        for side in ("home", "away"):
            lines.extend(["", side.upper(), SUBSECTION_SEPARATOR])
            data = squads.get(side)
            response = data.get("response", []) if data else []
            if not response:
                lines.append("N/D")
                continue

            for team_block in response:
                info = team_block.get("team", {}) or {}
                players = team_block.get("players", []) or []
                lines.append(f"Equipo: {info.get('name')} (ID {info.get('id')})")
                lines.append(f"Jugadoras/jugadores: {len(players)}")
                for p in players:
                    lines.append(
                        f"  ID={p.get('id')} | {p.get('name')} | age={p.get('age')} | "
                        f"number={p.get('number')} | position={p.get('position')}"
                    )

    @classmethod
    def _format_coachs(cls, lines: list[str], coachs: Any) -> None:
        cls._section(lines, "ENTRENADORES")
        lines.append("ALERTA: /coachs puede incluir técnicos históricos o carreras abiertas desactualizadas. Para DT del fixture actual priorizar coach en XI del objetivo y prensa fechada; NO atribuir DT por la primera fila.")
        if not isinstance(coachs, dict):
            lines.append("N/D")
            return

        for side in ("home", "away"):
            lines.extend(["", side.upper(), SUBSECTION_SEPARATOR])
            data = coachs.get(side)
            response = data.get("response", []) if data else []
            if not response:
                lines.append("N/D")
                continue
            lines.append(json.dumps(response, ensure_ascii=False, indent=2))


# =============================================================================
# GUARDADO
# =============================================================================


def context_output_dir() -> Path:
    """Destino por evento cuando se usa prematch_multi; modo individual intacto."""
    override = os.environ.get('PREMATCH_EVENT_DATA_DIR', '').strip()
    return Path(override).expanduser().resolve() if override else OUTPUT_DIR_DATA




def save_outputs(
        match: BwinMatch,
        fixture: ResolvedFixture,
        context: dict[str, Any],
) -> tuple[Path, Path]:
    # Evitar que el mismo cruce masculino/femenino comparta nombre de archivo.
    category = '_women' if match.home_gender == 'female' and match.away_gender == 'female' else ''
    base = safe_filename(f"{match.home_name}_vs_{match.away_name}{category}")
    event_id = os.environ.get('PREMATCH_EVENT_ID', '').strip()
    out_dir = context_output_dir()
    if event_id:
        if not re.fullmatch(r'\d{6,}', event_id) or str(match.event_id) != event_id:
            raise ValueError('PREMATCH_EVENT_ID no coincide con el Event ID Bwin detectado')
        if not os.environ.get('PREMATCH_EVENT_DATA_DIR'):
            raise ValueError('PREMATCH_EVENT_ID requiere PREMATCH_EVENT_DATA_DIR')
        base = event_id
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"match_engine_{base}.txt"
    json_path = out_dir / f"match_engine_{base}.json"

    with txt_path.open("w", encoding="utf-8") as file:
        file.write(context.get("_formatted_txt", ""))

    clean_context = {k: v for k, v in context.items() if not k.startswith("_")}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator_version": VERSION,
        "match": asdict(match),
        "fixture": asdict(fixture),
        "context": clean_context,
    }
    with json_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

    return txt_path, json_path

# =============================================================================
# DIAGNÓSTICO Y RESUMEN
# =============================================================================


def print_data_coverage(context: dict[str, Any]) -> None:
    print("\n" + "=" * 100 + "\nCOBERTURA DE DATOS\n" + "=" * 100)

    checks = [
        ("Fixture objetivo", "target_fixture_full"),
        ("Historial profundo HOME", "home_history_deep"),
        ("Historial profundo AWAY", "away_history_deep"),
        ("H2H", "h2h"),
        ("Standings", "standings"),
        ("Predictions", "predictions"),
        ("Injuries", "injuries"),
        ("Lineups", "lineups"),
        ("Statistics objetivo", "statistics"),
        ("Squads", "squads"),
        ("Coachs", "coachs"),
        ("Odds API", "api_odds"),
        ("Historial árbitro", "referee_history"),
    ]

    for label, key in checks:
        value = context.get(key)
        if value is None:
            status = "AUSENTE"
        elif isinstance(value, dict) and value.get("error"):
            status = f"ERROR: {value.get('error')}"
        else:
            status = "OK"
        print(f"{label:<35} : {status}")

    print("=" * 100)


def print_final_summary(
        match: BwinMatch,
        home_team: APITeam,
        away_team: APITeam,
        fixture: ResolvedFixture,
        api: APIFootballClient,
) -> None:
    print("\n" + "=" * 100 + f"\nRESOLUCIÓN FINAL {VERSION}\n" + "=" * 100)
    print(f"Bwin : {match.home_name_raw} vs {match.away_name_raw}")
    print(f"API      : {home_team.name} vs {away_team.name}")
    print(f"Teams    : {home_team.id} vs {away_team.id}")
    print(f"League   : {fixture.league_name} (ID {fixture.league_id})")
    print(f"Season   : {fixture.season}")
    print(f"Fixture  : {fixture.fixture_id}")
    print(f"Date     : {fixture.date}")
    print(f"Status   : {fixture.status}")
    print(f"API calls: {api.api_calls}")
    print(f"Cache    : {api.cache_hits} hits")
    print("=" * 100)


# =============================================================================
# SLUGS COMPUESTOS: RESOLUCIÓN POR PAREJA Y FIXTURE (SIN ALIAS MANUALES)
# =============================================================================


def _slug_title_pair(html: Optional[str], slug: str) -> Optional[tuple[str, str]]:
    """Usar título del EVENTO si delimita ambos equipos y corresponde al slug.

    No se interpreta un título genérico de Bwin como partido.
    """
    if not html:
        return None
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not title:
        return None
    name = re.sub(r"<[^>]*>", " ", unescape(title.group(1))).split("|")[0].strip()
    pairs = re.split(r"\s+-\s+|\s+vs\.?\s+|\s+contra\s+", name, maxsplit=1, flags=re.I)
    if len(pairs) != 2:
        return None
    left, right = [re.sub(r"\s+", " ", x).strip() for x in pairs]
    if not left or not right:
        return None
    slug_tokens = normalize_text(slug.replace('-', ' ')).split()
    composed = normalize_text(left + ' ' + right).split()
    # El título puede contener denominaciones extendidas; no aceptar un título ajeno.
    if len(set(slug_tokens) & set(composed)) < max(2, len(set(slug_tokens)) - 2):
        return None
    return left, right


def _slug_identity_score(query: str, candidate: APITeam) -> float:
    """Afinidad solo para GENERAR candidatos, nunca para aprobar la identidad."""
    def reduced(s: str) -> str:
        s = normalize_text(s)
        tokens = re.findall(r"[a-z0-9]+", s)
        removable = {'fc', 'cf', 'sc', 'ac', 'afc', 'club', 'de', 'del', 'the', 'football', 'club'}
        return ' '.join(t for t in tokens if t not in removable)
    names = name_variants(normalize_text(query))
    norm_team = reduced(candidate.name)
    # Núcleo institucional idéntico: identidad textual fuerte, PERO la identidad
    # definitiva continúa dependiendo de pareja, competición y fixture.
    if any(institutional_name_match(n, candidate.name, candidate.gender) for n in names):
        return 1.0
    values = [SequenceMatcher(None, reduced(n), norm_team).ratio() for n in names if reduced(n)]
    return max(values, default=0.0)


def resolve_compound_slug(
        match: BwinMatch, html: Optional[str], api: APIFootballClient,
) -> Optional[tuple[APITeam, APITeam, str, str]]:
    """Resuelve CUALQUIER partición solo si fixture verifica pareja y orden.

    Máximo 2 candidatos por lado y hasta 2 llamadas de fixtures por equipo ancla.
    No confunde un nombre parecido con una identidad confirmada.
    """
    url_match = BwinParser.EVENT_REGEX.search(unquote(urlparse(match.url).path))
    if not url_match:
        return None
    slug = url_match.group(1)
    if '-vs-' in slug or _slug_title_pair(html, slug):
        return None  # Ya existen nombres explícitamente separados.
    tokens = [v for v in slug.split('-') if v]
    if len(tokens) < 2:
        return None

    LOG.info('Resolviendo slug compuesto mediante candidatos + fixture, sin corte por número de palabras.')
    # En vez de consultar exhaustivamente cada mitad, buscar fragmentos con
    # nombres plausibles. Cachear términos compartidos entre particiones.
    term_cache: dict[str, list[APITeam]] = {}
    # Señales explícitas de categoría en el slug. Ante nombres sin señal,
    # no atribuir femenino a un equipo masculino por similitud de nombre.
    expected_gender = ('female' if re.search(r'(?:^|-)(?:women|woman|femenino|femenina|fem|w)(?:-|$)', slug, re.I)
                       else 'male')
    def find_side(text: str) -> list[APITeam]:
        normalized = normalize_text(text)
        if normalized in term_cache:
            return term_cache[normalized]
        terms = [normalized]
        variants = name_variants(normalized)
        terms.extend(variants[1:3])
        # Búsquedas API requieren mínimo 3 letras. Un prefijo institucional
        # incompleto nunca debe servir por sí solo para validar una entidad.
        search_terms = []
        for term in terms:
            clean = re.sub(r"[^a-z0-9 ]", " ", normalize_text(term)).strip()
            if len(clean) >= 3 and clean not in search_terms:
                search_terms.append(clean)
        found: dict[int, APITeam] = {}
        for term in search_terms[:3]:
            try:
                rows = api.get('teams', {'search': term}).get('response', []) or []
            except Exception as exc:
                LOG.warn(f'Búsqueda de tramo {term!r} falló: {exc}')
                continue
            for row in rows:
                team = parse_api_team(row)
                if not team or team.gender != expected_gender or team.national:
                    continue
                score = _slug_identity_score(normalized, team)
                # Un nombre corto como 'bayern' puede figurar como 'Bayern Munich'.
                # La coincidencia exacta de token genera candidato, NO lo confirma.
                target_tokens = set(re.findall(r'[a-z0-9]+', normalized))
                team_tokens = set(re.findall(r'[a-z0-9]+', normalize_text(team.name)))
                exact_token = len(target_tokens) == 1 and next(iter(target_tokens), '') in team_tokens and len(normalized) >= 5
                if score >= 0.78 or exact_token:
                    found[team.id] = team
        # Si el tramo es una selección femenina, el resolver tradicional con
        # separador explícito sigue disponible; aquí nunca se infiere género.
        ranked = sorted(found.values(), key=lambda team: _slug_identity_score(normalized, team), reverse=True)
        term_cache[normalized] = ranked[:2]
        return term_cache[normalized]

    alternatives = []
    for i in range(1, len(tokens)):
        left, right = ' '.join(tokens[:i]), ' '.join(tokens[i:])
        # Evita consultas por fragmentos evidentemente demasiado pequeños.
        if len(left) < 3 or len(right) < 3:
            continue
        hlist, alist = find_side(left), find_side(right)
        for h in hlist:
            for a in alist:
                if h.id != a.id:
                    alternatives.append((left, right, h, a))
    if not alternatives and len(tokens) == 2:
        # Bwin puede usar un exónimo: Friburgo (Bwin) / Freiburg (API).
        # Cuando SOLO hay dos tokens el límite entre clubes es inequívoco.
        # Resolver el lado reconocible y usar sus fixtures como evidencia de
        # identidad del rival, exigiendo localía, nombre similar y fecha cercana.
        left, right = tokens
        anchors = find_side(right)
        found_pairs: dict[tuple[int, int], tuple[float, APITeam, APITeam, str, str, int]] = {}
        now = datetime.now(timezone.utc)
        for anchor in anchors[:3]:
            for scope, limit in (('next', CONTEXT_FIXTURE_LOOKAHEAD), ('last', CONTEXT_FIXTURE_HISTORY)):
                try:
                    rows = api.get('fixtures', {'team': anchor.id, scope: limit}).get('response', []) or []
                except Exception as exc:
                    LOG.warn(f'Fixture de ancla {anchor.name}: {exc}')
                    continue
                for item in rows:
                    teams = item.get('teams', {}) or {}
                    home = teams.get('home', {}) or {}
                    away = teams.get('away', {}) or {}
                    fixture = item.get('fixture', {}) or {}
                    if away.get('id') != anchor.id or not home.get('id') or not fixture.get('id'):
                        continue
                    when = parse_date(fixture.get('date'))
                    if not when:
                        continue
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    reference = parse_date(match.event_date) if match.event_date else now
                    if reference.tzinfo is None:
                        reference = reference.replace(tzinfo=timezone.utc)
                    days = (when - reference).total_seconds() / 86400
                    if (abs(days) > 1.5 if match.event_date else not (-7 <= days <= 45)):
                        continue
                    status = (fixture.get('status', {}) or {}).get('short')
                    if status in LIVE_STATUSES:
                        continue
                    home_name = home.get('name') or ''
                    home_gender = detect_gender(home_name)
                    if home_gender != match.home_gender:
                        continue
                    if any(re.search(pattern, normalize_text(home_name)) for pattern in YOUTH_PATTERNS):
                        continue
                    similarity = _slug_identity_score(left, APITeam(
                        id=int(home['id']), name=home_name, code=None, country=None,
                        founded=None, national=False, gender=home_gender,
                        logo=home.get('logo'), venue_name=None))
                    if similarity < 0.80:
                        continue
                    candidate_home = APITeam(
                        id=int(home['id']), name=home_name, code=None, country=None,
                        founded=None, national=False, gender=home_gender,
                        logo=home.get('logo'), venue_name=None)
                    score = similarity * 100 - abs(days) * 0.2
                    if status in FUTURE_STATUSES:
                        score += 10
                    key = (candidate_home.id, anchor.id)
                    prev = found_pairs.get(key)
                    if prev is None or score > prev[0]:
                        found_pairs[key] = (score, candidate_home, anchor, left, right, int(fixture['id']))
        if found_pairs:
            ranked = sorted(found_pairs.values(), key=lambda pair: pair[0], reverse=True)
            if len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 15:
                _, h, a, l, r, fid = ranked[0]
                LOG.ok(f'Slug de dos clubes confirmado por fixture {fid}: {l} => {h.name} ({h.id}) / {r} => {a.name} ({a.id})')
                return h, a, l, r
            LOG.warn('Más de una pareja plausible para el slug de dos clubes; no se elige por intuición.')
    if not alternatives:
        LOG.warn('El slug no produjo dos candidatos compatibles; no se asignan IDs por intuición.')
        return None

    # Cada par se valida por fixtures del equipo local; todas las parejas comparten
    # las mismas respuestas cacheadas por ID, evitando múltiples consultas.
    anchors = {h.id: h for _, _, h, _ in alternatives}
    fixture_cache: dict[int, list[dict[str, Any]]] = {}
    for tid in anchors:
        fixtures = []
        for scope, limit in (('next', CONTEXT_FIXTURE_LOOKAHEAD), ('last', CONTEXT_FIXTURE_HISTORY)):
            try:
                fixtures += api.get('fixtures', {'team': tid, scope: limit}).get('response', []) or []
            except Exception as exc:
                LOG.warn(f'Fixture ancla {tid} ({scope}): {exc}')
        fixture_cache[tid] = fixtures

    now = datetime.now(timezone.utc)
    possible: dict[tuple[int, int], tuple[float, APITeam, APITeam, str, str, int]] = {}
    for left, right, h, a in alternatives:
        for obj in fixture_cache[h.id]:
            pair = obj.get('teams', {}) or {}
            hh = pair.get('home', {}) or {}
            aa = pair.get('away', {}) or {}
            if (hh.get('id'), aa.get('id')) != (h.id, a.id):
                continue
            f = obj.get('fixture', {}) or {}
            dt = parse_date(f.get('date'))
            if not dt or not f.get('id'):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            reference = parse_date(match.event_date) if match.event_date else now
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            days = (dt - reference).total_seconds() / 86400
            if match.event_date:
                if abs(days) > 1.5:
                    continue
            elif not (-7 <= days <= 45):
                continue
            status = (f.get('status', {}) or {}).get('short', '')
            # No elegir fixtures finalizados si ya hay otro futuro plausible.
            score = (_slug_identity_score(left, h) + _slug_identity_score(right, a)) * 100
            score -= abs(days) * 0.1
            if status in FUTURE_STATUSES:
                score += 10
            if status in LIVE_STATUSES:
                score -= 1000
            key = (h.id, a.id)
            if key not in possible or score > possible[key][0]:
                possible[key] = (score, h, a, left, right, int(f['id']))
    if not possible:
        LOG.warn('Ninguna partición corresponde a un fixture del periodo y localía esperados.')
        return None
    ranked = sorted(possible.values(), key=lambda row: row[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 15:
        LOG.error('Existen varias parejas plausibles; resolución ambigua, no se asignan IDs.')
        return None
    _, h, a, left, right, fid = ranked[0]
    LOG.ok(f'Slug confirmado por fixture {fid}: {left} => {h.name} ({h.id}) / {right} => {a.name} ({a.id})')
    return h, a, left, right


# =============================================================================
# RESOLUCIÓN DE EQUIPOS (orquestación)
# =============================================================================


def resolve_both_teams(
        match: BwinMatch,
        team_resolver: TeamResolver,
        contextual: ContextualTeamResolver,
) -> tuple[Optional[APITeam], Optional[APITeam]]:

    def attempt(name: str, gender: str, label: str) -> Optional[APITeam]:
        try:
            return team_resolver.resolve(name, gender)
        except Exception as exc:
            LOG.warn(f"Resolución inicial {label} falló: {exc}")
            return None

    home = attempt(match.home_name, match.home_gender, "HOME")
    away = attempt(match.away_name, match.away_gender, "AWAY")

    def contextual_fix(name: str, gender: str, anchor: APITeam, label: str) -> Optional[APITeam]:
        try:
            return contextual.resolve_from_opponent_fixtures(name, gender, anchor)
        except Exception as exc:
            LOG.warn(f"Resolución contextual {label} falló: {exc}")
            return None

    # Un equipo falló: el otro sirve de ancla.
    if home is None and away is not None:
        LOG.warn("HOME no pudo resolverse por nombre. Activando resolución contextual mediante AWAY.")
        home = contextual_fix(match.home_name, match.home_gender, away, "HOME")

    if away is None and home is not None:
        LOG.warn("AWAY no pudo resolverse por nombre. Activando resolución contextual mediante HOME.")
        away = contextual_fix(match.away_name, match.away_gender, home, "AWAY")

    if home is None or away is None:
        return home, away

    confident = team_resolver.confident_ids

    # Corrección por género incorrecto (caso BK Hacken masculino vs femenino).
    if home.gender != match.home_gender:
        LOG.warn(f"HOME resuelto como '{home.name}' pero el género esperado es '{match.home_gender}'.")
        fixed = contextual_fix(match.home_name, match.home_gender, away, "HOME")
        if fixed:
            LOG.ok(f"HOME corregido contextualmente: {home.name} ({home.id}) -> {fixed.name} ({fixed.id})")
            home = fixed

    if away.gender != match.away_gender:
        LOG.warn(f"AWAY resuelto como '{away.name}' pero el género esperado es '{match.away_gender}'.")
        fixed = contextual_fix(match.away_name, match.away_gender, home, "AWAY")
        if fixed:
            LOG.ok(f"AWAY corregido contextualmente: {away.name} ({away.id}) -> {fixed.name} ({fixed.id})")
            away = fixed

    # Verificación cruzada solo cuando la resolución por nombre NO es de alta confianza
    # (ahorra llamadas y evita sobrescribir selecciones/IDs ya verificados).
    if (home.gender == match.home_gender and away.gender == match.away_gender
            and home.id not in confident):
        fixed = contextual_fix(match.home_name, match.home_gender, away, "HOME")
        if fixed and fixed.id != home.id:
            LOG.warn(
                f"El contexto del AWAY revela una entidad HOME distinta: {fixed.name} ({fixed.id}) "
                f"vs candidato actual {home.name} ({home.id})."
            )
            home = fixed

    return home, away


def resolve_pair_by_fixture(
        match: BwinMatch, team_resolver: TeamResolver,
        api: APIFootballClient, home: Optional[APITeam], away: Optional[APITeam],
) -> tuple[Optional[APITeam], Optional[APITeam]]:
    """Fallback cuando uno o ambos equipos fallan la resolución normal.

    No acepta equipos sueltos: debe existir un fixture con la pareja exacta.
    Acota candidatos/consultas y evita elegir enfrentamientos antiguos como actuales.
    """
    LOG.warn("Activando rescate por pareja de candidatos + fixture (máx. 5 por equipo).")
    home_list = [home] if home else team_resolver.candidates_for_pair(match.home_name, match.home_gender)
    away_list = [away] if away else team_resolver.candidates_for_pair(match.away_name, match.away_gender)
    if not home_list or not away_list:
        LOG.error(f"Rescate sin candidatos: HOME={len(home_list)}, AWAY={len(away_list)}")
        return home, away

    home_ids = {t.id for t in home_list}
    away_ids = {t.id for t in away_list}
    # Buscar en un solo lado primero para reducir consumo de API.
    anchor_candidates = home_list if len(home_list) <= len(away_list) else away_list
    candidate_pairs: dict[tuple[int, int], tuple[float, APITeam, APITeam, int]] = {}
    now = datetime.now(timezone.utc)
    for anchor in anchor_candidates[:5]:
        for scope, limit in (("next", CONTEXT_FIXTURE_LOOKAHEAD), ("last", CONTEXT_FIXTURE_HISTORY)):
            try:
                fixtures = api.get("fixtures", {"team": anchor.id, scope: limit}).get("response", []) or []
            except Exception as exc:
                LOG.warn(f"Rescate fixture {anchor.name} / {scope}: {exc}")
                continue
            for item in fixtures:
                f = item.get("fixture", {}) or {}
                teams = item.get("teams", {}) or {}
                h_id = (teams.get("home", {}) or {}).get("id")
                a_id = (teams.get("away", {}) or {}).get("id")
                if h_id not in home_ids or a_id not in away_ids or h_id == a_id:
                    continue
                dt = parse_date(f.get("date"))
                if not dt:
                    continue
                # Sin fecha publicada por Bwin, no adjudicar un fixture lejano.
                if match.event_date:
                    expected = parse_date(match.event_date)
                    if not expected or abs((dt - expected).total_seconds()) > 36 * 3600:
                        continue
                elif not (-7 <= (dt - now).total_seconds() / 86400 <= 45):
                    continue
                ht = next(t for t in home_list if t.id == h_id)
                at = next(t for t in away_list if t.id == a_id)
                # Cercanía temporal puntúa; coincidencia por fecha prioritaria.
                distance_days = abs((dt - (parse_date(match.event_date) or now)).total_seconds()) / 86400
                quality = -distance_days
                key = (h_id, a_id)
                current = candidate_pairs.get(key)
                if current is None or quality > current[0]:
                    candidate_pairs[key] = (quality, ht, at, int(f.get("id")))
    if not candidate_pairs:
        LOG.error("No existe fixture próximo/reciente verificable que una a los candidatos en localía correcta.")
        return home, away
    ranked = sorted(candidate_pairs.values(), key=lambda x: x[0], reverse=True)
    if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 0.5:
        LOG.error("Más de una pareja plausible; no se asignan IDs automáticamente.")
        return home, away
    _, resolved_home, resolved_away, fixture_id = ranked[0]
    LOG.ok(f"Rescate verificado por fixture {fixture_id}: {resolved_home.name} ({resolved_home.id}) vs {resolved_away.name} ({resolved_away.id})")
    return resolved_home, resolved_away


def resolve_missing_from_fixture_calendar(
        match: BwinMatch, api: APIFootballClient,
        home: Optional[APITeam], away: Optional[APITeam],
) -> tuple[Optional[APITeam], Optional[APITeam]]:
    """Rescate PREMATCH por fixture verificado en ventana corta y localía exacta.

    Primero consulta fixtures DEL EQUIPO ancla + from/to (la consulta por next/last
    puede omitir encuentros próximos al pasar de NS a LIVE o por límites). Luego
    utiliza calendario por día. No selecciona un ID por coincidencia de nombre
    solamente; exige un fixture único, orden correcto, fecha y categoría.
    """
    if (home is None) == (away is None):
        return home, away
    anchor = home if home is not None else away
    missing_side = "away" if home is not None else "home"
    target_name = match.away_name if missing_side == "away" else match.home_name
    target_gender = match.away_gender if missing_side == "away" else match.home_gender
    if not anchor or not anchor.id:
        return home, away

    reference = parse_date(match.event_date) if match.event_date else datetime.now(timezone.utc)
    if reference is None:
        return home, away
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    day0 = (reference - timedelta(days=1)).date()
    days = [(day0 + timedelta(days=i)).isoformat() for i in range(3)]
    expected_variants = name_variants(normalize_text(target_name))
    found: dict[int, tuple[APITeam, APITeam, int, datetime]] = {}
    rejection_counts = {'anchor_or_order':0, 'name_or_category':0, 'date':0, 'invalid':0}
    source_counts: list[str] = []

    def check_rows(rows: list[dict[str, Any]], label: str) -> None:
        source_counts.append(f'{label}: {len(rows)} fixtures')
        for row in rows:
            teams = row.get('teams') or {}
            h, a = (teams.get('home') or {}), (teams.get('away') or {})
            if not h.get('id') or not a.get('id') or h['id'] == a['id']:
                rejection_counts['invalid'] += 1
                continue
            if missing_side == 'away':
                if h['id'] != anchor.id:
                    rejection_counts['anchor_or_order'] += 1
                    continue
                candidate = a
            else:
                if a['id'] != anchor.id:
                    rejection_counts['anchor_or_order'] += 1
                    continue
                candidate = h
            cand_name = candidate.get('name') or ''
            # Verificar el género/categoría y un nombre compatible; never inferir
            # que dos entidades llamadas Cruzeiro tienen el mismo team ID.
            similarity = max((_slug_identity_score(v, APITeam(
                id=int(candidate['id']), name=cand_name, code=None, country=None,
                founded=None, national=False, gender=detect_gender(cand_name),
                logo=None, venue_name=None)) for v in expected_variants), default=0.0)
            if (detect_gender(cand_name) != target_gender or
                    not club_category_compatible(target_name, cand_name, target_gender) or
                    similarity < 0.88):
                rejection_counts['name_or_category'] += 1
                continue
            fx = row.get('fixture') or {}
            dt = parse_date(fx.get('date'))
            if dt is None or not fx.get('id'):
                rejection_counts['invalid'] += 1
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            distance = (dt - reference).total_seconds()
            # Sin timestamp Bwin se exige proximidad al momento de ejecución.
            if abs(distance) > (36 * 3600 if match.event_date else 36 * 3600):
                rejection_counts['date'] += 1
                continue
            # Evitar confirmar un fixture pasado a partir de una referencia ambigua.
            if not match.event_date and (dt - now).total_seconds() < -36 * 3600:
                rejection_counts['date'] += 1
                continue
            new_team = APITeam(
                id=int(candidate['id']), name=cand_name, code=None,
                country=candidate.get('country'), founded=None, national=False,
                gender=target_gender, logo=candidate.get('logo'), venue_name=None)
            h_team, a_team = ((anchor, new_team) if missing_side == 'away'
                              else (new_team, anchor))
            found[int(fx['id'])] = (h_team, a_team, int(fx['id']), dt)

    LOG.warn(f"Rescate por fecha de '{target_name}' con ancla "
             f"{anchor.name} ({anchor.id}), UTC {days[0]} a {days[-1]}.")
    # API-Football puede exigir `season` cuando se combina team con from/to.
    # No fijar una temporada global (en enero la liga podría ser 2025/26).
    # Primero el año de la fecha objetivo; si no existe una pareja verificable,
    # probar el anterior. La ventana UTC y el fixture único siguen siendo filtros.
    seasons = [reference.year]
    # Solo enero-julio puede corresponder aún a una liga europea iniciada
    # el año anterior; en octubre no consultar 2025 para un partido de 2026.
    if reference.month <= 7:
        seasons.append(reference.year - 1)
    range_errors = []
    for season in seasons:
        if found:
            break
        query = {'team': anchor.id, 'season': season,
                 'from': days[0], 'to': days[-1]}
        LOG.info(f"Rescate focalizado: /fixtures team={anchor.id}, season={season}, "
                 f"from={days[0]}, to={days[-1]}")
        try:
            result = api.get('fixtures', query)
            records = result.get('response', []) or []
            check_rows(records, f"team={anchor.id},season={season},from/to")
        except Exception as exc:
            range_errors.append(f"season={season}: {exc}")
            LOG.warn(f"Rescate de fixtures por equipo/rango (season={season}): {exc}")
    if not found:
        # Última alternativa: calendario diario, nunca asumir que se halló un
        # fixture solo por coincidir el nombre en /teams?search.
        for day in days:
            try:
                records = api.get('fixtures', {'date':day}).get('response', []) or []
                check_rows(records, f'date={day}')
            except Exception as exc:
                LOG.warn(f'No se pudo consultar calendario {day}: {exc}')

    if len(found) != 1:
        if len(found)>1:
            LOG.error(f'FIXTURE_AMBIGUO: {len(found)} fixtures con ancla, nombres y fechas compatibles; no se asigna ID.')
        else:
            LOG.error('FIXTURE_NO_VERIFICABLE_EN_API: ninguno cumple ancla, orden, nombre y fecha. '
                      'Puede ser falta de cobertura, una fecha incorrecta o un team ID distinto; no se supone cuál.')
        LOG.warn('Diagnóstico fuentes: ' + '; '.join(source_counts))
        LOG.warn(f'Diagnóstico descartes: {rejection_counts}')
        if range_errors:
            LOG.warn('CONSULTAS_POR_EQUIPO_FALLIDAS: ' + '; '.join(range_errors))
            LOG.warn('La ausencia de fixture NO acredita falta de cobertura si '
                     'alguna consulta falló: revisar errores del proveedor.')
        return home, away
    h_team, a_team, fixture_id, dt = next(iter(found.values()))
    # Verificación final por fixture.id; si el proveedor no devuelve el detalle,
    # no elevamos la coincidencia a ID certificado.
    try:
        detail = api.get('fixtures', {'id':fixture_id}).get('response', []) or []
        exact = [x for x in detail if
                 (x.get('fixture') or {}).get('id') == fixture_id and
                 ((x.get('teams') or {}).get('home') or {}).get('id') == h_team.id and
                 ((x.get('teams') or {}).get('away') or {}).get('id') == a_team.id]
        if len(exact) != 1:
            LOG.error(f'FIXTURE_INCONSISTENTE: id={fixture_id} no reproduce los dos IDs/localía.')
            return home, away
    except Exception as exc:
        LOG.error(f'No fue posible validar fixture.id={fixture_id}: {exc}')
        return home, away
    match.verified_fixture_id = fixture_id
    LOG.ok(f'Identidad confirmada API fixture={fixture_id}, fecha={dt.isoformat()}: '
           f'{h_team.name} ({h_team.id}) vs {a_team.name} ({a_team.id}).')
    return h_team, a_team



def resolve_full_pair_from_competition_fixture(
        match: BwinMatch,
        api: APIFootballClient,
        resolver: TeamResolver,
        home: Optional[APITeam],
        away: Optional[APITeam],
        *,
        reference_now: Optional[datetime] = None,
) -> tuple[Optional[APITeam], Optional[APITeam]]:
    """Rescate de PAREJA cuando una identidad aparente puede pertenecer a otro ID.

    El nombre aislado NO autoriza un ID, ni siquiera cuando coincide exactamente.
    Exige: fixture único, orden local/visitante, nombres/género compatibles,
    competición si Bwin la identifica, fecha y respuesta por fixture.id.

    Los aliases son textuales; NUNCA autorizan por sí mismos reemplazos de IDs.
    """
    now = reference_now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ref = parse_date(match.event_date) if match.event_date else now
    if ref is None:
        ref = now
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    first = (ref - timedelta(days=1)).date()
    days = [(first + timedelta(days=n)).isoformat() for n in range(3)]
    year = ref.year
    seasons = [year] + ([year-1] if ref.month <= 7 else [])

    # Estas equivalencias institucionales se usan ÚNICAMENTE para contrastar
    # un fixture. No significan que dos IDs de /teams sean intercambiables.
    verified_name_aliases = {
        'atletico mineiro': {'atletico mg', 'clube atletico mineiro'},
        'atletico mg': {'atletico mineiro', 'clube atletico mineiro'},
    }

    def name_compatible(bwin_name: str, api_name: str) -> bool:
        # En esta fase los alias textuales son pistas: las identidades requieren
        # un fixture único con fecha, categoría, orden y liga verificados.
        variants = name_variants(normalize_text(bwin_name))
        norm = normalize_text(api_name)
        aliases = {
            'atletico mineiro': {'atletico mg', 'clube atletico mineiro'},
            'atletico mg': {'atletico mineiro', 'clube atletico mineiro'},
        }
        return any(norm == v or institutional_name_match(v,api_name)
                   or norm in aliases.get(v,set()) for v in variants)

    def competition_compatible(league: dict) -> bool:
        return bwin_league_compatible(match.competition_display, league)

    accepted: dict[int, tuple[dict, datetime, str]] = {}
    sources: list[str] = []
    rejected = {'name':0, 'gender':0, 'competition':0, 'time':0, 'malformed':0}

    def assess(items: list[dict], origin: str) -> None:
        sources.append(f'{origin}={len(items)}')
        for item in items:
            f = item.get('fixture') or {}
            teams = item.get('teams') or {}
            ht = teams.get('home') or {}
            at = teams.get('away') or {}
            league = item.get('league') or {}
            fid = f.get('id')
            dt = parse_date(f.get('date'))
            if not fid or not dt or not ht.get('id') or not at.get('id'):
                rejected['malformed'] += 1
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # El mismo encuentro deberá pertenecer a la ventana del evento.
            if abs((dt-ref).total_seconds()) > 36 * 3600:
                rejected['time'] += 1
                continue
            if not (name_compatible(match.home_name, ht.get('name','')) and
                    name_compatible(match.away_name, at.get('name',''))):
                rejected['name'] += 1
                continue
            if (detect_gender(ht.get('name','')) != match.home_gender or
                    detect_gender(at.get('name','')) != match.away_gender or
                    not club_category_compatible(match.home_name, ht.get('name',''), match.home_gender) or
                    not club_category_compatible(match.away_name, at.get('name',''), match.away_gender)):
                rejected['gender'] += 1
                continue
            if not competition_compatible(league):
                rejected['competition'] += 1
                continue
            accepted[int(fid)] = (item, dt, origin)

    # Si la resolución inicial de un lado fue errónea, el otro (aunque sea
    # ambiguo) permite consultar fixtures sin usar aquel ID incorrecto.
    # Si ambos IDs nominales resultaron erróneos, no anclarse en ellos:
    # reunir alternativas del local y visitante. Si ambos faltan, empezar
    # por el visitante (normalmente menos ambiguo en Brasil) y luego local.
    target_sides = (
        [(match.away_name, match.away_gender, away),
         (match.home_name, match.home_gender, home)]
        if home is None or away is None else
        [(match.home_name, match.home_gender, home),
         (match.away_name, match.away_gender, away)]
    )
    anchors_by_id: dict[int, APITeam] = {}
    for team_name, gender, previous in target_sides:
        if previous is not None:
            anchors_by_id[previous.id] = previous
        for candidate in resolver.candidates_for_pair(team_name, gender, max_candidates=4):
            anchors_by_id[candidate.id] = candidate
    anchors = list(anchors_by_id.values())[:8]
    if not anchors:
        LOG.warn('PAREJA_FIXTURE: sin candidatos de búsqueda para ambos equipos; calendario por fecha será la última prueba')
    for candidate in anchors:
        for season in seasons:
            # Verificar TODOS los candidatos homónimos del año objetivo.
            # No detenerse al encontrar el primer resultado: puede haber dos
            # fixtures distintos y sería un falso "fixture único".
            if accepted and season != year:
                continue
            query = {'team':candidate.id, 'season':season,
                     'from':days[0], 'to':days[-1]}
            try:
                rows = api.get('fixtures', query).get('response',[]) or []
                assess(rows, f'team={candidate.id},season={season}')
            except Exception as exc:
                LOG.warn(f'PAREJA_FIXTURE consulta {query} falló: {exc}')

    # Si el proveedor no devuelve la pareja al filtrar por team, calendario
    # por fecha del fixture (sin anclarlo a un ID posiblemente falso).
    if not accepted:
        for day in days:
            try:
                rows = api.get('fixtures', {'date':day}).get('response',[]) or []
                assess(rows, f'date={day}')
            except Exception as exc:
                LOG.warn(f'PAREJA_FIXTURE calendario {day} falló: {exc}')

    if len(accepted) != 1:
        LOG.warn(f'PAREJA_FIXTURE sin resolución única: {len(accepted)} fixture(s); '
                 f'fuentes={sources}; descartes={rejected}. No se asigna ID.')
        return home, away

    fid, (row, kickoff, origin) = next(iter(accepted.items()))
    try:
        details = api.get('fixtures', {'id':fid}).get('response',[]) or []
        exact = [it for it in details if
                 (it.get('fixture') or {}).get('id') == fid and
                 (it.get('teams') or {}).get('home',{}).get('id') == row['teams']['home']['id'] and
                 (it.get('teams') or {}).get('away',{}).get('id') == row['teams']['away']['id']]
    except Exception as exc:
        LOG.warn(f'PAREJA_FIXTURE no se pudo verificar fixture.id={fid}: {exc}')
        return home, away
    if len(exact) != 1:
        LOG.warn(f'PAREJA_FIXTURE fixture.id={fid} sin respuesta inequívoca')
        return home, away
    # Repetir controles ante cambios del detalle respecto al listado.
    check = exact[0]
    detail_dt = parse_date((check.get('fixture') or {}).get('date'))
    if detail_dt is not None and detail_dt.tzinfo is None:
        detail_dt = detail_dt.replace(tzinfo=timezone.utc)
    if not (name_compatible(match.home_name, check['teams']['home']['name']) and
            name_compatible(match.away_name, check['teams']['away']['name']) and
            detect_gender(check['teams']['home']['name']) == match.home_gender and
            detect_gender(check['teams']['away']['name']) == match.away_gender and
            competition_compatible(check.get('league') or {}) and
            detail_dt is not None and abs((detail_dt-ref).total_seconds()) <= 36 * 3600):
        LOG.warn('PAREJA_FIXTURE detalle contradice identidades, fecha o competición')
        return home, away

    def team(side: str, gender: str) -> APITeam:
        raw = check['teams'][side]
        return APITeam(id=int(raw['id']), name=raw['name'], code=raw.get('code'),
                       country=raw.get('country'), founded=None, national=False,
                       gender=gender, logo=raw.get('logo'), venue_name=None)

    validated_home, validated_away = team('home', match.home_gender), team('away', match.away_gender)
    if home is not None and home.id != validated_home.id:
        LOG.warn(f'HOME REASIGNADO POR FIXTURE, NO POR ALIAS: '
                 f'{home.name} ({home.id}) -> {validated_home.name} ({validated_home.id})')
    if away is not None and away.id != validated_away.id:
        LOG.warn(f'AWAY REASIGNADO POR FIXTURE, NO POR ALIAS: '
                 f'{away.name} ({away.id}) -> {validated_away.name} ({validated_away.id})')
    match.verified_fixture_id = fid
    LOG.ok(f'PAREJA_CONFIRMADA fixture={fid}, liga={check["league"].get("id")}, '
           f'season={check["league"].get("season")}, fecha={kickoff.isoformat()}, '
           f'fuente={origin}: {validated_home.name} ({validated_home.id}) vs '
           f'{validated_away.name} ({validated_away.id})')
    return validated_home, validated_away


# =============================================================================
# VERIFICACIÓN DE IDENTIDAD DE PAREJA (V5.7.9)
# =============================================================================


def bwin_league_compatible(label: Optional[str], league: dict[str, Any]) -> bool:
    """Frenar contradicciones comprobables, sin inventar equivalencias de ligas.

    Brasileirão A/B: denominación + país; NO basta 'Serie B' porque otros
    países usan ese nombre. No declarar certeza si falta el país de la API.
    """
    query = normalize_text(label or '')
    if not query:
        return True  # Sin torneo Bwin: no inferir país, exigir demás evidencia.
    name = normalize_text(league.get('name') or '')
    country = normalize_text(league.get('country') or '')
    if 'brasileiro' in query or 'brasileirao' in query or 'campeonato brasileiro' in query:
        tier = re.search(r'\bserie\s+([abcd])\b', query)
        return bool(country == 'brazil' and tier and re.search(r'\bserie\s+'+tier.group(1)+r'\b', name))
    if 'champions league' in query:
        return 'champions league' in name
    return True


def bwin_fixture_names_match(match: BwinMatch, row: dict[str, Any]) -> bool:
    pair = row.get('teams') or {}
    for side, target, gender in (
            ('home', match.home_name, match.home_gender),
            ('away', match.away_name, match.away_gender),
    ):
        candidate = (pair.get(side) or {}).get('name') or ''
        if not candidate or detect_gender(candidate) != gender or not club_category_compatible(target,candidate,gender):
            return False
        variants = name_variants(normalize_text(target))
        specials = {
            'atletico mineiro': {'atletico mg', 'clube atletico mineiro'},
            'atletico mg': {'atletico mineiro', 'clube atletico mineiro'},
        }
        if not any(normalize_text(candidate) == v
                   or institutional_name_match(v,candidate,gender)
                   or normalize_text(candidate) in specials.get(v,set())
                   for v in variants):
            return False
    return True


def fixture_in_event_window(match: BwinMatch, row: dict[str, Any], *,
                            reference_now: Optional[datetime] = None) -> bool:
    dt = parse_date((row.get('fixture') or {}).get('date'))
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = reference_now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    expected = parse_date(match.event_date) if match.event_date else now
    if expected is None:
        return False
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    gap = (dt-expected).total_seconds()
    return abs(gap) <= 36*3600 if match.event_date else -36*3600 <= gap <= 45*86400


def verify_initial_pair_fixture(match: BwinMatch, home: APITeam, away: APITeam,
                                api: APIFootballClient) -> bool:
    """Nunca confundir 'ambos IDs nominales encontrados' con fixture confirmado.

    Busca pareja exacta en el calendario del local, valida nombres, género,
    competición, tiempo y vuelve a consultar por fixture.id. No retorna un
    ID 'confirmado' cuando las respuestas de la API son insuficientes.
    """
    possible: dict[int, dict[str, Any]] = {}
    for window, limit in (('next', CONTEXT_FIXTURE_LOOKAHEAD),
                          ('last', CONTEXT_FIXTURE_HISTORY)):
        try:
            rows = api.get('fixtures',{'team':home.id,window:limit}).get('response',[]) or []
        except Exception as exc:
            LOG.warn(f'PRECHECK_ID consulta {window} falló: {exc}')
            continue
        for row in rows:
            teams = row.get('teams') or {}
            fid = (row.get('fixture') or {}).get('id')
            if (fid and (teams.get('home') or {}).get('id') == home.id
                    and (teams.get('away') or {}).get('id') == away.id
                    and bwin_fixture_names_match(match,row)
                    and bwin_league_compatible(match.competition_display,row.get('league') or {})
                    and fixture_in_event_window(match,row)):
                possible[int(fid)] = row
    if len(possible) != 1:
        LOG.warn(f'PRECHECK_ID: {len(possible)} fixture(s) compatibles para '
                 f'{home.name} ({home.id}) vs {away.name} ({away.id}); activar rescate si procede.')
        return False
    fid, row = next(iter(possible.items()))
    try:
        details = api.get('fixtures',{'id':fid}).get('response',[]) or []
    except Exception as exc:
        LOG.warn(f'PRECHECK_ID no pudo confirmar fixture={fid}: {exc}')
        return False
    verified = [r for r in details if
                (r.get('fixture') or {}).get('id') == fid and
                (r.get('teams') or {}).get('home',{}).get('id') == home.id and
                (r.get('teams') or {}).get('away',{}).get('id') == away.id and
                bwin_fixture_names_match(match,r) and
                bwin_league_compatible(match.competition_display,r.get('league') or {}) and
                fixture_in_event_window(match,r)]
    if len(verified) != 1:
        LOG.warn(f'PRECHECK_ID: fixture {fid} no verificable por ID y metadatos.')
        return False
    match.verified_fixture_id = fid
    LOG.ok(f'PRECHECK_ID: pareja validada por fixture={fid} '
           f'{home.name}({home.id}) vs {away.name}({away.id}).')
    return True


# =============================================================================
# MAIN
# =============================================================================


def main() -> int:
    context_output_dir().mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print("\n" + "=" * 100 + f"\nAPI-FOOTBALL PRO - GENERADOR DE CONTEXTO {VERSION}\n" + "=" * 100 + "\n")
    LOG.info(f"Parche de resolución de identidad: {RESOLVER_PATCH_VERSION}")

    if not API_KEY:
        LOG.error("Falta FOOTBALL_API_KEY.")
        LOG.info("Configúrala en config/.env")
        return 1

    cache_enabled = CACHE_ENABLED
    if "--force" in sys.argv:
        cache_enabled = False
        LOG.warn("Modo --force detectado: se ignorará el caché y se consultará la API en vivo.")
        sys.argv.remove("--force")

    if len(sys.argv) > 1:
        bwin_url = sys.argv[1].strip()
        LOG.info("URL recibida por argumento de consola.")
    else:
        bwin_url = BWIN_EVENT_URL
        LOG.info("URL obtenida desde config.constants.")
    LOG.info(f"URL objetivo: {bwin_url}")

    api = APIFootballClient(API_KEY, cache_enabled=cache_enabled)
    parser = BwinParser()
    team_resolver = TeamResolver(api)
    contextual_resolver = ContextualTeamResolver(api)
    competition_discovery = CompetitionDiscovery(api)
    fixture_resolver = FixtureResolver(api)
    context_collector = ContextCollector(api)

    # ---------------------------------------------------------------- Bwin
    html: Optional[str] = None
    try:
        html = fetch_bwin_html(bwin_url)
    except Exception as exc:
        # Parche: el HTML solo aporta metadatos; los equipos salen de la URL.
        LOG.warn(f"No se pudo descargar el HTML de Bwin ({exc}). Se continúa solo con la URL.")

    try:
        match = parser.parse(bwin_url, html)
    except Exception as exc:
        LOG.error(f"Error parseando Bwin: {exc}")
        return 1

    LOG.ok(f"Evento Bwin: {match.event_id}")
    LOG.info(f"Partido detectado: {match.home_name_raw} vs {match.away_name_raw}")

    # ----------------------------------------------------------------- Equipos
    # El slug sin separador no acredita dónde termina el nombre del local.
    # Resolver el PAR COMPLETO antes de aceptar cualquier corte heurístico.
    slug_match = BwinParser.EVENT_REGEX.search(unquote(urlparse(match.url).path))
    ambiguous_slug = bool(slug_match and '-vs-' not in slug_match.group(1)
                          and not _slug_title_pair(html, slug_match.group(1))
                          and not female_pair_from_slug(slug_match.group(1)))
    home_team: Optional[APITeam] = None
    away_team: Optional[APITeam] = None
    if ambiguous_slug:
        resolved = resolve_compound_slug(match, html, api)
        if resolved:
            home_team, away_team, true_home, true_away = resolved
            match.home_name_raw = true_home
            match.away_name_raw = true_away
            match.home_name, match.home_gender = clean_team_name(true_home)
            match.away_name, match.away_gender = clean_team_name(true_away)
        else:
            LOG.error('La URL no delimita equipos y no se confirmó una pareja mediante fixture. Se detiene para evitar datos de otro encuentro.')
            return 1
    else:
        home_team, away_team = resolve_both_teams(match, team_resolver, contextual_resolver)

    # Verificar SIEMPRE la pareja, incluso cuando /teams?search= devolvió dos
    # identidades nominalmente fuertes. Ese era el punto ciego de 5.7.8.
    initial_pair_verified = bool(home_team is not None and away_team is not None
                                 and verify_initial_pair_fixture(match, home_team, away_team, api))
    if not initial_pair_verified:
        LOG.warn('La pareja inicial no quedó verificada: búsqueda por fixture y competición.')
        home_team, away_team = resolve_full_pair_from_competition_fixture(
            match, api, team_resolver, home_team, away_team)
        if match.verified_fixture_id:
            initial_pair_verified = True

    if home_team is None or away_team is None:
        home_team, away_team = resolve_pair_by_fixture(
            match, team_resolver, api, home_team, away_team)

    if home_team is None or away_team is None:
        home_team, away_team = resolve_missing_from_fixture_calendar(
            match, api, home_team, away_team)

    if home_team is None or away_team is None:
        LOG.error("No fue posible resolver ambos equipos.")
        if home_team is None:
            LOG.error(f"HOME pendiente: {match.home_name}")
        if away_team is None:
            LOG.error(f"AWAY pendiente: {match.away_name}")
        return 1

    if (not match.verified_fixture_id and home_team is not None and away_team is not None
            and not verify_initial_pair_fixture(match, home_team, away_team, api)):
        LOG.error('FIXTURE_NO_VERIFICADO: se encontraron nombres, pero no se confirmó '
                  'un único encuentro válido por ID, localía, categoría, torneo y fecha.')
        return 1

    if home_team.id == away_team.id:
        LOG.error("HOME y AWAY fueron resueltos al mismo equipo.")
        return 1

    for label, team, expected in (
            ("HOME", home_team, match.home_gender),
            ("AWAY", away_team, match.away_gender),
    ):
        if team.gender != expected:
            LOG.error(f"{label} tiene género '{team.gender}' pero Bwin indica '{expected}'.")
            return 1

    LOG.ok(
        f"Equipos finales: {home_team.name} ({home_team.id}) vs {away_team.name} ({away_team.id})"
    )

    # ------------------------------------------------------------ Competiciones
    try:
        home_competitions = competition_discovery.discover(home_team)
        away_competitions = competition_discovery.discover(away_team)
    except Exception as exc:
        LOG.error(f"Error descubriendo competiciones: {exc}")
        return 1

    common = CompetitionDiscovery.common_competitions(home_competitions, away_competitions)
    if common:
        LOG.ok(f"Competición(es) común(es): {common}")
    else:
        LOG.warn(f"No hay competición común en los últimos {RECENT_FIXTURES_LIMIT} partidos.")

    # ----------------------------------------------------------------- Fixture
    try:
        fixture = fixture_resolver.resolve(
            match, home_team, away_team, home_competitions, away_competitions)
    except Exception as exc:
        LOG.error(f"No se pudo resolver el fixture: {exc}")
        return 1

    # Solo emitir el contexto del fixture objetivo: en ausencia de una hora Bwin
    # verificable, un encuentro entre esos dos clubes de otra temporada NO vale.
    fixture_dt = parse_date(fixture.date)
    if fixture_dt is None:
        LOG.error('FIXTURE_SIN_FECHA: no se ha podido validar cercanía del encuentro.')
        return 1
    if fixture_dt.tzinfo is None:
        fixture_dt = fixture_dt.replace(tzinfo=timezone.utc)
    expected_dt = parse_date(match.event_date) if match.event_date else datetime.now(timezone.utc)
    if expected_dt.tzinfo is None:
        expected_dt = expected_dt.replace(tzinfo=timezone.utc)
    gap = (fixture_dt - expected_dt).total_seconds()
    if match.event_date and abs(gap) > 36 * 3600:
        LOG.error(f'FIXTURE_FECHA_INCORRECTA: diferencia {gap/3600:.1f} horas respecto a Bwin.')
        return 1
    if not match.event_date and not (-7 * 86400 <= gap <= 45 * 86400):
        LOG.error(f'FIXTURE_FUERA_DE_VENTANA: diferencia {gap/86400:.1f} días sin fecha Bwin.')
        return 1

    # Seguridad: no cruzar identidades femeninas y fixtures masculinos.
    if match.home_gender == 'female' or match.away_gender == 'female':
        if (match.home_gender != 'female' or match.away_gender != 'female'):
            LOG.error('Bwin declara género distinto por equipo: revisión manual obligatoria.')
            return 1
        league_key = normalize_text(fixture.league_name)
        womens_comp = bool(re.search(
            r"\b(?:women|womens|woman|female|feminina?|feminino|femenina?|femenino|feminine|frauen|damen|fem|f)\b",
            league_key))
        when = parse_date(fixture.date)
        if not when or when.tzinfo is None:
            LOG.error('FIXTURE_SIN_FECHA_UTC: no se puede comprobar proximidad temporal.')
            return 1
        if not match.event_date:
            gap_days = (when - datetime.now(timezone.utc)).total_seconds() / 86400
            if not (-7 <= gap_days <= 45):
                LOG.error('FIXTURE_FUERA_DE_VENTANA: fecha sin confirmar por Bwin '
                          'y fixture API fuera del rango [-7,+45] días.')
                return 1
        if match.competition_display and 'champions league' in normalize_text(match.competition_display):
            if 'champions league' not in league_key:
                LOG.error(f'FIXTURE_COMPETICION_INCORRECTA: Bwin indica '
                          f'{match.competition_display!r}; API indica {fixture.league_name!r}.')
                return 1
        if not womens_comp:
            # Si las dos entidades API están catalogadas como femeninas,
            # puede tratarse de un torneo con nombre neutro; no confirmarlo
            # automáticamente como femenino sin señal del torneo.
            LOG.error(f'FIXTURE_GENERO_NO_VERIFICADO: liga API sin categoría femenina explícita: {fixture.league_name!r}.')
            return 1

    # Parche: en sede neutral Bwin y API-Football pueden invertir local/visitante.
    if fixture.home_id == away_team.id and fixture.away_id == home_team.id:
        LOG.warn(
            "La API lista el fixture con HOME/AWAY invertidos respecto a Bwin "
            "(posible sede neutral). Se alinean los equipos con el orden de la API."
        )
        home_team, away_team = away_team, home_team

    if fixture.home_id != home_team.id or fixture.away_id != away_team.id:
        LOG.error("Los equipos del fixture resuelto no coinciden con la resolución final.")
        LOG.error(f"Esperado: {home_team.name} ({home_team.id}) vs {away_team.name} ({away_team.id})")
        LOG.error(
            f"Encontrado: {fixture.home_name} ({fixture.home_id}) vs {fixture.away_name} ({fixture.away_id})"
        )
        return 1

    LOG.ok(
        f"Fixture validado: {fixture.home_name} ({fixture.home_id}) vs "
        f"{fixture.away_name} ({fixture.away_id})"
    )

    # ---------------------------------------------------------------- Contexto
    try:
        context = context_collector.collect(fixture, home_team, away_team)
    except Exception as exc:
        LOG.error(f"Error recolectando contexto: {exc}")
        return 1

    context["_formatted_txt"] = ContextFormatter.generate(
        match, home_team, away_team, fixture, context, api)

    try:
        txt_path, json_path = save_outputs(match, fixture, context)
    except Exception as exc:
        LOG.error(f"Error guardando outputs: {exc}")
        return 1

    # Identidad canónica generada únicamente tras resolver y guardar el fixture.
    try:
        from match_identity import write_identity
        identity_path = write_identity(
            json_path,
            json_path.with_name("match_identity.json") if os.environ.get("PREMATCH_EVENT_ID")
            else json_path.with_name(f"match_identity_{match.event_id}.json")
        )
    except Exception as exc:
        LOG.error(f"No se pudo persistir identidad canónica: {exc}")
        return 1
    LOG.ok(f"Identidad generada: {identity_path}")

    print_data_coverage(context)
    print_final_summary(match, home_team, away_team, fixture, api)
    print()
    LOG.ok(f"TXT generado: {txt_path}")
    LOG.ok(f"JSON generado: {json_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())