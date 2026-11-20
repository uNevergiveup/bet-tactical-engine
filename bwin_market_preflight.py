#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bwin market preflight V0.3: puente AUDITABLE Discovery -> scraper_bwin.py.

- No inventa URL/event IDs.
- No solicita datos de API-Football.
- Revalida identidad y horarios de Bwin con el catalogo observado.
- La ejecucion del scraper es opt-in; nunca reutiliza un TXT sin --reuse-existing.
- No modifica el parser del scraper original ni crea picks.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
import market_quality_gate as quality_gate
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

MADRID = ZoneInfo('Europe/Madrid')
VERSION = '0.3.1-market-preflight-time-evidence'
ID_RE = re.compile(r'(?:-2:|:)(\d{5,12})$')
CATALOG_TIME_RE = re.compile(r'\b(hoy|ma(?:ñ|n)ana)\s*/\s*(\d{1,2}):(\d{2})\b', re.I)
CATALOG_ABS_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2}):(\d{2})\b')
CATALOG_COUNTDOWN_RE = re.compile(r'\b(?:comienza|empieza)\s+en\s+(?:(?P<h>\d{1,2})\s*h(?:oras?)?\s*)?(?P<m>\d{1,3})\s*min(?:utos?)?\b', re.I)
LIVE_RE = re.compile(r'(?i)\blive(?:1t|2t|descanso|parado)?\b|\ben\s+vivo\b|\bdescanso\b|\b\d\s*[tT]\s*[•:]')
MARKET_LABEL_RE = re.compile(r'^\s*(?:mercado|market)\s*:\s*(.+?)\s*$', re.I)
SELECTION_LABEL_RE = re.compile(r'^\s*(?:selecci[oó]n|selection)\s*:\s*(.+?)\s*$', re.I)
PRICE_LABEL_RE = re.compile(r'^\s*(?:cuota|odds|precio)\s*:\s*(\d{1,4}(?:[.,]\d{1,3})?)\b', re.I)
PIPE_PRICE_RE = re.compile(r'^\s*([^|\n]{3,140})\s*\|\s*([^|\n]{1,180})\s*\|\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*$')
INLINE_PRICE_RE = re.compile(r'^\s*(.+?)\s+:\s+(\d{1,4}(?:[.,]\d{1,3})?)\s*$')
CAPTURE_TIME_RE = re.compile(r'(?mi)^Captura:\s*(\S+)\s*$')
CAPTURE_COUNTS_RE = re.compile(r'(?mi)^Acordeones:\s*(\d+)\s*\|\s*Selecciones:\s*(\d+)\s*$')
SNAPSHOT_URL_RE = re.compile(r'(?mi)^URL:\s*(\S+)\s*$')
SNAPSHOT_ID_RE = re.compile(r'(?m)^Event ID:\s*(\d{5,12})\s*$')
FAMILIES = {
    'corners': re.compile(r'c[oó]rners?|corners?|saques? de esquina', re.I),
    'cards': re.compile(r'tarjetas?|amonestaciones?|cards?\b', re.I),
    'shots': re.compile(r'tiros?|disparos?|remates?|shots?', re.I),
    'fouls': re.compile(r'faltas?|fouls?', re.I),
    'offsides': re.compile(r'fuera de juego|offsides?', re.I),
    'goals': re.compile(r'goles?|goals?|ambos equipos marcan|anotan ambos|goleadores|1er equipo en marcar', re.I),
    'result': re.compile(r'resultado|doble oportunidad|1x2|ganador|draw no bet|h[aá]ndicap|descanso[/ ]final|descanso\s+o\s+final|marcador exacto', re.I),
}

def utcnow(): return datetime.now(timezone.utc)
def isodt(s):
    if not isinstance(s,str): return None
    try: dt=datetime.fromisoformat(s.replace('Z','+00:00'))
    except ValueError: return None
    return dt if dt.tzinfo else None

def published_event_id(url):
    try:
        parsed=urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'www.bwin.es' or '/es/sports/eventos/' not in parsed.path:
            return None
        m=ID_RE.search(unquote(parsed.path))
        return m.group(1) if m else None
    except (TypeError, ValueError): return None

def time_evidence(match, catalog, *, tolerance_minutes=3):
    """Time evidence from a published Bwin catalog link: not official machine time."""
    eid=str(match.get('bwin_event_id') or '')
    kickoff=isodt(match.get('kickoff_madrid'))
    if not kickoff: return {'status':'NO_VALID_KICKOFF'}
    candidates=[]
    for item in catalog:
        if str(item.get('bwin_event_id'))!=eid or published_event_id(item.get('bwin_url'))!=eid:
            continue
        raw=str(item.get('text') or '')
        if LIVE_RE.search(raw): continue
        observation=isodt(item.get('text_observed_at_utc'))
        if not observation: continue
        local=observation.astimezone(MADRID)
        parsed=CATALOG_TIME_RE.search(raw)
        start=None; method=None
        if parsed:
            day_label=parsed.group(1).lower()
            day=local.date() + timedelta(days=(1 if day_label.startswith('ma') else 0))
            h,mn=int(parsed.group(2)),int(parsed.group(3))
            if h<24 and mn<60:
                start=datetime(day.year,day.month,day.day,h,mn,tzinfo=MADRID)
                method='BWIN_VISIBLE_HOY_MANANA_TIME'
        if start is None:
            absolute=CATALOG_ABS_RE.search(raw)
            if absolute:
                day,mon,y,h,mn=map(int,absolute.groups())
                if y<100:y+=2000
                try: start=datetime(y,mon,day,h,mn,tzinfo=MADRID); method='BWIN_VISIBLE_DATED_TIME'
                except ValueError:pass
        if start is None:
            cd=CATALOG_COUNTDOWN_RE.search(raw)
            if cd and item.get('browser_timezone', 'Europe/Madrid') == 'Europe/Madrid':
                cd_mins = int(cd.group('h') or 0) * 60 + int(cd.group('m'))
                if 0 < cd_mins <= 180:
                    start = observation.astimezone(MADRID) + timedelta(minutes=cd_mins)
                    method = 'BWIN_EVENT_COUNTDOWN_MINUTES'
        if start is not None:
            diff=abs((start-kickoff).total_seconds())/60
            eff_tol = 4 if method == 'BWIN_EVENT_COUNTDOWN_MINUTES' else tolerance_minutes
            ok_status = 'COUNTDOWN_TIME_CORROBORATED' if method == 'BWIN_EVENT_COUNTDOWN_MINUTES' else 'DISPLAY_TIME_CORROBORATED'
            candidates.append((diff,{'status':ok_status if diff<=eff_tol else 'DISPLAY_TIME_CONFLICT',
                                     'source':method,'observed_at_utc':observation.astimezone(timezone.utc).isoformat(),
                                     'bwin_visible_kickoff_madrid':start.isoformat(),
                                     'api_kickoff_madrid':kickoff.astimezone(MADRID).isoformat(),
                                     'difference_minutes':round(diff,2),'catalog_text':raw[:180]}))
    if not candidates: return {'status':'NO_DISPLAY_TIME_EVIDENCE'}
    candidates.sort(key=lambda x:x[0])
    return candidates[0][1]

