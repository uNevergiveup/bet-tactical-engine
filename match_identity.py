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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

VERSION='1.0-fixture-bound'
IDENTITY_TIME_VERIFIED='FIXTURE_AND_BWIN_TIME_VERIFIED'
IDENTITY_API_ONLY='API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN'
IDENTITY_UNVERIFIED='FIXTURE_UNVERIFIED'
LEGACY_IDENTITY='FIXTURE_VERIFIED_BY_GENERATOR'
LEGAL={'fc','cf','ec','sc','ac','afc','fk','bk','cd','ca','ss','ssc','club','de','del','do','da'}
FEMALE={'w','women','woman','f','femenino','femenina','feminino','feminina','frauen','damen','fem'}
MALE_MARKERS={'men','man','male','masculino','masculina','masculinos','masculinas'}
RESERVE_OR_B={'b','ii','iii','reserves','reserve','filial'}
CATEGORY={*RESERVE_OR_B,*FEMALE}
DISPLAY_PAREN={'local','visitante','home','away','titular','confirmado','probable','xi confirmado','xi oficial'}
LINGUISTIC_TEAM_ALIASES={
    'gales':'wales',
    'espana':'spain',
    'alemania':'germany',
    'inglaterra':'england',
    'escocia':'scotland',
    'irlanda':'ireland',
    'irlanda del norte':'northern ireland',
    'francia':'france',
    'italia':'italy',
    'belgica':'belgium',
    'suiza':'switzerland',
    'paises bajos':'netherlands',
    'holanda':'netherlands',
    'dinamarca':'denmark',
    'suecia':'sweden',
    'noruega':'norway',
    'polonia':'poland',
    'ucrania':'ukraine',
    'croacia':'croatia',
    'grecia':'greece',
    'turquia':'turkey',
    'rumania':'romania',
    'hungria':'hungary',
    'eslovaquia':'slovakia',
    'eslovenia':'slovenia',
    'republica checa':'czech republic',
    'chequia':'czech republic',
    'estados unidos':'usa',
    'eeuu':'usa',
    'brasil':'brazil',
    'japon':'japan',
    'corea del sur':'south korea',
    'marruecos':'morocco',
    'argelia':'algeria',
    'tunez':'tunisia',
    'egipto':'egypt',
    'sudafrica':'south africa',
    'costa de marfil':'ivory coast',
    'arabia saudi':'saudi arabia',
    'arabia saudita':'saudi arabia',
    'emiratos arabes unidos':'united arab emirates',
    'nueva zelanda':'new zealand',
    'islandia':'iceland',
    'finlandia':'finland',
    'lituania':'lithuania',
    'letonia':'latvia',
    'bielorrusia':'belarus',
    'rusia':'russia',
    'moldavia':'moldova',
    'macedonia del norte':'north macedonia',
    'bosnia y herzegovina':'bosnia herzegovina',
}

def norm(value):
    s=unicodedata.normalize('NFKD',str(value or '').casefold())
    s=''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+',' ',s).strip()

def category(value):
    text=norm(value)
    t=text.split()
    out=set()
    for idx,x in enumerate(t):
        if x in CATEGORY or re.fullmatch(r'u(?:1[5-9]|2[0-3])',x):
            out.add(x)
        elif x=='sub' and idx+1<len(t) and re.fullmatch(r'(?:1[5-9]|2[0-3])',t[idx+1]):
            out.add(f'u{t[idx+1]}')
        elif x in {'juvenil','juveniles'}:
            out.add('youth')
    return out

def normalize_category_gender(value: str | None) -> str:
    """Normaliza representación de categoría/género a 'female', 'male' o valor nominal limpio."""
    k = norm(value)
    if k in FEMALE or k == 'female':
        return 'female'
    if k in {'m', 'male', 'man', 'men', 'masculino', 'masculina', 'masculinos', 'masculinas'}:
        return 'male'
    return k or 'male'

