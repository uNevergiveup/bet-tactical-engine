#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Motor estadístico PRE-Codex V3.4.1: datos originales, no auditoría de Codex.

Python >= 3.10; biblioteca estándar. No usa red. No calcula p_model, EV ni stake.
Salidas por evento: ranking_probabilistico_<id>.json/.md/.csv,
matriz_evidencia_<id>.json, hipotesis_cruzadas_<id>.md.

Ejecutar desde la raíz del proyecto: python motor_probabilistico.py
También admite --odds RUTA --context RUTA --validation RUTA; --no-odds permite estudio sin cuotas.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = '3.4.1-pre-codex'


def norm(v):
    s = unicodedata.normalize('NFKD', str(v).casefold())
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(x for x in s if not unicodedata.combining(x))).strip()


def stamp(v):
    try:
        x = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return x.astimezone(timezone.utc) if x.tzinfo else None
    except (TypeError, ValueError):
        return None


def field(text, pat):
    m = re.search(pat, text, re.M | re.I)
    return m.group(1).strip() if m else None


def first_obj(text, title):
    """Extraer únicamente el primer objeto JSON completo tras cabecera estructurada."""
    h = re.search(r'^' + re.escape(title) + r'\s*\n=+\s*$', text, re.M)
    if not h:
        return None
    start = text.find('{', h.end())
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_odds(path):
    txt = path.read_text(encoding='utf-8-sig', errors='replace')
    ev = field(txt[:3000], r'^Event ID:\s*(\d+)\s*$')
    if not ev:
        raise ValueError(f'Sin Event ID en cuotas: {path}')
    capture = field(txt[:3000], r'^Captura:\s*(\S+)')
    blocks = []
    current = None
    for line in txt.splitlines():
        m = re.match(r'^MERCADO:\s*(.+)$', line, flags=re.I)
        if m:
            current = {'market': m[1].strip(), 'selections': []}
            blocks.append(current)
        elif current is not None:
            # Sin convertir coma dentro del nombre en separador; decimal punto o coma.
            x = re.match(r'^\s*(.+?)\s+:\s+(\d+(?:[.,]\d{1,3})?)\s*$', line)
            if x:
                odd = float(x[2].replace(',', '.'))
                current['selections'].append({'selection': x[1].strip(), 'odd': odd})
    return {'event_id': ev, 'capture': capture, 'markets': blocks}


TEAM_STAT = {
    'corner kicks': 'corner_kicks', 'total shots': 'total_shots',
    'shots on goal': 'shots_on_goal', 'shots off goal': 'shots_off_goal',
    'blocked shots': 'blocked_shots', 'fouls': 'fouls',
    'offsides': 'offsides', 'yellow cards': 'yellow_cards',
    'red cards': 'red_cards', 'total passes': 'passes',
    'ball possession': 'possession', 'goalkeeper saves': 'saves',
    'expected goals': 'xg', 'expected_goals': 'xg',
}
PLAYER_PATHS = {
    'shots': ('shots', 'total'), 'sot': ('shots', 'on'),
    'goals': ('goals', 'total'), 'assists': ('goals', 'assists'),
    'passes': ('passes', 'total'), 'key_passes': ('passes', 'key'),
    'fouls_committed': ('fouls', 'committed'),
    'fouls_drawn': ('fouls', 'drawn'),
    'tackles': ('tackles', 'total'),
    'interceptions': ('tackles', 'interceptions'),
    'saves': ('goals', 'saves'), 'yellow': ('cards', 'yellow'),
    'offsides': ('offsides',), 'dribbles': ('dribbles', 'attempts'),
}


def numbers_from_block(body, heading):
    """Sección acotada por próximo título en mayúsculas; jamás incluir RIVAL en EQUIPO."""
    m = re.search(r'^' + re.escape(heading) + r'\s*$', body, re.M)
    if not m:
        return {}
    out = {}
    for line in body[m.end():].splitlines():
        # Toda nueva cabecera o línea separadora termina el bloque.
        if not line.strip():
            if out:
                break
            continue
        if not line.startswith((' ', '\t')):
            break
        x = re.match(r'^\s{2,}([^:\n]+):\s*(-?\d+(?:\.\d+)?)%?\s*$', line)
        if x:
            key = TEAM_STAT.get(norm(x[1]).replace(' ', ' '))
            if key:
                out[key] = float(x[2])
    return out


def player_value(stat, path):
    item = stat
    for key in path:
        if not isinstance(item, dict):
            return None
        item = item.get(key)
    return float(item) if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) else None


def parse_context(path):
    txt = path.read_text(encoding='utf-8-sig', errors='replace')
    header = txt[:17000]
    event = field(header, r'^Event ID:\s*(\d+)\s*$')
    fixture = re.search(r'^FIXTURE RESUELTO\s*\n=+\s*\n(.*?)(?=\n=+)', header, re.M | re.S)
    if not fixture:
        raise ValueError('Contexto sin FIXTURE RESUELTO')
    h = field(fixture[1], r'^Home:\s*(.+?)\s*\(\d+\)\s*$')
    a = field(fixture[1], r'^Away:\s*(.+?)\s*\(\d+\)\s*$')
    home_id = field(fixture[1], r'^Home:\s*.+?\s*\((\d+)\)\s*$')
    away_id = field(fixture[1], r'^Away:\s*.+?\s*\((\d+)\)\s*$')
    kickoff = field(fixture[1], r'^Date UTC:\s*(\S+)')
    fixture_id = field(fixture[1], r'^Fixture ID:\s*(\d+)')
    target_competition = field(fixture[1], r'^League:\s*(.+)$')
    if not (h and a and home_id and away_id and kickoff and stamp(kickoff)):
        raise ValueError('Fixture objetivo sin equipos, IDs o fecha comprobable')
    target = {'home': h, 'away': a, 'home_id': home_id, 'away_id': away_id,
              'home_bwin': field(header, r'^Home Bwin:\s*(.+)$'),
              'away_bwin': field(header, r'^Away Bwin:\s*(.+)$'),
              'kickoff': kickoff, 'fixture_id': fixture_id, 'event_id': event,
              'target_competition': target_competition}
    xi = first_obj(txt, 'LINEUPS DEL FIXTURE OBJETIVO')
    target_players = {}
    xi_quality = {
        'home': {'team': h, 'reported': False, 'starter_rows': 0, 'numeric_ids': 0,
                 'missing_id_names': [], 'duplicate_ids': [], 'duplicate_shirt_numbers': [],
                 'status': 'XI_NO_DISPONIBLE'},
        'away': {'team': a, 'reported': False, 'starter_rows': 0, 'numeric_ids': 0,
                 'missing_id_names': [], 'duplicate_ids': [], 'duplicate_shirt_numbers': [],
                 'status': 'XI_NO_DISPONIBLE'},
    }
    for t in ((xi or {}).get('response') or []):
        team_id = str((t.get('team') or {}).get('id'))
        if team_id not in (home_id, away_id):
            continue
        side = 'home' if team_id == home_id else 'away'
        starts = [(v.get('player') or {}) for v in (t.get('startXI') or [])]
        id_list = [str(pl['id']) for pl in starts if pl.get('id') is not None]
        number_list = [str(pl['number']) for pl in starts if pl.get('number') is not None]
        quality = xi_quality[side]
        quality['lineup_coach'] = (t.get('coach') or {}).get('name')
        quality['formation'] = t.get('formation')
        quality.update(reported=True, starter_rows=len(starts), numeric_ids=len(id_list),
                       missing_id_names=[pl.get('name') for pl in starts if pl.get('id') is None],
                       duplicate_ids=sorted({pid for pid in id_list if id_list.count(pid)>1}),
                       duplicate_shirt_numbers=sorted({n for n in number_list if number_list.count(n)>1}))
        quality['status'] = ('XI_NOMINAL_INCOMPLETO' if len(starts) != 11 else
                             'XI_NOMINAL_COMPLETO_IDS_PARCIALES' if len(id_list)!=11 or
                                                                    quality['duplicate_ids'] or quality['duplicate_shirt_numbers'] else
                             'XI_ESTRUCTURADO_COMPLETO')
        for category, key in [('starter', 'startXI'), ('bench', 'substitutes')]:
            for wrap in t.get(key, []) or []:
                pl = wrap.get('player') or {}
                pid = pl.get('id')
                if pid is not None:
                    previous = target_players.get(str(pid))
                    if previous and previous['status'] == 'starter' and category == 'bench':
                        xi_quality[side].setdefault('other_warnings', []).append(
                            {'type': 'ID_BOTH_XI_AND_BENCH', 'id': str(pid),
                             'action': 'MANUAL_REVIEW_NO_OVERWRITE'})
                        continue
                    if previous and previous['side'] != side:
                        xi_quality[side].setdefault('other_warnings', []).append(
                            {'type': 'ID_IN_BOTH_TEAMS', 'id': str(pid),
                             'action': 'MANUAL_REVIEW_NO_OVERWRITE'})
                        continue
                    target_players[str(pid)] = {'name': pl.get('name'), 'team': target[side],
                                                'side': side, 'status': category, 'pos': pl.get('pos'),
                                                'grid': pl.get('grid'), 'jersey': pl.get('number')}

    # Plantillas secundarias: diferencias por ID+dorsal se reportan, NUNCA corrigen el proveedor.
    roster = {'home': {}, 'away': {}}
    squad_zone = re.search(r'^PLANTILLAS ACTUALES — SQUADS\s*\n=+\s*\n(.*?)(?=^ENTRENADORES\s*\n=+)', txt, re.M | re.S)
    if squad_zone:
        current_side = None
        for line in squad_zone[1].splitlines():
            if line.strip() in ('HOME','AWAY'):
                current_side=line.strip().lower()
            elif current_side:
                found=re.match(r'^\s+ID=(\d+)\s*\|\s*(.+?)\s*\|\s*age=.*?\|\s*number=(\S+)\s*\|\s*position=(.+)$',line)
                if found:
                    roster[current_side][found[1]]={'name': found[2].strip(), 'number': found[3], 'position': found[4].strip()}
    for side in ('home','away'):
        quality=xi_quality[side]
        quality['squad_crosscheck'] = []
        team_id=home_id if side=='home' else away_id
        for block in ((xi or {}).get('response') or []):
            if str((block.get('team') or {}).get('id')) != team_id:
                continue
            for wrap in block.get('startXI') or []:
                player=wrap.get('player') or {}
                pid=player.get('id')
                num=player.get('number')
                ref=roster[side].get(str(pid)) if pid is not None else None
                if ref and num is not None and ref['number'] not in ('None','null') and str(num) != ref['number']:
                    alternatives=[{'id':candidate_id,'name':candidate['name']} for candidate_id,candidate in roster[side].items() if candidate['number']==str(num)]
                    quality['squad_crosscheck'].append({'type':'ID_NUMBER_CONFLICT', 'lineup_id':pid,
                                                        'lineup_name':player.get('name'), 'lineup_number':num,
                                                        'squad_number':ref['number'], 'squad_name':ref['name'],
                                                        'same_number_candidates':alternatives, 'action':'MANUAL_REVIEW_NO_REASSIGN'})
                elif pid is None and num is not None:
                    alternatives=[{'id':candidate_id,'name':candidate['name']} for candidate_id,candidate in roster[side].items() if candidate['number']==str(num)]
                    if alternatives:
                        quality['squad_crosscheck'].append({'type':'MISSING_ID_WITH_SQUAD_JERSEY_CANDIDATE',
                                                            'lineup_name':player.get('name'), 'lineup_number':num,
                                                            'candidates':alternatives,'action':'MANUAL_REVIEW_NO_REASSIGN'})
                # Discrepancias de rol GRAVES (pos de la alineación vs posición
                # amplia de plantilla). M/F pueden variar tácticamente: NO bloquear.
                if ref:
                    lineup_pos = str(player.get('pos') or '').strip().upper()
                    squad_pos = norm(ref['position'])
                    role_map = {'goalkeeper':'G','goleiro':'G','portero':'G',
                                'defender':'D','defensa':'D',
                                'midfielder':'M','mediocampista':'M',
                                'attacker':'F','forward':'F','delantero':'F'}
                    roster_pos = role_map.get(squad_pos)
                    if (roster_pos and lineup_pos and (((lineup_pos == 'G') != (roster_pos == 'G')) or
                                                       (lineup_pos, roster_pos) in {('D','F'),('F','D')})):
                        quality['squad_crosscheck'].append({
                            'type':'POSITION_ROLE_CONFLICT', 'lineup_id':pid,
                            'lineup_name':player.get('name'), 'lineup_pos':lineup_pos,
                            'lineup_grid':player.get('grid'), 'squad_position':ref['position'],
                            'note':'Comparar con prensa y roles por fase. No reasignar posición/ID automáticamente.',
                            'action':'TACTICAL_REVIEW_NO_AUTOCORRECTION'})

    matches = list(re.finditer(r'^PARTIDO HISTÓRICO P-(\d+)\s+—\s+(.+?)\s*\n=+\s*$', txt, re.M))
    if not matches:
        raise ValueError('No hay PARTIDO HISTÓRICO P-n en contexto')
    histories = defaultdict(list)
    for i, match in enumerate(matches):
        body = txt[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(txt)]
        # El último histórico debe terminar ante una nueva cabecera global.
        body = re.split(r'\n={20,}\s*\n(?:AUDITORÍA|LINEUPS DEL FIXTURE OBJETIVO|STATISTICS DEL FIXTURE OBJETIVO|PREDICTIONS|INJURIES|ODDS)', body, maxsplit=1)[0]
        team_name = match[2].strip()
        team = h if norm(team_name) == norm(h) else a if norm(team_name) == norm(a) else None
        if not team or int(match[1]) > 6:
            continue
        fid = field(body[:1200], r'^Fixture ID:\s*(\d+)')
        dt = field(body[:1200], r'^Fecha:\s*(\S+)')
        cond = field(body[:1200], r'^Condición:\s*(\w+)')
        competition = field(body[:1200], r'^Competición:\s*(.+)$')
        if not (fid and dt and cond and stamp(dt)):
            continue
        own = numbers_from_block(body, 'ESTADÍSTICAS COLECTIVAS — EQUIPO')
        opp = numbers_from_block(body, 'ESTADÍSTICAS COLECTIVAS — RIVAL')
        rec = {'fixture_id': fid, 'date': dt, 'p': int(match[1]),
               'condition': cond.lower(), 'competition': competition,
               'same_competition': bool(competition and target_competition and norm(competition) == norm(target_competition)),
               'team': team,
               'metrics': own, 'opponent_metrics': opp, 'players': {}}
        score = field(body[:1200], r'^Resultado:\s*(\d+\s*-\s*\d+)')
        if score:
            rec['score'] = [int(v.strip()) for v in score.split('-')]
        # Acotar estrictamente las estadísticas de jugadores de EQUIPO, no las de RIVAL.
        ps = re.search(r'^ESTADÍSTICAS DE TODOS LOS JUGADORES DEL PARTIDO\s*$', body, re.M)
        if ps:
            remaining = body[ps.end():]
            own_section = re.search(r'^\s*--- EQUIPO \(\d+\) ---\s*$', remaining, re.M)
            rival_section = re.search(r'^\s*--- RIVAL \(\d+\) ---\s*$', remaining, re.M)
            if own_section:
                portion = remaining[own_section.end():rival_section.start() if rival_section else len(remaining)]
                for line in portion.splitlines():
                    s = line.strip()
                    if not s.startswith('{"player":'):
                        continue
                    try:
                        obj = json.loads(s.rstrip(','))
                    except json.JSONDecodeError:
                        continue
                    p = obj.get('player') or {}
                    stat = obj.get('statistics') or {}
                    minutes = player_value(stat, ('games', 'minutes'))
                    if p.get('id') is None or not minutes or minutes <= 0:
                        continue
                    rec['players'][str(p['id'])] = {
                        'id': str(p['id']), 'name': p.get('name'), 'minutes': minutes,
                        'metrics': {key: player_value(stat, route) for key, route in PLAYER_PATHS.items()}}
        histories[norm(team)].append(rec)
    ko = stamp(kickoff)
    for team in (h, a):
        seen = set()
        clean = []
        for r in sorted(histories.get(norm(team), []), key=lambda z: z['p']):
            if r['fixture_id'] in seen or stamp(r['date']) >= ko:
                continue
            seen.add(r['fixture_id'])
            clean.append(r)
        histories[norm(team)] = clean
    return {'target': target, 'target_players': target_players, 'xi_quality': xi_quality, 'histories': dict(histories)}