def safe_relative_countdown_evidence(match, catalog, *, tolerance_minutes=4):
    """Only accept a timestamped Bwin *event-specific* countdown already
    corroborated by Discovery, when tied to the original catalog event ID.

    A general page countdown, unmatched event, or fabricated report timestamp
    is not an acceptable verification of fixture kickoff.
    """
    inspection = match.get('inspection') or {}
    eid = str(match.get('bwin_event_id') or '')
    if not eid or published_event_id(match.get('bwin_url')) != eid:
        return None
    if match.get('identity_status') != 'NAME_AND_RELATIVE_TIME_CORROBORATED':
        return None
    if not inspection.get('title_teams_match') or str(inspection.get('event_id_from_url')) != eid:
        return None
    if inspection.get('time_evidence_type') != 'RELATIVE_CATALOG_COUNTDOWN':
        return None
    observed = isodt(inspection.get('countdown_observed_at_utc'))
    inferred = isodt(inspection.get('event_start_from_countdown'))
    kickoff = isodt(match.get('kickoff_madrid'))
    if not all((observed, inferred, kickoff)):
        return None
    try:
        minutes = float(inspection.get('countdown_minutes'))
        diff = abs((inferred - kickoff).total_seconds()) / 60
    except (TypeError, ValueError, OverflowError):
        return None
    recomputed = observed + timedelta(minutes=minutes)
    if abs((recomputed - inferred).total_seconds()) > 60:
        return None
    if not (0 <= minutes <= 24 * 60 and diff <= tolerance_minutes):
        return None
    # The observed-at instant must be from this exact event's Bwin catalog
    # anchor, not from the report generation time or an unrelated match.
    tied_to_catalog = any(
        str(item.get('bwin_event_id')) == eid
        and published_event_id(item.get('bwin_url')) == eid
        and (ts := isodt(item.get('text_observed_at_utc'))) is not None
        and abs((ts - observed).total_seconds()) <= 1
        and not LIVE_RE.search(str(item.get('text') or ''))
        for item in catalog
    )
    if not tied_to_catalog:
        return None
    return {
        'status': 'COUNTDOWN_TIME_CORROBORATED',
        'source': 'BWIN_EVENT_CATALOG_TIMESTAMPED_COUNTDOWN',
        'observed_at_utc': observed.astimezone(timezone.utc).isoformat(),
        'bwin_inferred_kickoff_madrid': inferred.astimezone(MADRID).isoformat(),
        'api_kickoff_madrid': kickoff.astimezone(MADRID).isoformat(),
        'difference_minutes': round(diff, 2),
        'warning': 'Cuenta atrás contextual, no fecha absoluta publicada por Bwin',
    }

def read_discovery(path):
    obj=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(obj,dict) or not isinstance(obj.get('matches'),list):
        raise ValueError('El JSON no contiene matches[]')
    return obj

def select_matches(data, fixture_ids, top):
    rows=data['matches']
    if fixture_ids:
        byid={int(x['fixture_id']):x for x in rows if x.get('fixture_id') is not None}
        missing=[f for f in fixture_ids if f not in byid]
        if missing: raise ValueError(f'Fixture IDs no presentes en discovery: {missing}')
        return [byid[f] for f in dict.fromkeys(fixture_ids)]
    found=[x for x in rows if x.get('bwin_event_id') and x.get('bwin_url')]
    found.sort(key=lambda x:(-(float(x.get('research_priority_score') or 0)),int(x.get('fixture_id') or 0)))
    return found[:top] if top else list(rows)


def exploration_check(m, catalog, now, min_lead, max_age):
    """Research admission only. Never upgrades the strict identity evidence."""
    from bwin_discovery import match_catalog, catalog_relative_kickoff, category_compatibility
    checked=validate_match(m,catalog,now,min_lead,max_age)
    strict_reasons=list(checked['reasons'])
    checked.update(validation_level='EXPLORATION',final_validation_reasons=strict_reasons,
                   final_validation_pending=bool(strict_reasons),actionable_bet=False)
    candidates=[row for row in catalog if str(row.get('bwin_event_id'))==str(m.get('bwin_event_id'))]
    # Reapply the existing team/order/category matcher to the actual observed
    # event. A manual confirmation is provenance, not a bypass of this gate.
    possible=match_catalog(m,candidates)
    hard=[r for r in strict_reasons if r in {
        'EVENT_ID_AND_URL_MISMATCH','INSPECTED_EVENT_ID_MISMATCH','BWIN_EVENT_LIVE',
        'INVALID_TIMEZONE_AWARE_KICKOFF','ALREADY_STARTED_OR_INSUFFICIENT_PREMATCH_MARGIN',
        'BWIN_API_KICKOFF_CONFLICT','CATALOG_TIMESTAMP_FROM_FUTURE','DISCOVERY_SNAPSHOT_TOO_OLD'}]
    if len(possible)!=1 or possible[0]['name_score']<.94:hard.append('TEAM_IDENTITY_NOT_CORROBORATED')
    if m.get('candidates_found',1)>1:hard.append('AMBIGUOUS_EVENT_IDENTITY')
    inspection=m.get('inspection') or {}
    if inspection.get('identity_status') in {'TITLE_MISMATCH_REVIEW','EVENT_ID_CHANGED_REVIEW',
        'KICKOFF_MISMATCH_REVIEW','CONFLICTING_TIME_SOURCES_REVIEW','KICKOFF_COUNTDOWN_MISMATCH_REVIEW',
        'CATEGORY_CONFLICT','COMPETITION_CONFLICT'}:
        hard.append(inspection['identity_status'])
    if m.get('competition_compatibility')=='COMPETITION_CONFLICT':hard.append('COMPETITION_CONFLICT')
    page_category=category_compatibility(m,inspection.get('page_title') or '')
    if page_category in {'GENDER_CONFLICT','NON_ADULT_CONFLICT'}:hard.append('CATEGORY_CONFLICT')
    kickoff=isodt(m.get('kickoff_madrid'))
    event_time=isodt(inspection.get('event_start_machine_readable'))
    if not event_time and len(candidates)==1:event_time=catalog_relative_kickoff(candidates[0])
    if event_time and kickoff and abs((event_time-kickoff).total_seconds())>15*60:
        hard.append('BWIN_API_KICKOFF_CONFLICT')
    warnings=[]
    if any(r in strict_reasons for r in ('KICKOFF_NOT_CORROBORATED','NO_TIMESTAMPED_CATALOG_EVIDENCE')):
        warnings.append('TIME_UNCONFIRMED')
    if not m.get('gender') or m.get('gender')=='UNKNOWN' or not m.get('category_evidence_status'):
        warnings.append('CATEGORY_UNKNOWN')
    if not m.get('competition_similarity') or m.get('competition_similarity',0)<.82:
        warnings.append('COMPETITION_UNCONFIRMED')
    checked['final_validation_pending']=bool(strict_reasons or warnings)
    checked['final_validation_reasons']=sorted(set(strict_reasons+warnings))
    if m.get('manual_identity_review',{}).get('decision')=='REJECT':hard.append('MANUAL_IDENTITY_REJECTED')
    checked.update(reasons=sorted(set(hard)),warnings=warnings,
                   status='BLOCKED' if hard else 'READY_FOR_SCRAPER')
    return checked