def split_team_descriptor(value):
    """Separa nombre núcleo de equipo, paréntesis bilingües y sufijos descriptivos tras `;`."""
    cleaned=re.sub(r'\[([^]]+)\]\([^)]*\)',r'\1',str(value or '')).strip(' `*.;,')
    # Separa metadatos de competición/categoría tras ';' (ej. 'Portugal; selecciones absolutas masculinas')
    base,descriptor=(cleaned.split(';',1)+[''])[:2]
    base=base.strip(' `*;,.')
    descriptor=descriptor.strip(' `*;,.')
    primary=re.sub(r'\s*\([^)]*\)','',base).strip()
    parens=[p.strip() for p in re.findall(r'\(([^()]*)\)',base) if p.strip()]
    return {'raw':cleaned,'base':base,'primary':primary,'parens':parens,'descriptor':descriptor}

def _canonical_variants(text):
    k=norm(text)
    if not k:
        return set()
    variants={k}
    if k in LINGUISTIC_TEAM_ALIASES:
        variants.add(LINGUISTIC_TEAM_ALIASES[k])
    tokens=k.split()
    t_front=list(tokens)
    while len(t_front)>1 and t_front[0] in LEGAL:
        t_front=t_front[1:]
    while len(t_front)>1 and t_front[-1] in LEGAL:
        t_front=t_front[:-1]
    reduced=' '.join(t_front)
    if reduced:
        variants.add(reduced)
        if reduced in LINGUISTIC_TEAM_ALIASES:
            variants.add(LINGUISTIC_TEAM_ALIASES[reduced])
    if tokens and tokens[0]=='deportivo' and len(tokens)>=2:
        # Solo si núcleo completo coincide con el nombre canónico, nunca por una palabra común.
        dep=' '.join(tokens[1:])
        variants.add(dep)
        if dep in LINGUISTIC_TEAM_ALIASES:
            variants.add(LINGUISTIC_TEAM_ALIASES[dep])
    return {x for x in variants if x and len(x)>=3}

def keys(value):
    parts=split_team_descriptor(value)
    out=set()
    primary_vars=_canonical_variants(parts['primary'])
    out.update(primary_vars)
    # Solo incluye alias entre paréntesis si no es etiqueta genérica y comparte vínculo lingüístico o núcleo
    for part in parts['parens']:
        pk=norm(part)
        if not pk or pk in DISPLAY_PAREN:
            continue
        p_vars=_canonical_variants(part)
        if p_vars & primary_vars:
            out.update(p_vars)
        else:
            main_tok=set(norm(parts['primary']).split())-LEGAL
            part_tok=set(pk.split())-LEGAL
            if main_tok & part_tok:
                out.update(p_vars)
    return {x for x in out if x and (len(x)>=4 or x in {'usa'})}