def load_validation(path, eid):
    if not path or not path.is_file():
        return {'state': 'SIN_VALIDACION', 'issues': [], 'files': {}}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    veid = str((data.get('match') or {}).get('event_id') or '')
    if veid and veid != eid:
        raise ValueError(f'Validación event ID {veid} != {eid}')
    return {'state': data.get('status') or 'DESCONOCIDO',
            'issues': data.get('issues') or [], 'files': data.get('files') or {},
            'checks': data.get('checks') or {}}


def get_event_from_constants():
    p = Path('config/constants.py')
    if not p.is_file():
        return None
    txt = p.read_text(encoding='utf-8-sig')
    url = field(txt, r'''^\s*BWIN_EVENT_URL\s*=\s*["']([^"']+)["']''')
    return field(url or '', r':(\d+)(?:[?#]|$)')


def read_header_event(path):
    with path.open(encoding='utf-8-sig', errors='replace') as f:
        head = ''.join(f.readline() for _ in range(110))
    return field(head, r'^Event ID:\s*(\d+)\s*$')


def paths_for_event(args):
    """Resolver por Event ID, no por orden de archivos ni nombre de otro partido.

    Sin cuotas se permite investigación estadística. Las líneas de apuesta solo
    existen cuando Bwin las incluye efectivamente en una captura del evento.
    """
    # En ejecución multip partido, el Event ID explícito tiene prioridad.
    # constants.py solo es fallback en modo tradicional sin --event-id.
    expected = str(args.event_id) if args.event_id is not None else get_event_from_constants()
    if args.validation is None and expected:
        candidate = Path(f'outputs/validation/validacion_{expected}.json')
        if candidate.is_file():
            args.validation = candidate
    validation = None
    if args.validation is not None:
        if not args.validation.is_file():
            raise ValueError(f'Validación explícita no encontrada: {args.validation}')
        validation = json.loads(args.validation.read_text(encoding='utf-8-sig'))
        veid = str((validation.get('match') or {}).get('event_id') or '') or None
        if veid and expected and expected != veid:
            raise ValueError(f'Event ID solicitado {expected} contradice validación {veid}')
        expected = expected or veid

    def available(kind):
        bases = ([Path('outputs/scrapers'), Path('outputs/odds')] if kind == 'odds'
                 else [Path('outputs/data')])
        pattern = 'cuotas_bwin*.txt' if kind == 'odds' else 'match_engine*.txt'
        return sorted({p for b in bases if b.is_dir() for p in b.rglob(pattern)
                       if 'cache' not in p.parts})

    def resolve(kind, explicit, optional=False):
        if explicit is not None:
            if not explicit.is_file():
                raise ValueError(f'Archivo {kind} no existe: {explicit}')
            return explicit
        stored = (validation or {}).get('files') or {}
        path = Path(stored[kind]) if stored.get(kind) else None
        if path is not None and path.is_file() and (not expected or read_header_event(path) == expected):
            return path
        matches = [p for p in available(kind) if not expected or read_header_event(p) == expected]
        if len(matches) == 1:
            return matches[0]
        if optional and not matches:
            return None
        if optional and len(matches) > 1:
            # No escoger cuotas ambiguas; conservar análisis sin cuotas.
            print(f'[WARN] {len(matches)} archivos de cuotas posibles; no se seleccionan automáticamente.')
            return None
        raise ValueError(f'{len(matches)} archivos {kind} del evento {expected or "sin ID"}; usa --{"odds" if kind == "odds" else "context"} RUTA')

    context_path = resolve('api', args.context)
    event_context = read_header_event(context_path)
    if expected and event_context != expected:
        raise ValueError(f'El contexto pertenece a {event_context}, se esperaba {expected}')
    expected = expected or event_context
    odds_path = None if args.no_odds else resolve('odds', args.odds, optional=True)
    if odds_path is not None and read_header_event(odds_path) != expected:
        raise ValueError('Cuotas y contexto de partidos diferentes')
    return odds_path, context_path, args.validation, expected


def rate(vals, mode, limit):
    if not vals:
        return None
    passed = sum((v >= limit if mode == 'at_least' else v > limit if mode == 'over' else v < limit) for v in vals)
    return {'n': len(vals), 'hits': passed, 'frequency': round(passed / len(vals), 4),
            'mean': round(statistics.mean(vals), 3), 'values': vals}


