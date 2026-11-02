#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validación LOCAL V1.5.3 multi-informe (sin red ni LLM) de Bwin + API-Football + Perplexity.

Uso: desde la raíz del proyecto, `python validate_prematch.py`.
Requisitos: Python >= 3.9, biblioteca estándar, tzdata del sistema para Europe/Madrid.
NO ejecuta el scraper ni consulta la API; NO modifica archivos de entrada.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from match_identity import validate_identity, compare_report_name

UTC = timezone.utc
try:
    MADRID = ZoneInfo('Europe/Madrid')
except ZoneInfoNotFoundError:
    sys.exit('Falta la zona Europe/Madrid. Instala tzdata: python -m pip install tzdata')
LIVE_STATES = {'1H','2H','HT','ET','BT','P','INT','LIVE','FT','AET','PEN','SUSP','ABD'}
NON_START_STATES = {'NS','TBD','NOT STARTED','NOT_STARTED'}
OTHER_STATES = {'PST','CANC','AWD','WO'}
ISO = re.compile(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)')


def normalize(value: str) -> str:
    text = unicodedata.normalize('NFKD', str(value).casefold())
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(x for x in text if not unicodedata.combining(x))).strip()


def similar_names(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # Variantes ortográficas de nombres propios entre idiomas: Friburgo/Freiburg,
    # Múnich/München. Solo cuando la semejanza es alta y el contexto es equipo.
    stopwords={'sc','fc','cf','ac','club','de','del','real','sport','sporting'}
    ta=[v for v in a.split() if v not in stopwords]
    tb=[v for v in b.split() if v not in stopwords]
    if ta and tb and all(any(SequenceMatcher(None,x,y).ratio()>=0.68 for y in tb) for x in ta) and all(any(SequenceMatcher(None,x,y).ratio()>=0.68 for x in ta) for y in tb):
        return True
    stop = {'de','del','la','el','club','cf','fc','ca','sc','cd','ac', 'football','college','sport','sports','futbol','futebol','deportes','the'}
    x, y = set(a.split()) - stop, set(b.split()) - stop
    if not x or not y:
        return False
    overlap = x & y
    if len(overlap) >= 2 and len(overlap) / min(len(x),len(y)) >= 0.65:
        return True
    return len(x)==1 and len(y)==1 and x==y and len(next(iter(x)))>=5



# Equivalencias institucionales explícitas: no se deduce identidad únicamente
# por similitud de cadenas. Los alias se usan tras identificar el fixture.
KNOWN_CLUB_ALIASES = {
    'olympique lyonnais': 'lyon',
    'o lyon': 'lyon',
    'ol lyon': 'lyon',
    'psg': 'paris saint germain',
    'paris st germain': 'paris saint germain',
    # Alias inequívocos, comprobados contra fixture objetivo, nunca por parecido.
    'ceara sporting club': 'ceara',
    'goias esporte clube': 'goias',
    # Nombre institucional oficial del club de Rio de Janeiro.
    'botafogo de futebol e regatas': 'botafogo',
    # Alias específico: Deportivo Alavés / Dep. Alavés → Alaves.
    # No quitar 'deportivo' indiscriminadamente: identifica muchos otros clubes.
    'deportivo alaves': 'alaves',
    'dep alaves': 'alaves',
}


# Partículas societarias/administrativas inequívocas (incluso intercaladas).
# No quitar «Real», «City», «Sporting», «United», ciudades ni categorías de edad.
CLUB_LEGAL_TOKENS = {'fc', 'cf', 'cd', 'ca', 'sc', 'ac', 'afc', 'club'}
DISPLAY_PARENTHESES = {'local', 'visitante', 'home', 'away', 'titular',
                       'confirmado', 'publicado prepartido', 'probable',
                       'publicado', 'xi confirmado', 'xi oficial'}


def clean_report_team(value: str) -> str:
    """Elimina formato Markdown y solo etiquetas de PRESENTACIÓN."""
    value = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', str(value))
    value = re.sub(r'\s*\(\s*(?:local|visitante|home|away)\s*\)\s*$', '', value, flags=re.I)
    value = re.split(r',\s*(?:kickoff_utc|analysis_cutoff_utc|report_generated_utc|fecha|estadio)\s*[:=]?\s*', value, maxsplit=1, flags=re.I)[0]
    return re.sub(r'\s+', ' ', value).strip(' `*.;,')


def _club_spelling_variants(value: str) -> set[str]:
    """Crea claves conservadoras. Paréntesis descriptivos nunca sustituyen el nombre."""
    cleaned = clean_report_team(value)
    main = re.sub(r'\s*\([^)]*\)', '', cleaned).strip()
    raw_variants = {cleaned, main}
    main_key = normalize(main)
    # «Junior FC (Junior de Barranquilla)»: el nombre entre paréntesis debe
    # compartir el núcleo, nunca basta un alias completamente diferente.
    for part in re.findall(r'\(([^()]*)\)', cleaned):
        part_key = normalize(part)
        if part_key in DISPLAY_PARENTHESES:
            continue
        main_tokens = set(main_key.split()) - CLUB_LEGAL_TOKENS - {'de','del','la','el'}
        part_tokens = set(part_key.split()) - CLUB_LEGAL_TOKENS - {'de','del','la','el'}
        if main_tokens & part_tokens:
            raw_variants.add(part)
    variants = set()
    for candidate in raw_variants:
        key = normalize(candidate)
        if not key:
            continue
        variants.add(KNOWN_CLUB_ALIASES.get(key, key))
        parts = key.split()
        # Solo en inicio y final; nunca suprimir palabras en medio.
        while len(parts) > 1 and parts[0] in CLUB_LEGAL_TOKENS:
            parts.pop(0)
        while len(parts) > 1 and parts[-1] in CLUB_LEGAL_TOKENS:
            parts.pop()
        reduced = ' '.join(parts)
        variants.add(KNOWN_CLUB_ALIASES.get(reduced, reduced))
        # Casos «Instituto AC Córdoba» frente a «Instituto Córdoba»:
        # eliminar siglas legales intercaladas, pero NUNCA topónimos.
        without_legal = ' '.join(x for x in key.split() if x not in CLUB_LEGAL_TOKENS)
        if without_legal:
            variants.add(KNOWN_CLUB_ALIASES.get(without_legal, without_legal))
    return {v for v in variants if v}


def same_club_report(report_name: str, api_name: str, bwin_name: str | None = None) -> bool:
    """Equivalencia exacta de alias legales y variantes Bwin del fixture.

    Evita fuzzy matching: Real Sociedad no pasa por Real Madrid, ni Junior
    Córdoba se convierte en Junior Barranquilla solo por compartir «Junior».
    La pertenencia del nombre Bwin al evento/fixture se audita por separado.
    """
    # Alias institucional específico: jamás equiparar el club de Rio con
    # Botafogo-SP, Botafogo-PB, filiales, equipos femeninos o juveniles.
    # Se contrasta con el calificador Bwin disponible para ESTE fixture.
    report_primary = normalize(re.sub(r'\s*\([^)]*\)', '', clean_report_team(report_name)))
    if report_primary == 'botafogo de futebol e regatas' and bwin_name:
        bwin_key = normalize(bwin_name)
        if re.search(r'\bbotafogo\s+(?:sp|pb|sao paulo|paraiba|b|ii|u\d+|sub\s*\d+|femenino|femenina|feminino|women|fem)\b', bwin_key):
            return False
    # Equivalencias femeninas controladas por la identidad del fixture.
    # No se elimina W/F indiscriminadamente: solo se admiten estas variantes
    # cuando API o Bwin identifican explícitamente el encuentro femenino.
    female_marker = re.compile(r'\b(?:w|women|woman|f|femenina|femenino|frauen|fem)\b', re.I)
    fixture_female = any(female_marker.search(normalize(n)) for n in (api_name, bwin_name) if n)
    report_key = normalize(clean_report_team(report_name))
    if fixture_female and not re.search(r'\b(?:men|masculino|masculina|hombres)\b', report_key):
        def female_core(n):
            tokens = [t for t in normalize(clean_report_team(n)).split()
                      if t not in {'w','women','woman','f','femenina','femenino','frauen','fem','fc','fk','cf','club','de','del'}]
            return ' '.join(tokens)
        core = female_core(report_name)
        accepted = {
            'austria wien': {'austria wien'},
            'juventus': {'juventus', 'juventus turin', 'juventus turino'},
            'hacken': {'hacken', 'bk hacken'},
            'paris': {'paris', 'paris fc'},
        }
        expected_core = female_core(api_name)
        bwin_core = female_core(bwin_name) if bwin_name else ''
        if expected_core in accepted and core in accepted[expected_core]:
            return True
        if bwin_core in accepted and core in accepted[bwin_core]:
            return True
    report_keys = _club_spelling_variants(report_name)
    targets = set().union(*(_club_spelling_variants(n) for n in (api_name,bwin_name) if n))
    if not (report_keys & targets):
        return False
    # Si el reporte ADEMÁS da un nombre geográfico explícito en paréntesis,
    # rechazar contradicciones con una ubicación que sí identifica Bwin.
    # Ej.: «Junior FC (Junior de Córdoba)» NO equivale a Bwin «Junior Barranquilla».
    if bwin_name:
        raw = clean_report_team(report_name)
        primary = re.sub(r'\([^)]*\)', '', raw)
        primary_core = set(normalize(primary).split()) - CLUB_LEGAL_TOKENS
        bwin_tokens = (set(normalize(bwin_name).split()) -
                       primary_core - CLUB_LEGAL_TOKENS -
                       {'futbol','futebol','primera','segunda','tercera','serie','liga','a','b','c','division','de','del','el','la'})
        for inner in re.findall(r'\(([^()]*)\)', raw):
            inner_key = normalize(inner)
            if inner_key in DISPLAY_PARENTHESES:
                continue
            match = re.search(r'\b(?:de|del|do|da)\s+(.+)$', inner_key)
            if not match:
                continue
            named_place = set(match.group(1).split()) - {'la','el','los','las','the'}
            # Solo hay contradicción objetiva cuando Bwin dispone de un
            # calificador adicional y el mismo calificador no coincide.
            if named_place and bwin_tokens and not (named_place & bwin_tokens):
                return False
    return True


def field(raw: str, pattern: str) -> str | None:
    match = re.search(pattern, raw, flags=re.I | re.M)
    return match.group(1).strip() if match else None


def iso_dt(value) -> datetime | None:
    if value is None:
        return None
    match = ISO.search(str(value))
    if not match:
        return None
    try:
        d = datetime.fromisoformat(match.group().replace('Z','+00:00'))
        return d.astimezone(UTC) if d.tzinfo else None
    except ValueError:
        return None


def local(value: datetime | None) -> str | None:
    return value.astimezone(MADRID).isoformat(timespec='seconds') if value else None


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8-sig',errors='replace')


def issue(result: dict, severity: str, code: str, detail: str):
    result['issues'].append({'severity':severity,'code':code,'detail':detail})


def safe_url_from_constants(root: Path) -> str:
    filename = root / 'config' / 'constants.py'
    if not filename.is_file():
        raise ValueError(f'No existe {filename}. Copia el validador a la raíz de scraper.')
    tree = ast.parse(read(filename), filename=str(filename))
    for node in tree.body:
        assigned = []
        if isinstance(node, ast.Assign):
            assigned = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,ast.Name):
            assigned = [node.target.id]
        if 'BWIN_EVENT_URL' in assigned and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
            return node.value.value.strip()
    raise ValueError('BWIN_EVENT_URL debe ser una cadena literal en config/constants.py. No se importa/ejecuta el módulo por seguridad.')


