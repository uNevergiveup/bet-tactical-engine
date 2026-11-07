#!/usr/bin/env python3
"""Puente Bwin/API-Football por fixture; nunca traduce Event ID en team ID.

Las identidades provienen de un contexto API ya resuelto. Las cadenas del informe
son corroboración textual, no una prueba de pertenencia por carpeta o ID declarado.
"""
from __future__ import annotations
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

VERSION='1.0-fixture-bound'
IDENTITY_TIME_VERIFIED='FIXTURE_AND_BWIN_TIME_VERIFIED'
IDENTITY_API_ONLY='API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN'
IDENTITY_UNVERIFIED='FIXTURE_UNVERIFIED'
LEGACY_IDENTITY='FIXTURE_VERIFIED_BY_GENERATOR'
LEGAL={'fc','cf','ec','sc','ac','afc','fk','bk','cd','ca','ss','ssc','club','de','del','do','da'}
FEMALE={'w','women','woman','f','femenino','femenina','feminino','feminina','frauen','damen'}
CATEGORY={'b','ii','iii','reserves','reserve',*FEMALE}

def norm(value):
    s=unicodedata.normalize('NFKD',str(value or '').casefold())
    s=''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+',' ',s).strip()

def category(value):
    t=norm(value).split()
    return {x for x in t if x in CATEGORY or re.fullmatch(r'u(?:1[5-9]|2[0-3])',x)}

def keys(value):
    text=norm(value)
    tokens=text.split()
    variants={text}
    while len(tokens)>1 and tokens[0] in LEGAL:
        tokens=tokens[1:]
    while len(tokens)>1 and tokens[-1] in LEGAL:
        tokens=tokens[:-1]
    variants.add(' '.join(tokens))
    if tokens and tokens[0]=='deportivo' and len(tokens)>=2:
        # Solo si núcleo completo coincide con el nombre canónico, nunca por una palabra común.
        variants.add(' '.join(tokens[1:]))
    return {x for x in variants if x and len(x)>=4}

def compare_report_name(name, side, opposite):
    """EXACT/ALIAS/AMBIGUOUS/MISMATCH. Nunca acepta una mera coincidencia parcial."""
    targets=[side.get('api_name'),side.get('bwin_name')]
    other=[opposite.get('api_name'),opposite.get('bwin_name')]
    report_categories=category(name)
    expected=side.get('category','male')
    if (report_categories & FEMALE) and expected!='female': return 'MISMATCH'
    if (report_categories & {'b','ii','iii'}) and not (category(' '.join(t or '' for t in targets)) & report_categories):
        return 'MISMATCH'
    if expected=='female' and not report_categories:
        # Un nombre sin W en informe femenino puede existir; no se certifica por ello.
        pass
    k=keys(name)
    theirs=set().union(*(keys(n) for n in targets if n))
    others=set().union(*(keys(n) for n in other if n))
    if k & others and not k & theirs: return 'MISMATCH'
    if k & theirs:
        if k & others: return 'AMBIGUOUS'
        return 'EXACT' if norm(name) in {norm(n) for n in targets if n} else 'ALIAS'
    return 'MISMATCH'

def identity_from_payload(payload):
    match=payload.get('match') or {}; fx=payload.get('fixture') or {}
    eid=str(match.get('event_id') or ''); fid=fx.get('fixture_id')
    if not re.fullmatch(r'\d{6,}',eid) or not isinstance(fid,int) or fid<=0:
        raise ValueError('Contexto sin Event ID/fixture ID verificables')
    if fx.get('home_id') == fx.get('away_id') or not fx.get('home_id') or not fx.get('away_id'):
        raise ValueError('Fixture sin IDs HOME/AWAY distintos')
    url=match.get('url') or ''
    parsed=urlsplit(url)
    if parsed.hostname not in {'www.bwin.es','bwin.es'} or not re.search(r':'+re.escape(eid)+r'$',unquote(parsed.path)):
        raise ValueError('URL Bwin no coincide con Event ID')
    dt=datetime.fromisoformat(str(fx.get('date') or '').replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('Fixture sin timezone')
    source_status=str((match.get('identity_status') or LEGACY_IDENTITY))
    if source_status not in {IDENTITY_TIME_VERIFIED, IDENTITY_API_ONLY, LEGACY_IDENTITY}:
        raise ValueError('Contexto sin estado de identidad temporal verificable')
    out={
        'schema_version':VERSION,'identity_status':source_status,
        'provenance':'generate_context.py: fixture validado y persistido; no equivale a vínculo oficial publicado por Bwin',
        'bwin_event_id':eid,'bwin_url':url,'api_fixture_id':fid,
        'league_id':fx.get('league_id'),'season':fx.get('season'),'league_name':fx.get('league_name'),
        'kickoff_utc':dt.astimezone(timezone.utc).isoformat(),
        'home':{'api_team_id':fx['home_id'],'api_name':fx.get('home_name'),
                'bwin_name':match.get('home_name_raw') or match.get('home_name'),
                'category':match.get('home_gender','male')},
        'away':{'api_team_id':fx['away_id'],'api_name':fx.get('away_name'),
                'bwin_name':match.get('away_name_raw') or match.get('away_name'),
                'category':match.get('away_gender','male')},
    }
    if not all(out[s]['api_name'] and out[s]['bwin_name'] for s in ('home','away')):
        raise ValueError('Fixture sin nombres de equipo acreditados')
    return out

def validate_identity(identity, payload=None, event=None, url=None):
    if identity.get('identity_status') not in {IDENTITY_TIME_VERIFIED, IDENTITY_API_ONLY, LEGACY_IDENTITY}:
        raise ValueError('Estado de identidad inválido')
    if event is not None and str(identity.get('bwin_event_id'))!=str(event):raise ValueError('Event ID de identidad diferente')
    if url and identity.get('bwin_url')!=url:raise ValueError('URL de identidad diferente')
    if payload is not None:
        expected=identity_from_payload(payload)
        for field in ('bwin_event_id','bwin_url','api_fixture_id','league_id','season','kickoff_utc','home','away'):
            if identity.get(field)!=expected.get(field):raise ValueError('Identidad contradice contexto API: '+field)
    return True

def write_identity(context_json, output=None):
    p=Path(context_json)
    payload=json.loads(p.read_text(encoding='utf-8-sig'))
    ident=identity_from_payload(payload)
    target=Path(output) if output else p.with_name('match_identity.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_name(target.name+'.'+str(os.getpid())+'.tmp')
    try:
        tmp.write_text(json.dumps(ident,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.replace(target)
    finally:tmp.unlink(missing_ok=True)
    return target