def point_series(records, metric, side='own', player_id=None):
    rows = []
    for r in records:
        if player_id:
            p = r['players'].get(player_id)
            val = (p or {}).get('metrics', {}).get(metric)
            minutes = p['minutes'] if p else None
        elif metric in ('total_goals', 'goals_for', 'goals_against'):
            score = r.get('score')
            val = ((sum(score) if metric == 'total_goals' else
                    score[0] if metric == 'goals_for' else score[1])
                   if score and len(score) == 2 else None)
            minutes = None
        elif side == 'combined':
            aa, bb = r['metrics'].get(metric), r['opponent_metrics'].get(metric)
            val = aa + bb if aa is not None and bb is not None else None
            minutes = None
        else:
            val = (r['opponent_metrics'] if side == 'opponent' else r['metrics']).get(metric)
            minutes = None
        rows.append({'fixture_id': r['fixture_id'], 'p': r['p'],
                     'condition': r['condition'], 'date': r['date'],
                     'competition': r.get('competition'),
                     'same_competition': r.get('same_competition'),
                     'value': val, 'minutes': minutes,
                     'present': bool(player_id and player_id in r['players']) if player_id else None})
    return rows


def summarize_rows(rows, mode, threshold, scope):
    good = [r for r in rows if r['value'] is not None]
    b = rate([r['value'] for r in good], mode, threshold)
    same_comp = [r for r in good if r.get('same_competition') is True]
    other_comp = [r for r in good if r.get('same_competition') is False]
    return {'scope': scope, 'sample_total': len(rows), 'sample_valid': len(good),
            'sample_missing': len(rows) - len(good), 'series': rows,
            'descriptive': b,
            'same_competition': {'n': len(same_comp),
                                 'descriptive': rate([r['value'] for r in same_comp], mode, threshold)},
            'other_competitions': {'n': len(other_comp),
                                   'names': sorted({r['competition'] for r in other_comp if r.get('competition')})},
            'home': rate([r['value'] for r in good if r['condition'] == 'home'], mode, threshold),
            'away': rate([r['value'] for r in good if r['condition'] == 'away'], mode, threshold)}


def identify_market(market, selection, teams, bwin_aliases=None):
    """Analiza mercados sin confundir totales con producción de un equipo.

    Solo una cabecera de equipo resuelta inequívocamente habilita evidencia
    individual. Si no se resuelve, se conserva en el inventario sin frecuencia.
    """
    m, s = norm(market), norm(selection)
    if not m or not s:
        return {'reason': 'MERCADO_VACIO'}
    partial = ('entre el', 'al min', 'del minuto', 'desde el minuto',
               '1a parte', '2a parte', '1 parte', '2 parte',
               '1er tiempo', '2do tiempo', 'primer tiempo', 'segundo tiempo',
               '1er periodo', '2o periodo', 'descanso', 'primera mitad',
               'segunda mitad', '1 mitad', '2 mitad', 'primera parte', 'segunda parte',
               'primeros 15', 'primeros 30', 'minutos', ' intervalo ',
               'hasta el min', 'a los 15', 'a los 30')
    if any(t in m for t in partial):
        return {'reason': 'PERIODO_NO_SOPORTADO_SIN_SERIE_TEMPORAL'}
    combination = ('crea tu', 'crea tus', 'cuotas especiales', 'apuestas recomendadas',
                   'handicap', 'ambos equipos marcan y', 'resultado del partido y',
                   'resultado y', ' y total de ', ' y ambos ', 'pase y gol',
                   'cuotas mejoradas', 'crack bwin', 'marcador exacto',
                   'doble oportunidad y', 'multigol', 'resultado y total',
                   'marca y gana', 'par impar', 'ambos equipos marcan')
    if any(t in m for t in combination):
        return {'reason': 'COMBINADO_O_SEMANTICA_NO_SOPORTADA'}
    if m.startswith('jugador '):
        mm = re.search(r'\((\d+)\+\)', market)
        if not mm:
            return {'reason': 'LINEA_JUGADOR_NO_RECONOCIDA'}
        metric = None
        if 'tiros a porteria' in m or 'remates a puerta' in m:
            metric = 'sot'
        elif 'total de tiros' in m or 'total de remates' in m:
            metric = 'shots'
        elif 'faltas recibidas' in m:
            metric = 'fouls_drawn'
        elif 'total de faltas' in m:
            metric = 'fouls_committed'
        elif 'total de asistencias' in m:
            metric = 'assists'
        elif 'total de pases' in m:
            metric = 'passes'
        elif 'total de tackles' in m or 'total de entradas' in m:
            metric = 'tackles'
        elif 'total de fueras de juego' in m:
            metric = 'offsides'
        if metric:
            return {'type': 'player', 'metric': metric, 'mode': 'at_least',
                    'threshold': int(mm[1]), 'player_label': selection,
                    'settlement': 'UMBRAL_N_MAS'}
        return {'reason': 'PROP_NO_IMPLEMENTADO'}
    mapping = [('total de corners', 'corner_kicks'), ('total de saques de esquina', 'corner_kicks'),
               ('total de tiros a porteria', 'shots_on_goal'), ('total de remates a puerta', 'shots_on_goal'),
               ('total de tiros', 'total_shots'), ('total de remates', 'total_shots'),
               ('total de faltas', 'fouls'), ('total de fueras de juego', 'offsides'),
               ('total de fuera de juego', 'offsides'), ('total de tarjetas amarillas', 'yellow_cards'),
               ('total de goles', 'total_goals')]
    metric = next((v for k, v in mapping if k in m), None)
    if metric is None:
        return {'reason': 'MERCADO_NO_IMPLEMENTADO'}
    line = re.search(r'\b(m[aá]s de|menos de|over|under)\s*(\d+(?:[.,]\d+)?)', selection, re.I)
    if not line:
        return {'reason': 'SELECCION_NO_OVER_UNDER'}
    threshold = float(line[2].replace(',', '.'))
    if threshold.is_integer():
        return {'reason': 'LINEA_ENTERA_PUSH_NO_VERIFICADO'}
    mode = 'over' if norm(line[1]) in ('mas de', 'over') else 'under'

    # Cabeceras por equipo en Bwin usan "Nombre - Total de ...". El guion
    # establece el propietario y evita falsos positivos por prefijos parciales.
    prefix = re.match(r'^(.+?)\s+[-–—]\s+(?=total de )', market, flags=re.I)
    owner = None
    if prefix:
        name = norm(prefix[1])
        aliases = bwin_aliases or {}
        normalized_candidates = {}
        for team in teams:
            variants = {norm(team)}
            for text in (aliases.get(team) or []):
                if text:
                    variants.add(norm(text))
            # Solo eliminar siglas institucionales, sin retirar nombres de ciudades.
            institutional = {'fc', 'cf', 'ca', 'cd', 'sc', 'ac'}
            for t in list(variants):
                tokens = t.split()
                stripped = ' '.join(x for x in tokens if x not in institutional)
                if stripped:
                    variants.add(stripped)
            for alias in variants:
                normalized_candidates.setdefault(alias, set()).add(team)
        matched = normalized_candidates.get(name, set())
        if len(matched) != 1:
            return {'reason': 'EQUIPO_EN_CABECERA_NO_RESUELTO', 'team_label': prefix[1]}
        owner = next(iter(matched))
    if owner and metric == 'total_goals':
        metric = 'goals_for'
    return {'type': 'team' if owner else 'total', 'metric': metric,
            'owner': owner, 'mode': mode, 'threshold': threshold,
            'settlement': 'OVER_UNDER_MEDIA_LINEA'}


def verified_market_aliases(ctx):
    """Alias de cabecera Bwin, restringidos a IDs del fixture y al nombre Bwin."""
    t = ctx['target']
    result = {}
    for side in ('home', 'away'):
        team = t[side]
        raw = t.get(side + '_bwin') or ''
        variants = [raw]
        # Bwin añade sufijos de liga/deporte después del club.
        # Solo separar el sufijo editorial de competición/deporte. No inferir
        # equivalencias entre clubes distintos; exige fixture/ID ya resueltos.
        root = re.split(r'\s+(?:brasileiro|brasileira|serie|primera|saudi|pro\s+league|liga|futbol|futebol)\b',
                        raw, maxsplit=1, flags=re.I)[0]
        if root and root != raw:
            variants.append(root)
        # El club carioca ID 120 tiene esta abreviatura institucional en Bwin.
        # NO se extiende a Botafogo PB/SP ni otros equipos homónimos.
        if str(t.get(side+'_id')) == '120' and 'botafogo' in norm(raw) and re.search(r'\brj\b', norm(raw)):
            variants += ['Botafogo RJ', 'Botafogo FR RJ', 'Botafogo de Futebol e Regatas']
        result[team] = variants
    return result


def resolve_player(label, context):
    target = context['target_players']
    wanted = norm(label)
    matches = [(pid, p) for pid, p in target.items() if norm(p['name']) == wanted]
    if len(matches) == 1:
        return matches[0]
    # Utilizar SOLO nombres históricos vinculados al ID del XI objetivo; no emparejar por apellido ambiguo.
    if not matches:
        names = defaultdict(set)
        for rset in context['histories'].values():
            for r in rset:
                for pid, p in r['players'].items():
                    if pid in target and norm(p['name']) == wanted:
                        names[pid].add(p['name'])
        if len(names) == 1:
            pid = next(iter(names))
            return pid, target[pid]
    # Si todavía no hay XI en API, permitir investigación individual por identidad
    # exacta del histórico. Nunca declarar titularidad por esta coincidencia.
    if not target:
        candidates = {}
        home, away = context['target']['home'], context['target']['away']
        for team in (home, away):
            for rec in context['histories'].get(norm(team), []):
                for pid, p in rec['players'].items():
                    if norm(p['name']) == wanted:
                        candidates.setdefault(pid, {'name': p['name'], 'team': team,
                                                    'side': 'home' if team == home else 'away',
                                                    'status': 'HISTORICO_XI_OBJETIVO_DESCONOCIDO'})
        if len(candidates) == 1:
            return next(iter(candidates.items()))
    return None, None