def event_id_from_url(url: str) -> str:
    url = unquote(url)
    host = (urlparse(url).hostname or '').lower()
    if not host.endswith('bwin.es'):
        raise ValueError('BWIN_EVENT_URL no apunta a bwin.es.')
    match = re.search(r':(\d+)(?:$|[?#])',url)
    if not match:
        raise ValueError('No se pudo extraer el ID Bwin de BWIN_EVENT_URL.')
    return match.group(1)


def api_meta(raw: str) -> dict:
    # La cabecera termina antes del microcontexto histórico, evitando mezclar fixtures pasados.
    head = raw[:min(len(raw), 25000)]
    return {
        'event_id':field(head,r'^Event ID:\s*(\d+)\s*$'),
        'bwin_url':field(head,r'^Bwin URL:\s*(https?://\S+)'),
        'home_bwin':field(head,r'^Home Bwin:\s*(.+)$'),
        'away_bwin':field(head,r'^Away Bwin:\s*(.+)$'),
        'fixture_id':field(head,r'^Fixture ID:\s*(\d+)\s*$'),
        'home_api':field(head,r'^Home:\s*(.+?)\s*\(\d+\)\s*$'),
        'home_id':field(head,r'^Home:\s*.+?\s*\((\d+)\)\s*$'),
        'away_api':field(head,r'^Away:\s*(.+?)\s*\(\d+\)\s*$'),
        'away_id':field(head,r'^Away:\s*.+?\s*\((\d+)\)\s*$'),
        'status':field(head,r'^Status:\s*(\S+)'),
        'competition':field(head,r'^League:\s*(.+)$'),
        'season':field(head,r'^Season:\s*(\d{4})'),
        'kickoff_utc':iso_dt(field(head,r'^Date UTC:\s*(\S+)')),
        'generated_utc':iso_dt(field(head,r'^Generado:\s*(\S+)')),
        'historical_fixtures':len(re.findall(r'^PARTIDO HISTÓRICO P-\d+ —',raw,re.M)),
        'historical_statistics_ok':len(re.findall(r'^\s+statistics: OK\s*$',raw,re.M)),
        'historical_players_ok':len(re.findall(r'^\s+players: OK\s*$',raw,re.M)),
        'historical_players_nd':len(re.findall(r'^\s+players: N/D\s*$',raw,re.M)),
        'xi_api_home_missing':'HOME: XI NO DISPONIBLE' in raw,
        'xi_api_away_missing':'AWAY: XI NO DISPONIBLE' in raw,
    }


def odds_meta(raw: str) -> dict:
    head = raw[:10000]
    market_headers = re.findall(r'^MERCADO:\s*(.+)$', raw, flags=re.I|re.M)
    live_heads = [name for name in market_headers if re.search(r'rest of the match|current score|en vivo|live|resultado actual|resto del partido|marcador actual',name,re.I)]
    lines = re.findall(r'^([^\n:]{1,220}?)\s+:\s+(\d+[.,]\d{1,3})\s*$', raw, flags=re.M)
    odds_values = [float(num.replace(',','.')) for _,num in lines]
    return {
        'event_id':field(head,r'^Event ID:\s*(\d+)\s*$'),
        'url':field(head,r'^URL:\s*(https?://\S+)'),
        'match':field(head,r'^Partido:\s*(.+)$'),
        'captured_utc':iso_dt(field(head,r'^Captura:\s*(\S+)')),
        'market_count':len(market_headers),
        'live_market_count':len(live_heads),
        'live_market_names':live_heads[:12],
        'odds_selections':len(odds_values),
        'invalid_odds':sum(x<=1 for x in odds_values),
    }