def compare_report_name(name, side, opposite):
    """EXACT/ALIAS/AMBIGUOUS/MISMATCH. Nunca acepta una mera coincidencia parcial."""
    parts=split_team_descriptor(name)
    targets=[side.get('api_name'),side.get('bwin_name')]
    other=[opposite.get('api_name'),opposite.get('bwin_name')]
    report_categories=category(name)
    expected=normalize_category_gender(side.get('category') or 'male')
    full_tokens=set(norm(name).split())
    if expected=='female' and (full_tokens & MALE_MARKERS):
        return 'MISMATCH'
    if (report_categories & FEMALE) and expected!='female': return 'MISMATCH'
    target_cats=category(' '.join(t or '' for t in targets))
    if (report_categories & {'b','ii','iii','youth'}) and not (target_cats & report_categories):
        return 'MISMATCH'
    if any(c.startswith('u') for c in report_categories) and not (target_cats & {c for c in report_categories if c.startswith('u')}):
        return 'MISMATCH'
    if expected=='female' and not report_categories:
        # Un nombre sin W en informe femenino puede existir; no se certifica por ello.
        pass
    k=keys(name)
    theirs=set().union(*(keys(n) for n in targets if n))
    others=set().union(*(keys(n) for n in other if n))
    # Rechaza si un paréntesis explícito menciona al equipo rival (ej. 'Wales (Portugal)')
    for part in parts['parens']:
        if norm(part) not in DISPLAY_PAREN and (_canonical_variants(part) & others) and not (_canonical_variants(part) & theirs):
            return 'MISMATCH'
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
        'provenance':('Fase 3 + PREMATCH: fixture API y kickoff publicado por Bwin '
                      'revalidados mediante contrato temporal con fuentes SHA-256'
                      if match.get('temporal_identity_evidence') else
                      'generate_context.py: fixture validado y persistido; no equivale a vínculo oficial publicado por Bwin'),
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
    temporal=match.get('temporal_identity_evidence')
    if temporal is not None:
        if source_status!=IDENTITY_TIME_VERIFIED or not isinstance(temporal,dict):
            raise ValueError('Evidencia temporal incompatible con estado de identidad')
        if temporal.get('schema_version') in {'1.0-browser-event-time', '1.1-browser-event-time'}:
            for field in ('source','kickoff_utc'):
                if temporal.get(field) in (None,''):
                    raise ValueError('Evidencia temporal incompleta: '+field)
            obs=temporal.get('catalog_observations') or []
            if not isinstance(obs,list) or not obs or not all(isinstance(r,dict) for r in obs):
                raise ValueError('Evidencia temporal incompleta: catalog_observations')
            if any(str(r.get('bwin_event_id'))!=eid or r.get('bwin_url')!=url for r in obs):
                raise ValueError('Evidencia temporal contradice la identidad del contexto')
            if temporal.get('method') == 'BWIN_EVENT_COUNTDOWN_MINUTES':
                obs_raw = temporal.get('observed_at_utc')
                cd_raw = temporal.get('countdown_minutes')
                if not obs_raw or cd_raw is None:
                    raise ValueError('Evidencia temporal countdown incompleta: observed_at_utc/countdown_minutes')
                obs_dt = datetime.fromisoformat(str(obs_raw).replace('Z', '+00:00'))
                ko_dt = datetime.fromisoformat(str(temporal['kickoff_utc']).replace('Z', '+00:00'))
                cd_mins = int(cd_raw)
                if obs_dt.tzinfo is None or ko_dt.tzinfo is None or cd_mins <= 0 or cd_mins > 180:
                    raise ValueError('Evidencia temporal countdown inválida')
                if abs(((obs_dt + timedelta(minutes=cd_mins)) - ko_dt).total_seconds()) > 1.0:
                    raise ValueError('Evidencia temporal countdown inconsistente con kickoff_utc')
        else:
            for field in ('fixture_id','bwin_event_id','bwin_url','home','away','kickoff_utc',
                          'preflight_sha256','discovery_sha256'):
                if temporal.get(field) in (None,''):
                    raise ValueError('Evidencia temporal incompleta: '+field)
            if (str(temporal['fixture_id'])!=str(fid) or str(temporal['bwin_event_id'])!=eid or
                    temporal['bwin_url']!=url or norm(temporal['home'])!=norm(out['home']['api_name']) or
                    norm(temporal['away'])!=norm(out['away']['api_name'])):
                raise ValueError('Evidencia temporal contradice la identidad del contexto')
        temporal_kickoff=datetime.fromisoformat(str(temporal['kickoff_utc']).replace('Z','+00:00'))
        tol_sec = min(240.0, float(temporal.get('tolerance_minutes') or 3) * 60.0)
        if temporal_kickoff.tzinfo is None or abs((temporal_kickoff-dt).total_seconds())>tol_sec:
            raise ValueError('Evidencia temporal contradice el kickoff API')
        out['temporal_identity_evidence']=temporal
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
        for field in ('bwin_event_id','bwin_url','api_fixture_id','league_id','season','kickoff_utc',
                      'temporal_identity_evidence'):
            if identity.get(field)!=expected.get(field):raise ValueError('Identidad contradice contexto API: '+field)
        for side in ('home','away'):
            s_id=identity.get(side) or {}
            s_exp=expected.get(side) or {}
            if (s_id.get('api_team_id')!=s_exp.get('api_team_id') or
                s_id.get('api_name')!=s_exp.get('api_name') or
                s_id.get('bwin_name')!=s_exp.get('bwin_name') or
                normalize_category_gender(s_id.get('category'))!=normalize_category_gender(s_exp.get('category'))):
                raise ValueError('Identidad contradice contexto API: '+side)
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