def evidence_for(spec, context):
    home, away = context['target']['home'], context['target']['away']
    histories = context['histories']
    mode, threshold, metric = spec['mode'], spec['threshold'], spec['metric']
    groups = []
    if spec['type'] == 'player':
        pid, pl = resolve_player(spec['player_label'], context)
        if not pid:
            return {'reason': 'IDENTIDAD_JUGADOR_NO_RESUELTA_EN_XI', 'groups': []}
        own = histories.get(norm(pl['team']), [])
        rows = point_series(own, metric, player_id=pid)
        # Apareció, pero atributo null: NO imputar cero.
        played = [r for r in rows if r['present']]
        evidence = summarize_rows(played, mode, threshold, f'jugador {pid} | {pl["team"]}')
        evidence.update({'player_id': pid, 'player_name': pl['name'],
                         'xi_status': pl['status'], 'appearances': len(played),
                         'historical_games_checked': len(own)})
        groups.append(evidence)
        opp_team = away if norm(pl['team']) == norm(home) else home
        collective_metric = {'shots': 'total_shots', 'sot': 'shots_on_goal',
                             'fouls_committed': 'fouls', 'fouls_drawn': 'fouls',
                             'offsides': 'offsides', 'goals': 'goals_for'}.get(metric)
        if collective_metric:
            groups.append(summarize_rows(point_series(histories.get(norm(opp_team), []),
                                                      collective_metric, side='opponent'),
                                         mode, threshold, f'rival {opp_team} | métrica colectiva permitida (NO jugador)'))
    elif spec['type'] == 'team':
        owner = spec['owner']
        if not owner:
            return {'reason': 'EQUIPO_INDETERMINADO', 'groups': []}
        opp_team = away if norm(owner) == norm(home) else home
        own_series = point_series(histories.get(norm(owner), []), metric, side='own')
        if metric == 'goals_for':
            opp_series = point_series(histories.get(norm(opp_team), []), 'goals_against')
        else:
            opp_series = point_series(histories.get(norm(opp_team), []), metric, side='opponent')
        groups = [summarize_rows(own_series, mode, threshold, f'{owner} produce'),
                  summarize_rows(opp_series, mode, threshold, f'{opp_team} concede')]
    else:
        for team in (home, away):
            if metric == 'total_goals':
                rows = point_series(histories.get(norm(team), []), metric)
            else:
                rows = point_series(histories.get(norm(team), []), metric, side='combined')
            groups.append(summarize_rows(rows, mode, threshold, f'total partido histórico de {team}'))
    return {'reason': None, 'groups': groups}


def quick_fragility(spec):
    metric = spec.get('metric', '')
    mode = spec.get('mode')
    if metric in ('corner_kicks', 'total_shots', 'shots', 'sot', 'shots_on_goal'):
        return {'heuristic_1_5': 5 if mode == 'under' else 4,
                'warning': 'Cambios de marcador, roja y ritmo pueden alterar volumen; no calibrado'}
    if metric in ('fouls_committed', 'fouls_drawn', 'fouls'):
        return {'heuristic_1_5': 4, 'warning': 'Depende de emparejamientos, minutos y arbitraje; no calibrado'}
    if metric == 'offsides':
        return {'heuristic_1_5': 4, 'warning': 'Depende de altura de línea, desmarques, posesión y criterio arbitral'}
    return {'heuristic_1_5': 4, 'warning': 'Revisar guiones alternativos; heurística NO predictiva'}


def independent_player_patterns(context):
    """Descubrimiento de jugadores AUN SIN props/cuotas; no afirma titularidad.

    Cobertura distingue ausencias de juego (no muestra) y nulos dentro de apariciones.
    """
    result = []
    relevant = ('shots', 'sot', 'fouls_committed', 'fouls_drawn',
                'passes', 'tackles', 'offsides', 'assists', 'saves')
    target = context['target_players']
    for team in (context['target']['home'], context['target']['away']):
        records = context['histories'].get(norm(team), [])
        players = {}
        # Diferenciar XI no disponible del jugador ausente del XI/banquillo del fixture.
        complete_team_xi = sum(1 for x in target.values() if norm(x.get('team')) == norm(team) and x.get('status') == 'starter') == 11
        for rec in records:
            for pid, player in rec['players'].items():
                obj = players.setdefault(pid, {'id': pid, 'name': player['name'],
                                               'team': team, 'appearances': 0,
                                               'minutes': [], 'metrics': {metric: [] for metric in relevant},
                                               'xi_status': target[pid]['status'] if pid in target else ('NO_LISTADO_EN_XI_OBJETIVO' if complete_team_xi else 'NO_CONFIRMADO_XI_OBJETIVO')})
                obj['appearances'] += 1
                obj['minutes'].append(player['minutes'])
                for metric in relevant:
                    obj['metrics'][metric].append(player['metrics'].get(metric))
        for player in players.values():
            player['history_total'] = len(records)
            player['avg_minutes_per_appearance'] = round(statistics.mean(player['minutes']), 2)
            details = {}
            for metric, values in player.pop('metrics').items():
                observed = [v for v in values if v is not None]
                details[metric] = {'n_valid': len(observed),
                                   'n_missing_among_appearances': len(values) - len(observed),
                                   'n_appearances': player['appearances'],
                                   'values_observed': observed,
                                   'hit_at_least_1': sum(v >= 1 for v in observed),
                                   'mean_when_observed': round(statistics.mean(observed), 3) if observed else None}
            player['metric_coverage'] = details
            result.append(player)
    result.sort(key=lambda p: (p['team'], p['xi_status'] != 'starter',
                               -p['appearances'], -sum(p['minutes']), p['name']))
    return result


def cross_hypotheses(context, odds):
    """Patrones cruzados descriptivos aun cuando no existe mercado ofrecido."""
    h, a = context['target']['home'], context['target']['away']
    histories = context['histories']
    markets = [norm(b['market']) for b in odds['markets']]
    patterns = []
    for attacking, defending in ((h, a), (a, h)):
        for name, metric, label in [
            ('offsides', 'offsides', 'fuera de juego'),
            ('corner_kicks', 'corner_kicks', 'córners'),
            ('fouls', 'fouls', 'faltas'),
            ('shots_on_goal', 'shots_on_goal', 'tiros a puerta'),
            ('total_shots', 'total_shots', 'tiros totales'),
            ('yellow_cards', 'yellow_cards', 'tarjetas amarillas'),
            ('goals_for', 'goals_for', 'goles')]:
            x = point_series(histories.get(norm(attacking), []), metric, side='own')
            y = (point_series(histories.get(norm(defending), []), 'goals_against')
                 if metric == 'goals_for' else
                 point_series(histories.get(norm(defending), []), metric, side='opponent'))
            xs = [r['value'] for r in x if r['value'] is not None]
            ys = [r['value'] for r in y if r['value'] is not None]
            if not xs or not ys:
                continue
            # Es una hipótesis, no una cuota, ni mezcla aritmética de promedios.
            available = any((('fuera' in market or 'offside' in market) if metric == 'offsides' else
                             ('corner' in market) if metric == 'corner_kicks' else
                             ('falta' in market) if metric == 'fouls' else
                             ('tiros a porteria' in market) if metric == 'shots_on_goal' else
                             ('tarjeta' in market) if metric == 'yellow_cards' else
                             ('gol' in market) if metric == 'goals_for' else
                             ('tiros' in market or 'remates' in market)) for market in markets)
            patterns.append({'attacking_team': attacking, 'defending_team': defending,
                             'metric': name, 'label': label,
                             'attack_production': {'n': len(xs), 'values': xs, 'mean': round(statistics.mean(xs), 3)},
                             'defense_concedes': {'n': len(ys), 'values': ys, 'mean': round(statistics.mean(ys), 3)},
                             'matching_market_family_visible': available,
                             'status': 'HIPOTESIS_DESCRIPTIVA_NO_PROBABILIDAD',
                             'note': 'Perspectivas separadas, NO combinar como ensayos independientes'})
    return patterns



def _distribution(records, metric, side='own', condition=None, last_n=None):
    """Raw historical distribution, no probabilistic estimate and no zero imputation."""
    items = records[:last_n] if last_n else records
    series = point_series(items, metric, side=side)
    if condition:
        series = [row for row in series if row['condition'] == condition]
    observed = [row['value'] for row in series if row['value'] is not None]
    return {'n_available': len(observed), 'n_checked': len(series),
            'values': observed, 'mean': round(statistics.mean(observed), 3) if observed else None,
            'median': round(statistics.median(observed), 3) if observed else None}


def territorial_matchup(ctx):
    """Two-sided matchup observations; NOT an auto-score, forecast or pick.

    Never average separate teams as independent samples of the target fixture.
    Venue subsets and recent subsets are small and deliberately labelled.
    """
    home, away = ctx['target']['home'], ctx['target']['away']
    histories = ctx['histories']
    schema = {}
    for team, rival, condition in ((home, away, 'home'), (away, home, 'away')):
        own = histories.get(norm(team), [])
        opp = histories.get(norm(rival), [])
        summary = {'team': team, 'opponent': rival, 'target_condition': condition,
                   'recent_last_3': {}, 'venue_sample': {}, 'rival_concedes': {},
                   'all_sample': {}}
        for metric in ('corner_kicks', 'possession', 'total_shots',
                       'blocked_shots', 'shots_on_goal', 'goals_for', 'offsides',
                       'fouls', 'yellow_cards', 'red_cards', 'passes', 'saves'):
            summary['all_sample'][metric] = _distribution(own, metric)
            summary['recent_last_3'][metric] = _distribution(own, metric, last_n=3)
            summary['venue_sample'][metric] = _distribution(own, metric, condition=condition)
            # Separate opposing team's concessions in the corresponding venue.
            # The opponent is only a proxy for a future adversary, not same fixtures.
            rival_condition = 'away' if condition == 'home' else 'home'
            rival_metric = 'goals_against' if metric == 'goals_for' else metric
            rival_side = 'own' if metric == 'goals_for' else 'opponent'
            summary['rival_concedes'][metric] = _distribution(
                opp, rival_metric, side=rival_side, condition=rival_condition)
        summary['total_corners_historical'] = {
            'all': _distribution(own, 'corner_kicks', side='combined'),
            'recent_last_3': _distribution(own, 'corner_kicks', side='combined', last_n=3),
            'venue_sample': _distribution(own, 'corner_kicks', side='combined', condition=condition),
        }
        schema['home' if condition == 'home' else 'away'] = summary
    return {'status': 'DESCRIPTIVO_NO_PROBABILISTICO', 'sides': schema,
            'warning': 'Las medias no prueban presión, centros, intención ni dominio del fixture futuro. '
                       'Un 0-0 prolongado puede mantener cautela O generar asedio; investigar escenarios opuestos '
                       'con fuentes observacionales anteriores al kickoff. '
                       'Producción, concesión y localía son perspectivas diferentes, no ensayos combinables.'}


def territorial_markdown(tactical):
    lines = ['## Emparejamiento territorial y riesgo de cambio de régimen — SIN PREDICCIÓN',
             'No inferir córners futuros a partir de clasificación, formación o cuota solamente. '
             'Comprobar centros, amplitud, progresiones, repliegue, despejes y escenarios del 0-0 en el reporte.', '']
    def summary(d):
        return (f"{d['values']} (n={d['n_available']}/{d['n_checked']}; "
                f"media={d['mean'] if d['mean'] is not None else 'N/D'})")
    for side in ('home', 'away'):
        t = tactical['sides'][side]
        lines += [f"### {t['team']} ({'local' if side == 'home' else 'visitante'})",
                  f"- Córners propios, últimos 3: {summary(t['recent_last_3']['corner_kicks'])}",
                  f"- Córners propios en condición objetivo: {summary(t['venue_sample']['corner_kicks'])}",
                  f"- Córners que el rival permitió en la condición complementaria: {summary(t['rival_concedes']['corner_kicks'])}",
                  f"- Córners totales de sus partidos, últimos 3: {summary(t['total_corners_historical']['recent_last_3'])}",
                  f"- Posesión propia en condición objetivo: {summary(t['venue_sample']['possession'])}",
                  f"- Remates propios en condición objetivo: {summary(t['venue_sample']['total_shots'])}",
                  '- Preguntas: ¿bloque bajo y ataques reiterados favorecen repetición de centros/córners? '
                  '¿La transición o un gol temprano alteran el mecanismo? Evidencia cualitativa obligatoria.', '']
    lines.append('**Advertencia:** localía/últimos tres pueden tener n<3; sin fuentes de centros y alturas de bloque '
                 'no convertir este esquema en señal direccional ni probabilística.')
    return lines