def find_candidates(root: Path, kind: str, event_id: str) -> list:
    """Busca capturas por Event ID interno, nunca por semejanza de archivo.

    Se admiten las dos carpetas habituales de cuotas sin leer cachés ni JSON.
    """
    if kind == 'api':
        bases = [root/'outputs'/'data']
        pattern = 'match_engine*.txt'
    else:
        bases = [root/'outputs'/'scrapers', root/'outputs'/'odds']
        pattern = 'cuotas_bwin*.txt'
    results = []
    seen = set()
    for base in bases:
        if not base.is_dir():
            continue
        for path in base.rglob(pattern):
            if 'cache' in path.parts or not path.is_file() or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            try:
                with path.open(encoding='utf-8-sig', errors='replace') as file:
                    head = ''.join(file.readline() for _ in range(80))
                ident = field(head, r'^Event ID:\s*(\d+)\s*$')
                if ident == event_id:
                    time_key = 'Generado' if kind == 'api' else 'Captura'
                    stamp = iso_dt(field(head, rf'^{time_key}:\s*(\S+)'))
                    results.append((path, stamp))
            except (OSError, UnicodeError):
                continue
    return results


def select_candidate(found: list, label: str, out: dict, expected_kickoff: datetime | None) -> Path | None:
    if not found:
        issue(out,'BLOCK',f'{label}_NOT_FOUND',f'No se encontró ningún archivo {label} cuyo Event ID sea el configurado.')
        return None
    if len(found)==1:
        return found[0][0]
    # Preferir la captura más reciente ACREDITADA anterior al kickoff.
    if expected_kickoff:
        good=[(p,t) for p,t in found if t and t<expected_kickoff]
        if good:
            last=max(t for _,t in good)
            choice=[p for p,t in good if t==last]
            if len(choice)==1:
                issue(out,'INFO',f'{label}_MULTIPLE',f'{len(found)} archivos del evento: seleccionado el de captura prepartido más reciente, {choice[0].name}.')
                return choice[0]
    # No inventar qué archivo es válido si no hay una forma temporal de decidir.
    issue(out,'BLOCK',f'{label}_AMBIGUOUS',f'Hay {len(found)} archivos {label} del evento y no se puede escoger con certeza. Usa --api/--odds. ' + ', '.join(str(x[0]) for x in found[:6]))
    return None


def scalar_from_report(raw: str, name: str) -> str | None:
    """Campos Markdown en lista, tabla o negrita, incluidos **clave:** valor.

    El parser no confunde una fecha de corte con la finalización del informe.
    """
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith('|') or ':' not in text:
            continue
        lhs, rhs = text.split(':', 1)
        lhs = re.sub(r'[`*]', '', lhs).lstrip('-* ').strip()
        if lhs.casefold() == name.casefold():
            return rhs.strip(' `*')
    for line in raw.splitlines():
        if not line.lstrip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 2:
            continue
        lhs = re.sub(r'[*`\s]', '', cells[0]).casefold()
        if lhs == name.casefold():
            return cells[1].strip(' *`')
    return None


def clean_player(name: str) -> str:
    name=re.sub(r'\[[^\]]+\]\([^)]*\)','',name)
    name=re.sub(r'\*|`','',name)
    return re.sub(r'\s+',' ',name).strip(' ;.,')


def split_players(raw: str) -> list[str]:
    if normalize(raw.strip()) in {'n d','no encontrado','no disponible','sin datos','ninguno','pendiente'}:
        return []
    return [val for v in raw.split(';') if (val:=clean_player(v))]


def _press_xi_from_sections(raw: str, home: str | None, away: str | None) -> dict:
    """Extrae listas narrativas tipo ### Hull City / **XI:** ..., sin mezclar XI históricos.

    La extracción de nombres NO certifica origen oficial ni hora de publicación.
    """
    if not home or not away:
        return {}
    sections = re.split(r'(?=^#{2,5}\s+)', raw, flags=re.M)
    result = {}
    for section in sections:
        heading = re.match(r'^#{2,5}\s+([^\n]+)', section)
        if not heading:
            continue
        title = clean_player(heading.group(1))
        matched_side = None
        for side, team in (('local', home), ('visitante', away)):
            # El encabezado debe nombrar al equipo (puede añadir formación),
            # evitando capturar XI de una crónica de un rival histórico.
            title_core = re.sub(r'\s*\([^)]*\)', '', title).strip()
            if same_club_report(title_core, team) or normalize(title_core).startswith(normalize(team) + ' '):
                matched_side = side
                break
        if not matched_side or matched_side in result:
            continue
        body = section[heading.end():]
        hit = re.search(r'^\s*(?:[-*]\s*)?(?:\*\*)?XI(?:\s+(?:inicial|titular))?\s*:\s*(?:\*\*)?\s*(.+)$', body, re.I | re.M)
        if not hit:
            continue
        value = re.sub(r'\s*\[[^]]+\]\([^)]*\).*$', '', hit.group(1))
        value = re.sub(r'\s*\([^)]*(?:capit[aá]n|seg[uú]n)[^)]*\)', '', value, flags=re.I)
        result[matched_side] = [clean_player(v) for v in re.split(r'[,;]', value) if clean_player(v)]
    return result


def official_xi_tables(raw: str, home: str | None, away: str | None) -> dict:
    """Extrae exclusivamente tablas de titulares dentro de secciones oficiales.

    No confunde suplentes, alineaciones probables ni tablas de prensa con XI.
    La etiqueta oficial del documento no constituye verificación externa.
    """
    if not home or not away:
        return {}
    section = re.search(
        r'^#{2,4}\s+(?:Planillas oficiales confirmadas|Alineaciones oficiales confirmadas|Listas oficiales confirmadas)[^\n]*$',
        raw, re.I | re.M)
    if not section:
        return {}
    tail = raw[section.end():]
    # Limitar al bloque de alineaciones, sin arrastrar apartados posteriores.
    stop = re.search(r'^#{2,3}\s+', tail, re.M)
    if stop:
        tail = tail[:stop.start()]
    chunks = re.split(r'(?=^#{4,5}\s+)', tail, flags=re.M)
    out = {}
    for chunk in chunks:
        h = re.match(r'^#{4,5}\s+([^\n]+)', chunk)
        if not h:
            continue
        title = re.sub(r'`[^`]+`|\([^)]*\)', '', h.group(1)).split('—')[0].strip(' —-')
        side = next((side for side, team in (('local', home), ('visitante', away))
                     if same_club_report(title, team) or normalize(title).startswith(normalize(team) + ' ')), None)
        if side is None or side in out:
            continue
        players = []
        in_table = False
        for line in chunk.splitlines()[1:]:
            if not line.lstrip().startswith('|'):
                if in_table:
                    break
                continue
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(cells) < 3:
                continue
            if normalize(cells[0]) == 'dorsal' and normalize(cells[1]) in {'jugador', 'player'}:
                in_table = True
                continue
            if not re.fullmatch(r'`?\d{1,3}`?', cells[0]):
                continue
            in_table = True
            player = clean_player(cells[1])
            if player:
                players.append(player)
        if players:
            out[side] = players
    return out