def reviewed_discovery(data, path, source_sha256):
    """Apply auditable user decisions to a copy, preserving the source snapshot."""
    import copy
    doc=copy.deepcopy(data);review=json.loads(path.read_text(encoding='utf-8'))
    if review.get('source_discovery_sha256')!=source_sha256:
        raise ValueError('MANUAL_REVIEW_SOURCE_CONTENT_MISMATCH')
    seen=set()
    for decision in review.get('decisions',[]):
        fid=decision['fixture_id']
        if fid in seen:raise ValueError('DUPLICATE_MANUAL_DECISION')
        seen.add(fid)
        if (decision.get('decision') not in {'CONFIRM','CORRECT','REJECT'} or
            not decision.get('reviewer') or not decision.get('evidence') or not isodt(decision.get('reviewed_at'))):
            raise ValueError('MANUAL_REVIEW_REQUIRES_REVIEWER_TIME_AND_EVIDENCE')
        matches=[m for m in doc['matches'] if m['fixture_id']==fid]
        if len(matches)!=1:raise ValueError('MANUAL_FIXTURE_NOT_UNIQUE')
        m=matches[0]
        m['manual_identity_review']=decision
        if decision['decision']=='CORRECT':
            event=decision['event']
            if published_event_id(event.get('bwin_url'))!=str(event.get('bwin_event_id')):
                raise ValueError('MANUAL_EVENT_ID_URL_CONFLICT')
            m.update(bwin_event_id=str(event['bwin_event_id']),bwin_url=event['bwin_url'],
                     status='POSSIBLE_EVENT_FOUND',identity_status='NOT_VERIFIED',inspection=None,candidates_found=1)
            doc['catalog_observed']=[e for e in doc.get('catalog_observed',[])
                                      if str(e.get('bwin_event_id'))!=str(event['bwin_event_id'])]+[event]
    return doc

def validate_match(m, catalog, now, min_lead, max_age):
    info={'fixture_id':m.get('fixture_id'),'home':m.get('home'),'away':m.get('away'),
          'kickoff_madrid':m.get('kickoff_madrid'),'bwin_event_id':m.get('bwin_event_id'),
          'bwin_url':m.get('bwin_url'),'discovery_identity_status':m.get('identity_status'),
          'discovery_status':m.get('status'),'markets_discovery':m.get('markets'),
          'status':'BLOCKED','reasons':[]}
    reasons=info['reasons']; eid=str(m.get('bwin_event_id') or '')
    if not eid or published_event_id(m.get('bwin_url')) != eid: reasons.append('EVENT_ID_AND_URL_MISMATCH')
    if not m.get('status','').startswith(('EVENT_FOUND_','IDENTITY_VERIFIED_')):
        reasons.append('DISCOVERY_NOT_ACCEPTED')
    if m.get('identity_status') not in ('NAME_AND_RELATIVE_TIME_CORROBORATED', 'NAME_AND_ABSOLUTE_TIME_MATCH', 'NAME_MATCH_TIME_NOT_MACHINE_VERIFIED'):
        reasons.append('NAME_IDENTITY_NOT_VERIFIED')
    inspection = m.get('inspection') or {}
    if not inspection.get('title_teams_match',False): reasons.append('PAGE_TITLE_TEAMS_NOT_MATCHED')
    if inspection.get('event_id_from_url') is not None and str(inspection['event_id_from_url']) != eid:
        reasons.append('INSPECTED_EVENT_ID_MISMATCH')
    if inspection.get('is_live') is True: reasons.append('BWIN_EVENT_LIVE')
    kickoff=isodt(m.get('kickoff_madrid'))
    if not kickoff: reasons.append('INVALID_TIMEZONE_AWARE_KICKOFF')
    elif (kickoff-now).total_seconds() <= 0 or (kickoff-now).total_seconds() < min_lead*60:
        reasons.append('ALREADY_STARTED_OR_INSUFFICIENT_PREMATCH_MARGIN')
    evidence=time_evidence(m,catalog)
    # A verified relative countdown is permitted ONLY when no conflicting Bwin
    # clock was found, is tied to the same catalog event and has a real capture time.
    if evidence['status'] == 'NO_DISPLAY_TIME_EVIDENCE':
        countdown = safe_relative_countdown_evidence(m,catalog)
        if countdown is not None:
            evidence = countdown
    info['time_evidence']=evidence
    # Reconstruct absolute evidence from the inspected event; never trust a label alone.
    machine_start = isodt(inspection.get('event_start_machine_readable'))
    machine_observed = isodt(inspection.get('observed_at_utc'))
    machine_source = 'INSPECTED_EVENT_JSON_LD'
    if inspection.get('time_evidence_type') == 'SCOPED_PREMATCH_HEADER':
        from bwin_discovery import displayed_header_kickoff
        rebuilt = displayed_header_kickoff(inspection.get('displayed_time_evidence') or {})
        if rebuilt is None or rebuilt != machine_start:machine_start = None
        machine_source = 'INSPECTED_EVENT_PREMATCH_HEADER'
    machine_verified = bool(m.get('identity_status')=='NAME_AND_ABSOLUTE_TIME_MATCH' and
                            machine_start and kickoff and machine_observed and
                            str(inspection.get('event_id_from_url')) == eid and
                            abs((machine_start-kickoff).total_seconds()) <= 180)
    if machine_verified and evidence['status'] == 'NO_DISPLAY_TIME_EVIDENCE':
        evidence = {'status':'ABSOLUTE_TIME_VERIFIED','source':machine_source,
                    'observed_at_utc':machine_observed.isoformat(),
                    'difference_minutes':abs((machine_start-kickoff).total_seconds())/60}
        info['time_evidence'] = evidence
    if evidence['status'] == 'DISPLAY_TIME_CONFLICT': reasons.append('BWIN_API_KICKOFF_CONFLICT')
    elif evidence['status'] not in ('DISPLAY_TIME_CORROBORATED','COUNTDOWN_TIME_CORROBORATED','ABSOLUTE_TIME_VERIFIED') and not machine_verified:
        reasons.append('KICKOFF_NOT_CORROBORATED')
    observed=isodt(evidence.get('observed_at_utc'))
    if observed and (now-observed).total_seconds() < -60:
        reasons.append('CATALOG_TIMESTAMP_FROM_FUTURE')
    if observed and (now-observed).total_seconds() > max_age*60:
        reasons.append('DISCOVERY_SNAPSHOT_TOO_OLD')
    if not observed: reasons.append('NO_TIMESTAMPED_CATALOG_EVIDENCE')
    if not reasons: info['status']='READY_FOR_SCRAPER'
    return info

def declared_capture_time(path):
    """Marca temporal escrita por el scraper. Afirmacion del TXT, no prueba independiente."""
    head=path.read_text(encoding='utf-8-sig',errors='replace')[:20000]
    match=CAPTURE_TIME_RE.search(head)
    return isodt(match.group(1)) if match else None


def market_kind(market):
    """Clasificación unificada de mercado delegada a market_quality_gate (sin divergencias)."""
    return quality_gate.market_kind(market)