def tactical_coaching_brief(ctx, territorial, event):
    """Evidence dashboard for a conditional coach-vs-coach tactical evaluation.

    Not a tactical model: formations/aggregates cannot prove intention or causality.
    Derived from the same historical samples; no further API calls or probabilities.
    """
    axes = (
        ('Territorio y circulación', 'possession', 'passes'),
        ('Ataque y volumen', 'total_shots', 'blocked_shots'),
        ('Precisión en el remate', 'shots_on_goal', 'goals_for'),
        ('Bandas, centros y balón parado', 'corner_kicks', None),
        ('Presión, transiciones y fricción', 'fouls', 'yellow_cards'),
        ('Rupturas a la espalda', 'offsides', None),
        ('Protección del área y portero', 'saves', None),
    )
    out = [f'# Mapa táctico pre-Codex V3.4 — evento {event}', '',
           'DATOS observados exclusivamente en API. No son instrucciones tácticas confirmadas ni predicciones.',
           'La fuente externa Perplexity aporta comportamientos/roles publicados; Codex conecta mecanismos y contrapruebas.',
           'No convertir formación, posesión, media de córners o faltas en relación causal automática.', '']
    for side in ('home','away'):
        item=territorial['sides'][side]
        out += [f'## {item["team"]} frente a {item["opponent"]} — {item["target_condition"]}', '']
        for title, m1, m2 in axes:
            components=[]
            for metric in (m1,m2):
                if not metric:continue
                recent=item['recent_last_3'][metric]
                venue=item['venue_sample'][metric]
                conces=item['rival_concedes'][metric]
                components.append(f'{metric}: P1–3 {recent["values"]} n={recent["n_available"]}; '
                                  f'condición {venue["values"]} n={venue["n_available"]}; '
                                  f'rival permite/contraparte {conces["values"]} n={conces["n_available"]}')
            out += [f'### {title}', *[f'- {c}' for c in components]]
        out.append('')
    out += ['## Hipótesis de entrenador contra entrenador — INVESTIGAR, nunca asumir',
            '1. Plan de ambos CON balón y SIN balón: alturas, base de salida, carriles, funciones y puntos débiles constatados.',
            '2. Plan A de cada entrenador vs ajustes del otro: ¿quién gana qué zona, con qué jugadores y a qué coste defensivo?',
            '3. Respuestas alternativas a 0-0, marcar primero, recibir primero y roja: ¿qué mecanismo cambia y para qué mercado?',
            '4. Balón parado: lanzadores, rutinas observadas, marcas, segundo balón, bloqueos, faltas y córners; N/D si no hay evidencia.',
            '5. Fuera de juego: línea alta COORDINADA, presión sobre el pasador, rupturas y temporización; la formación no lo acredita.',
            '6. Distinguir dato observado, mecanismo plausible, contramecanismo, información ausente y mercado Bwin exacto.',
            '7. Priorizar solo 2–3 mecanismos con mejor evidencia y potencial impacto; no llenar una taxonomía sin datos.',
            '', '## Métricas NO garantizadas por API en esta captura',
            '- Centros intentados/completados, centros bloqueados, altura de bloque, PPDA, presión sobre pasador, toques en área,',
            '  rutinas específicas de córner/falta, mapas de calor y comportamiento por estado de marcador: N/D salvo fuente trazable.',
            '- Córners no equivalen a centros; fueras de juego no prueban trampa del fuera de juego; faltas no prueban presión.',
            '- No sumar muestras producción/concesión como si fueran partidos independientes.',
            '- No hay p_model, EV, apuesta ni cuota calculada en este mapa.', '']
    return '\n'.join(out)

# ─────────────────────────────────────────────────────────────────────────────
# CONTEXTO COMPETITIVO (observacional, sin cuotas ni pronósticos)
# ─────────────────────────────────────────────────────────────────────────────

def _api_response(node):
    return node.get('response', []) if isinstance(node, dict) and isinstance(node.get('response'), list) else []


def _integer(value):
    return value if type(value) is int else None


def _rank_row(row):
    """Restringir el análisis a clasificación verificable por ID y temporada."""
    team = row.get('team') or {}
    all_stats = row.get('all') or {}
    goals = all_stats.get('goals') or {}
    if not isinstance(team, dict) or _integer(team.get('id')) is None:
        return None
    return {
        'team_id': team['id'], 'team': team.get('name'),
        'rank': _integer(row.get('rank')), 'points': _integer(row.get('points')),
        'played': _integer(all_stats.get('played')),
        'wins': _integer(all_stats.get('win')),
        'draws': _integer(all_stats.get('draw')),
        'losses': _integer(all_stats.get('lose')),
        'goals_for': _integer(goals.get('for')),
        'goals_against': _integer(goals.get('against')),
        'goal_difference': _integer(row.get('goalsDiff')),
        'description': row.get('description'),
        'updated_utc': row.get('update'),
        'home': row.get('home'), 'away': row.get('away'),
    }


def _goal_split(row, condition):
    part = row.get(condition) or {}
    goal = part.get('goals') or {}
    return {'played': _integer(part.get('played')),
            'goals_for': _integer(goal.get('for')),
            'goals_against': _integer(goal.get('against'))}


def _leaderboard_context(rows, team_id):
    ordered = sorted((r for r in rows if r.get('rank') is not None), key=lambda r: r['rank'])
    target = next((r for r in ordered if r['team_id'] == team_id), None)
    if target is None:
        return {'status': 'TEAM_NOT_IN_STANDINGS', 'team_id': team_id}
    idx = ordered.index(target)
    relegated = [r for r in ordered if 'relegation' in str(r.get('description') or '').casefold()]
    first_drop_rank = min((r['rank'] for r in relegated), default=None)
    safe = next((r for r in ordered if first_drop_rank is not None and r['rank'] == first_drop_rank - 1), None)
    nearest_drop = next((r for r in ordered if first_drop_rank is not None and r['rank'] == first_drop_rank), None)
    own_points = target['points']
    boundaries = {'relegation_zone_start_rank': first_drop_rank,
                  'first_safe_team': {'team': safe['team'], 'rank': safe['rank'], 'points': safe['points']} if safe else None,
                  'highest_relegation_team': {'team': nearest_drop['team'], 'rank': nearest_drop['rank'], 'points': nearest_drop['points']} if nearest_drop else None,
                  'in_relegation_zone': target['rank'] >= first_drop_rank if first_drop_rank is not None else None,
                  'gap_to_first_safe_points': max(0, safe['points'] - own_points) if safe and own_points is not None and safe['points'] is not None and target['rank'] >= first_drop_rank else None,
                  'margin_over_first_relegation_points': own_points - nearest_drop['points'] if nearest_drop and own_points is not None and nearest_drop['points'] is not None and target['rank'] < first_drop_rank else None,
                  'note': 'Solo distancia de puntos en la foto actual. Empates a puntos, resultados ajenos y criterios de desempate pueden alterar posiciones; no indica seguridad o condena matemática.'}
    leader = ordered[0]
    return {'status': 'AVAILABLE', 'table': {k: v for k, v in target.items() if k not in ('home','away')},
            'home_split': _goal_split(target, 'home'), 'away_split': _goal_split(target, 'away'),
            'leader_points_gap': leader['points'] - own_points if leader['points'] is not None and own_points is not None else None,
            'neighbors': [{k:r.get(k) for k in ('rank','team','points','goals_for','goals_against','description')}
                          for r in ordered[max(0,idx-2): idx+3] if r['team_id'] != team_id],
            'boundaries': boundaries,
            'three_result_points': {'victory': own_points+3, 'draw': own_points+1, 'defeat': own_points} if own_points is not None else None}


def _next_fixture_info(blob, team_id, kickoff, target_fixture_id, table_by_id):
    schedule = []
    for entry in _api_response(blob):
        fx = entry.get('fixture') or {}
        teams = entry.get('teams') or {}
        home = teams.get('home') or {}
        away = teams.get('away') or {}
        if team_id not in (home.get('id'), away.get('id')):
            continue
        date = stamp(fx.get('date'))
        if not date or date <= kickoff or str(fx.get('id')) == target_fixture_id:
            continue
        if ((fx.get('status') or {}).get('short') or '').upper() not in ('NS','TBD'):
            continue
        is_home = home.get('id') == team_id
        rival = away if is_home else home
        league = entry.get('league') or {}
        opponent_rank = table_by_id.get(rival.get('id'), {}).get('rank')
        schedule.append({'fixture_id': fx.get('id'), 'date_utc': date.isoformat(),
                         'opponent': rival.get('name'), 'opponent_id': rival.get('id'),
                         'opponent_rank_in_target_league': opponent_rank if league.get('id') == table_by_id.get('_league_id') else None,
                         'condition': 'home' if is_home else 'away',
                         'league': league.get('name'), 'league_id': league.get('id'),
                         'round': league.get('round'),
                         'days_from_target_kickoff': round((date - kickoff).total_seconds() / 86400, 2)})
    schedule.sort(key=lambda item: item['date_utc'])
    return schedule