def named_lineup_tables(raw: str, home: str | None, away: str | None) -> dict:
    """Lee tablas del once bajo secciones por club (ej. «3.1. Sevilla FC»).

    La extracción NO verifica el carácter oficial de la publicación.
    No se extraen tablas de suplentes ni de encuentros históricos.
    """
    if not home or not away:
        return {}
    sections = re.split(r'(?=^#{2,5}\s+)', raw, flags=re.M)
    found = {}
    for section in sections:
        heading = re.match(r'^#{2,5}\s+([^\n]+)', section)
        if not heading:
            continue
        label = re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', heading[1]).strip()
        title = re.split(r'\s+[—–]\s+|\s+-\s+', label, maxsplit=1)[0].strip()
        side = next((which for which, team in (('local', home), ('visitante', away))
                     if same_club_report(title, team)), None)
        if side is None or side in found:
            continue
        # No interpretar una lista de convocados o un once probable como XI.
        if not re.search(r'\balineaci[oó]n\b|\bonce titular\b|\bonce inicial\b', label, re.I):
            continue
        if re.search(r'\bprobable\b|\bproyectad[oa]\b', label, re.I):
            continue
        players = []
        in_table = False
        for line in section[heading.end():].splitlines():
            if not line.lstrip().startswith('|'):
                if in_table:
                    break
                continue
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(cells) < 3:
                continue
            if normalize(cells[0]) == 'dorsal' and normalize(cells[1]) in {'jugador','player'}:
                in_table = True
                continue
            if not re.fullmatch(r'\*{0,2}`?\d{1,3}`?\*{0,2}', cells[0]):
                continue
            if not in_table:
                continue
            # Exigir columna explícita de titular para evitar mezclar banquillo.
            if not any('titular' in normalize(c) for c in cells[2:]):
                continue
            name = clean_player(cells[1])
            if name:
                players.append(name)
        if players:
            found[side] = players
    return found



def lineup_tables_from_labeled_section(raw: str, home: str | None, away: str | None) -> dict:
    """Extrae XI en tablas dentro del apartado «Alineaciones y Banquillos».

    Admite columnas N.º | Dorsal | Jugador | Rol | Estado, sin afirmar
    verificación externa. Exige la etiqueta 'titular' por cada fila.
    """
    if not home or not away:
        return {}
    top = re.search(r'^#{2,3}\s+[^\n]*(?:alineaciones\s+y\s+banquillos|alineaciones\s+confirmadas|onces\s+confirmados)[^\n]*$', raw, re.I | re.M)
    if not top:
        return {}
    tail = raw[top.end():]
    # Acotar a este apartado: nunca leer XI de otros apartados o históricos.
    end = re.search(r'^##\s+', tail, re.M)
    if end:
        tail = tail[:end.start()]
    heads = list(re.finditer(r'^#{3,5}\s+([^\n]+)$', tail, re.M))
    found = {}
    for i, heading in enumerate(heads):
        title = heading.group(1).strip()
        if re.search(r'\bprobable\b|\bproyectad[oa]\b|\bestimad[oa]\b', title, re.I):
            continue
        club_title = re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', title)
        club_title = re.sub(r'\s*\([^)]*\)\s*$', '', club_title).strip(' `*–—-')
        side = next((side for side, team in (('local', home), ('visitante', away))
                     if same_club_report(club_title, team)), None)
        if not side or side in found:
            continue
        body = tail[heading.end(): heads[i + 1].start() if i + 1 < len(heads) else len(tail)]
        name_index = status_index = None
        players = []
        started = False
        for line in body.splitlines():
            if not line.lstrip().startswith('|'):
                if started:
                    break
                continue
            cells = [x.strip() for x in line.strip().strip('|').split('|')]
            normalized = [normalize(x) for x in cells]
            if 'jugador' in normalized and any(c == 'estado' or c.startswith('estado ') for c in normalized):
                name_index = normalized.index('jugador')
                status_index = next(i for i,c in enumerate(normalized) if c == 'estado' or c.startswith('estado '))
                started = True
                continue
            if not started or name_index is None or status_index is None:
                continue
            if len(cells) <= max(name_index, status_index):
                continue
            if not re.search(r'\btitular\b', normalize(cells[status_index])):
                continue
            # La cabecera puede contener una columna ordinal previa al dorsal.
            if not re.fullmatch(r'\d{1,2}', re.sub(r'[`*]', '', cells[0])):
                continue
            player = clean_player(cells[name_index])
            player = re.sub(r'\s*\((?:c|capit[aá]n)\)\s*$', '', player, flags=re.I)
            if player:
                players.append(player)
        if players:
            found[side] = players
    return found


def _report_team_heading(label: str) -> str:
    """Conserva el nombre de club y elimina SOLO etiquetas visuales del título.

    Los informes pueden usar (PUBLICADO_PREPARTIDO) o [PROBABLE],
    ambas sintaxis pueden incluir escapes de Markdown. No se elimina
    información del cuerpo de la tabla ni se certifica oficialidad.
    """
    label = label.replace(r"\[", "[").replace(r"\]", "]")
    label = re.sub(r"\s*\([^)]*\)", "", label)
    label = re.sub(r"\s*\[[^]]*\]", "", label)
    return re.split(r"\s+[—–]\s+", label, maxsplit=1)[0].strip()


def _match_fixture_team_heading(label: str, api_team: str, bwin_team: str | None) -> bool:
    """Equivalencia por API o nombre Bwin con sufijo de competición conocido.

    Esta equivalencia identifica el lado declarado del fixture, NO al jugador.
    El prefijo Bwin únicamente se acepta cuando el resto es nombre de liga/deporte
    y el nombre de club tiene al menos dos tokens. Nunca fuzzy matching.
    """
    if same_club_report(label, api_team, bwin_team):
        return True
    name = normalize(label)
    bwin = normalize(bwin_team or "")
    if len(name.split()) < 2 or not bwin.startswith(name + " "):
        return False
    suffix = bwin[len(name):].strip()
    return bool(re.match(r"^(?:brasileiro|brasileira|serie|primera|saudi|pro league|liga|futbol|futebol)\b", suffix))


def classified_lineup_tables(raw: str, home: str | None, away: str | None,
                             home_bwin: str | None = None, away_bwin: str | None = None) -> dict:
    """Lee las tablas por club de Alineaciones y Banquillos sin elevar PROBABLE a OFICIAL.

    Incluye onces de prensa etiquetados PROBABLE; descarta banco, bajas y las
    filas NO DISPONIBLE. La credibilidad de una etiqueta requiere verificar fuente.
    """
    if not home or not away:
        return {}
    section = re.search(r"(?im)^##\s+alineaciones\s+y\s+banquillos[^\n]*$", raw)
    if not section:
        return {}
    after = raw[section.end():]
    stop = re.search(r"(?m)^##\s+", after)
    body = after[:stop.start()] if stop else after
    heads = list(re.finditer(r"(?m)^###\s+([^\n]+)$", body))
    result = {}
    for index, heading in enumerate(heads):
        label = heading.group(1).strip()
        core = _report_team_heading(label)
        side = next((k for k,t,b in (("local",home,home_bwin), ("visitante",away,away_bwin))
                     if _match_fixture_team_heading(core,t,b)), None)
        if side is None or side in result:
            continue
        block = body[heading.end():heads[index+1].start() if index+1 < len(heads) else len(body)]
        header_names = None
        players = []
        row_states = []
        started = False
        for line in block.splitlines():
            if not line.lstrip().startswith('|'):
                if started and players:
                    break
                continue
            cells = [c.strip().strip(' *`') for c in line.strip().strip('|').split('|')]
            normalized = [normalize(c) for c in cells]
            name_index = next((j for j,c in enumerate(normalized) if c in {'jugador','jugadora','player'}), None)
            if name_index is not None and 'estado' in normalized:
                header_names=(name_index, normalized.index('estado'))
                started=True
                continue
            if not started or header_names is None:
                continue
            name_col, status_col=header_names
            if max(name_col,status_col)>=len(cells):
                continue
            # La primera columna es el ordinal 1..11, no un separador ni suplente.
            if not re.fullmatch(r'\d{1,2}',cells[0]):
                continue
            state=normalize(cells[status_col])
            if not any(x in state for x in ('probable','titular','confirmado','publicado')):
                continue
            player=clean_player(cells[name_col])
            if player and '?' not in player and normalize(player) not in {'no disponible','n d'}:
                players.append(player)
                row_states.append(state)
        if not players:
            continue
        if ('probable' in normalize(label) or any('probable' in x for x in row_states)):
            classification='PROBABLE'
        elif 'confirmado oficial' in normalize(label):
            classification='CONFIRMADO_OFICIAL_DECLARADO_NO_VERIFICADO'
        else:
            classification='PUBLICADO_PREPARTIDO_DECLARADO_NO_VERIFICADO'
        result[side]={'players':players,'classification':classification,
                      'heading':label,'rows':len(players)}
    return result


