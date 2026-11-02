"""Configuración compartida por el scraper de Bwin y el generador de contexto."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
BASE_DIR = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / "config" / ".env")

# Fuentes y credenciales (solo se exige la clave de API-Football al ejecutar el generador).
FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()
BWIN_EVENT_URL = "https://www.bwin.es/es/sports/eventos/inter-miami-cf-fc-cincinnati-2:7896499?market=-1"
BWIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Rutas.
OUTPUT_DIR_DATA = BASE_DIR / "outputs" / "data"
CACHE_DIR = OUTPUT_DIR_DATA / "cache"
OUTPUT_DIR_SCRAPER = BASE_DIR / "outputs" / "scrapers"

# Scraper.
HEADLESS = True
SLOW_MO = 0
WAIT_BEFORE_EXTRACTION_MS = 1500
PAUSAR_AL_FINAL = False
MAX_INTENTOS_SCRAPER = 3
ESPERA_ENTRE_INTENTOS_SEGUNDOS = 3

# API-Football.
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
RECENT_FIXTURES_LIMIT = 20
MAX_COMPETITION_FIXTURES = 100
CACHE_ENABLED = True
GET_LAST_MATCHES = True
LAST_MATCHES_LIMIT = 10
GET_NEXT_MATCHES = True
NEXT_MATCHES_LIMIT = 5
GET_H2H = True
GET_STANDINGS = True
GET_PREDICTIONS = True
GET_INJURIES = True
GET_LINEUPS = True
GET_STATISTICS = True

# Formato y validación del contexto.
SECTION_SEPARATOR = "=" * 100
SUBSECTION_SEPARATOR = "-" * 100
PERMANENT_API_ERROR_MARKERS = (
    "field do not exist", "field does not exist", "may only contain",
    "invalid parameter", "invalid value", "parameter is required",
    "missing required", "cannot be used", "not allowed",
)

# Contexto e historial.
VERSION = "V5.7-MICRO"
API_USER_AGENT = f"AnalistaBet-API-Football-{VERSION}"
GET_DEEP_HISTORY = True
MICRO_HISTORY_LIMIT = 6
DEEP_HISTORY_LIMIT = MICRO_HISTORY_LIMIT
MICRO_RECENT_WINDOW = 3
MICRO_CONTEXT_WINDOW = 6
MICRO_HISTORY_WINDOWS = (MICRO_RECENT_WINDOW, MICRO_CONTEXT_WINDOW)
FIXTURE_IDS_BATCH_SIZE = 20
COMPLETED_STATUSES = {"FT", "AET", "PEN"}
FUTURE_STATUSES = {"NS", "TBD", "PST", "SUSP"}
LIVE_STATUSES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}
GET_SQUADS = True
GET_COACHS = True
GET_API_ODDS = True
INCLUDE_RAW_CONTEXT_JSON = False
CONTEXT_FIXTURE_LOOKAHEAD = 20
CONTEXT_FIXTURE_HISTORY = 20
GET_REFEREE_HISTORY = True
REFEREE_HISTORY_LIMIT = 15
REFEREE_HISTORY_DAYS_BACK = 180
REFEREE_HISTORY_COMPETITION_PAGES = 12
REFEREE_HISTORY_GLOBAL_DAYS = 180
MAX_WORKERS = 4
CACHE_TTL_DEFAULT = 6 * 3600
CACHE_TTL_EMPTY = 30 * 60
CACHE_TTL_FIXTURE_IDS = 86400
CACHE_TTL_BY_ENDPOINT = {
    "teams": 7 * 86400,
    "coachs": 7 * 86400,
    "players/squads": 86400,
}


# Normalización de equipos.
TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "cusco fc": ("cusco",),
    "red bull bragantino": ("rb bragantino", "bragantino"),
    "vasco da gama": ("vasco da gama",),
    "corinthians sp": ("corinthians",),
    "montevideo city torque": ("atletico torque",),
    "penarol montevideo": ("penarol",),
    "paris s.g.": ("paris sg", "paris saint germain", "paris saint germain w", "psg"),
    "paris sg": ("paris saint germain", "paris saint germain w", "psg"),
    "oud heverlee leuven": ("oh leuven", "oh leuven w", "oud heverlee leuven w", "leuven w"),
    "sao paulo sp": ("sao paulo",),
    "santos sp": ("santos",),
    "athletic club": ("athletic", "athletic bilbao"),
    "getafe": ("getafe cf", "club getafe"),
    "hull city": ("hull",),
    "brentford": ("brentford fc",),
    "barca": ("barcelona", "fc barcelona"),
    "r madrid": ("real madrid", "real madrid cf"),
    "atlas fc": ("atlas",),
    "club puebla fc": ("puebla", "club puebla"),
    "psg": ("paris saint germain", "paris sg"),
    "o lyon": ("olympique lyonnais", "lyon"),
    "instituto ac cordoba": ("instituto",),
    "boyaca chico cd": ("boyaca chico",),
    "junior fc barranquilla": ("junior",),
    "cd junior fc barranquilla primera a futbol": ("junior",),
    "boyaca chico": ("boyaca chico fc",),
    "psg ligue 1 futbol":("psg",),
    "elversberg": ("sv elversberg"),
    "mainz 05 bundesliga futbol": ("fsv mainz 05"),
}

COUNTRY_ES_EN: dict[str, tuple[str, ...]] = {
    "albania": ("albania",),
    "alemania": ("germany",),
    "arabia saudita": ("saudi arabia",),
    "argelia": ("algeria",),
    "argentina": ("argentina",),
    "australia": ("australia",),
    "austria": ("austria",),
    "belgica": ("belgium",),
    "bolivia": ("bolivia",),
    "bosnia y herzegovina": ("bosnia & herzegovina", "bosnia and herzegovina", "bosnia"),
    "brasil": ("brazil",),
    "bulgaria": ("bulgaria",),
    "camerun": ("cameroon",),
    "canada": ("canada",),
    "chile": ("chile",),
    "china": ("china",),
    "colombia": ("colombia",),
    "corea del sur": ("south korea", "korea republic"),
    "costa de marfil": ("ivory coast", "cote d ivoire"),
    "costa rica": ("costa rica",),
    "croacia": ("croatia",),
    "dinamarca": ("denmark",),
    "ecuador": ("ecuador",),
    "egipto": ("egypt",),
    "escocia": ("scotland",),
    "eslovaquia": ("slovakia",),
    "eslovenia": ("slovenia",),
    "espana": ("spain",),
    "estados unidos": ("usa", "united states"),
    "estonia": ("estonia",),
    "finlandia": ("finland",),
    "francia": ("france",),
    "gales": ("wales",),
    "georgia": ("georgia",),
    "ghana": ("ghana",),
    "grecia": ("greece",),
    "haiti": ("haiti",),
    "holanda": ("netherlands",),
    "honduras": ("honduras",),
    "hungria": ("hungary",),
    "inglaterra": ("england",),
    "irak": ("iraq",),
    "iran": ("iran",),
    "irlanda": ("rep. of ireland", "ireland", "republic of ireland"),
    "irlanda del norte": ("northern ireland",),
    "islandia": ("iceland",),
    "israel": ("israel",),
    "italia": ("italy",),
    "jamaica": ("jamaica",),
    "japon": ("japan",),
    "kazajistan": ("kazakhstan",),
    "kosovo": ("kosovo",),
    "letonia": ("latvia",),
    "lituania": ("lithuania",),
    "luxemburgo": ("luxembourg",),
    "macedonia del norte": ("fyr macedonia", "north macedonia", "macedonia"),
    "marruecos": ("morocco",),
    "mexico": ("mexico",),
    "montenegro": ("montenegro",),
    "nigeria": ("nigeria",),
    "noruega": ("norway",),
    "nueva zelanda": ("new zealand",),
    "paises bajos": ("netherlands",),
    "panama": ("panama",),
    "paraguay": ("paraguay",),
    "peru": ("peru",),
    "polonia": ("poland",),
    "portugal": ("portugal",),
    "qatar": ("qatar",),
    "republica checa": ("czech republic", "czechia"),
    "rumania": ("romania",),
    "rusia": ("russia",),
    "senegal": ("senegal",),
    "serbia": ("serbia",),
    "sudafrica": ("south africa",),
    "suecia": ("sweden",),
    "suiza": ("switzerland",),
    "tunez": ("tunisia",),
    "turquia": ("turkey", "turkiye"),
    "ucrania": ("ukraine",),
    "uruguay": ("uruguay",),
    "uzbekistan": ("uzbekistan",),
    "venezuela": ("venezuela",),
    "bielorrusia": ("belarus",),
    "azerbaiyan": ("azerbaijan",),
    "armenia": ("armenia",),
    "chipre": ("cyprus",),
    "malta": ("malta",),
    "moldavia": ("moldova",),
    "andorra": ("andorra",),
    "gibraltar": ("gibraltar",),
    "san marino": ("san marino",),
    "liechtenstein": ("liechtenstein",),
    "islas feroe": ("faroe islands",),
    "feroe": ("faroe islands",),
}
FEMALE_QUERY_SUFFIXES = ("w", "women", "woman", "female", "femenino", "femenina")
FEMALE_SUFFIXES = set(FEMALE_QUERY_SUFFIXES)
YOUTH_PATTERNS = (
    "u17", "u18", "u19", "u20", "u21", "u22", "u23",
    "b team", "reserve", "res ", " ii", " iii",
)
BRAZIL_STATE_SUFFIXES = {
    "sp", "rj", "mg", "rs", "pr", "sc", "ba", "pe", "ce", "go", "df",
    "es", "pa", "am", "mt", "ms", "rn", "pb", "al", "se", "pi", "ma", "to", "ro", "ac", "ap", "rr",
}
CLUB_PREFIXES = {"bk", "fc", "sc", "afc", "ac", "as", "fk", "if"}
CUSTOM_TEAM_IDS: dict[tuple[str, str], int] = {
    # Selecciones UEFA verificadas con /teams?league=5&season=2026
    ("alemania", "male"): 25,
    ("albania", "male"): 778,
    ("andorra", "male"): 1110,
    ("armenia", "male"): 1094,
    ("austria", "male"): 775,
    ("peru", "male"): 30,
    ("colombia" ,"male"): 8,
    ("mexico", "male"): 16,
    ("chile", "male"): 2383,
    ("azerbaiyan", "male"): 1096,
    ("belgica", "male"): 1,
    ("bielorrusia", "male"): 1100,
    ("bosnia y herzegovina", "male"): 1113,
    ("bulgaria", "male"): 1103,
    ("chipre", "male"): 1106,
    ("croacia", "male"): 3,
    ("dinamarca", "male"): 21,
    ("escocia", "male"): 1108,
    ("eslovaquia", "male"): 773,
    ("eslovenia", "male"): 1091,
    ("espana", "male"): 9,
    ("estonia", "male"): 1101,
    ("finlandia", "male"): 1099,
    ("francia", "male"): 2,
    ("gales", "male"): 767,
    ("georgia", "male"): 1104,
    ("gibraltar", "male"): 1093,
    ("grecia", "male"): 1117,
    ("holanda", "male"): 1118,
    ("hungria", "male"): 769,
    ("inglaterra", "male"): 10,
    ("irlanda", "male"): 776,
    ("irlanda del norte", "male"): 771,
    ("islandia", "male"): 18,
    ("islas feroe", "male"): 1098,
    ("israel", "male"): 1116,
    ("italia", "male"): 768,
    ("kazajistan", "male"): 1095,
    ("kosovo", "male"): 1111,
    ("letonia", "male"): 1092,
    ("liechtenstein", "male"): 1107,
    ("lituania", "male"): 1097,
    ("luxemburgo", "male"): 1102,
    ("macedonia del norte", "male"): 1105,
    ("malta", "male"): 1112,
    ("moldavia", "male"): 1114,
    ("montenegro", "male"): 1109,
    ("noruega", "male"): 1090,
    ("paises bajos", "male"): 1118,
    ("polonia", "male"): 24,
    ("portugal", "male"): 27,
    ("republica checa", "male"): 770,
    ("rumania", "male"): 774,
    ("san marino", "male"): 1115,
    ("serbia", "male"): 14,
    ("suecia", "male"): 5,
    ("suiza", "male"): 15,
    ("turquia", "male"): 777,
    ("ucrania", "male"): 772,
    # Otros
    ("japon", "male"): 12,
    ("venezuela", "male"): 2379,
    ("roma", "female"): 11849,
    ("barcelona", "female"): 11832,
    ("santos sp", "male"): 128,
    ("sao paulo sp", "male"): 126,
    ("vasco da gama", "male"): 133,
    ("corinthians sp", "male"): 131,
    ("cucuta deportivo", "male"): 1470,
    ("estudiantes rio cuarto", "male"): 2424,
    ("republica dominicana","male") : 5532
}


# Estadísticas de jugadores.
PLAYER_STAT_MAPPING = {
    "shots_total": ("shots", "total"),
    "shots_on_target": ("shots", "on"),
    "goals": ("goals", "total"),
    "assists": ("goals", "assists"),
    "key_passes": ("passes", "key"),
    "tackles": ("tackles", "total"),
    "interceptions": ("tackles", "interceptions"),
    "duels_total": ("duels", "total"),
    "duels_won": ("duels", "won"),
    "dribbles_attempts": ("dribbles", "attempts"),
    "dribbles_success": ("dribbles", "success"),
    "fouls_committed": ("fouls", "committed"),
    "fouls_drawn": ("fouls", "drawn"),
    "yellow_cards": ("cards", "yellow"),
    "red_cards": ("cards", "red"),
}