def build_competitive_context(context_json_path, ctx, txt_path):
    """Lee JSON del mismo snapshot; no consulta red ni transforma motivación en probabilidad."""
    target = ctx['target']
    event = str(target['event_id'])
    result = {'version': 'competitive-v1', 'event_id': event,
              'fixture_id': target['fixture_id'], 'source_file': str(context_json_path) if context_json_path else None,
              'status': 'NO_JSON', 'teams': {}, 'limitations': [],
              'source_sha256': None}
    if context_json_path is None or not context_json_path.is_file():
        result['limitations'].append('JSON de contexto no disponible: clasificación/calendario no acreditados. Se mantiene motor histórico.')
        return result
    data = json.loads(context_json_path.read_text(encoding='utf-8-sig'))
    match = data.get('match') or {}
    fx = data.get('fixture') or {}
    if str(match.get('event_id')) != event or str(fx.get('fixture_id')) != str(target['fixture_id']):
        raise ValueError('JSON competitivo no corresponde al evento y fixture del TXT')
    if (str(fx.get('home_id')) != str(target['home_id']) or str(fx.get('away_id')) != str(target['away_id'])):
        raise ValueError('JSON competitivo con equipos/IDs diferentes al TXT')
    kickoff = stamp(target['kickoff'])
    generated_json = stamp(data.get('generated_at'))
    generated_txt = stamp(field(txt_path.read_text(encoding='utf-8-sig', errors='replace')[:1600], r'^Generado:\s*(\S+)'))
    if not generated_json or not generated_txt or generated_json >= kickoff:
        raise ValueError('JSON competitivo sin fecha fiable pre-kickoff compatible con el TXT')
    if abs((generated_json - generated_txt).total_seconds()) > 30:
        raise ValueError('JSON competitivo pertenece a otra ejecución: diferencia >30s respecto al TXT')
    raw_ctx = data.get('context') or {}
    source = _api_response(raw_ctx.get('standings'))
    rows = []
    league_id = fx.get('league_id')
    season = fx.get('season')
    for section in source:
        league = section.get('league') or {}
        if league.get('id') != league_id or league.get('season') != season:
            continue
        for group in league.get('standings') or []:
            for raw in group:
                rec = _rank_row(raw)
                if not rec:
                    continue
                updated = stamp(rec.get('updated_utc'))
                if updated and updated > kickoff:
                    continue  # No admitir una tabla actualizada después del partido objetivo.
                rows.append(rec)
    if len({r['team_id'] for r in rows}) != len(rows):
        result['limitations'].append('Tabla con equipos repetidos (grupos/fases); no se calcula distancia a zonas.')
        rows = []
    if not rows:
        result['limitations'].append('Sin clasificación estructurada de liga objetivo anterior al kickoff.')
    table_by_id = {r['team_id']: r for r in rows}
    table_by_id['_league_id'] = league_id
    for side in ('home','away'):
        team_id = int(target[side+'_id'])
        home_away = _leaderboard_context(rows, team_id) if rows else {'status':'NO_STANDINGS'}
        schedule = _next_fixture_info(raw_ctx.get(side+'_next_5'), team_id, kickoff,
                                      str(target['fixture_id']), table_by_id)
        own = ctx['histories'].get(norm(target[side]), [])
        past = [stamp(rec.get('date')) for rec in own]
        past = [date for date in past if date and date < kickoff]
        last = max(past) if past else None
        last_gap = round((kickoff - last).total_seconds()/86400, 2) if last else None
        if last_gap is None:
            result['limitations'].append(f'{target[side]}: sin fecha de último encuentro para calcular intervalo entre kickoffs.')
        home_away.update({'team_name': target[side], 'team_id': team_id,
                          'prior_fixture_utc': last.isoformat() if last else None,
                          'days_since_prior_kickoff': last_gap,
                          'next_known_fixtures': schedule,
                          'next_fixtures_coverage': len(schedule),
                          'days_until_next_kickoff': schedule[0]['days_from_target_kickoff'] if schedule else None,
                          'competitive_need': ('IN_RELEGATION_ZONE_SNAPSHOT' if home_away.get('boundaries',{}).get('in_relegation_zone') is True else
                                               'ABOVE_RELEGATION_ZONE_SNAPSHOT' if home_away.get('boundaries',{}).get('in_relegation_zone') is False else
                                               'NOT_DETERMINABLE'),
                          'need_note': 'La clasificación puede explicar incentivos, NO acredita actitud, esquema, volumen ni obligación matemática de ganar.'})
        result['teams'][side] = home_away
    result.update({'status': 'AVAILABLE' if rows else 'PARTIAL_NO_STANDINGS',
                   'source_sha256': hashlib.sha256(context_json_path.read_bytes()).hexdigest(),
                   'source_generated_utc': generated_json.isoformat(),
                   'target_kickoff_utc': kickoff.isoformat(),
                   'league_id': league_id, 'season': season,
                   'standings_teams_count': len(rows),
                   'table_snapshot': [{k:r.get(k) for k in ('rank','team_id','team','points','played','goals_for','goals_against','goal_difference','description')}
                                      for r in sorted(rows,key=lambda r:r['rank'] if r['rank'] is not None else 999)],
                   'interpretation_limits': ['Calendario next=5 de API incluye el partido objetivo; se excluye del listado futuro.',
                                             'Dificultad de rivales: posición en tabla como proxy descriptiva, NO fuerza causal ni probabilidad.',
                                             'No se afirma número total de jornadas restantes: la lista next=5 no es todo el calendario.',
                                             'Distancia a zonas de puntos no es clasificación matemática, ni prueba de necesidad de ganar.',
                                             'Días calculados entre horarios de inicio; NO son horas de descanso efectivo.',
                                             'No se mezclan puntos/goles de otras ligas ni tablas actualizadas tras kickoff.',
                                             'No usar esta sección para crear p_model, EV, picks ni cambios de cuota.']})
    return result


def competitive_markdown(comp):
    lines = [f'# Contexto competitivo pre-Codex — evento {comp["event_id"]}', '',
             f'Estado: **{comp["status"]}** · fixture {comp["fixture_id"]}',
             'Hechos estadísticos de API-Football; las motivaciones y el plan táctico se INVESTIGAN, no se deducen automáticamente.', '']
    for side in ('home','away'):
        t = comp['teams'].get(side)
        if not t:
            continue
        lines += [f'## {t["team_name"]} ({"local" if side == "home" else "visitante"})', '']
        if t.get('status') == 'AVAILABLE':
            r = t['table']
            lines += [f'- Tabla: puesto {r["rank"]} · {r["points"]} puntos · {r["played"]} PJ · '
                      f'{r["wins"]}V-{r["draws"]}E-{r["losses"]}D · GF {r["goals_for"]} · GC {r["goals_against"]} · DG {r["goal_difference"]}.',
                      f'- Descripción de zona en API: {r.get("description") or "N/D"}.',
                      f'- Local: {t["home_split"]}; visitante: {t["away_split"]}.',
                      f'- Diferencia respecto a puntos del líder: {t["leader_points_gap"]}.',
                      f'- Límites de zona descenso según rótulos API: {t["boundaries"]}.',
                      f'- Vecinos en tabla: {t["neighbors"]}.',
                      f'- Puntos propios hipotéticos tras este partido (sin otros resultados): {t["three_result_points"]}.']
        else:
            lines.append('- Clasificación: NO VERIFICADA EN JSON.')
        lines += [f'- Intervalo entre kickoffs últimos: {t["days_since_prior_kickoff"] if t["days_since_prior_kickoff"] is not None else "N/D"} días.',
                  f'- Necesidad clasificada SOLO por zona: {t["competitive_need"]}.',
                  f'- Nota: {t["need_note"]}',
                  '- Próximos encuentros disponibles (excluye partido objetivo):']
        if t['next_known_fixtures']:
            for match in t['next_known_fixtures']:
                rank = match['opponent_rank_in_target_league']
                lines.append(f'  - {match["date_utc"]} · {match["condition"]} vs {match["opponent"]} · '
                             f'{match["league"]} / {match["round"]} · puesto rival en liga objetivo: {rank if rank is not None else "N/D"} · '
                             f'{match["days_from_target_kickoff"]} días desde kickoff objetivo.')
        else:
            lines.append('  - N/D; no inventar calendario.')
        lines.append('')
    lines += ['## Interpretación competitiva permitida',
              '1. Contrastar necesidad matemática vs discurso del DT, rotaciones y rival. Clasificación no prueba intensidad.',
              '2. Analizar 0-0, gol temprano de cada lado, empate, roja y ajustes de ambos cuerpos técnicos con contrapruebas.',
              '3. Considerar calendarios, viajes y descansos solo con procedencia temporal contrastable.',
              '4. No inferir córners, remates ni ganador a partir de la posición o urgencia.', '',
              '## Limitaciones',
              *[f'- {v}' for v in comp.get('limitations',[])],
              *[f'- {v}' for v in comp.get('interpretation_limits',[])], '']
    return '\n'.join(lines)

def report_info(path):
    """Leer marcas temporales explícitas sin inferir publicación ni oficialidad.

    Soporta `- **analysis_cutoff_utc:** `2026-...Z`` de Perplexity.
    El valor es autodeclarado y NO acredita la hora real de cierre.
    """
    if not path or not path.is_file():
        return {'path': str(path) if path else None, 'status': 'NO_DISPONIBLE'}
    raw = path.read_text(encoding='utf-8-sig', errors='replace')
    title = bool(re.search(r'(?im)^#{1,5}\s+.*(?:alineaciones|onc(?:e|es)|planillas)', raw))

    def explicit_utc(key):
        for line in raw.splitlines():
            if ':' not in line or line.lstrip().startswith('|'):
                continue
            lhs, rhs = line.strip().split(':', 1)
            lhs = lhs.lstrip('-* ').replace('`', '').replace('*', '').strip()
            if lhs.casefold() != key.casefold():
                continue
            hit = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})', rhs)
            return hit.group() if hit and stamp(hit.group()) else None
        return None

    cutoff = explicit_utc('analysis_cutoff_utc')
    generated = explicit_utc('report_generated_utc')
    return {'path': str(path), 'status': 'REPORTE_PRESENTE',
            'has_lineup_section': title, 'analysis_cutoff_utc': cutoff,
            'report_generated_utc': generated,
            'lineup_verification': 'NO_ACREDITADA_POR_EL_MOTOR',
            'note': 'Fechas declaradas, no verificadas externamente. Codex debe discriminar XI oficial respaldado, lista periodística, probable y precarga de jornada anterior.'}


def resolve_report_for_event(args, guard, event):
    """Reporte opcional pero SIEMPRE ligado al Event ID.

    Orden: --report, ruta validada, informe por ID en instructions, snapshot
    por ID. Nunca buscar un documento genérico ni usar el de otro partido.
    """
    expected = f'reporte_cualitativo_{event}.md'
    if args.report is not None:
        candidate = args.report
        source = '--report'
    elif (guard.get('files') or {}).get('report'):
        candidate = Path(guard['files']['report'])
        source = 'files.report del validador'
    else:
        choices = [Path('instructions') / expected,
                   Path('outputs') / 'matches' / str(event) / 'inputs' / expected]
        return next((path for path in choices if path.is_file()), None)

    if candidate.name != expected:
        raise ValueError(f'{source} apunta a {candidate.name!r}; se requiere {expected} '
                         'para no mezclar informes entre eventos.')
    if not candidate.is_file():
        raise ValueError(f'No existe el informe {source}: {candidate}')
    return candidate