def press_xi(raw: str, home: str | None = None, away: str | None = None) -> dict:
    lists = {}
    for side in ('local','visitante'):
        explicit = scalar_from_report(raw,'xi_'+side)
        if explicit:
            lists[side] = split_players(explicit)
    if len(lists) != 2:
        legacy = re.search(r'^\|\s*Lista publicada\s*\|([^\n]+)',raw,re.I|re.M)
        if legacy:
            cells = legacy.group(1).split('|')
            if len(cells) >= 2:
                lists.setdefault('local', split_players(cells[0]))
                lists.setdefault('visitante', split_players(cells[1]))
    if len(lists) != 2:
        for side, values in _press_xi_from_sections(raw, home, away).items():
            lists.setdefault(side, values)
    if len(lists) != 2:
        for side, values in official_xi_tables(raw, home, away).items():
            lists.setdefault(side, values)
    if len(lists) != 2:
        for side, values in named_lineup_tables(raw, home, away).items():
            lists.setdefault(side, values)
    if len(lists) != 2:
        for side, values in lineup_tables_from_labeled_section(raw, home, away).items():
            lists.setdefault(side, values)
    return lists


def heading_teams(raw: str) -> tuple[str, str] | None:
    # Se limita la búsqueda al encabezado: no interpretar enfrentamientos
    # históricos mencionados más abajo como el encuentro objetivo.
    probe = raw[:3000]
    candidates = []
    explicit = scalar_from_report(probe, 'Partido')
    if explicit:
        candidates.append(explicit)
    for line in probe.splitlines():
        if not line.lstrip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) > 1 and normalize(re.sub(r'[*`]', '', cells[0])) == 'partido':
            candidates.append(cells[1])
    for entry in candidates:
        value = re.sub(r'\s*\[[^]]+\]\([^)]*\)', '', entry).strip(' `*.')
        match = re.match(r'^(.+?)\s+(?:vs\.?|contra)\s+(.+)$', value, flags=re.I)
        if match:
            return clean_report_team(match[1]), clean_report_team(match[2])
    match = re.search(r'(?:\*\*)?([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ. -]{2,80})\s+vs\.?\s+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ. -]{2,80}?)(?:\s+—|\*\*|\s*\n)', probe, re.I)
    return (clean_report_team(match[1]), clean_report_team(match[2])) if match else None


def report_field_embedded(raw: str, name: str) -> str | None:
    """Recupera una fecha ISO explícita en la cabecera aunque comparta línea con Partido.

    Solo primera sección: nunca extraer fechas de ejemplos históricos o crónicas.
    No inventa timestamps ni convierte horas sin offset.
    """
    headings = list(re.finditer(r"(?m)^##\s+", raw))
    header = raw[:(headings[1].start() if len(headings) > 1 else min(len(raw), 4000))]
    pattern = rf"(?i)\b{re.escape(name)}\s*[:=]?\s*({ISO.pattern})"
    match = re.search(pattern, header)
    return match.group(1) if match else None


def report_meta(raw: str, home: str | None = None, away: str | None = None,
                home_bwin: str | None = None, away_bwin: str | None = None) -> dict:
    # Intentar nombres canónicos API primero. Si la API abrevia «Boyaca Chico»
    # como «Chico», una segunda lectura utiliza nombres Bwin ligados al fixture.
    xi = press_xi(raw, home, away)
    classified = classified_lineup_tables(raw, home, away, home_bwin, away_bwin)
    for side, entry in classified.items():
        if not xi.get(side):
            xi[side] = entry['players']
    if (not xi.get('local') or not xi.get('visitante')) and (home_bwin or away_bwin):
        other = press_xi(raw, home_bwin or home, away_bwin or away)
        for side in ('local', 'visitante'):
            if not xi.get(side) and other.get(side):
                xi[side] = other[side]
    fields={name:scalar_from_report(raw,name) for name in ('kickoff_madrid','kickoff_utc','analysis_cutoff_utc','cutoff_madrid','report_generated_utc','report_completed_madrid','event_id_bwin','xi_local_estado','xi_visitante_estado','xi_local_fuente','xi_visitante_fuente','xi_local_publicado_madrid','xi_visitante_publicado_madrid','xi_local','xi_visitante')}
    for name in ('kickoff_utc', 'kickoff_madrid', 'analysis_cutoff_utc', 'report_generated_utc'):
        if not fields.get(name):
            fields[name] = report_field_embedded(raw, name)
    fields['event_id_bwin'] = (fields['event_id_bwin'] or scalar_from_report(raw,'Evento Bwin')
                               or scalar_from_report(raw,'Event ID Bwin') or scalar_from_report(raw,'Event ID'))
    return {
        'fields':fields,
        'teams':heading_teams(raw),
        'kickoff_utc':iso_dt(fields.get('kickoff_utc') or fields.get('kickoff_madrid')),
        'cutoff_utc':iso_dt(fields.get('analysis_cutoff_utc') or fields.get('cutoff_madrid')),
        'generated_utc':iso_dt(fields.get('report_generated_utc') or fields.get('report_completed_madrid')),
        'xi':xi,
        'xi_classification': {side:entry['classification'] for side,entry in classified.items()},
        'declared_timestamps_verified': False,
        'url_count':len(set(re.findall(r'https?://[^\s\])>]+',raw))),
        'has_source_registry':bool(re.search(r'registro de fuentes|registro documental de fuentes|fuentes y enlaces|fuentes consultadas|fuentes relevantes',raw,re.I)),
    }


def audit_api(meta: dict, out: dict, url_id: str):
    if meta['event_id']!=url_id:
        issue(out,'BLOCK','API_WRONG_EVENT',f'Contexto API del evento {meta["event_id"]}, esperado {url_id}.')
    if not meta['fixture_id']:
        issue(out,'BLOCK','API_NO_FIXTURE','El contexto no contiene un fixture_id identificado.')
    for key in ('home_bwin','away_bwin','home_api','away_api'):
        if not meta[key]:issue(out,'REVIEW',f'API_NO_{key.upper()}',f'No se reconoció el campo {key}.')
    for side in ('home','away'):
        a,b=meta.get(side+'_bwin'),meta.get(side+'_api')
        if a and b and not similar_names(a,b):
            # Fixture API identificado con ambos IDs y URL del mismo evento Bwin.
            # Se registra la traducción como INFO, sin afirmar verificación externa.
            fixture_backed = bool(meta.get('fixture_id') and meta.get('home_api') and meta.get('away_api')
                                  and meta.get('bwin_url') and url_id in meta['bwin_url']
                                  and meta.get('home_id') and meta.get('away_id'))
            if fixture_backed:
                issue(out,'INFO',f'ALIAS_{side.upper()}_FIXTURE',f'Alias {a} / {b} aceptado por identificación de fixture y IDs en el contexto API; no equivale a verificación independiente de Bwin.')
            else:
                issue(out,'REVIEW',f'ALIAS_{side.upper()}',f'No se pudo verificar automáticamente alias de {a} vs {b}; revisar identidad, no asumir error.')
    if not meta['kickoff_utc']:
        issue(out,'BLOCK','API_NO_KICKOFF','No se encontró Date UTC con offset en el fixture objetivo.')
    if not meta['generated_utc']:
        issue(out,'REVIEW','API_NO_CAPTURE','No se encontró fecha de generación verificable del contexto.')
    state=(meta.get('status') or '').upper()
    if state in LIVE_STATES:
        issue(out,'BLOCK','API_MATCH_STARTED',f'El fixture figura en estado {state}; no ejecutar nuevos análisis prepartido.')
    elif state in OTHER_STATES:
        issue(out,'BLOCK','API_MATCH_NOT_PLAYABLE',f'Fixture en estado {state}, requiere actualización de fecha/estado.')
    elif not state or state not in NON_START_STATES:
        issue(out,'REVIEW','API_STATUS_UNKNOWN',f'Estado del fixture no identificado: {state or "N/D"}.')
    if meta['kickoff_utc'] and meta['generated_utc'] and meta['generated_utc']>=meta['kickoff_utc']:
        issue(out,'BLOCK','API_CAPTURE_LATE','Generador API ejecutado después del kickoff que reporta API-Football.')
    if meta['historical_fixtures'] < 2:
        issue(out,'REVIEW','API_HISTORY_LOW','El TXT contiene menos de dos fixtures históricos auditables; comprobar cobertura antes del análisis.')
    if meta['historical_players_nd']:
        issue(out,'INFO','HISTORICAL_PLAYER_NULLS',f"Faltan estadísticas de jugadores en {meta['historical_players_nd']} fixture(s) histórico(s). No imputar cero.")
    if meta['xi_api_home_missing'] or meta['xi_api_away_missing']:
        issue(out,'INFO','API_XI_ABSENT','Falta el XI objetivo en API; no equivale a ausencia de XI en prensa ni a error del script.')


