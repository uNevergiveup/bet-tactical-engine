#!/usr/bin/env python3
"""Mapeo offline de patrones descriptivos a selecciones Bwin verificadas.

No calcula probabilidades, EV, picks ni consulta ninguna API. Las cuotas son
evidencia histórica de un preflight y nunca se convierten en una recomendación.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import math
from collections import Counter
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

VERSION = '1.1-market-mapping-contract'
SUPPORTED = {
    'goals': ('total_goals', 'goles'),
    'corners': ('corners', 'córners'),
    'shots': ('shots', 'tiros'),
    'on_target': ('shots_on_target', 'tiros_a_puerta'),
    'fouls': ('fouls', 'faltas'),
    'yellow': ('cards', 'tarjetas_amarillas'),
}
UNSUPPORTED = {'xg', 'offsides'}


class MappingError(RuntimeError):
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_sha() -> str:
    return sha(Path(__file__).resolve())


def read(path: Path):
    from prematch_paths import control_file
    path = control_file(path)
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingError(f'INPUT_INVALID:{path}') from exc
    if not isinstance(value, dict):
        raise MappingError(f'INPUT_ROOT_INVALID:{path}')
    return value


def dt(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def norm(value) -> str:
    raw = unicodedata.normalize('NFKD', str(value or ''))
    return ' '.join(''.join(c for c in raw if not unicodedata.combining(c)).casefold().split())


def parse_line(selection):
    text = norm(selection)
    direction = 'OVER' if text.startswith(('mas ', 'más ')) or ' mas de ' in f' {text} ' else None
    if direction is None and (text.startswith(('menos ',)) or ' menos de ' in f' {text} '):
        direction = 'UNDER'
    match = re.search(r'(\d+(?:[\.,]\d+)?)', text)
    if not direction or not match:
        return None
    return {'direction': direction, 'line': float(match.group(1).replace(',', '.'))}


def pattern_rows(patterns):
    descriptive = patterns.get('descriptive_discovery')
    if isinstance(descriptive, dict) and isinstance(descriptive.get('patterns'), list):
        return descriptive['patterns']
    rows = patterns.get('patterns')
    if not isinstance(rows, list):
        raise MappingError('PATTERN_ROWS_MISSING')
    return rows


def pattern_status(pattern):
    """Normaliza los dos contratos sin fusionar sus filas."""
    value = str(pattern.get('validation_status') or pattern.get('status') or 'INSUFFICIENT_DATA')
    return {'MUESTRA_DESCRIPTIVA_OK': 'DESCRIPTIVE_SUPPORTED',
            'MUESTRA_PARCIAL': 'EXPLORATORY_UNVALIDATED',
            'SIN_DATOS': 'INSUFFICIENT_DATA'}.get(value, value)


def _preflight_identity(pattern, match):
    if not isinstance(match, dict):
        return False, 'PREFLIGHT_FIXTURE_NOT_UNIQUE'
    if match.get('is_live') is True or str(match.get('status','')).upper() in ('LIVE','IN_PLAY'):
        return False,'LIVE_EVENT_REJECTED'
    for key in ('fixture_id', 'home', 'away'):
        if str(match.get(key)) != str(pattern.get(key)):
            return False, f'PREFLIGHT_{key.upper()}_MISMATCH'
    a, b = dt(match.get('kickoff_madrid')), dt(pattern.get('kickoff_madrid'))
    if not a or not b or abs((a - b).total_seconds()) > 180:
        return False, 'PREFLIGHT_KICKOFF_MISMATCH'
    eid = str(match.get('bwin_event_id') or '')
    url = str(match.get('bwin_url') or '')
    snap = match.get('snapshot') or {}
    if not eid or not re.search(rf'(?:-2:|:){re.escape(eid)}(?:\?.*)?$', url):
        return False, 'BWIN_EVENT_URL_MISMATCH'
    if not snap.get('event_id_verified_in_txt'):
        return False, 'BWIN_EVENT_ID_NOT_VERIFIED_IN_TXT'
    if (match.get('quality_gate') or {}).get('join_status') != 'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF':
        return False, 'API_BWIN_JOIN_NOT_VERIFIED'
    evidence = match.get('time_evidence') or {}
    if evidence.get('status') not in ('DISPLAY_TIME_CORROBORATED', 'COUNTDOWN_TIME_CORROBORATED', 'ABSOLUTE_TIME_VERIFIED'):
        return False, 'BWIN_KICKOFF_TIME_NOT_CORROBORATED'
    path = Path(str(snap.get('snapshot_path') or ''))
    if not path.is_file() or sha(path) != snap.get('sha256'):
        return False, 'SNAPSHOT_SHA256_MISMATCH_OR_MISSING'
    header = path.read_text(encoding='utf-8-sig', errors='replace')[:1600]
    if not re.search(rf'(?m)^Event ID:\s*{re.escape(eid)}\s*$', header):
        return False, 'SNAPSHOT_EVENT_ID_CONFLICT'
    captured = dt(snap.get('capture_declared_madrid'))
    if not captured or captured >= a:
        return False, 'SNAPSHOT_CAPTURE_NOT_PREMATCH'
    if snap.get('source_url') != url:
        return False, 'SNAPSHOT_SOURCE_URL_CONFLICT'
    observed = dt(evidence.get('observed_at_utc'))
    if not observed or observed > captured or (captured-observed).total_seconds() > 180*60:
        return False, 'BWIN_TIME_EVIDENCE_UNDATED_OR_STALE'
    for key in ('bwin_visible_kickoff_madrid', 'api_kickoff_madrid'):
        value=dt(evidence.get(key))
        if value and abs((value-a).total_seconds())>180:
            return False, 'BWIN_TIME_EVIDENCE_CONFLICT'
    return True, None


def _load_market_rows(preflight_path: Path, match):
    # Reconstruct prices from the SHA-checked original; a CSV cannot invent a
    # selection or alter a price. Reuse the existing scraper parser.
    from bwin_market_preflight import snapshot_odds, market_kind
    try:
        snap=snapshot_odds(Path(match['snapshot']['snapshot_path']),match['bwin_event_id'])
    except (OSError, ValueError, KeyError) as exc:
        return [], 'SNAPSHOT_PARSE_INVALID'
    if snap['capture_declared_madrid']!=match['snapshot'].get('capture_declared_madrid') or snap['source_url']!=match.get('bwin_url'):
        return [], 'SNAPSHOT_HEADER_METADATA_CONFLICT'
    genuine={(r['market'],r['selection'],float(r['odd'])) for r in snap['_parsed_selection_rows']}
    path = preflight_path.parent / 'market_selection_review.csv'
    if not path.is_file():
        return [], 'MARKET_REVIEW_MISSING'
    rows = []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            if str(row.get('fixture_id')) != str(match.get('fixture_id')) or str(row.get('bwin_event_id')) != str(match.get('bwin_event_id')):
                continue
            line = parse_line(row.get('selection'))
            if line is None:
                continue
            try:
                odd = float(str(row.get('odd')).replace(',', '.'))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(odd) or odd <= 1:
                continue
            if (row.get('market'),row.get('selection'),odd) not in genuine:
                return [], 'MARKET_REVIEW_NOT_IN_ORIGINAL_TXT'
            if market_kind(row['market']) != row.get('market_kind'):
                return [], 'MARKET_REVIEW_KIND_CONFLICT'
            rows.append({**row, **line, 'odd': odd, 'period':market_period(row['market']), 'source_path': str(path.resolve()),'review_sha256':sha(path)})
    return rows, None

def market_period(name):
    text=norm(name)
    if re.search(r'\blive\b|en vivo|resto del partido|marcador actual',text): return 'LIVE'
    if re.search(r'1[ªaº]?\s*parte|primera\s+(?:parte|mitad)|1st half',text): return 'FIRST_HALF'
    if re.search(r'2[ªaº]?\s*parte|segunda\s+(?:parte|mitad)|2nd half',text): return 'SECOND_HALF'
    if 'tiempo reglamentario' in text or 'full time' in text: return 'FULL_TIME'
    return 'UNKNOWN'

def pending_evidence(fid, ledger):
    row=next((r for r in ledger.get('fixtures',[]) if str(r.get('fixture_id'))==str(fid)),None)
    if row is None: return {'cause':'NO_CAPTURE_EVIDENCE_UNAVAILABLE','evidence':[]}
    reason=str(row.get('reason',''))
    cause=('CAPTURE_LIMIT_REACHED' if reason=='MAX_SCRAPES_BUDGET_NOT_ANALYZED' else
           'EVENT_NOT_FOUND_IN_SCANNED_PAGES' if 'NOT_FOUND_IN_SCANNED_PAGES' in reason else
           'IDENTITY_OR_TIME_PENDING' if row.get('state') in ('IDENTIDAD_INCORRECTA','PENDIENTE_CUOTAS') else
           'QUOTES_UNAVAILABLE' if row.get('state')=='SIN_CUOTAS' else 'NO_CAPTURE_OTHER_RECORDED_REASON')
    return {'cause':cause,'state':row.get('state'),'reason':reason,'evidence':row.get('history',[])}


def _compatible(row, pattern):
    metric = str(pattern.get('metric') or '')
    if metric not in SUPPORTED:
        return False
    kind, _ = SUPPORTED[metric]
    if row.get('market_kind') != kind:
        return False
    if row.get('period') != pattern.get('period','FULL_TIME'):
        return False
    team = norm(pattern.get('team'))
    opponent = norm(pattern.get('opponent'))
    market = norm(row.get('market'))
    if not team or team not in market or (opponent and opponent in market):
        # API W and Bwin (F) are different literal labels. Reuse the existing
        # category-aware exact alias contract only for an explicit team owner;
        # the caller has already verified the fixture/preflight identity.
        owner = str(row.get('market') or '').split(' - ', 1)[0]
        female = r'\b(?:w|women|f|femenina|femenino|frauen)\b'
        if not (re.search(female, norm(owner)) and re.search(female, team)):
            return False
        from generate_context import team_name_similarity
        if (team_name_similarity(owner, pattern.get('team'), 'female') != 100
                or team_name_similarity(owner, pattern.get('opponent'), 'female') == 100):
            return False
    # Direct team lines only: totals without a team and player props are not
    # silently promoted to a team market.
    if 'jugador' in market or 'goleador' in market or 'player' in market:
        return False
    return True


def _dedupe(rows):
    seen, output, duplicates = set(), [], 0
    for row in rows:
        key = (row.get('fixture_id'), row.get('bwin_event_id'), row.get('market'),
               row.get('selection'), row.get('line'), row.get('odd'))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        output.append(row)
    return output, duplicates


def map_markets(pattern_path: Path, preflight_paths=(), *, output_dir: Path | None = None, min_odd=1.20, candidate_ledger=None):
    pattern_path = Path(pattern_path).resolve()
    patterns = read(pattern_path)
    pattern_digest = sha(pattern_path)
    from prematch_predictive_evidence_ranking_v1 import unique_patterns
    rows = unique_patterns(pattern_rows(patterns))
    if not math.isfinite(min_odd) or min_odd<=1: raise MappingError('MIN_ODD_INVALID')
    ledger=read(candidate_ledger) if candidate_ledger else {}
    pattern_source = ('predictive_patterns.json:descriptive_discovery.patterns'
                      if isinstance(patterns.get('descriptive_discovery'), dict)
                      and isinstance(patterns['descriptive_discovery'].get('patterns'), list)
                      else 'pattern_engine.json:patterns')
    preflights = {}
    event_usage = {}
    duplicate_fixtures = set()
    for path in preflight_paths:
        doc = read(Path(path))
        for match in doc.get('matches', []):
            from prematch_paths import local_reference, reference_root
            snap = match.get('snapshot') or {}
            if snap.get('snapshot_path'):
                try:
                    snap['snapshot_path'] = str(local_reference(reference_root(path), snap['snapshot_path'], snap.get('sha256')))
                except (ValueError, OSError):
                    # Fail before any attempt to read an external original.
                    snap['snapshot_path'] = ''
            fid = str(match.get('fixture_id'))
            if fid in preflights:
                duplicate_fixtures.add(fid)
            preflights[fid] = (Path(path).resolve(), match)
            event_usage.setdefault(str(match.get('bwin_event_id')),set()).add(fid)
    by_fixture = {}
    for row in rows:
        by_fixture.setdefault(str(row.get('fixture_id')), []).append(row)
    mappings, counts = [], {
        'patterns_total': len(rows), 'market_matched': 0, 'pending_odds': 0,
        'pending_validation': 0, 'market_not_available': 0,
        'unsupported_market': 0, 'identity_blocked': 0, 'duplicate_market_rows_removed': 0, 'discarded_candidate':0,
    }
    for fid, fixture_patterns in sorted(by_fixture.items(), key=lambda x: x[0]):
        base = fixture_patterns[0]
        entry = preflights.get(fid)
        if fid in duplicate_fixtures:
            identity_reason = 'DUPLICATE_PREFLIGHT_FIXTURE'
            valid_identity = False
            match = None
            market_rows = []
        elif not entry:
            identity_reason = None
            valid_identity = None
            match = None
            market_rows = []
        else:
            preflight_path, match = entry
            valid_identity, identity_reason = _preflight_identity(base, match)
            if len(event_usage.get(str(match.get('bwin_event_id')),set()))>1:
                valid_identity,identity_reason=False,'BWIN_EVENT_REUSED_BY_MULTIPLE_FIXTURES'
            market_rows, market_reason = _load_market_rows(preflight_path, match)
            if market_reason and valid_identity:
                identity_reason = market_reason
                valid_identity = False
        market_rows, duplicates = _dedupe(market_rows)
        counts['duplicate_market_rows_removed'] += duplicates
        for pattern in fixture_patterns:
            metric = str(pattern.get('metric') or '')
            if valid_identity is False:
                status, reason, links = 'IDENTITY_BLOCKED', identity_reason, []
            elif valid_identity is None:
                status, reason, links = 'PENDING_ODDS', 'NO_VERIFIED_PREFLIGHT', []
            elif metric in UNSUPPORTED or metric not in SUPPORTED:
                status, reason, links = 'UNSUPPORTED_MARKET', 'NO_COMPATIBLE_TEAM_MODEL_IN_V1', []
            else:
                links = [row for row in market_rows if _compatible(row, pattern)]
                if not links:
                    status, reason = 'MARKET_NOT_AVAILABLE', 'NO_EXACT_TEAM_MARKET_LINE_IN_VERIFIED_CAPTURE'
                    if any(_compatible(row,dict(pattern,period=row['period'])) for row in market_rows):
                        reason='ONLY_INCOMPATIBLE_PERIOD_OBSERVED'
                    else:
                        from bwin_market_preflight import MARKET_LABEL_RE, market_kind
                        text=Path(match['snapshot']['snapshot_path']).read_text(encoding='utf-8-sig')
                        headings=[m.group(1) for line in text.splitlines() if (m:=MARKET_LABEL_RE.match(line))]
                        if any(_compatible({'market':h,'market_kind':market_kind(h),'period':market_period(h)},pattern) for h in headings):
                            reason='MARKET_OBSERVED_WITHOUT_USABLE_PRICE'
                else:
                    prices = {}
                    for link in links:
                        key = (link.get('market'), link.get('selection'), link.get('line'), link.get('direction'))
                        prices.setdefault(key, set()).add(str(link.get('odd')))
                    ambiguous = any(len(values) > 1 for values in prices.values())
                if links and ambiguous:
                    status, reason, links = 'MARKET_NOT_AVAILABLE', 'AMBIGUOUS_MARKET_SELECTION', []
                elif links and not any(x['odd']>=min_odd for x in links):
                    status, reason = 'DISCARDED_CANDIDATE', 'ALL_OBSERVED_PRICES_BELOW_CONFIGURED_MINIMUM'
                elif links and pattern_status(pattern) != 'DESCRIPTIVE_SUPPORTED':
                    status, reason = 'PENDING_VALIDATION', 'DESCRIPTIVE_PATTERN_LACKS_MINIMUM_SAMPLE'
                elif links:
                    status, reason = 'MARKET_MATCHED', 'EXACT_TEAM_MARKET_LINE_AND_DESCRIPTIVE_PATTERN'
            counts[status.lower()] += 1
            mappings.append({
                'pattern_id':pattern.get('pattern_id') or hashlib.sha256(json.dumps(pattern,sort_keys=True).encode()).hexdigest(),
                'pattern_source':str(pattern_path),'pattern_source_sha256':pattern_digest,'pattern_engine_version':patterns.get('engine_version'),
                'fixture_id': pattern.get('fixture_id'), 'bwin_event_id': (match or {}).get('bwin_event_id'),
                'team_id':pattern.get('team_id'),'player_id':pattern.get('player_id'), 'period':pattern.get('period','FULL_TIME'),
                'home': pattern.get('home'), 'away': pattern.get('away'), 'team': pattern.get('team'),
                'team_side': pattern.get('team_side'), 'metric': metric,
                'commercial_model_supported':metric in SUPPORTED,
                'pattern_status': pattern_status(pattern), 'pattern_flag': pattern.get('pattern_flag') or pattern.get('effect_descriptive'),
                'mapping_status': status, 'reason': reason, 'actionable_bet': False,
                'validation_status': 'NOT_CALIBRATED_FOR_BETTING',
                'identity_status':'PREFLIGHT_CONTRACT_CORROBORATED' if valid_identity else 'UNVERIFIED',
                'identity_evidence':(match or {}).get('time_evidence'),
                'pending_evidence':pending_evidence(fid,ledger) if status=='PENDING_ODDS' else None,
                'observation_status':('MARKET_OBSERVED_WITHOUT_USABLE_PRICE' if reason=='MARKET_OBSERVED_WITHOUT_USABLE_PRICE' else 'MARKET_LINKED' if links else 'PATTERN_DISCOVERED_PENDING_CAPTURE' if status=='PENDING_ODDS' else 'MARKET_NOT_OBSERVED' if status=='MARKET_NOT_AVAILABLE' else status),
                'market_links': [{**{k: row.get(k) for k in ('market', 'selection', 'line', 'direction', 'odd', 'period', 'market_kind', 'price_tier', 'data_status', 'source_path','review_sha256')},
                    'fixture_id':pattern.get('fixture_id'),'event_id':(match or {}).get('bwin_event_id'),
                    'snapshot_path':(match or {}).get('snapshot',{}).get('snapshot_path'),
                    'snapshot_sha256':(match or {}).get('snapshot',{}).get('sha256'),
                    'captured_at':(match or {}).get('snapshot',{}).get('capture_declared_madrid'),
                    'price_eligible':row['odd']>=min_odd,'commercial_status':'ELIGIBLE_FOR_EVALUATION' if row['odd']>=min_odd else 'DISCARDED_BELOW_MIN_ODD',
                    'settlement_rule':'BWIN_RULES_REVIEW_REQUIRED'} for row in links],
            })
    result = {
        'schema_version': VERSION, 'prematch_only': True, 'actionable_bet': False,
        'generated_at_utc': patterns.get('asof_madrid'), 'code_sha256': code_sha(),
        'minimum_odd':min_odd,'mode':'FROZEN_SNAPSHOT_AUDIT_NOT_CURRENT_AUTHORIZATION',
        'pending_causes':dict(Counter(x['pending_evidence']['cause'] for x in mappings if x['pending_evidence'])),
        'candidate_ledger':{'path':str(candidate_ledger),'sha256':sha(candidate_ledger)} if candidate_ledger else None,
        'data_sources': {'patterns': str(pattern_path), 'patterns_sha256': sha(pattern_path),
                         'preflights': [{'path': str(Path(p).resolve()), 'sha256': sha(Path(p))} for p in preflight_paths]},
        'methodology': {'direct_team_markets_only': True, 'no_probabilities_ev_or_picks': True,
                        'pattern_source': pattern_source,
                        'supported_metrics': sorted(SUPPORTED), 'unsupported_metrics': sorted(UNSUPPORTED),
                        'null_not_zero': True},
        'counts': {**counts, 'rows_output': len(mappings)}, 'mappings': mappings,
    }
    if output_dir:
        output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / 'market_mapping.json'
        raw = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
        if target.is_file():
            if target.read_text(encoding='utf-8') != raw:
                raise MappingError('MARKET_MAPPING_OUTPUT_EXISTS_CONFLICT')
        else:
            tmp = target.with_name('.market_mapping.json.tmp')
            tmp.write_text(raw, encoding='utf-8'); tmp.replace(target)
        md_target = output_dir / 'market_mapping.md'
        md_raw = report(result)
        if not md_target.is_file() or md_target.read_text(encoding='utf-8') != md_raw:
            md_tmp = md_target.with_name('.market_mapping.md.tmp')
            md_tmp.write_text(md_raw, encoding='utf-8'); md_tmp.replace(md_target)
    return result


def report(result):
    c = result['counts']
    lines = [f'# Market Mapping V1 ({VERSION})', '',
             'No contiene picks, probabilidades, EV ni autorización comercial.', '',
             '| Estado | Cantidad |', '|---|---:|']
    for key in ('patterns_total', 'market_matched', 'pending_odds', 'pending_validation',
                'market_not_available', 'unsupported_market', 'identity_blocked','discarded_candidate'):
        lines.append(f"| {key} | {c[key]} |")
    lines += ['', '## Evidencia', '']
    lines += [f"- Pendientes: {json.dumps(result.get('pending_causes',{}),ensure_ascii=False,sort_keys=True)}",
              f"- Cuota mínima comercial: {result.get('minimum_odd')}. No equivale a EV ni probabilidad propia.",
              '- Ausencia de mercado: observada sólo en esta captura y alcance.',
              '- Siguiente acción obligatoria: resolver identidad/rutas pendientes o revalidar cuotas antes del uso actual. XI oficial cuando la tesis dependa del jugador; investigación cualitativa opcional. No se ejecuta adquisición.', '']
    for row in result['mappings']:
        if row['mapping_status'] == 'MARKET_MATCHED':
            links = '; '.join(f"{x['market']} / {x['selection']} @ {x['odd']}" for x in row['market_links'])
            lines.append(f"- Fixture `{row['fixture_id']}`, Bwin `{row['bwin_event_id']}`, {row['team']} / {row['metric']}: {links}")
    return '\n'.join(lines) + '\n'


def classify_signal_market(signals, metric, side, period, line):
    """Structural evidence only; no probability, direction forecast or joint model."""
    if metric=='offsides':return 'DESCRIPTIVE_ONLY',[],['OFFSIDES_EXPERIMENTAL_NOT_COMMERCIAL']
    if period!='FULL_TIME':return 'MARKET_WITHOUT_EVIDENCE',[],['NO_COMPATIBLE_PERIOD_MODEL']
    family=[s for s in signals if s.get('metric')==metric and (side is None or s.get('team_side')==side)]
    supported=[s for s in family if s.get('status')=='SUPPORTED_DESCRIPTIVE_SIGNAL' and not s.get('contradictions')]
    if not family:return 'MARKET_WITHOUT_EVIDENCE',[],['NO_COMPATIBLE_SIGNAL']
    if side is None:
        return ('DESCRIPTIVE_ONLY' if supported else 'CONTRADICTED' if any(s.get('contradictions') for s in family) else 'DATA_INSUFFICIENT'),family,['MATCH_TOTAL_REQUIRES_JOINT_MODEL_NOT_TEAM_SIGNAL']
    if any(s.get('contradictions') for s in family):return 'CONTRADICTED',family,['MATERIAL_SERIES_CONTRADICTION']
    if supported and line is not None:return 'SUPPORTED_MARKET_MATCH',supported,['DIRECT_TEAM_FT_STRUCTURAL_MATCH_NOT_DIRECTION_FORECAST']
    return 'DATA_INSUFFICIENT',family,['NO_SUPPORTED_SIGNAL_OR_SUPPORTED_SETTLEMENT_LINE']


def integrate_reconciliation(root, output_dir, *, asof=None):
    """Join existing scanner-verified quotes with their tactical evidence offline.

    This adapter does not merge tactical signals into Predictor V1 patterns.
    Default clock is the frozen scanner cutoff: audit only, never current approval.
    """
    from prematch_opportunity_scanner import validate_snapshot, identify_market, parse_line as safe_line
    from prematch_predictive_evidence_ranking_v1 import write_once
    root=Path(root).resolve();output_dir=Path(output_dir)
    from prematch_paths import local_reference, manifest_reference, reference_root
    project = reference_root(root / 'INTEGRITY_MANIFEST.json')
    scanpath=root/'scanner/opportunity_scan.json';scan=read(scanpath)
    try: prepath=local_reference(project,scan['sources']['preselection'],scan['sources']['preselection_sha256'])
    except ValueError as exc: raise MappingError('SCANNER_PRESELECTION_SHA_MISMATCH') from exc
    pre=read(prepath)
    if sha(prepath)!=scan['sources']['preselection_sha256']:raise MappingError('SCANNER_PRESELECTION_SHA_MISMATCH')
    tacticalpath=root/'tactical_reused/tactical_signal_engine.json';tactical=read(tacticalpath)
    ledgerpath=root/'ledger_final.json';ledger=read(ledgerpath)['fixtures']
    statpaths=list((root/'ranking_final').glob('runs/*/ranking.json'))
    if len(statpaths)!=1:raise MappingError('STATISTICAL_RANKING_SOURCE_NOT_UNIQUE')
    statpath=statpaths[0];statdoc=read(statpath);stat_byid={r['fixture_id']:r for r in statdoc['ranking']}
    integrity=read(root/'INTEGRITY_MANIFEST.json')['files_sha256']
    for path in (scanpath,prepath,tacticalpath,ledgerpath,statpath):
        try: manifest_reference(project,path,integrity)
        except ValueError as exc: raise MappingError('INTEGRITY_MANIFEST_SOURCE_MISMATCH:'+str(path)) from exc
    if tactical['sources']['preselection_sha256']!=sha(prepath):raise MappingError('TACTICAL_PRESELECTION_SHA_MISMATCH')
    clock=dt(asof or scan['asof_madrid']);audit_clock=dt(scan['asof_madrid'])
    if clock is None or audit_clock is None:raise MappingError('CLOCK_INVALID')
    fixtures={int(f['fixture_id']):f for f in pre['matches']}
    if len(fixtures)!=len(pre['matches']) or set(fixtures)!=set(map(int,tactical['fixtures'])):raise MappingError('FIXTURE_UNIVERSE_MISMATCH')
    audits={};events={};sources={str(p):sha(p) for p in (scanpath,prepath,tacticalpath,ledgerpath,statpath)}
    for a in scan['audits']:
        fid=int(a['fixture_id'])
        if fid in audits:raise MappingError('DUPLICATE_SCANNER_FIXTURE')
        audits[fid]=a
        eid=str(a.get('bwin_event_id'))
        if eid in events and events[eid]!=fid:raise MappingError('EVENT_REUSED_ACROSS_FIXTURES')
        events[eid]=fid
    quotes=[];fixture_rows=[];coverage=[];pattern_results=[];seen=set()
    ledgers={r['fixture_id']:r for r in ledger}
    for fid,f in fixtures.items():
        tf=tactical['fixtures'][str(fid)];signals=tf.get('signals',[]);supported=[s for s in signals if s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL' and s.get('metric')!='offsides']
        a=audits.get(fid);reasons=[];parsed=[];pm=None;path=None
        if a:
            path=manifest_reference(project,a['preflight_path'],integrity)
            pf=read(path);rows=[r for r in pf['matches'] if r['fixture_id']==fid]
            if len(rows)!=1:raise MappingError('PREFLIGHT_FIXTURE_NOT_UNIQUE')
            pm=rows[0];snap=manifest_reference(project,pm['snapshot']['snapshot_path'],integrity);sources[str(path)]=sha(path)
            pm['snapshot']['snapshot_path']=str(snap)
            if a['status']!='VERIFIED':reasons.append('SCANNER_NOT_VERIFIED')
            if str(a['bwin_event_id'])!=str(pm['bwin_event_id']) or a['snapshot']['sha256']!=pm['snapshot']['sha256']:reasons.append('SCANNER_PREFLIGHT_ID_OR_SHA_CONFLICT')
            if not snap.is_file() or sha(snap)!=pm['snapshot'].get('sha256'):reasons.append('SNAPSHOT_MANIFEST_SHA_MISMATCH_OR_MISSING')
            bad,raw,meta=validate_snapshot(f,pm,pf,path,prepath,None,audit_clock,120,5,True)
            reasons.extend(bad)
            # Verify the original discovery for every time-evidence type.
            discpath=local_reference(project,pf['source_discovery'],pf['source_sha256']);disc=read(discpath)
            if sha(discpath)!=pf['source_sha256']:reasons.append('DISCOVERY_SHA_MISMATCH')
            origin=[r for r in disc['matches'] if r['fixture_id']==fid and str(r.get('bwin_event_id'))==str(pm['bwin_event_id'])]
            if len(origin)!=1 or any(origin[0].get(k)!=f.get(k) for k in ('home_id','away_id','league_id','season','gender')):reasons.append('DISCOVERY_API_IDENTITY_CONFLICT')
            if len(origin)==1:
                from bwin_market_preflight import validate_match
                check=validate_match(origin[0],disc.get('catalog_observed',[]),audit_clock,0,180)
                if check['status']!='READY_FOR_SCRAPER':reasons.extend(check['reasons'])
            sources[str(discpath)]=sha(discpath)
            if snap.is_file():sources[str(snap)]=sha(snap)
            if not reasons:parsed=raw
        else:reasons=['NO_SCANNER_VERIFIED_CAPTURE']
        capture=dt(pm['snapshot']['capture_declared_madrid']) if pm else None
        ko=dt(f['kickoff_madrid']);age=(clock-capture).total_seconds()/60 if capture else None
        freshness={'statistics_cutoff':f.get('analysis_cutoff_utc'),'schedule_observed_at':f.get('schedule_reconciliation',{}).get('observed_at_utc'),
                   'odds_capture':capture.isoformat() if capture else None,'odds_age_minutes':round(age,3) if age is not None else None,
                   'odds_within_15_minutes_at_evaluation':age is not None and 0<=age<=15,'lineups':'NOT_VERIFIED_BY_THIS_JOIN',
                   'evaluation_asof':clock.isoformat(),'current_final_validation_required':True}
        per_series=[{'signal_id':s['signal_id'],'metric':s['metric'],'team_side':s['team_side'],'status':s['status'],'contradictions':s.get('contradictions',[]),
                    'own':s.get('own',{}),'opponent_conceded':s.get('opponent_conceded',{})} for s in signals]
        coverage.append({'fixture_id':fid,'legacy_minimum_projection':tf.get('coverage_summary'),
                         'metric_availability_mean_pct':tf.get('data_coverage'),'series':per_series,
                         'explanation':'Minimum across heterogeneous series is not goals coverage; missing metric stays null or n=0, never a sporting zero.'})
        bound_ids=set();fixture_quote_rows=[]
        for q in parsed:
            key=(fid,str(pm['bwin_event_id']),q['market'],q['selection'],q['odd'])
            if key in seen:raise MappingError('DUPLICATE_QUOTE')
            seen.add(key)
            metric,side,structure=identify_market(q['market'],f['home'],f['away'])
            period=market_period(q['market']);line=safe_line(q['selection'])
            # Reuse strict scanner identification; unsupported structures cannot
            # become a direct team association just by containing its name.
            if structure!='MODELABLE':metric=None if metric!='offsides' else metric
            state,matched,why=classify_signal_market(signals,metric,side,period,line)
            if metric is None:state='MARKET_WITHOUT_EVIDENCE';why=[structure]
            if metric=='offsides':state='DESCRIPTIVE_ONLY';why=['OFFSIDES_EXPERIMENTAL_NOT_COMMERCIAL']
            bindings=[{'signal_id':s['signal_id'],'source':str(tacticalpath),'source_sha256':sha(tacticalpath),'version':tactical['schema_version'],
                       'metric':s['metric'],'team_side':s['team_side'],'team_id':s.get('team_id'),'status':s['status'],
                       'own_n':s.get('own',{}).get('n'),'opponent_n':s.get('opponent_conceded',{}).get('n'),'contradictions':s.get('contradictions',[])} for s in matched]
            bound_ids.update(s['signal_id'] for s in matched)
            token=hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()
            row={'quote_id':token,'fixture_id':fid,'bwin_event_id':str(pm['bwin_event_id']),**q,'period':period,'metric':metric,'team_side':side,
                 'line':(parse_line(q['selection']) or {}).get('line'),'direction':(parse_line(q['selection']) or {}).get('direction'),'settlement_line_supported':line is not None,
                 'classification':state,'reasons':why,'pattern_evidence':bindings,'capture_timestamp':capture.isoformat(),
                 'txt_path':pm['snapshot']['snapshot_path'],'txt_sha256':pm['snapshot']['sha256'],'preflight_path':str(path),'preflight_sha256':sha(path),
                 'identity_status':'VERIFIED_AT_CAPTURE','commercial_market_eligible':state=='SUPPORTED_MARKET_MATCH' and metric!='offsides',
                 'price_filter_passed':q['odd']>=1.20,'actionable_bet':False}
            fixture_quote_rows.append(row);quotes.append(row)
        for s in supported:
            pattern_results.append({'fixture_id':fid,'signal_id':s['signal_id'],'source':str(tacticalpath),'source_sha256':sha(tacticalpath),
                'classification':'MARKET_NOT_VERIFIED' if not parsed else 'CONTRADICTED' if s.get('contradictions') else 'SUPPORTED_MARKET_MATCH' if any(q['classification']=='SUPPORTED_MARKET_MATCH' and any(e['signal_id']==s['signal_id'] for e in q['pattern_evidence']) for q in fixture_quote_rows) else 'DESCRIPTIVE_ONLY' if s['signal_id'] in bound_ids else 'NO_MATCHING_MARKET',
                'reason':'See individual quote bindings; team signal cannot supply a joint FT match-total model.'})
        direct=sum(q['classification']=='SUPPORTED_MARKET_MATCH' for q in fixture_quote_rows)
        eligible=bool(parsed) and bool(supported) and ledgers[fid]['research_eligible'] and ko is not None and clock<ko and freshness['odds_within_15_minutes_at_evaluation']
        fixture_rows.append({'fixture_id':fid,'home':f['home'],'away':f['away'],'event_id':str(pm['bwin_event_id']) if pm else ledgers[fid].get('event_id'),
            'kickoff_madrid':f['kickoff_madrid'],'market_state':'MARKETS_VERIFIED' if parsed else 'MARKET_NOT_VERIFIED',
            'captured_selections':len(parsed),'market_count':len({q['market'] for q in parsed}),'supported_patterns':len(supported),'supported_market_matches':direct,'supported_signal_market_groups':len({e['signal_id'] for q in fixture_quote_rows if q['classification']=='SUPPORTED_MARKET_MATCH' for e in q['pattern_evidence']}),
            'classifications':dict(Counter(q['classification'] for q in fixture_quote_rows)),'coverage':tf.get('coverage_summary'),
            'tier':ledgers[fid]['selection_tier'],'research_eligible':ledgers[fid]['research_eligible'],'hard_block_reasons':ledgers[fid]['hard_block_reasons'],
            'research_priority':stat_byid.get(fid,{}).get('research_priority'),'statistical_rank':next((i+1 for i,r in enumerate(statdoc['ranking']) if r['fixture_id']==fid),None),
            'operational_research_queue_eligible_at_evaluation':eligible,'research_mode':'CONDITIONED_RESEARCH' if eligible else 'SUPPORTED_RESEARCH_ODDS_REFRESH_REQUIRED' if parsed and supported and ledgers[fid]['research_eligible'] else 'EXPLORATORY_ONLY' if parsed else 'BLOCKED_OR_PENDING_CAPTURE',
            'freshness':freshness,'capture_rejection_reasons':sorted(set(reasons)),'original_binding_state':ledgers[fid]['binding_state'],'actionable_bet':False})
    # Preserve the statistical ordering; market availability never changes it.
    statistical=[dict(r,statistical_rank=i+1) for i,r in enumerate(statdoc['ranking'])]+[dict(r,statistical_rank=None) for r in statdoc['ledger'] if r['fixture_id'] not in stat_byid]
    # Lexicographic heuristic: gate, structural matches, existing evidence score,
    # kickoff/ID. Quote count and club popularity do not increase priority.
    commercial=sorted(fixture_rows,key=lambda r:(not r['operational_research_queue_eligible_at_evaluation'],r['market_state']!='MARKETS_VERIFIED',-r['supported_signal_market_groups'],-(r['research_priority'] if r['research_priority'] is not None else -999),r['kickoff_madrid'],r['fixture_id']))
    for i,r in enumerate(commercial,1):r['commercial_research_rank']=i
    result={'schema_version':'1.0-reconciliation-commercial-join','mode':'OFFLINE_FROZEN_AUDIT_NOT_CURRENT_APPROVAL','evaluation_asof':clock.isoformat(),
            'scanner_evidence_asof':scan['asof_madrid'],'input_hashes':sources,'code_sha256':code_sha(),
            'statistical_ranking':statistical,'commercial_ranking':commercial,
            'counts':{'fixtures':len(fixtures),'scanner_verified_captures':sum(r['market_state']=='MARKETS_VERIFIED' for r in commercial),'selections':len(quotes),
                      'supported_market_matches':sum(r['supported_market_matches'] for r in commercial),'classifications':dict(Counter(q['classification'] for q in quotes))},
            'policy':{'max_final_odds_age_minutes':15,'final_validation_required':True,'heuristic':'Gate, distinct signal-market groups (not line count), unchanged evidence score, kickoff and fixture ID. Not calibrated predictability.','statistical_source':'TACTICAL_SIGNAL_ENGINE_ONLY_NO_PREDICTOR_V1_MIX','offside_commercial_use':False},'actionable_bet':False}
    payloads={'commercial_ranking.json':result,'market_pattern_matches.json':{'quotes':quotes,'pattern_results':pattern_results,'counts':result['counts'],'input_hashes':sources},
              'coverage_diagnostics.json':{'fixtures':coverage,'basis':'METRIC_SPECIFIC_DENOMINATORS_NOT_LEGACY_MINIMUM'}}
    md=['# Cruce comercial offline — no picks','',f"Corte evaluado: {clock.isoformat()}; scanner: {scan['asof_madrid']}. No acredita vigencia actual. Recaptura final ≤15 minutos y demás controles obligatorios.",
        '',f"{result['counts']['scanner_verified_captures']} capturas verificadas; {result['counts']['selections']} selecciones únicas; {result['counts']['supported_market_matches']} coincidencias directas respaldadas.",'','Ranking estadístico y comercial son independientes. Tier original preservado. Un mercado total de partido no hereda el modelo de un equipo.','',
        '|Fixture|Event ID|Selecciones / mercados|Patrones respaldados|Coincidencias directas|Cobertura mínima|Motivos|Investigación|','|---:|---|---|---:|---:|---|---|---|']
    for r in commercial:md.append(f"|{r['fixture_id']}|{r['event_id']}|{r['captured_selections']} / {r['market_count']}|{r['supported_patterns']}|{r['supported_market_matches']}|{r['coverage'].get('historical_effective_coverage_pct')}|{'; '.join(r['capture_rejection_reasons']+r['hard_block_reasons'])}|{r['research_mode']}; Tier {r['tier']}|")
    for r in commercial:
        if not r['captured_selections']:continue
        md += ['',f"## {r['home']}–{r['away']} ({r['fixture_id']})",'','Todas las selecciones, líneas, precios, referencias TXT y razones están en market_pattern_matches.json. Ejemplos reales:']
        sample=[q for q in quotes if q['fixture_id']==r['fixture_id'] and q['period']=='FULL_TIME' and q['line'] is not None][:4]
        for q in sample:md.append(f"- {q['market']} / {q['selection']} @ {q['odd']}: {q['classification']}; {q['reasons']}; TXT línea {q['txt_line']}.")
    payloads['commercial_ranking.md']='\n'.join(md)+'\n'
    for name,value in payloads.items():
        content=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        write_once(output_dir/name,content)
    return result


def audit_coverage_models(root, output_dir):
    """Offline derivative only; no acquisition, picks, EV or final validation."""
    from prematch_tactical_history import audit_frozen_coverage
    from prematch_predictive_engine import joint_ft_goal_baseline,poisson_pmf,distribution,probability_over,result_available_at
    from prematch_tactical_signal_engine import analyze_fixture
    from prematch_predictive_evidence_ranking_v1 import write_once
    from collections import defaultdict
    root=Path(root).resolve();out=Path(output_dir);oldroot=root/'commercial_join'
    old=read(oldroot/'commercial_ranking.json');quotes=read(oldroot/'market_pattern_matches.json')['quotes'];diagnostics=read(oldroot/'coverage_diagnostics.json')
    for path,digest in old['input_hashes'].items():
        from prematch_paths import local_reference, reference_root
        try: local_reference(reference_root(root/'commercial_join/commercial_ranking.json'),path,digest)
        except ValueError as exc: raise MappingError('FROZEN_INPUT_CHANGED:'+path) from exc
    source=root/'verified_schedule/preselection_reconciled.json';pre=read(source)
    recovered,audit=audit_frozen_coverage(Path(__file__).parent,pre)
    recovered.update(coverage_recovery_is_offline_derivative=True,original_preselection_path=str(source),original_preselection_sha256=sha(source),coverage_recovery_sources=audit['source_hashes'])
    old_statuses=Counter(s['status'] for f in diagnostics['fixtures'] for s in f['series'])
    models={};post_signals={};before={}
    for f in pre['matches']:
        fid=f['fixture_id'];at=dt(f['analysis_cutoff_utc'])
        before[str(fid)]=analyze_fixture(f,at,f.get('statistical_observations') or {})
    for f in recovered['matches']:
        fid=f['fixture_id'];at=dt(f['analysis_cutoff_utc']);obs=f.get('statistical_observations') or {}
        models[str(fid)]=joint_ft_goal_baseline(f,obs,at)
        models[str(fid)]['commercial_min_n_existing_scanner']=15
        post_signals[str(fid)]=analyze_fixture(f,at,obs)
    # Temporal retrospective tests use actual historical home/away identities.
    team_rows=defaultdict(dict);targets={}
    for f in recovered['matches']:
        for side,rows in f.get('statistical_observations',{}).items():
            for r in rows:
                team_rows[r['team_id']][r['fixture_id']]=r
                if r['status']=='FT' and all(isinstance(r.get(k,{}).get('goals'),(int,float)) for k in ('own','conceded')):
                    hid=r['team_id'] if r['venue']=='HOME' else r['opponent_id'];aid=r['opponent_id'] if r['venue']=='HOME' else r['team_id']
                    target={'fixture_id':r['fixture_id'],'home_id':hid,'away_id':aid,'league_id':r['league_id'],'season':r['season'],'kickoff_madrid':r['kickoff_utc']}
                    actual=r['own']['goals']+r['conceded']['goals']
                    if r['fixture_id'] in targets and targets[r['fixture_id']][1]!=actual:raise MappingError('BACKTEST_TARGET_SCORE_CONFLICT')
                    targets[r['fixture_id']]=(target,actual)
    backtests=[]
    for target,actual in sorted(targets.values(),key=lambda x:(x[0]['kickoff_madrid'],x[0]['fixture_id'])):
        if target['home_id'] not in team_rows or target['away_id'] not in team_rows:continue
        at=dt(target['kickoff_madrid']);obs={s:list(team_rows[target[s+'_id']].values()) for s in ('home','away')}
        model=joint_ft_goal_baseline(target,obs,at,bootstrap_samples=0)
        if model['status']!='MODEL_EVALUABLE':continue
        for side in ('home','away'):
            for kind in ('own','conceded'):
                ids=model['series'][side+'_'+kind]['fixture_ids']
                assert target['fixture_id'] not in ids
                assert all(result_available_at(team_rows[target[side+'_id']][i])[0]<=at for i in ids)
        pp=poisson_pmf(int(actual),model['lambda_total']);ep=model['empirical_total_distribution'].get(str(int(actual)),0)
        backtests.append({'fixture_id':target['fixture_id'],'home_id':target['home_id'],'away_id':target['away_id'],'league_id':target['league_id'],'season':target['season'],'prediction_cutoff':at.isoformat(),
            'actual_total_goals':actual,'predicted_lambda':model['lambda_total'],'poisson_log_loss':-math.log(max(pp,1e-12)),
            'empirical_log_loss':-math.log(max(ep,1e-12)),'training_fixture_ids':{k:v['fixture_ids'] for k,v in model['series'].items()},
            'acquisition_availability':'RETROSPECTIVE_RECONSTRUCTED_NOT_POINT_IN_TIME_PROOF','model_validated':False})
    from market_quality_gate import market_kind,is_player_market
    updated=[];family_counts=defaultdict(Counter)
    for q in quotes:
        row=dict(q);fid=str(q['fixture_id']);model=models[fid]
        underlying='UNDERLYING_SIGNAL_CONTRADICTED' if q['classification']=='CONTRADICTED' or any(e.get('contradictions') for e in q.get('pattern_evidence',[])) else 'NO_RECORDED_CONTRADICTION'
        metric=q.get('metric');period=q['period'];family=market_kind(q['market']);status='DATA_INSUFFICIENT';category='MISSING_DATA';reasons=[]
        if metric=='offsides':category='EXPERIMENTAL_EXCLUDED';reasons=['OFFSIDES_NOT_COMMERCIAL']
        elif is_player_market(q['market'],family):category='MISSING_DATA';reasons=['PLAYER_EXPOSURE_AND_PLAYER_MODEL_UNAVAILABLE_NO_TEAM_PROXY']
        elif period in ('FIRST_HALF','SECOND_HALF'):status='JOINT_MODEL_MISSING';category='MISSING_MODEL';reasons=['PERIOD_SPECIFIC_MODEL_MISSING_FT_NOT_TRANSFERRED']
        elif period!='FULL_TIME':category='INCOMPATIBLE_MARKET';reasons=['PERIOD_NOT_CORROBORATED_OR_STRUCTURE_INCOMPATIBLE']
        elif metric=='goals' and q.get('line') is not None and q.get('settlement_line_supported'):
            if model['status']=='MODEL_EVALUABLE':
                status='MODEL_EVALUABLE';category='UNVALIDATED_MODEL';reasons=['JOINT_GOALS_BASELINE_NOT_CALIBRATED','FINAL_VALIDATION_REQUIRED']
                rate=model['lambda_total'] if q['team_side'] is None else model['lambda_home' if q['team_side']=='home' else 'lambda_away']
                over=probability_over(distribution(rate,'POISSON'),q['line']);row['baseline_probability_estimate']=over if q['direction']=='OVER' else 1-over
                row['probability_label']='UNCALIBRATED_BASELINE_ESTIMATE_NOT_BWIN_EDGE'
                if q['team_side'] is None:
                    emp=sum(v for k,v in model['empirical_total_distribution'].items() if int(k)>q['line']);row['empirical_probability_estimate']=emp if q['direction']=='OVER' else 1-emp
                    bounds=model['parameter_uncertainty']['lambda_total_interval_90']
                    row['probability_sampling_interval_under_iid']=sorted([(probability_over(distribution(lam,'POISSON'),q['line']) if q['direction']=='OVER' else 1-probability_over(distribution(lam,'POISSON'),q['line'])) for lam in bounds])
            else:category='SMALL_SAMPLE';reasons=['FOUR_GOALS_SERIES_REQUIRE_AT_LEAST_6_FOR_DIAGNOSTIC_ESTIMATION','COMMERCIAL_THRESHOLD_15_UNCHANGED']
        elif metric in ('corners','shots','on_target','fouls','yellow'):
            status='JOINT_MODEL_MISSING';category='MISSING_MODEL';reasons=['NO_FITTED_COMPATIBLE_JOINT_OR_TEAM_MODEL_FOR_THIS_FAMILY']
        elif metric is None:
            if family in ('result','double_chance','handicap','correct_score','both_teams_to_score','half_full_time','combined_or_builder','first_team_to_score'):status='JOINT_MODEL_MISSING';category='MISSING_MODEL';reasons=['JOINT_FT_SCORE_OR_EVENT_MODEL_NOT_IMPLEMENTED_NOT_TOTAL_GOALS']
            else:category='INCOMPATIBLE_MARKET';reasons=['NO_COMPATIBLE_FT_METRIC_AND_SETTLEMENT_CONTRACT']
        else:category='INCOMPATIBLE_SETTLEMENT';reasons=['SETTLEMENT_OR_DIRECTION_NOT_SUPPORTED']
        row.update(classification_before=q['classification'],classification=status,underlying_signal_status=underlying,
                   market_direction_status='NOT_TESTED_NO_VALIDATED_DIRECTIONAL_MODEL',market_family=family,blocker_category=category,model_reasons=reasons,
                   model_validated=False,predictive_use_authorized=False,commercial_market_eligible=False,actionable_bet=False)
        updated.append(row);family_counts[family][category]+=1
    joined=defaultdict(list)
    for q in updated:joined[q['fixture_id']].append(q)
    ranks=[]
    for prior in old['commercial_ranking']:
        fid=prior['fixture_id'];qs=joined[fid];post=post_signals[str(fid)];model=models[str(fid)]
        ranks.append({**prior,'coverage_audit_basis':'METRIC_SPECIFIC_ELIGIBLE_REQUESTED_AND_USABLE_DENOMINATORS',
                      'model_status':model['status'],'model_validated':False,'evaluable_selections':sum(q['classification']=='MODEL_EVALUABLE' for q in qs),
                      'supported_signals_after_cache_recovery':post['signal_count'],'original_tier_unchanged':True,
                      'research_mode_after_audit':'JOINT_GOALS_BASELINE_RESEARCH_PENDING_VALIDATION' if qs and model['status']=='MODEL_EVALUABLE' else 'EXPLORATORY_ONLY' if qs else 'BLOCKED_OR_PENDING_CAPTURE',
                      'operational_research_queue_eligible_at_evaluation':False,'actionable_bet':False})
    ranks.sort(key=lambda r:(r['market_state']!='MARKETS_VERIFIED',r['model_status']!='MODEL_EVALUABLE',-(r['research_priority'] if r['research_priority'] is not None else -999),r['fixture_id']))
    for i,r in enumerate(ranks,1):r['model_research_rank']=i
    comparable_before=Counter(s['status'] for fx in before.values() for s in fx['signals'])
    comparable_after=Counter(s['status'] for fx in post_signals.values() for s in fx['signals'])
    counts={'fixtures':len(ranks),'captured_selections':len(updated),'series_audited':len(audit['series']),'source_signal_rows_by_status':dict(old_statuses),'same_snapshot_cutoff_signal_status_before':dict(comparable_before),'same_snapshot_cutoff_signal_status_after':dict(comparable_after),
            'recovered_observations':len(audit['recovered']),'series_with_recovery':sum(s['counts'].get('RECOVERED_FROM_PRE_CUTOFF_CACHE',0)>0 for s in audit['series']),
            'parser_failures':sum(s['counts'].get('PARSER_OR_DUPLICATE_BLOCK_FAILURE',0) for s in audit['series']),
            'evaluable_selections':sum(q['classification']=='MODEL_EVALUABLE' for q in updated),'validated_selections':0,
            'new_classifications':dict(Counter(q['classification'] for q in updated)),
            'backtest_reconstructed_targets':len(backtests),'point_in_time_verified_backtests':0,'api_calls':0}
    result={'counts':counts,'ranking':ranks,'statistical_ranking_original':old['statistical_ranking'],'family_blockers':{k:dict(v) for k,v in family_counts.items()},
            'policy':'OFFLINE_RECONSTRUCTION_ONLY_NO_CALIBRATION_NO_EV_NO_PICKS','model_validation_status':'NOT_VALIDATED',
            'code_sha256':code_sha(),'predictor_code_sha256':sha(Path(__file__).with_name('prematch_predictive_engine.py')),
            'history_code_sha256':sha(Path(__file__).with_name('prematch_tactical_history.py')),'input_hashes':old['input_hashes']}
    outputs={'coverage_audit.json':audit,'preselection_cache_recovered.json':recovered,'goal_joint_baselines.json':models,
             'temporal_backtest.json':{'mode':'RETROSPECTIVE_RECONSTRUCTED','rows':backtests,'validated':False,'limitation':'Prior results only; cache acquisition after historical targets cannot prove historical availability. Selected-team universe and small samples prevent commercial validation.'},
             'market_model_evaluation.json':{'counts':counts,'quotes':updated,'family_blockers':result['family_blockers']},'comparative_ranking.json':result}
    md=['# Cobertura y modelos — auditoría offline','',str(counts),'','MODEL_EVALUABLE significa estimable, nunca MODEL_VALIDATED. No picks ni EV. Las contradicciones de series no son un rechazo de ambas direcciones.','',
        '|Fixture|Cuotas|Baseline goles|Evaluables|Validados|Tier original|','|---:|---:|---|---:|---:|---|']
    for r in ranks:md.append(f"|{r['fixture_id']}|{r['captured_selections']}|{r['model_status']}|{r['evaluable_selections']}|0|{r['tier']}|")
    outputs['comparative_ranking.md']='\n'.join(md)+'\n'
    for name,value in outputs.items():write_once(out/name,value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--patterns', type=Path)
    ap.add_argument('--coverage-model-root',type=Path,help='Auditoría offline de cobertura y baseline FT, sin adquisición')
    ap.add_argument('--reconciliation-root', type=Path, help='Cruce offline de capturas scanner VERIFIED y señales tácticas existentes')
    ap.add_argument('--asof', help='Reloj de evaluación con zona; auditoría offline, nunca aprobación actual')
    ap.add_argument('--preflight', action='append', default=[], type=Path)
    ap.add_argument('--output-dir', required=True, type=Path)
    ap.add_argument('--min-odd',type=float,default=1.20)
    ap.add_argument('--candidate-ledger',type=Path)
    args = ap.parse_args(argv)
    try:
        if args.coverage_model_root:
            result=audit_coverage_models(args.coverage_model_root,args.output_dir)
            print(json.dumps(result['counts'],ensure_ascii=False));return 0
        if args.reconciliation_root:
            result=integrate_reconciliation(args.reconciliation_root,args.output_dir,asof=args.asof)
            print(json.dumps(result['counts'],ensure_ascii=False));return 0
        if args.patterns is None:raise MappingError('PATTERNS_OR_RECONCILIATION_ROOT_REQUIRED')
        result = map_markets(args.patterns, args.preflight, output_dir=args.output_dir,min_odd=args.min_odd,candidate_ledger=args.candidate_ledger)
    except MappingError as exc:
        ap.error(str(exc))
    print(json.dumps(result['counts'], ensure_ascii=False, sort_keys=True))
    print('Siguiente: revisar market_mapping.md y evidencia pendiente. Identidad y cuotas frescas obligatorias antes del uso comercial; investigación externa opcional. No se ejecuta adquisición.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