def evaluate_selection(market, selection, odd, ctx, market_aliases, min_n, cache):
    teams = [ctx['target']['home'], ctx['target']['away']]
    spec = identify_market(market, selection, teams, market_aliases)
    live = bool(re.search(r'(?i)(en vivo|\blive\b|resto del partido|resultado actual|rest of the match|current score)', market))
    item = {'market': market, 'selection': selection, 'odd': odd,
            'market_live_excluded': live,
            'break_even_raw_1_over_odd': round(1 / odd, 5) if odd > 0 else None,
            'parsed_market': spec, 'value_state': 'VALUE_NO_VERIFICADO'}
    if live:
        item['evidence_status'] = 'MERCADO_EN_VIVO_NO_APTO'
    elif spec.get('type'):
        item['evidence'] = cache.evaluate(spec, ctx, evidence_for)
        item['fragility_heuristic'] = quick_fragility(spec)
        groups = item['evidence'].get('groups', [])
        is_direct = bool(groups and groups[0]['sample_valid'] >= min_n)
        item['evidence_status'] = ('EVIDENCIA_DESCRIPTIVA' if is_direct else
                                   'EVIDENCIA_PARCIAL' if groups and groups[0]['sample_valid'] >= 1 else
                                   'MUESTRA_INSUFICIENTE_O_IDENTIDAD')
    else:
        item['evidence_status'] = spec.get('reason', 'NO_EVALUABLE')
    return item