def audit_odds(meta:dict, out:dict, url_id:str, api:dict):
    if meta['event_id']!=url_id:
        issue(out,'BLOCK','ODDS_WRONG_EVENT',f'Cuotas pertenecen al evento {meta["event_id"]}, esperado {url_id}.')
    if meta['url']:
        try:
            if event_id_from_url(meta['url'])!=url_id:
                issue(out,'BLOCK','ODDS_WRONG_URL','La URL incorporada a las cuotas apunta a otro evento.')
        except ValueError:
            issue(out,'REVIEW','ODDS_URL_UNRECOGNIZED','No se pudo interpretar la URL de las cuotas.')
    stamp=meta.get('captured_utc'); kick=api.get('kickoff_utc')
    if not stamp:
        issue(out,'REVIEW','ODDS_NO_TIMESTAMP','Cuotas sin hora de captura ISO con offset; no se acredita que sean prepartido.')
    elif kick and stamp>=kick:
        issue(out,'BLOCK','ODDS_CAPTURE_LATE','Cuotas capturadas al inicio o después del kickoff de API.')
    if not meta['market_count'] or not meta['odds_selections']:
        issue(out,'BLOCK','ODDS_EMPTY','Sin mercados o selecciones numéricas reconocibles en el TXT.')
    if meta['invalid_odds']:
        issue(out,'REVIEW','ODDS_VALUES','Hay cuotas <=1; deben excluirse esas selecciones.')
    if meta['live_market_count']:
        # El informe conserva explícitamente el inventario excluido. Codex debe
        # filtrar por cabecera íntegra; no invalida el resto de mercados prematch.
        meta['excluded_live_market_names'] = list(meta['live_market_names'])
        issue(out,'INFO','ODDS_LIVE_EXCLUDED',f'{meta["live_market_count"]} mercado(s) tipo live/rest-of-match identificados como NO ELEGIBLES para el análisis prepartido. Excluidos por nombre en la lista de validación; Codex debe mantener la exclusión.')
    if meta['market_count'] and meta['market_count'] == meta['live_market_count']:
        issue(out,'BLOCK','ODDS_ONLY_LIVE','Todos los mercados reconocidos parecen live.')
    if stamp and kick and kick-stamp > timedelta(hours=6):
        issue(out,'REVIEW','ODDS_STALE','Las cuotas se capturaron más de 6 horas antes del kickoff; verificar vigencia.')


def audit_report(meta:dict, out:dict, api:dict, expected_id:str):
    label=meta['fields'].get('event_id_bwin')
    if label and re.fullmatch(r'\d+',label) and label!=expected_id:
        issue(out,'BLOCK','REPORT_WRONG_EVENT',f'Reporte indica evento Bwin {label} distinto del configurado {expected_id}.')
    if meta['teams']:
        for i,side in enumerate(('home','away')):
            expected=api.get(side+'_api') or api.get(side+'_bwin')
            got=meta['teams'][i]
            if expected and not same_club_report(got, expected, api.get(side+'_bwin')):
                issue(out,'BLOCK','REPORT_WRONG_TEAM_'+side.upper(),f'Encabezado del reporte {side}={got!r}, esperado {expected!r}.')
            elif expected and normalize(clean_report_team(got)) != normalize(expected):
                issue(out,'INFO','REPORT_ALIAS_'+side.upper(),f'Alias del reporte {got!r} aceptado para {expected!r} (fixture objetivo).')
    else:
        issue(out,'REVIEW','REPORT_TEAM_UNSTRUCTURED','No se identificó encabezado de partido LOCAL vs VISITANTE en el reporte.')
    k=meta['kickoff_utc']; ak=api.get('kickoff_utc')
    # Comparar explícitamente ambos metadatos del reporte y verificar cambio CET/CEST.
    spanish_raw = meta['fields'].get('kickoff_madrid')
    utc_raw = meta['fields'].get('kickoff_utc')
    spanish = iso_dt(spanish_raw)
    utc_report = iso_dt(utc_raw)
    if spanish and utc_report and abs((spanish - utc_report).total_seconds()) > 60:
        issue(out,'BLOCK','REPORT_INTERNAL_TIME_CONFLICT','kickoff_madrid y kickoff_utc representan instantes diferentes.')
    if spanish_raw and spanish:
        source = ISO.search(spanish_raw)
        if source:
            original = datetime.fromisoformat(source.group().replace('Z','+00:00'))
            expected = original.astimezone(MADRID)
            if original.utcoffset() != expected.utcoffset() or original.replace(tzinfo=None) != expected.replace(tzinfo=None):
                issue(out,'BLOCK','REPORT_SPANISH_DST_MISMATCH',f'kickoff_madrid tiene hora/offset inválido para España; corresponde {local(spanish)}.')
    if not k:
        issue(out,'REVIEW','REPORT_NO_KICKOFF','Reporte sin kickoff_madrid/kickoff_utc ISO. No se puede comparar su programación.')
    elif ak and abs((k-ak).total_seconds())>120:
        issue(out,'BLOCK','KICKOFF_CONFLICT',f'API indica {local(ak)} y reporte {local(k)} (Europe/Madrid). Diferencia importante, resolver antes de analizar.')
    if api.get('generated_utc') and k and api['generated_utc']>=k:
        issue(out,'BLOCK','API_CAPTURE_AFTER_REPORT_KICKOFF','El contexto API fue generado al inicio o después del kickoff declarado en el informe; riesgo de información del partido objetivo.')
    if k and api.get('status') in NON_START_STATES and api.get('kickoff_utc') and abs((k-api['kickoff_utc']).total_seconds())>120:
        issue(out,'REVIEW','SCHEDULE_RECHECK','Programaciones discordantes; verificar aplazamiento o error de conversión horaria.')
    cutoff=meta['cutoff_utc']
    if not cutoff:
        issue(out,'REVIEW','REPORT_NO_CUTOFF','Reporte sin corte analítico ISO verificable; no equivale a generación prepartido.')
    elif k and cutoff>=k:
        issue(out,'BLOCK','REPORT_CUTOFF_LATE','El corte analítico declarado es posterior al kickoff del reporte.')
    if meta['generated_utc']:
        issue(out,'INFO','REPORT_GENERATION_SELF_REPORTED',
              'report_generated_utc procede del texto de Perplexity; no demuestra la hora real de generación ni de descarga.')
        if meta['cutoff_utc'] and meta['cutoff_utc'] > meta['generated_utc']:
            issue(out,'REVIEW','REPORT_CUTOFF_AFTER_GENERATION',
                  'El reporte declara un corte documental posterior a su propia generación: verificar marcas temporales.')
    else:
        issue(out,'REVIEW','REPORT_COMPLETION_UNKNOWN','No se acredita hora de finalización real del informe. Su corte declarado no es prueba temporal.')
    if k and meta['generated_utc'] and meta['generated_utc']>=k:
        issue(out,'BLOCK','REPORT_COMPLETION_LATE','El informe finalizó después del kickoff; contiene posible filtración posterior.')
    if not meta['url_count']:
        issue(out,'REVIEW','REPORT_NO_LINKS','Informe sin URLs de fuentes comprobables.')
    lists=meta['xi']
    for side, status in meta.get('xi_classification', {}).items():
        issue(out,'INFO','REPORT_XI_'+status.replace('_DECLARADO_NO_VERIFICADO','')+'_'+side.upper(),
              f'XI {side}: {status}. El parser leyó nombres, pero la etiqueta del informe no acredita oficialidad.')
    if not lists:
        issue(out,'INFO' if api.get('xi_api_home_missing') and api.get('xi_api_away_missing') else 'REVIEW','REPORT_XI_NOT_EXTRACTED','Sin XI legibles en el reporte; si todavía no se publicaron, es normal. No inferir oficialidad ni ausencia a partir del parser.')
    else:
        for side in ('local','visitante'):
            members=lists.get(side)
            if not members:
                issue(out,'REVIEW','REPORT_XI_NO_'+side.upper(),f'No se extrajo lista para {side}.')
                continue
            if len(members)!=11:
                issue(out,'REVIEW','REPORT_XI_COUNT_'+side.upper(),f'{len(members)} nombres en XI {side}; una lista incompleta no es un XI confirmado.')
            if len({normalize(n) for n in members})<len(members):
                issue(out,'REVIEW','REPORT_XI_DUP_'+side.upper(),f'XI {side} tiene nombres duplicados.')
            is_explicit=bool(meta['fields'].get('xi_'+side))
            stamp=iso_dt(meta['fields'].get('xi_'+side+'_publicado_madrid'))
            if stamp and k and stamp>=k:
                issue(out,'BLOCK','REPORT_XI_SOURCE_LATE_'+side.upper(),f'Fecha de publicación del XI {side} es posterior al kickoff declarado.')
            if is_explicit and not stamp:
                issue(out,'REVIEW','REPORT_XI_SOURCE_TIME_'+side.upper(),f'Falta fecha de publicación contrastable para el XI {side}.')
            if is_explicit and normalize(meta['fields'].get('xi_'+side+'_fuente') or '') in {'','null','none','no encontrado','no verificable','n d'}:
                issue(out,'REVIEW','REPORT_XI_SOURCE_LINK_'+side.upper(),f'Falta enlace de procedencia de XI {side}.')
    if not meta['has_source_registry']:
        issue(out,'INFO','REPORT_SOURCES_REGISTRY','Recomendado: sección FUENTES RELEVANTES con fecha y enlace por hallazgo.')
    issue(out,'INFO','REPORT_NOT_WEB_VERIFIED','Validación estática: el programa no abre URLs ni certifica si una publicación fue modificada.')