def snapshot_odds(path,eid):
    """Audita, sin navegar, el formato real 'MERCADO: ...\nSelección : cuota'.

    Conserva compatibilidad con los dos esquemas previos y mantiene intacto el TXT.
    Los precios son evidencia del instante declarado, NO de vigencia posterior.
    """
    data=path.read_bytes()
    text=data.decode('utf-8-sig',errors='replace')
    head=text[:20000]
    match=SNAPSHOT_ID_RE.search(head)
    if not match or match.group(1)!=str(eid):
        raise ValueError('Event ID incorrecto o ausente en TXT')
    declared_url=SNAPSHOT_URL_RE.search(head)
    if declared_url and published_event_id(declared_url.group(1)) != str(eid):
        raise ValueError('URL del TXT no corresponde a su Event ID')

    capture=declared_capture_time(path)
    counts=CAPTURE_COUNTS_RE.search(head)
    accordions_declared=int(counts.group(1)) if counts else None
    selections_declared=int(counts.group(2)) if counts else None
    coverage_warnings=[line.strip() for line in text.splitlines() if line.startswith('Aviso de cobertura:')]
    triples=[]; market=None; selection=None; market_blocks=[]; unparsed_price_lines=[]
    for line_number,raw in enumerate(text.splitlines(),1):
        line=raw.strip()
        mm=MARKET_LABEL_RE.match(line)
        if mm:
            market=mm.group(1).strip(); selection=None; market_blocks.append(market)
            continue
        ms=SELECTION_LABEL_RE.match(line)
        if ms: selection=ms.group(1).strip(); continue
        mp=PRICE_LABEL_RE.match(line)
        if mp and market and selection:
            triples.append((market,selection,mp.group(1),line_number)); selection=None
            continue
        pp=PIPE_PRICE_RE.match(line)
        if pp:
            triples.append((*[x.strip() for x in pp.groups()],line_number)); continue
        # El scraper real escribe una seleccion con su cuota en la misma linea.
        inline=INLINE_PRICE_RE.match(line)
        if inline and market:
            choice,price=inline.groups()
            # Excluye cualquier cabecera o campo de metadatos accidentalmente coincidente.
            if choice.strip() and not re.match(r'^(?:mercado|market|event id|url|captura|acordeones|aviso|cuota|odds|precio)\b',choice,re.I):
                triples.append((market,choice.strip(),price,line_number))
                continue
        if market and line and ':' in line and not line.startswith(('Aviso de cobertura:',)):
            unparsed_price_lines.append({'line':line_number,'text':line[:140]})

    unique={}; invalid_prices=[]; repeated=0
    for market,selection,price_text,line_number in triples:
        try: odd=float(price_text.replace(',','.'))
        except ValueError:
            invalid_prices.append({'line':line_number,'price':price_text}); continue
        if not (1.0<odd<=1000.0) or not market or not selection:
            invalid_prices.append({'line':line_number,'price':price_text}); continue
        if re.search(r'^(?:mercado|market|selecci[oó]n|selection)$',market,re.I):
            continue
        item={'market':market[:180],'selection':selection[:180],'odd':odd}
        key=(item['market'],item['selection'],item['odd'])
        if key in unique: repeated+=1
        unique[key]=item

    rows=list(unique.values())
    groups={k:{'verified_selection_count':0,'example':None,'market_blocks_count':0} for k in FAMILIES}
    groups['other']={'verified_selection_count':0,'example':None,'market_blocks_count':0}
    kinds={}
    for market_name in market_blocks:
        fam=next((k for k,pattern in FAMILIES.items() if pattern.search(market_name)),'other')
        groups[fam]['market_blocks_count']+=1
    for item in rows:
        fam=next((k for k,pattern in FAMILIES.items() if pattern.search(item['market'])),'other')
        groups[fam]['verified_selection_count']+=1
        if groups[fam]['example'] is None: groups[fam]['example']=item
        kind=market_kind(item['market'])
        kinds[kind]=kinds.get(kind,0)+1
    warnings=[]
    if coverage_warnings:warnings.append('SCRAPER_DECLARED_PARTIAL_RECOMMENDATIONS_COVERAGE')
    if selections_declared is not None and selections_declared!=len(triples):warnings.append('DECLARED_SELECTION_COUNT_DIFFERS_FROM_PARSED')
    if repeated:warnings.append('DUPLICATE_MARKET_SELECTION_PRICE_TRIPLES')
    if invalid_prices:warnings.append('INVALID_PRICE_VALUES')
    if unparsed_price_lines:warnings.append('UNPARSED_COLON_LINES')
    if capture is None:warnings.append('NO_VALID_SCRAPER_CAPTURE_TIME')
    status='MARKET_TRIPLES_EXTRACTED' if rows else 'TXT_CAPTURED_FORMAT_NOT_RECOGNIZED'
    return {'snapshot_path':str(path),'sha256':hashlib.sha256(data).hexdigest(),
            'snapshot_bytes':len(data),'event_id_verified_in_txt':True,
            'source_url':declared_url.group(1) if declared_url else None,
            'capture_declared_madrid':capture.astimezone(MADRID).isoformat() if capture else None,
            'capture_time_source':'SCRAPER_TXT_HEADER_NOT_INDEPENDENTLY_VERIFIED',
            'format_recognized':bool(rows),'raw_price_lines_recognized':len(triples),
            'selection_triplets_verified':len(rows),'market_blocks_count':len(market_blocks),
            'distinct_markets_count':len(set(market_blocks)),
            'scraper_reported_accordions':accordions_declared,
            'scraper_reported_selections':selections_declared,
            'selection_count_matches_scraper_header':(len(triples)==selections_declared) if selections_declared is not None else None,
            'duplicate_triplets_removed':repeated,'invalid_price_values':invalid_prices,
            'unparsed_colon_lines':unparsed_price_lines,
            'market_families':groups,'market_kinds':dict(sorted(kinds.items())),
            'coverage_warnings_from_txt':coverage_warnings,
            'warnings':warnings,'status':status,
            '_parsed_selection_rows':rows}