def run(args):
    odds_path, ctx_path, val_path, expected = paths_for_event(args)
    ctx = parse_context(ctx_path)
    event = ctx['target']['event_id']
    odds = parse_odds(odds_path) if odds_path else {'event_id': event, 'capture': None, 'markets': []}
    if not event or odds['event_id'] != event or (expected and expected != event):
        raise ValueError(f'Identidad incompatible: Bwin {event}, contexto {ctx["target"]["event_id"]}, constants {expected}')
    guard = load_validation(val_path, event)
    report_path = resolve_report_for_event(args, guard, event)
    report = report_info(report_path)
    ko = stamp(ctx['target']['kickoff'])
    captured = stamp(odds['capture'])
    guard['temporal_state'] = ('SIN_CUOTAS_ANALISIS_ESTADISTICO' if odds_path is None else
                               'CAPTURA_NO_ACREDITADA' if captured is None else
                               'CAPTURA_NO_PREPARTIDO' if captured >= ko else
                               'RETROSPECTIVO_NO_EJECUTABLE' if datetime.now(timezone.utc) >= ko else
                               'PREPARTIDO_CUOTAS_POR_CONFIRMAR')
    if guard['state'] == 'BLOQUEADO_PREPARTIDO':
        guard['temporal_state'] = 'BLOQUEADO_PREPARTIDO'
    competitive = build_competitive_context(args.context_json or (ctx_path.with_suffix('.json') if ctx_path.with_suffix('.json').is_file() else None), ctx, ctx_path)
    market_aliases = verified_market_aliases(ctx)
    from prematch_context_delta import EvidenceCache
    evidence_cache = EvidenceCache(event, getattr(args, 'evidence_cache', None),
                                   getattr(args, 'evidence_cache_sha256', None))
    catalog = []
    rows = []
    teams = [ctx['target']['home'], ctx['target']['away']]
    for block in odds['markets']:
        for sel in block['selections']:
            item = evaluate_selection(block['market'], sel['selection'], sel['odd'], ctx, market_aliases, args.min_n, evidence_cache)
            catalog.append(item)
            if args.min_odd <= sel['odd'] <= args.max_odd:
                rows.append(item)
    # El ranking es orden de inspección, no p_model ni criterio para descartar el resto.
    rows.sort(key=lambda r: (r['evidence_status'] != 'EVIDENCIA_DESCRIPTIVA',
                             -((r.get('evidence') or {}).get('groups') or [{'sample_valid': 0}])[0]['sample_valid'],
                             r['market'], r['selection']))
    crosses = cross_hypotheses(ctx, odds)
    territorial = territorial_matchup(ctx)
    player_patterns = independent_player_patterns(ctx)
    warnings = [
        'Estudiar patrones aunque no existan cuotas: patrón sin línea Bwin NO es apuesta ejecutable.',
        'Cuotas son fotografía de captura; confirmar precio vigente antes de apostar.',
        'XI ausente en API no invalida estadísticas históricas; props sin XI son exploratorios.',
        'NO predice el evento objetivo ni estima p_model, EV, value o stake.',
        'Frecuencias descriptivas; muestras históricas heterogéneas y con posibles fixtures comunes.',
        'Valores nulos de API-Football NO son 0; los suplentes sin minutos NO son ensayos.',
        'Producción propia, concesiones del rival y totales se calculan separadamente.',
        'Para props individuales, los agregados permitidos por el rival NO predicen el resultado del jugador.',
        'Los mercados no implementados permanecen en matriz para revisión independiente por Codex.',
        'La cuota puede haber cambiado; nunca convertir 1/cuota en probabilidad del modelo.',
        'La fragilidad heurística no equivale a robustez real y se valida cualitativamente.',
        'Una defensa baja no garantiza pocos córners: evaluar tanto poco ritmo como asedio sostenido a 0-0.',
        'Un equipo favorito/primero no garantiza dominio territorial; contrastar estilos y datos observados.',
        'Sin datos temporales de cada evento no es posible cuantificar faltas tras pérdidas ni líneas de fuera de juego.',
        'XI de 11 IDs NO asegura posiciones correctas: confrontar roles del XI con plantilla y fuentes prepartido.',
        'Partidos históricos de copa y liga no son intercambiables: revisar same_competition y other_competitions.',
    ]
    # Huellas de los snapshots: Codex no puede confundir capturas diferentes de un mismo Event ID.
    snapshots = {label: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                 for label, path in (('odds', odds_path), ('context', ctx_path), ('report', report_path))
                 if path is not None and path.is_file()}
    doc = {'version': VERSION, 'event_id': event,
           'odds_availability': 'CAPTURA_DISPONIBLE' if odds_path else 'SIN_CUOTAS_O_AMBIGUAS',
           'xi_api_status': ('PRESENTE_AMBOS' if all(ctx['xi_quality'][side]['status'] == 'XI_ESTRUCTURADO_COMPLETO' for side in ('home','away')) else
                             'XI_NOMINAL_DISPONIBLE_CON_INCIDENCIAS' if all(ctx['xi_quality'][side]['starter_rows'] == 11 for side in ('home','away')) else
                             'XI_AUSENTE_O_INCOMPLETO'),
           'xi_quality': ctx['xi_quality'],
           'xi_role_review': {side: [issue for issue in ctx['xi_quality'][side].get('squad_crosscheck', [])
                                     if issue['type'] == 'POSITION_ROLE_CONFLICT'] for side in ('home', 'away')},
           'target_competition': ctx['target'].get('target_competition'),
           'teams': teams, 'kickoff_utc': ctx['target']['kickoff'],
           'odds_capture': odds['capture'],
           'odds_capture_utc': captured.isoformat() if captured else None,
           'input_snapshots': snapshots,
           'qualitative_report': report,
           'sources': {'odds': str(odds_path) if odds_path else None, 'context': str(ctx_path),
                       'validation': str(val_path) if val_path else None,
                       'report': report['path']},
           'validation_guard': guard, 'n_markets': len(odds['markets']),
           'n_selections_total': len(catalog), 'n_evaluated_in_odds_range': len(rows),
           'n_with_direct_descriptive_evidence': sum(r['evidence_status'] == 'EVIDENCIA_DESCRIPTIVA' for r in rows),
           'warnings': warnings, 'ranking': rows,
           'competitive_context_status': competitive['status'],
           'competitive_context_snapshot': competitive.get('source_sha256')}
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / f'evidence_cache_{event}.json').write_text(json.dumps(evidence_cache.document(), ensure_ascii=False))
    def write_json(path, obj):
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    ranking = out / f'ranking_probabilistico_{event}.json'
    matrix = out / f'matriz_evidencia_{event}.json'
    crossmd = out / f'hipotesis_cruzadas_{event}.md'
    playermd = out / f'patrones_jugadores_{event}.md'
    md = out / f'ranking_probabilistico_{event}.md'
    csvpath = out / f'ranking_probabilistico_{event}.csv'
    compjson = out / f'contexto_competitivo_{event}.json'
    compmd = out / f'contexto_competitivo_{event}.md'
    briefpath = out / f'resumen_codex_{event}.md'
    tacticspath = out / f'mapa_tactico_{event}.md'
    write_json(compjson, competitive)
    compmd.write_text(competitive_markdown(competitive), encoding='utf-8')
    write_json(ranking, doc)
    write_json(matrix, {'version': VERSION, 'event_id': event,
                        'target_players': ctx['target_players'],
                        'target_xi_quality': ctx['xi_quality'],
                        'historical_sources': {k: [{'fixture_id': r['fixture_id'], 'date': r['date'],
                                                    'condition': r['condition'], 'competition': r.get('competition'),
                                                    'same_competition': r.get('same_competition'),
                                                    'metrics': r['metrics'],
                                                    'opponent_metrics': r['opponent_metrics']}
                                                   for r in vals] for k, vals in ctx['histories'].items()},
                        'market_inventory': catalog, 'cross_hypotheses': crosses,
                        'territorial_matchup': territorial,
                        'independent_player_patterns': player_patterns,
                        'warnings': warnings, 'competitive_context_file': str(compjson),
                        'competitive_context_status': competitive['status']})
    mdrows = [f'# Preanálisis descriptivo V3.4.1 — {teams[0]} vs {teams[1]}',
              f'Evento {event} · Validación {guard["state"]} · Tiempo {guard["temporal_state"]}',
              '', '**El ranking NO predice aciertos y NO demuestra value.**',
              f'Selecciones capturadas: {len(catalog)} · En rango: {len(rows)} · Evidencia descriptiva (n≥{args.min_n}): {doc["n_with_direct_descriptive_evidence"]}',
              '', '| Mercado | Selección | Cuota | Muestra directa | Frecuencia observada | Grupo origen | Grupo contraste |',
              '|---|---|---:|---:|---|---|---|']
    shown = 0
    for item in rows:
        if item['evidence_status'] != 'EVIDENCIA_DESCRIPTIVA' or shown >= args.top:
            continue
        groups = item['evidence']['groups']
        g0 = groups[0]
        dr = g0['descriptive']
        dn = 'N/D' if not dr else f'{dr["hits"]}/{dr["n"]}'
        opp = groups[1]['scope'] if len(groups) > 1 else 'N/D'
        same = (g0.get('same_competition') or {}).get('descriptive')
        suffix = f' [misma competición: {same["hits"]}/{same["n"]}]' if same and same['n'] != dr['n'] else ''
        mdrows.append(f'| {item["market"].replace("|", "/")} | {item["selection"].replace("|", "/")} | {item["odd"]:g} | {g0["sample_valid"]}/{g0["sample_total"]} | {dn}{suffix} | {g0["scope"]} | {opp} |')
        shown += 1
    mdrows.extend(['', '## Revisión independiente',
                   'Usar matriz_evidencia_<id>.json para TODO el catálogo, incluidas selecciones sin cálculo automático.',
                   'El orden anterior es para revisión; Codex debe explorar hipótesis sin anclaje en este ranking.',
                   '', '## Límites', *[f'- {w}' for w in warnings]])
    md.write_text('\n'.join(mdrows) + '\n', encoding='utf-8')
    with csvpath.open('w', newline='', encoding='utf-8') as f:
        wr = csv.DictWriter(f, fieldnames=['market', 'selection', 'odd', 'evidence_status',
                                           'n_direct', 'hits_direct', 'missing_direct', 'same_competition_n',
                                           'same_competition_hits', 'source_scope', 'contrast_scope', 'value_state'])
        wr.writeheader()
        for r in rows:
            groups = (r.get('evidence') or {}).get('groups') or []
            main = groups[0] if groups else {}
            direct = main.get('descriptive') or {}
            wr.writerow({'market': r['market'], 'selection': r['selection'], 'odd': r['odd'],
                         'evidence_status': r['evidence_status'], 'n_direct': main.get('sample_valid'),
                         'hits_direct': direct.get('hits'), 'missing_direct': main.get('sample_missing'),
                         'same_competition_n': ((main.get('same_competition') or {}).get('n')),
                         'same_competition_hits': (((main.get('same_competition') or {}).get('descriptive') or {}).get('hits')),
                         'source_scope':main.get('scope'),
                         'contrast_scope':groups[1].get('scope') if len(groups)>1 else None,
                         'value_state': r['value_state']})
    clines = [f'# Hipótesis cruzadas V3.4 — evento {event}', '',
              'Estas son propuestas de INVESTIGACIÓN, no picks ni probabilidades. Los dos grupos NO se fusionan.',
              'Una familia marcada como presente NO garantiza cuota para la selección concreta.', '']
    for c in crosses:
        prod = c['attack_production']
        allow = c['defense_concedes']
        clines += [f'## {c["attacking_team"]} {c["label"]} vs {c["defending_team"]}',
                   f'- Produce: {prod["values"]} (n={prod["n"]}, media={prod["mean"]}).',
                   f'- El rival permite: {allow["values"]} (n={allow["n"]}, media={allow["mean"]}).',
                   f'- Familia visible en Bwin: {"sí" if c["matching_market_family_visible"] else "no detectada"}.',
                   '- Verificar XI, roles, localía, rival comparable, cuota exacta, reglas y escenarios de partido.', '']
    clines += ['', *territorial_markdown(territorial)]
    crossmd.write_text('\n'.join(clines), encoding='utf-8')
    plines = [f'# Patrones individuales independientes de cuota — {event}',
              'No son picks; se extraen incluso sin mercados Bwin ni XI del fixture objetivo.',
              'Las apariciones en estadísticas de jugadores pueden ser menores que las titularidades en LINEUPS si API no publica detalle en algunos partidos.',
              'Los ceros son valores numéricos 0; N/D y ausencia en partido NO se imputan como 0.',
              'Xi_status=NO_LISTADO_EN_XI_OBJETIVO significa que el jugador no figura en el XI/banquillo extraído; NO_CONFIRMADO_XI_OBJETIVO indica alineaciones no disponibles.',
              '', '| Equipo | Jugador | XI objetivo | Aparece en histórico | Media min/partido jugado | Remates >=1 | Faltas cometidas >=1 | Offsides >=1 |',
              '|---|---|---|---:|---:|---:|---:|---:|']
    for player in player_patterns:
        def present(metric):
            c = player['metric_coverage'][metric]
            return f'{c["hit_at_least_1"]}/{c["n_valid"]}' if c['n_valid'] else 'N/D'
        plines.append(f'| {player["team"]} | {player["name"].replace("|", "/")} | {player["xi_status"]} | '
                      f'{player["appearances"]}/{player["history_total"]} | {player["avg_minutes_per_appearance"]} | '
                      f'{present("shots")} | {present("fouls_committed")} | {present("offsides")} |')
    plines.extend(['', '## Limitaciones',
                   '- N/D en una estadística NO equivale a ausencia del evento.',
                   '- Si solo existe registro histórico, falta comprobar XI/rol actual.',
                   '- Ni el acierto 6/6 ni el promedio individual son p_model o EV.',
                   '- Para detalle de cada métrica y valores originales, usar `independent_player_patterns` de matriz_evidencia.'])
    playermd.write_text('\n'.join(plines) + '\n', encoding='utf-8')
    from collections import Counter
    status_counts = Counter(r['evidence_status'] for r in catalog)
    # Archivo de lectura rápida: no forzar a Codex a volcar JSON enormes al contexto.
    compact = [f'# Resumen de entrada Codex V3.4.1 — {teams[0]} vs {teams[1]}',
               f'Evento: {event} | kickoff: {ctx["target"]["kickoff"]}',
               f'Cuotas: {doc["odds_availability"]} | captura: {odds["capture"] or "N/D"}',
               f'Hash SHA256 cuotas: {(snapshots.get("odds") or {}).get("sha256", "SIN_CUOTAS")}',
               f'Validación: {guard["state"]} | temporal: {guard["temporal_state"]}',
               f'XI API: {doc["xi_api_status"]}; reporte: {report["status"]}',
               f'Mercados: {doc["n_markets"]}; selecciones: {len(catalog)}; evidencia n>={args.min_n}: {doc["n_with_direct_descriptive_evidence"]}',
               '', '## Auditoría de roles y competiciones',
               f'- Competición objetivo: {ctx["target"].get("target_competition") or "N/D"}',
               *[f'- {side}: conflicto posicional con plantilla: {x["lineup_name"]} XI={x["lineup_pos"]}, SQUAD={x["squad_position"]}'
                 for side in ('home','away') for x in doc['xi_role_review'][side]],
               '- Los conflictos de rol requieren contraste táctico, no cambian IDs ni el estado de XI nominal.',
               '', '## Cobertura por tipo',
               *[f'- {k}: {v}' for k, v in sorted(status_counts.items())],
               '', '## Inspección sugerida, NO veredicto',
               'Los valores históricos requieren auditoría puntual de las fuentes. Seleccionar máximo un candidato final.',
               '']
    # Evitar anclaje: mostrar cobertura por FAMILIA, no "picks" arbitrarios
    # ordenados alfabéticamente (que podrían privilegiar unders inexistentes).
    families = defaultdict(lambda: {'selections': 0, 'with_evidence': 0})
    for item in catalog:
        parsed = item.get('parsed_market') or {}
        fam = f"{parsed.get('type', 'otro')} / {parsed.get('metric', parsed.get('reason', 'sin_mapear'))}"
        families[fam]['selections'] += 1
        if item['evidence_status'] == 'EVIDENCIA_DESCRIPTIVA':
            families[fam]['with_evidence'] += 1
    compact.append('## Familias y cobertura — SIN PRIORIDAD DE APUESTA')
    compact += [f'- {fam}: {data["selections"]} selecciones, {data["with_evidence"]} con n suficiente'
                for fam, data in sorted(families.items())]
    compact += ['', '## Cruces estadísticos a investigar (sin cuota implícita)']
    for cross in crosses:
        compact.append(f'- {cross["attacking_team"]} {cross["label"]} frente a {cross["defending_team"]}: '
                       f'producción {cross["attack_production"]["mean"]} (n={cross["attack_production"]["n"]}), '
                       f'concesión {cross["defense_concedes"]["mean"]} (n={cross["defense_concedes"]["n"]}); '
                       f'familia observada en Bwin: {cross["matching_market_family_visible"]}')
    compact += ['', *territorial_markdown(territorial),
                '', f'## Patrones independientes por jugador: {len(player_patterns)} identidades históricas',
                f'Consultar patrones_jugadores_{event}.md; XI ausente no invalida el histórico.',
                f'Consultar mapa_tactico_{event}.md para ejes observacionales del duelo entre entrenadores (NO es predicción).',
                '', '## Fuentes primarias',
                f'- contexto: {ctx_path}',
                f'- cuotas: {odds_path or "no disponible"}',
                f'- validación: {val_path or "no disponible"}',
                f'- reporte cualitativo: {report["path"] or "no disponible"}',
                '- El informe cualitativo se lee directamente; no usar el resumen como sustituto.',
                '', '## Límites', *[f'- {w}' for w in warnings]]
    briefpath.write_text('\n'.join(compact)+'\n', encoding='utf-8')
    tacticspath.write_text(tactical_coaching_brief(ctx, territorial, event), encoding='utf-8')
    for p in (ranking, matrix, md, csvpath, crossmd, playermd, briefpath, tacticspath, compjson, compmd):
        print(f'[OK] {p}')
    print(f'[INFO] evento={event} selecciones={len(catalog)} evidencia_directa={doc["n_with_direct_descriptive_evidence"]} tiempo={guard["temporal_state"]}')
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--odds', type=Path, help='Ruta exacta cuotas_bwin_*.txt')
    ap.add_argument('--context', type=Path, help='Ruta exacta match_engine_*.txt')
    ap.add_argument('--context-json', type=Path, help='JSON original API-Football del MISMO evento (opcional, sin consultas)')
    ap.add_argument('--no-odds', action='store_true', help='Analizar estadísticas sin cuotas disponibles')
    ap.add_argument('--event-id', help='ID Bwin, si config/constants.py no está disponible')
    ap.add_argument('--validation', type=Path, help='validacion_<event_id>.json')
    ap.add_argument('--report', type=Path, help='Informe Perplexity .md (para trazabilidad documental)')
    ap.add_argument('--output-dir', type=Path, default=Path('outputs/probabilistic'))
    ap.add_argument('--min-n', type=int, default=5)
    ap.add_argument('--min-odd', type=float, default=1.01)
    ap.add_argument('--max-odd', type=float, default=1000.0)
    ap.add_argument('--top', type=int, default=30)
    ap.add_argument('--evidence-cache', type=Path, help='Cálculos descriptivos previos vinculados por SHA; precios siempre reevaluados')
    ap.add_argument('--evidence-cache-sha256')
    args = ap.parse_args()
    if args.no_odds and args.odds is not None:
        ap.error('No combinar --no-odds y --odds')
    if args.min_n < 1 or args.min_odd <= 1 or args.min_odd > args.max_odd or args.top < 1:
        ap.error('min-n>=1, min-odd>1, min-odd<=max-odd y top>=1')
    try:
        run(args)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        ap.error(str(e))


if __name__ == '__main__':
    main()