def md_summary(result:dict) -> str:
    summary=result.get('match',{})
    s=[f'# Validación {summary.get("event_id","N/D")}',
       '',f'**Estado:** {result["status"]}',
       f'**Partido:** {summary.get("home","N/D")} vs {summary.get("away","N/D")}',
       f'**Kickoff API (Madrid):** {summary.get("kickoff_madrid") or "N/D"}',
       f'**Fixture API:** {summary.get("fixture_id") or "N/D"}',
       '', '## Archivos']
    for k,v in result.get('files',{}).items():s.append(f'- {k}: `{v}`')
    s+=['','## Incidencias']
    for x in result['issues']:
        s.append(f'- **[{x["severity"]}] {x["code"]}:** {x["detail"]}')
    s+=['','## Alcance','Se validó estructura, identidad, fechas, algunos mercados y listas del XI. Esto NO valida veracidad periodística, IDs individuales, muestras estadísticas completas ni probabilidades.']
    return '\n'.join(s)+'\n'



def choose_report_path(root: Path, event_id: str, override: Path | None = None,
                       explicit_url: bool = False) -> Path:
    """Selecciona exclusivamente un informe identificado por Event ID.

    Si se especifica --report, esa ruta debe identificar el mismo evento.
    Sin --report se exige instructions/reporte_cualitativo_<event_id>.md,
    también al ejecutar el validador individual sin --url.
    `explicit_url` se conserva en la firma por compatibilidad.
    """
    if override is not None:
        return override if override.is_absolute() else root / override
    return root / 'instructions' / f'reporte_cualitativo_{event_id}.md'


def report_filename_event(path: Path) -> str | None:
    hit = re.fullmatch(r'reporte_cualitativo_(\d{6,})\.md', path.name, re.I)
    return hit.group(1) if hit else None