def run_scraper(root,out,eid,url,refresh=False,immutable=False,timeout_seconds=240,scraper_attempts=1):
    root=Path(root).resolve();out=Path(out).resolve();eid=str(eid)
    canonical=root/'outputs'/'matches'/eid/'inputs'/f'cuotas_bwin_{eid}.txt'
    if published_event_id(url)!=eid:
        return {'returncode':None,'error':'EVENT_ID_URL_CONFLICT'}
    if immutable or refresh:
        if (canonical.parent.parent/'manifest.json').exists() and not immutable:
            return {'returncode':None,'error':'Evento preparado: usar prematch_single.py refresh-odds para actualizar manifest e invalidar derivados'}
        # Use precisely the operational staging contract already enforced by
        # scraper_bwin.rutas_salida_evento. The batch archive is never an env path.
        started=utcnow().astimezone(timezone.utc);stamp=started.strftime('%Y%m%dT%H%M%S%fZ')
        stage=canonical.parent.parent/'staging'/stamp/eid/'inputs'
        history=out/'captures'/eid/stamp
        stage.mkdir(parents=True,exist_ok=False);history.mkdir(parents=True,exist_ok=False)
        fresh=stage/canonical.name;archived=history/canonical.name;log=history/'scraper.log'
        old_sha=hashlib.sha256(canonical.read_bytes()).hexdigest() if canonical.is_file() else None
        info={'returncode':None,'log':str(log),'started_at_utc':started.isoformat(),
              'operational_inputs':str(stage),'archive_directory':str(history),
              'original_snapshot_path':str(canonical),'original_sha256':old_sha}
        env=os.environ.copy();env.update(PREMATCH_BATCH_MODE='1',PREMATCH_EVENT_ID=eid,
            PREMATCH_EVENT_DATA_DIR=str(stage),PREMATCH_IMMUTABLE_CAPTURE='1')
        command=[sys.executable,'-u',str(root/'scraper_bwin.py'),'--url',url,'--intentos',str(scraper_attempts)]
        info['command']=command
        try:
            with log.open('w',encoding='utf-8') as fh:
                result=subprocess.run(command,cwd=root,env=env,stdout=fh,stderr=subprocess.STDOUT,timeout=timeout_seconds)
            info['returncode']=result.returncode
            if result.returncode:
                raise ValueError('SCRAPER_EXIT_NONZERO')
            if not fresh.is_file():raise ValueError('NO_NEW_SCRAPER_OUTPUT')
            declared=re.findall(r'(?m)^TXT:\s*(.+?\.txt)\s*$',log.read_text(encoding='utf-8',errors='replace'))
            declared_path=Path(declared[-1].strip()) if declared else None
            if declared_path and not declared_path.is_absolute():declared_path=root/declared_path
            if not declared_path or declared_path.resolve()!=fresh.resolve():raise ValueError('SCRAPER_PATH_NOT_DECLARED')
            snap=snapshot_odds(fresh,eid);captured=isodt(snap.get('capture_declared_madrid'))
            if not captured or not started-timedelta(seconds=5)<=captured<=utcnow()+timedelta(seconds=5):
                raise ValueError('CAPTURE_NOT_FROM_THIS_ATTEMPT')
            if snap.get('source_url')!=url:raise ValueError('CAPTURE_URL_MISMATCH')
            if (snap['status']!='MARKET_TRIPLES_EXTRACTED' or not snap['selection_triplets_verified'] or
                snap.get('invalid_price_values') or snap.get('unparsed_colon_lines') or
                snap.get('selection_count_matches_scraper_header') is False):
                raise ValueError('CAPTURE_MARKETS_OR_COUNTS_INVALID')
            current_sha=hashlib.sha256(canonical.read_bytes()).hexdigest() if canonical.is_file() else None
            if current_sha!=old_sha:raise ValueError('ORIGINAL_CHANGED_DURING_CAPTURE')
            shutil.copy2(fresh,archived)
            if hashlib.sha256(archived.read_bytes()).hexdigest()!=snap['sha256']:raise ValueError('ARCHIVE_SHA_MISMATCH')
            info.update(archived_snapshot_path=str(archived),snapshot_sha256=snap['sha256'],
                        capture_madrid=snap['capture_declared_madrid'],canonical_unchanged=True)
            if immutable:
                info['fresh_snapshot_path']=str(archived)
            else:
                if old_sha:
                    previous=history/'previous'/canonical.name;previous.parent.mkdir()
                    shutil.copy2(canonical,previous)
                    if hashlib.sha256(previous.read_bytes()).hexdigest()!=old_sha:raise ValueError('PREVIOUS_ARCHIVE_SHA_MISMATCH')
                    info['archived_previous']=str(previous)
                canonical.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(archived,canonical)
                info.update(fresh_snapshot_path=str(canonical),canonical_unchanged=False)
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            info.pop('fresh_snapshot_path',None)
            info['error']=f'{type(exc).__name__}: {exc}'
            info['timed_out']=isinstance(exc,subprocess.TimeoutExpired)
        finally:
            info['canonical_unchanged']=(hashlib.sha256(canonical.read_bytes()).hexdigest() if canonical.is_file() else None)==old_sha
            info['finished_at_utc']=utcnow().isoformat()
            info['duration_seconds']=round((isodt(info['finished_at_utc'])-started).total_seconds(),6)
            text=log.read_text(encoding='utf-8',errors='replace') if log.is_file() else ''
            info['subprocess_output_tail']=text[-4000:]
            if 'error' in info and text:info['error']+=' | '+text[-1500:].strip()
            diagnostics=stage.parent/'scraper'
            if diagnostics.is_dir():shutil.copytree(diagnostics,history/'scraper_diagnostics')
            (history/'provenance.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return info
    script=root/'scraper_bwin.py'
    if not script.is_file():return {'returncode':None,'error':f'No existe {script}; no se ejecutó scraping'}
    path=root/'outputs'/'matches'/eid/'inputs'/f'cuotas_bwin_{eid}.txt'
    if path.exists():return {'returncode':None,'error':f'Ya existe {path}: no se sobrescribe sin intervención manual'}
    (out/'logs').mkdir(parents=True,exist_ok=True)
    log=out/'logs'/f'bwin_scraper_{eid}.log'
    env=os.environ.copy()
    env['PREMATCH_BATCH_MODE']='1'
    env['PREMATCH_EVENT_ID']=eid
    env['PREMATCH_EVENT_DATA_DIR']=str(path.parent.resolve())
    command=[sys.executable,'-u',str(script),'--url',url,'--intentos',str(scraper_attempts)]
    started=utcnow()
    try:
        with log.open('w',encoding='utf-8') as fh:
            fh.write(f'# {started.isoformat()} | Event ID {eid} | {script.name}\n');fh.flush()
            completed=subprocess.run(command,cwd=root,env=env,stdout=fh,stderr=subprocess.STDOUT,check=False,timeout=timeout_seconds)
        code=completed.returncode
    except (OSError,subprocess.TimeoutExpired) as ex: return {'returncode':None,'error':f'{type(ex).__name__}: {ex}','log':str(log)}
    info={'returncode':code,'log':str(log),'started_at_utc':started.isoformat(),
          'finished_at_utc':utcnow().isoformat()}
    if code != 0:
        tail=log.read_text(encoding='utf-8',errors='replace')[-4000:]
        info.update(error='El scraper devolvió código distinto de cero: '+tail[-1500:],subprocess_output_tail=tail)
    # Require new output (not a previously existing file) and the declared TXT path.
    if code==0:
        text=log.read_text(encoding='utf-8',errors='replace')
        declarations=re.findall(r'(?m)^TXT:\s*(.+?\.txt)\s*$',text)
        if not declarations:
            info['error']='No se encontró línea TXT: ... en log del scraper'
        else:
            declared=Path(declarations[-1].strip())
            if not declared.is_absolute():declared=root/declared
            if declared.resolve()!=path.resolve():info['error']='TXT del log no apunta a la carpeta propia del evento'
            elif not path.is_file():info['error']='TXT declarado no existe'
            else:
                try:
                    snapshot = snapshot_odds(path, eid)
                    captured = datetime.fromisoformat(snapshot['capture_declared_madrid'])
                    if not started-timedelta(seconds=5) <= captured <= utcnow()+timedelta(seconds=5):
                        info['error']='Captura TXT fuera del intervalo de adquisición de esta ejecución'
                    elif snapshot.get('source_url') != url:
                        info['error']='URL del snapshot distinta de la adquisición solicitada'
                    else:
                        info['fresh_snapshot_path']=str(path)
                except (ValueError,KeyError,OSError) as exc:
                    info['error']='Captura nueva no verificable: '+str(exc)
    return info

def write_outputs(folder,payload):
    folder.mkdir(parents=True,exist_ok=True)
    # Exporta cada cuota y su decisión a CSV; no duplica 500 filas en el JSON resumen.
    details=[]
    for match in payload['matches']:
        for item in match.pop('_quality_rows',[]):
            details.append({'fixture_id':match.get('fixture_id'),'bwin_event_id':match.get('bwin_event_id'),**item})
    detail_fields=['fixture_id','bwin_event_id','market','selection','odd','market_kind','price_tier',
                   'data_status','data_source','decision','rationale',
                   'evidence_status','model_status','evaluation_state']
    with (folder/'market_selection_review.csv').open('w',newline='',encoding='utf-8-sig') as fh:
        writer=csv.DictWriter(fh,fieldnames=detail_fields,extrasaction='ignore',restval='')
        writer.writeheader();writer.writerows(details)
    shortlist=[d for d in details if d['decision']=='READY_FOR_RESEARCH']
    with (folder/'market_research_shortlist.csv').open('w',newline='',encoding='utf-8-sig') as fh:
        writer=csv.DictWriter(fh,fieldnames=detail_fields,extrasaction='ignore',restval='')
        writer.writeheader();writer.writerows(shortlist)
    kind_rows=[]
    for match in payload['matches']:
        for kind,values in ((match.get('quality_gate') or {}).get('market_kinds') or {}).items():
            kind_rows.append({'fixture_id':match.get('fixture_id'),'market_kind':kind,
                              'captured':values.get('total',0),
                              'min_120':values.get('PRICE_ELIGIBLE',0),
                              'observation_110_119':values.get('LOW_ODD_OBSERVATION',0),
                              'researchable':values.get('researchable',0)})
    with (folder/'market_family_readiness.csv').open('w',newline='',encoding='utf-8-sig') as fh:
        writer=csv.DictWriter(fh,fieldnames=['fixture_id','market_kind','captured','min_120',
                                              'observation_110_119','researchable'])
        writer.writeheader();writer.writerows(kind_rows)
    (folder/'market_preflight.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    columns=['fixture_id','home','away','bwin_event_id','kickoff_madrid','status','time_evidence_status','scraper_rc','triples','market_blocks','scraper_reported_selections','selections_match_header','result','goals','corners','cards','shots','fouls','offsides','warnings','reason','price_eligible','low_odd_observation','below_observation','researchable_after_data_gate','price_eligible_pending_evidence','join_status']
    with (folder/'market_preflight.csv').open('w',newline='',encoding='utf-8-sig') as fh:
        w=csv.DictWriter(fh,fieldnames=columns);w.writeheader()
        for m in payload['matches']:
            odds=(m.get('snapshot') or {});groups=odds.get('market_families') or {}
            row={k:m.get(k) for k in ('fixture_id','home','away','bwin_event_id','kickoff_madrid','status')}
            row['time_evidence_status']=(m.get('time_evidence') or {}).get('status')
            row['scraper_rc']=(m.get('scraper') or {}).get('returncode')
            row['triples']=odds.get('selection_triplets_verified')
            row['market_blocks']=odds.get('market_blocks_count')
            row['scraper_reported_selections']=odds.get('scraper_reported_selections')
            row['selections_match_header']=odds.get('selection_count_matches_scraper_header')
            quality= m.get('quality_gate') or {}
            for field in ('price_eligible','low_odd_observation','below_observation',
                          'researchable_after_data_gate','price_eligible_pending_evidence','join_status'):
                row[field]=quality.get(field)
            row['warnings']=';'.join(odds.get('warnings') or [])
            row['reason']=';'.join(m.get('reasons') or []) or (m.get('scraper') or {}).get('error') or ''
            for f in FAMILIES:row[f]=(groups.get(f) or {}).get('verified_selection_count')
            w.writerow(row)
    lines=['# Bwin market preflight — V0.3','',f"Generado UTC: {payload['generated_at_utc']}",'',
           '| Fixture | Partido | Bwin ID | Identidad/hora | Cuotas (triples) | Estado |',
           '|---|---|---|---|---:|---|']
    for m in payload['matches']:
        snapshot=m.get('snapshot') or {}
        lines.append(f"| {m.get('fixture_id')} | {m.get('home')}–{m.get('away')} | {m.get('bwin_event_id') or '—'} | {(m.get('time_evidence') or {}).get('status')} | {snapshot.get('selection_triplets_verified','—')} | {m.get('status')} |")
        if snapshot:
            gate=m.get('quality_gate') or {}
            if gate:
                ev_counts=gate.get('evaluation_state_counts') or {}
                missing_fams=gate.get('missing_market_families') or []
                lines.extend(['',f"- Precios: **{gate.get('captured_valid_selections')}** capturados; **{gate.get('price_eligible')}** desde {gate.get('minimum_candidate_odd')}; **{gate.get('low_odd_observation')}** en observación; **{gate.get('below_observation')}** inferiores a {gate.get('minimum_observation_odd')}.",
                              f"- Cruce estadístico: **{gate.get('researchable_after_data_gate')}** selecciones con datos iniciales investigables; **{gate.get('price_eligible_pending_evidence')}** pendientes de evidencia/modelo. Unión: **{gate.get('join_status')}**.",
                              f"- Estados de evaluación (sin confundir con NO BET): `MODEL_EVALUATION_PENDING={ev_counts.get('MODEL_EVALUATION_PENDING',0)}`, `MODEL_UNAVAILABLE={ev_counts.get('MODEL_UNAVAILABLE',0)}`, `INSUFFICIENT_STATISTICAL_EVIDENCE={ev_counts.get('INSUFFICIENT_STATISTICAL_EVIDENCE',0)}`, `MARKET_CAPTURED_UNMAPPED={ev_counts.get('MARKET_CAPTURED_UNMAPPED',0)}`.",
                              f"- Familias monitorizadas sin cuotas en captura (`NO_MARKET_CAPTURED`): **{', '.join(missing_fams) or 'ninguna'}**."])
            lines.append('')
            lines.append(f"- Bloques de mercado: **{snapshot.get('market_blocks_count')}**; selecciones declaradas por scraper: **{snapshot.get('scraper_reported_selections')}**; diferencia: **{(snapshot.get('raw_price_lines_recognized') or 0)-(snapshot.get('scraper_reported_selections') or 0)}**")
            lines.append(f"- Captura declarada: **{snapshot.get('capture_declared_madrid') or 'sin dato'}**. Evidencia retrospectiva, no cuotas actuales.")
            kinds=snapshot.get('market_kinds') or {}
            if kinds:lines.append('- Detalle por tipo: '+', '.join(f'{k}: {v}' for k,v in kinds.items()))
            if snapshot.get('warnings'):lines.append('- Advertencias: '+', '.join(snapshot['warnings']))
            lines.append('')
    lines.extend(['','Un encabezado Bwin no es una cuota. Se cuentan lineas Mercado/Seleccion/Cuota del TXT, incluidas las variantes inline; NO se garantiza su vigencia posterior.',
                  'Si el TXT tiene otro formato, la captura se conserva pero las cuotas NO se declaran verificadas.',
                  'El archivo market_selection_review.csv conserva las 500 líneas para inspección; market_research_shortlist.csv contiene solo datos aptos para INVESTIGACIÓN, no picks.',
                  'Para mercados de jugadores se requiere XI y estadísticas individuales; para periodos, distribuciones por mitad; para combinadas/marcador exacto, modelo especializado.',
                  'No se ejecuta API-Football, ni el motor, ni modelos LLM. No se colocan apuestas.',''])
    (folder/'market_preflight.md').write_text('\n'.join(lines),encoding='utf-8')

def main(argv=None):
    p=argparse.ArgumentParser(description='Cruce conservador Bwin Discovery -> scraper de cuotas')
    p.add_argument('--discovery',type=Path,required=True)
    p.add_argument('--preselection',type=Path,help='JSON V1.3 de API-Football para cobertura estadística (SIN llamar API)')
    p.add_argument('--min-odd-candidate',type=str,default='1.20',help='Cuota mínima para pasar el filtro de precio')
    p.add_argument('--min-odd-observation',type=str,default='1.10',help='Cuota mínima para mantener en observación')
    p.add_argument('--fixture-id',action='append',type=int,default=[])
    p.add_argument('--top',type=int,default=0,help='0=todos; sin recorte implícito del inventario')
    p.add_argument('--exploration',action='store_true',help='Permite hora/categoría pendientes para investigar, nunca para apuestas')
    p.add_argument('--max-captures',type=int,default=0,help='0=todos; límite de intentos, incluidos fallos')
    p.add_argument('--capture-timeout-seconds',type=int,default=240)
    p.add_argument('--resume',action='store_true',help='Reutilizar solo capturas de este lote con mismos hashes y aún frescas')
    p.add_argument('--identity-review',type=Path,help='Decisiones manuales identificadas y con evidencia; nunca fuerza VERIFIED')
    p.add_argument('--write-identity-review',type=Path,help='Crear plantilla de revisión manual, sin red')
    p.add_argument('--root',type=Path,default=Path.cwd())
    p.add_argument('--output-dir',type=Path,default=Path('outputs/discovery/market_preflight'))
    p.add_argument('--run-scraper',action='store_true',help='EJECUTAR scraper_bwin.py para encuentros validos; por defecto solo auditoria')
    p.add_argument('--confirm-external',action='store_true')
    p.add_argument('--refresh-existing',action='store_true',help='Con --run-scraper: recaptura primero en staging y archiva el TXT anterior por SHA')
    p.add_argument('--immutable-capture',action='store_true',help='Recaptura versionada sin modificar el TXT ni manifest previos; requiere --refresh-existing')
    p.add_argument('--reuse-existing',action='store_true',help='Auditar TXT ya capturado, sin ejecutar scraper (retrospectivo)')
    p.add_argument('--audit-txt',type=Path,help='Auditar un TXT indicado sin navegar, basado en su captura declarada; exige exactamente un --fixture-id')
    p.add_argument('--min-lead-minutes',type=int,default=5)
    p.add_argument('--max-discovery-age-minutes',type=int,default=180)
    p.add_argument('--now-utc',help='Reloj UTC explícito opcional para evaluación reproducible')
    args=p.parse_args(argv)
    now_override=isodt(args.now_utc) if args.now_utc else None
    if args.now_utc and (now_override is None or now_override.tzinfo is None):
        p.error('--now-utc inválido: requiere ISO8601 con zona horaria')
    if args.run_scraper:
        from prematch_execution_control import require_external_confirmation
        try:require_external_confirmation(args.confirm_external)
        except ValueError as error:p.error(str(error))
    if args.immutable_capture and not args.refresh_existing:p.error('--immutable-capture requiere --refresh-existing')
    if args.refresh_existing and not args.run_scraper:p.error('--refresh-existing requiere --run-scraper')
    if sum(bool(x) for x in (args.run_scraper,args.reuse_existing,args.audit_txt))>1:
        p.error('Usa solo uno: --run-scraper, --reuse-existing o --audit-txt')
    if args.audit_txt and len(set(args.fixture_id))!=1:
        p.error('--audit-txt exige exactamente un --fixture-id')
    if args.top<0 or args.max_captures<0 or args.capture_timeout_seconds<1:p.error('Presupuestos inválidos')
    try:
        candidate_limit=Decimal(args.min_odd_candidate)
        observation_limit=Decimal(args.min_odd_observation)
        if not candidate_limit.is_finite() or not observation_limit.is_finite() or not (Decimal('1')<observation_limit<=candidate_limit):
            raise ValueError()
    except (ValueError,ArithmeticError):p.error('Umbrales: 1 < --min-odd-observation <= --min-odd-candidate')
    if args.min_lead_minutes<0 or args.max_discovery_age_minutes<1:p.error('Margen o antigüedad inválidos')
    root=args.root.resolve();out=args.output_dir.resolve();data=read_discovery(args.discovery)
    if args.write_identity_review:
        template={'source_discovery_sha256':hashlib.sha256(args.discovery.read_bytes()).hexdigest(),
                  'decisions':[],
                  'example':{'fixture_id':None,'decision':'CONFIRM','reviewer':'',
                             'reviewed_at':'ISO8601 con offset','evidence':'ruta/hash o descripción comprobable',
                             'event':{'bwin_event_id':'','bwin_url':'','text':'','text_observed_at_utc':''}}}
        with args.write_identity_review.open('x',encoding='utf-8') as fh:json.dump(template,fh,ensure_ascii=False,indent=2)
        return 0
    source_path=args.discovery
    if args.identity_review:
        data=reviewed_discovery(data,args.identity_review,hashlib.sha256(args.discovery.read_bytes()).hexdigest())
        out.mkdir(parents=True,exist_ok=True);source_path=out/'reviewed_discovery.json'
        reviewed_bytes=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
        if source_path.exists() and source_path.read_bytes()!=reviewed_bytes:p.error('MANUAL_REVIEW_WRITE_ONCE_CONFLICT')
        source_path.write_bytes(reviewed_bytes)
    statistical_index={}
    if args.preselection:
        try:_,statistical_index=quality_gate.load_preselection(args.preselection)
        except (OSError,ValueError,json.JSONDecodeError) as ex:p.error(f'Preselection inválida: {ex}')
    selected=select_matches(data,args.fixture_id,args.top)
    previous={};report_path=out/'market_preflight.json'
    if report_path.exists():
        if not args.resume:p.error('Salida existente: usa otro directorio o --resume explícito')
        old=json.loads(report_path.read_text())
        if old.get('source_sha256')!=hashlib.sha256(source_path.read_bytes()).hexdigest():p.error('RESUME_DISCOVERY_CHANGED')
        if (old.get('validation_level','STRICT')!=('EXPLORATION' if args.exploration else 'STRICT') or
            old.get('min_odd_candidate')!=str(candidate_limit) or old.get('min_odd_observation')!=str(observation_limit)):
            p.error('RESUME_VALIDATION_CONTRACT_CHANGED')
        prehash=hashlib.sha256(args.preselection.read_bytes()).hexdigest() if args.preselection else None
        if old.get('source_preselection_sha256')!=prehash:p.error('RESUME_STATISTICS_CHANGED')
        previous={str(m['fixture_id']):m for m in old.get('matches',[])}
        table=out/'market_selection_review.csv'
        if table.is_file():
            with table.open(encoding='utf-8-sig',newline='') as fh:
                for row in csv.DictReader(fh):
                    prior=previous.get(str(row['fixture_id']))
                    if prior is not None:
                        prior.setdefault('_quality_rows',[]).append({k:v for k,v in row.items()
                                                                   if k not in {'fixture_id','bwin_event_id'}})
    results=[];attempts=0
    owners={}
    for m in data['matches']:
        if m.get('bwin_event_id'):owners.setdefault(str(m['bwin_event_id']),set()).add(str(m['fixture_id']))
    for m in selected:
        eid=str(m.get('bwin_event_id') or '')
        target=root/'outputs'/'matches'/eid/'inputs'/f'cuotas_bwin_{eid}.txt'
        audit_path=args.audit_txt.resolve() if args.audit_txt else target if args.reuse_existing else None
        declared_time=declared_capture_time(audit_path) if audit_path and audit_path.is_file() else None
        validation_at=declared_time.astimezone(timezone.utc) if declared_time else (now_override.astimezone(timezone.utc) if now_override else utcnow())
        check=exploration_check if args.exploration else validate_match
        checked=check(m,data.get('catalog_observed',[]),validation_at,args.min_lead_minutes,args.max_discovery_age_minutes)
        if len(owners.get(eid,set()))>1:
            checked['reasons'].append('BWIN_EVENT_REUSED_ACROSS_FIXTURES');checked['status']='BLOCKED'
        prior=previous.get(str(m['fixture_id']),{})
        prior_snap=prior.get('snapshot',{});prior_path=Path(prior_snap.get('snapshot_path') or '_missing_')
        prior_time=isodt(prior_snap.get('capture_declared_madrid'))
        if (args.resume and checked['status']=='READY_FOR_SCRAPER' and prior_path.is_file() and prior_time and
            0<=(utcnow()-prior_time).total_seconds()<=args.max_discovery_age_minutes*60 and
            hashlib.sha256(prior_path.read_bytes()).hexdigest()==prior_snap.get('sha256')):
            results.append(prior);continue
        if audit_path:
            checked['time_validation_basis']='SCRAPER_TXT_DECLARED_CAPTURE_TIME' if declared_time else 'CURRENT_TIME_FALLBACK_NO_CAPTURE_TIME'
            checked['audit_mode']='RETROSPECTIVE_SNAPSHOT_NOT_CURRENT_ODDS'
            if declared_time is None:checked['reasons'].append('NO_VALID_DECLARED_CAPTURE_TIME');checked['status']='BLOCKED'
        eid=str(m.get('bwin_event_id') or '')
        if checked['status']!='READY_FOR_SCRAPER':
            print(f"[{m.get('fixture_id')}] BLOQUEADO: {', '.join(checked['reasons'])}",flush=True)
            results.append(checked);continue
        clock_label='TIME_UNCONFIRMED' if 'TIME_UNCONFIRMED' in checked.get('warnings',[]) else 'hora CORROBORADA'
        print(f"[{m.get('fixture_id')}] {m.get('home')}–{m.get('away')} | bwin={eid} | {clock_label}",flush=True)
        if args.run_scraper:
            if args.max_captures and attempts>=args.max_captures:
                checked['status']='NOT_EVALUATED_BUDGET';checked['reasons'].append('CAPTURE_BUDGET_REACHED')
                results.append(checked);continue
            attempts+=1
            checked['scraper']=run_scraper(root,out,eid,m['bwin_url'],refresh=args.refresh_existing,
                immutable=args.immutable_capture,timeout_seconds=args.capture_timeout_seconds)
            found=(checked['scraper'] or {}).get('fresh_snapshot_path')
            if found:
                try:
                    checked['snapshot']=snapshot_odds(Path(found),eid)
                    checked['status']=checked['snapshot']['status']
                except (ValueError,OSError) as e:checked['status']='SCRAPER_OUTPUT_INVALID';checked['reasons'].append(str(e))
            else:checked['status']='SCRAPER_FAILED_OR_UNVERIFIED'
        elif args.reuse_existing or args.audit_txt:
            if audit_path.is_file():
                try:
                    checked['snapshot']=snapshot_odds(audit_path,eid)
                    checked['status']='HISTORICAL_'+checked['snapshot']['status']
                except (ValueError,OSError) as e:checked['status']='EXISTING_SNAPSHOT_INVALID';checked['reasons'].append(str(e))
            else:checked['status']='HISTORICAL_SNAPSHOT_NOT_FOUND'
        else:checked['status']='READY_NOT_EXECUTED'
        if 'snapshot' in checked:
            try:
                rows=checked['snapshot'].pop('_parsed_selection_rows',[])
                stat=statistical_index.get(int(m['fixture_id'])) if args.preselection else None
                summary,details=quality_gate.analyze_rows(rows,m,stat,
                    min_candidate=candidate_limit,min_observation=observation_limit)
                checked['quality_gate']=summary
                checked['_quality_rows']=details
                if args.preselection and summary['join_status'] != 'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF':
                    checked['reasons'].append('PRESELECTION_IDENTITY_CHECK_FAILED')
                    checked['status']='STATISTICAL_IDENTITY_MISMATCH'
            except (ValueError,KeyError) as ex:
                checked['reasons'].append(f'QUALITY_GATE_ERROR: {ex}')
                checked['status']='QUALITY_GATE_ERROR'
        print(f"   {checked['status']}" + (f" | cuotas={checked['snapshot']['selection_triplets_verified']}" if 'snapshot' in checked else ''),flush=True)
        results.append(checked)
    pending=[m['fixture_id'] for m in results if m['status']=='NOT_EVALUATED_BUDGET']
    retryable=[m['fixture_id'] for m in results if m['status'] in {'SCRAPER_FAILED_OR_UNVERIFIED','SCRAPER_OUTPUT_INVALID'}]
    payload={'schema_version':VERSION,'generated_at_utc':utcnow().isoformat(),'source_discovery':str(source_path),
             'source_sha256':hashlib.sha256(source_path.read_bytes()).hexdigest(),'action':'RUN_SCRAPER' if args.run_scraper else 'AUDIT_TXT' if args.audit_txt else 'REUSE_EXISTING' if args.reuse_existing else 'AUDIT_ONLY',
             'validation_level':'EXPLORATION' if args.exploration else 'STRICT',
             'batch_summary':{'planned':len(selected),'attempted_this_invocation':attempts,
                 'status':'INCOMPLETE' if pending else 'COMPLETED_WITH_ERRORS' if retryable else 'COMPLETED',
                 'pending_fixture_ids':pending,'retryable_fixture_ids':retryable,
                 'states':dict(__import__('collections').Counter(m['status'] for m in results))},
             'timezone':'Europe/Madrid','prematch_only':True,
             'source_preselection':str(args.preselection) if args.preselection else None,
             'source_preselection_sha256':hashlib.sha256(args.preselection.read_bytes()).hexdigest() if args.preselection else None,
             'min_odd_candidate':str(candidate_limit),'min_odd_observation':str(observation_limit),
             'matches':results}
    write_outputs(out,payload)
    print(f"Salida: {out/'market_preflight.json'} | {out/'market_preflight.csv'} | {out/'market_preflight.md'}")
    print(f"Selecciones: {out/'market_selection_review.csv'} | investigables: {out/'market_research_shortlist.csv'}")
    success_statuses={'READY_NOT_EXECUTED','MARKET_TRIPLES_EXTRACTED','HISTORICAL_MARKET_TRIPLES_EXTRACTED'}
    # A structured per-fixture rejection/failure is not a global CLI failure.
    return 0

from prematch_execution_control import confirmed_cli
main = confirmed_cli(main)

if __name__=='__main__':raise SystemExit(main())