def main() -> int:
    ap=argparse.ArgumentParser(description='Auditor autónomo Bwin/API/Perplexity: ejecuta python validate_prematch.py')
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parent, help='Ruta raíz del proyecto')
    ap.add_argument('--url',help='URL Bwin objetivo explícita; anula BWIN_EVENT_URL sin editar constants.py')
    ap.add_argument('--api',type=Path, help='Opcional: TXT API si hay varias capturas del evento')
    ap.add_argument('--odds',type=Path, help='Opcional: TXT Bwin si hay varias capturas del evento')
    ap.add_argument('--no-odds',action='store_true',help='Investigación estadística sin captura Bwin; no autoriza picks ejecutables')
    ap.add_argument('--report',type=Path, help='Opcional: reporte Markdown')
    ap.add_argument('--identity',type=Path,help='Identidad canónica vinculada al fixture del mismo evento')
    ap.add_argument('--historical',action='store_true',help='Solo auditar capturas históricas, sin pretender que el partido sea futuro')
    ap.add_argument('--out',type=Path, help='Opcional: carpeta de salidas')
    args=ap.parse_args()
    if args.no_odds and args.odds:
        ap.error('No combinar --no-odds con --odds')
    root=args.root.resolve()
    output={'version':'1.5.4-fixture-identity-bridge','timezone':'Europe/Madrid','issues':[],'files':{},'checks':{}}
    try:
        bwin_url=args.url.strip() if args.url else safe_url_from_constants(root)
        event_id=event_id_from_url(bwin_url)
    except (ValueError,SyntaxError,OSError) as err:
        print(f'ERROR CONFIGURACIÓN: {err}',file=sys.stderr)
        return 2
    output['match']={'event_id':event_id,'bwin_url':bwin_url}
    api_candidates=find_candidates(root,'api',event_id)
    # Primera pasada: leer metadata de las capturas API para resolver kickoff si hay duplicados.
    known_kickoffs={iso_dt(field(read(p)[:25000],r'^Date UTC:\s*(\S+)')) for p,_ in api_candidates}
    known_kickoffs.discard(None)
    expected_kickoff=next(iter(known_kickoffs)) if len(known_kickoffs)==1 else None
    if len(known_kickoffs)>1:
        issue(output,'BLOCK','API_KICKOFF_MULTIPLE','Mismo evento Bwin tiene diferentes kickoff en capturas API. Revisar reprogramación manualmente.')
    api_path=(args.api if args.api.is_absolute() else root/args.api) if args.api else select_candidate(api_candidates,'API',output,expected_kickoff)
    odds_path=(None if args.no_odds else (args.odds if args.odds.is_absolute() else root/args.odds) if args.odds else select_candidate(find_candidates(root,'odds',event_id),'ODDS',output,expected_kickoff))
    if args.no_odds:
        issue(output,'INFO','SIN_CUOTAS_ESTADISTICO','Modo sin cuotas explícito: no existe selección Bwin ejecutable.')
    report_path=choose_report_path(root,event_id,args.report,explicit_url=bool(args.url))
    report_named_event=report_filename_event(report_path)
    if report_named_event != event_id:
        issue(output,'BLOCK','REPORT_FILENAME_WRONG_EVENT',
              f'El informe debe llamarse reporte_cualitativo_{event_id}.md; ruta recibida: {report_path}. '
              'Se rechazan nombres genéricos o de otros eventos para evitar mezclas.')
    for key,p in (('api',api_path),('odds',odds_path),('report',report_path)):
        output['files'][key]=str(p) if p else None
        if p and (not p.is_file() or p.stat().st_size==0):
            issue(output,'BLOCK','FILE_'+key.upper()+'_INVALID',f'Archivo ausente o vacío: {p}')
    api=api_meta(read(api_path)) if api_path and api_path.is_file() and api_path.stat().st_size else None
    odds=odds_meta(read(odds_path)) if odds_path and odds_path.is_file() and odds_path.stat().st_size else None
    report=report_meta(read(report_path), api.get('home_api') if api else None, api.get('away_api') if api else None, api.get('home_bwin') if api else None, api.get('away_bwin') if api else None) if report_path and report_path.is_file() and report_path.stat().st_size else None
    if api:
        audit_api(api,output,event_id)
        if api.get('kickoff_utc') and datetime.now(UTC)>=api['kickoff_utc']:
            if args.historical:
                issue(output,'INFO','HISTORICAL_MODE','Evento programado en el pasado: auditando archivos históricos; esto no constituye una decisión prepartido actual.')
            else:
                issue(output,'BLOCK','CURRENT_TIME_AFTER_KICKOFF','La hora actual ya supera el kickoff de API. Para auditar una captura pasada utiliza --historical, sin tratar el resultado como prematch actual.')
        output['match'].update({'home':api.get('home_bwin') or api.get('home_api'),'away':api.get('away_bwin') or api.get('away_api'),
                                'fixture_id':api.get('fixture_id'),'kickoff_madrid':local(api.get('kickoff_utc')),'api_status':api.get('status')})
    identity=None
    if args.identity:
        identity_path=args.identity if args.identity.is_absolute() else root/args.identity
        output['files']['identity']=str(identity_path)
        try:
            identity=json.loads(read(identity_path))
            context_json_path=api_path.with_suffix('.json') if api_path else None
            context_json=json.loads(read(context_json_path)) if context_json_path and context_json_path.is_file() else None
            validate_identity(identity,context_json,event_id,bwin_url)
            if not api or str(api.get('fixture_id'))!=str(identity['api_fixture_id']):
                raise ValueError('Fixture ID contradice el TXT API')
            for side in ('home','away'):
                if str(api.get(side+'_id'))!=str(identity[side]['api_team_id']):
                    raise ValueError('Team ID contradice TXT API: '+side)
            if api.get('kickoff_utc') and abs((api['kickoff_utc']-iso_dt(identity['kickoff_utc'])).total_seconds())>60:
                raise ValueError('Kickoff contradice TXT API')
            issue(output,'INFO','CANONICAL_IDENTITY_VERIFIED',f"Fixture {identity['api_fixture_id']} vinculado al evento Bwin {event_id}.")
        except (ValueError,TypeError,OSError,KeyError,json.JSONDecodeError) as err:
            issue(output,'BLOCK','IDENTITY_INVALID',str(err))
            identity=None
    if odds and api:audit_odds(odds,output,event_id,api)
    if report and api:
        # El resto de verificaciones de reporte, XI y tiempos siguen en audit_report.
        audit_report(report,output,api,event_id)
        if identity and report.get('teams'):
            for idx,side in enumerate(('home','away')):
                result=compare_report_name(report['teams'][idx],identity[side],identity['away' if side=='home' else 'home'])
                if result in {'EXACT','ALIAS'}:
                    output['issues']=[item for item in output['issues'] if item.get('code')!='REPORT_WRONG_TEAM_'+side.upper()]
                    if result=='ALIAS':issue(output,'INFO','IDENTITY_ALIAS_'+side.upper(),f"Alias {report['teams'][idx]!r} confirmado por núcleo nominal completo del fixture.")
                elif result=='AMBIGUOUS':
                    issue(output,'BLOCK','IDENTITY_REPORT_AMBIGUOUS_'+side.upper(),'El nombre de reporte puede corresponder a más de un equipo del fixture.')
                else:
                    issue(output,'BLOCK','IDENTITY_REPORT_MISMATCH_'+side.upper(),f"Reporte: {report['teams'][idx]!r}; equipo fixture: {identity[side]['api_name']!r}.")

    if not api and (odds or report):
        issue(output,'BLOCK','NO_API_TO_COMPARE','Imposible comparar archivos sin contexto API válido.')
    if api:
        output['checks']['api']={k:(local(v) if isinstance(v,datetime) else v) for k,v in api.items()}
    if odds:
        output['checks']['odds']={k:(local(v) if isinstance(v,datetime) else v) for k,v in odds.items()}
    if report:
        output['checks']['report']={k:(local(v) if isinstance(v,datetime) else v) for k,v in report.items()}
        output['checks']['xi_reconciliation']={
            side: {
                'api_xi_reported_missing': bool(api.get('xi_api_home_missing' if side=='local' else 'xi_api_away_missing')) if api else None,
                'press_player_count':len(report['xi'].get(side,[])),
                'press_names':report['xi'].get(side,[]),
                'press_claimed_status':report['fields'].get('xi_'+side+'_estado') or 'NO_CALIFICADO',
                'source_url':report['fields'].get('xi_'+side+'_fuente'),
                'source_publication_madrid':local(iso_dt(report['fields'].get('xi_'+side+'_publicado_madrid'))),
                'identity_ids_verified':False,
                'comment':'El verificador no certifica que una lista periodística sea XI oficial ni que sus nombres correspondan a IDs únicos.'
            } for side in ('local','visitante')
        }
    blocks=[x for x in output['issues'] if x['severity']=='BLOCK']
    reviews=[x for x in output['issues'] if x['severity']=='REVIEW']
    output['status']='BLOQUEADO_PREPARTIDO' if blocks else 'REVISION_REQUERIDA' if reviews else 'APTO_VALIDACION_BASICA'
    output['note']='Auditoría local: no verifica automáticamente artículos, nombres abreviados, perfiles tácticos, datos nulos completos ni valor esperado.'
    outdir=args.out if args.out and args.out.is_absolute() else root/(args.out or Path('outputs/validation'))
    outdir.mkdir(parents=True,exist_ok=True)
    dest_json=outdir/f'validacion_{event_id}.json'
    dest_md=outdir/f'validacion_{event_id}.md'
    dest_json.write_text(json.dumps(output,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    dest_md.write_text(md_summary(output),encoding='utf-8')
    print(f'Bwin: {event_id} | {output["match"].get("home","?")} vs {output["match"].get("away","?")}')
    print('Inicio previsto (España):',output['match'].get('kickoff_madrid') or 'N/D')
    print(f'ESTADO: {output["status"]} | BLOCK={len(blocks)} REVIEW={len(reviews)}')
    for item in output['issues']:
        if item['severity']!='INFO': print(f'[{item["severity"]}] {item["code"]}: {item["detail"]}')
    print('JSON:',dest_json.relative_to(root) if dest_json.is_relative_to(root) else dest_json)
    print('Markdown:',dest_md.relative_to(root) if dest_md.is_relative_to(root) else dest_md)
    return 2 if blocks else 1 if reviews else 0


if __name__=='__main__':
    raise SystemExit(main())