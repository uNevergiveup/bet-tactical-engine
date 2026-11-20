#!/usr/bin/env python3
"""Filtro configurable de competiciones adultas PREMATCH, sin consultar Bwin.

Por defecto admite todas las ligas y copas adultas identificables, incluidas
las internacionales. Excluye categorias juveniles/reservas detectables y envia
a REVIEW los casos ambiguos. Conserva el esquema de salida legacy.
La politica estricta anterior sigue disponible con --policy domestic-top-tier.

Ejemplo:
 python filter_major_leagues.py --input outputs/discovery/2026-11-01_1600_0200/fixtures.json
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = '1.0-major-leagues'  # Esquema compatible con preselect_matches y trial
FILTER_IMPLEMENTATION = '2.2-category-aware-scopes'
# allowlist EXPLÍCITA de primeras divisiones nacionales adultas.
# No presume que API-Football marque el nivel de una liga con league.type.
# Revisar este catálogo para cada liga nueva; IDs de API-Football, no Bwin.
# Una competición con copa regional/segunda división/no catalogada NO pasa automáticamente.
CATALOG_BY_COUNTRY: dict[str, tuple[int, ...]] = {
 'Albania': (310,), 'Andorra': (312,), 'Angola': (397,),
 'Argentina': (128,), 'Armenia': (342,), 'Aruba': (421,),
 'Austria': (218,), 'Bahrain': (417,), 'Belarus': (116,),
 'Belgium': (144,), 'Benin': (415,), 'Bolivia': (344,),
 'Botswana': (412,), 'Brazil': (71,), 'Burkina-Faso': (423,),
 'Cameroon': (411,), 'Canada': (479,), 'Chile': (265,),
 'Colombia': (239,), 'Costa-Rica': (162,), 'Croatia': (210,),
 'Cyprus': (318,), 'Czech-Republic': (345,), 'Denmark': (119,),
 'Dominican-Republic': (759,), 'Ecuador': (242,), 'El-Salvador': (370,),
 'England': (39,), 'Estonia': (329,), 'Ethiopia': (363,),
 'France': (61,), 'Georgia': (327,), 'Germany': (78,),
 'Ghana': (570,), 'Gibraltar': (758,), 'Greece': (197,),
 'Guatemala': (339,), 'Honduras': (234,), 'Hungary': (271,),
 'Ireland': (357,), 'Israel': (383,), 'Italy': (135,139),
 'Ivory-Coast': (386,), 'Jamaica': (322,), 'Jordan': (387,),
 'Kosovo': (664,), 'Kuwait': (330,), 'Malta': (393,),
 'Mali': (598,), 'Mauritania': (762,), 'Mexico': (262,673),
 'Montenegro': (355,), 'Morocco': (200,), 'Netherlands': (88,91),
 'Nicaragua': (396,), 'Nigeria': (399,), 'Norway': (103,),
 'Panama': (304,), 'Paraguay': (252,), 'Peru': (281,1229),
 'Portugal': (94,948), 'Romania': (283,), 'Russia': (235,),
 'Saudi-Arabia': (307,), 'Scotland': (179,), 'Senegal': (403,),
 'Serbia': (286,), 'Slovakia': (332,), 'Slovenia': (373,),
 'Spain': (140,142,), 'Suriname': (568,), 'Sweden': (113,),
 'Switzerland': (207,739), 'Tanzania': (567,), 'Turkey': (203,),
 'USA': (253,254,1130), 'Uruguay': (268,), 'Venezuela': (299,),
}
# IDs de categorías no admisibles reconocidas en el barrido inicial:
# segundas/terceras divisiones, copas, categorías juveniles y reservas.
# No sustituye la revisión de nuevos IDs de otras jornadas.
KNOWN_EXCLUDED_IDS = {
 72, 73, 80, 86, 87, 95, 104, 129, 131, 134, 136, 138, 145, 185, 201,
 141, 204, 240, 255, 263, 269, 272, 287, 300, 308, 328, 358, 361, 392,
 433, 435, 436, 439, 440, 442, 443, 444, 445, 446, 447, 448, 449,
 451, 452, 454, 455, 456, 457, 458, 459, 460, 489, 496, 510, 563, 564,
 600, 622, 711, 722, 872, 865, 875, 876, 877, 878, 879, 909, 942,
 943, 950, 1036, 1049, 1126, 1133, 1135, 1150, 1155, 1164, 1165,
 1196, 1200, 1242, 163, 710,
}
WOMENS_LEAGUE_IDS = {139, 142, 91, 673, 1229, 948, 739, 254, 1130}
# Catálogo de categoría explícita independiente del catálogo de *nivel*.
# League ID 141 no es primera división y por eso debe seguir excluido de
# CATALOG_BY_COUNTRY, pero su identidad como competición masculina sénior sí
# está acreditada. La combinación completa evita inferir género por ausencia
# de palabras como Women/U19.
EXPLICIT_COMPETITION_CATEGORIES: dict[int, dict[str,Any]] = {
 1130: {
   'provider':'API_FOOTBALL','country':'USA','canonical_name':'USL Super League',
   'names':('USL Super League',),'bwin_aliases':('USL Super League',),
   'gender':'F','level':'SENIOR','category_status':'ELIGIBLE',
   'season_policy':'POSITIVE_API_SEASON',
   'evidence':'EXISTING_WOMENS_LEAGUE_IDS_1130_USL_SUPER_LEAGUE_SENIOR',
 },
 5: {
   'provider':'API_FOOTBALL','country':'World','canonical_name':'UEFA Nations League',
   'names':('UEFA Nations League',),'bwin_aliases':('UEFA Nations League',),
   'gender':'M','level':'SENIOR','category_status':'ELIGIBLE',
   'season_policy':'POSITIVE_API_SEASON',
   'evidence':'CURATED_API_FOOTBALL_LEAGUE_5_UEFA_MALE_SENIOR_NATIONS_LEAGUE',
 },
 66: {
   'provider':'API_FOOTBALL','country':'France','canonical_name':'Coupe de France',
   'names':('Coupe de France',),'bwin_aliases':('Copa de Francia',),
   'gender':'M','level':'SENIOR','category_status':'ELIGIBLE',
   'season_policy':'POSITIVE_API_SEASON',
   'evidence':'CURATED_API_FOOTBALL_LEAGUE_66_FRENCH_MALE_SENIOR_CUP',
 },
 72: {
   'provider':'API_FOOTBALL','country':'Brazil','canonical_name':'Serie B',
   'names':('Serie B',),'bwin_aliases':('Brasileiro Serie B',),
   'gender':'M','level':'SENIOR','category_status':'ELIGIBLE',
   'season_policy':'POSITIVE_API_SEASON',
   'evidence':'CURATED_API_FOOTBALL_LEAGUE_72_BRAZILIAN_MALE_SENIOR_SECOND_DIVISION',
 },
 141: {
   'provider':'API_FOOTBALL','country': 'Spain','canonical_name':'Segunda División',
   'names': ('Segunda División', 'Segunda Division', 'LaLiga 2', 'La Liga 2'),
   'bwin_aliases':('LaLiga 2','La Liga 2'),'gender': 'M','level':'SENIOR',
   'category_status': 'ELIGIBLE',
   'season_policy': 'POSITIVE_API_SEASON',
   'evidence': 'CURATED_API_FOOTBALL_LEAGUE_141_SPANISH_MALE_SENIOR_SECOND_DIVISION',
 },
 143: {
   'provider':'API_FOOTBALL','country':'Spain','canonical_name':'Copa del Rey',
   'names':('Copa del Rey',),'bwin_aliases':('Copa del Rey',),
   'gender':'M','level':'SENIOR','category_status':'ELIGIBLE',
   'season_policy':'POSITIVE_API_SEASON',
   'evidence':'CURATED_API_FOOTBALL_LEAGUE_143_SPANISH_MALE_SENIOR_CUP',
 },
 239: {
   'provider':'API_FOOTBALL','country':'Colombia','canonical_name':'Primera A',
   'names':('Primera A',),'bwin_aliases':('Primera A',),
   'gender':'M','level':'SENIOR','category_status':'ELIGIBLE',
   'season_policy':'POSITIVE_API_SEASON',
   'evidence':'CURATED_API_FOOTBALL_LEAGUE_239_COLOMBIAN_MALE_SENIOR_FIRST_DIVISION',
 },
}
# La categorización por región no modifica la decisión de elegibilidad.
REGIONAL_GROUPS = {
 'EUROPE': {'Albania','Andorra','Armenia','Austria','Belarus','Belgium','Croatia','Cyprus',
 'Czech-Republic','Denmark','England','Estonia','France','Georgia','Germany','Gibraltar',
 'Greece','Hungary','Ireland','Israel','Italy','Kosovo','Malta','Montenegro','Netherlands',
 'Norway','Portugal','Romania','Russia','Scotland','Serbia','Slovakia','Slovenia','Spain',
 'Sweden','Switzerland','Turkey'},
 'SOUTH_AMERICA': {'Argentina','Bolivia','Brazil','Chile','Colombia','Ecuador',
 'Paraguay','Peru','Uruguay','Venezuela'},
 'NORTH_CENTRAL_AMERICA': {'Aruba','Canada','Costa-Rica','Dominican-Republic',
 'El-Salvador','Guatemala','Honduras','Jamaica','Mexico','Nicaragua','Panama','USA','Suriname'},
 'AFRICA': {'Angola','Benin','Botswana','Burkina-Faso','Cameroon','Ethiopia','Ghana',
 'Ivory-Coast','Mali','Mauritania','Morocco','Nigeria','Senegal','Tanzania'},
 'ASIA': {'Bahrain','Jordan','Kuwait','Saudi-Arabia'},
}
REGION_PRIORITY = {'EUROPE':1,'SOUTH_AMERICA':1,'NORTH_CENTRAL_AMERICA':1,
                   'AFRICA':2,'ASIA':2,'OTHER':3}
# No permitir que un id mal catalogado deje pasar una competencia juvenil/filial/copa.
# El nombre de la competición ES un control adicional; el nombre del equipo no,
# dado que un filial puede ascender legalmente a una primera división en algunos países.
import re
DISALLOWED_LEAGUE = re.compile(
 r'(?i)(?:\bu\s*[- ]?\d{2}\b|\bunder\s*[- ]?\d{2}\b|\byouth\b|'
 r'\bjuvenil\b|\breserves?\b|\breservas?\b|\bnext\s*pro\b|'
 r'\bsegunda\b|\bsecond\b|\bthird\b|\btercera\b|\bserie\s*[bcd]\b|'
 r'\bdivision\s*(?:one|two|three|1|2|3)\b|\bdivisi[oó]n\s*[234]\b|'
 r'\bliga\s*(?:2|3|de ascenso|de expansi[oó]n)\b|\bcup\b|\bcopa\b|'
 r'\bchampions\s*league\b|\bprimeira\s*liga\s*2\b)', re.I)
# Excepción: "1. Division" puede ser el nombre de la primera división de un país.
# El catálogo explícito manda: esta regex se aplica sólo a alertas concretas
# juveniles / copas / reservas, NO a ordinales ambiguos.
HARD_STOP = re.compile(r'(?i)(?:\bu\s*[- ]?\d{2}\b|\bunder\s*[- ]?\d{2}\b|\byouth\b|'
                       r'\bjuvenil\b|\breserves?\b|\breservas?\b|\bnext\s*pro\b|'
                       r'\bcopa\b|\bcup\b|\bchampions\s*league\b)')


def region_for(country: str) -> str:
    for region, countries in REGIONAL_GROUPS.items():
        if country in countries:
            return region
    return 'OTHER'


def _identity_label(value: object) -> str:
    text=unicodedata.normalize('NFKD',str(value or ''))
    text=''.join(char for char in text if not unicodedata.combining(char)).lower()
    return ' '.join(re.sub(r'[^a-z0-9]+',' ',text).split())


def explicit_competition_category(row:dict[str,Any]) -> dict[str,Any] | None:
    """Return category only when every curated competition key agrees."""
    spec=EXPLICIT_COMPETITION_CATEGORIES.get(row.get('league_id'))
    if not spec:return None
    season=row.get('season')
    if (row.get('country')!=spec['country'] or
        _identity_label(row.get('league')) not in {_identity_label(name) for name in spec['names']} or
        not isinstance(season,int) or isinstance(season,bool) or season<=0):
        return None
    return {'gender':spec['gender'],'category_status':spec['category_status'],
      'category_evidence_status':'CORROBORATED','category_level':spec['level'],
      'category_provenance':{'method':'EXPLICIT_COMPETITION_CATEGORY_CATALOG',
        'provider':spec['provider'],'competition_id':row['league_id'],
        'canonical_name':spec['canonical_name'],'league_id':row['league_id'],
        'league_name':row.get('league'),'country':row.get('country'),
        'season':season,'season_policy':spec['season_policy'],'evidence':spec['evidence']}}


def verified_bwin_competition_alias(row:dict[str,Any], evidence:object) -> bool:
    """Recognize a Bwin label only through a fully keyed registry entry."""
    if not explicit_competition_category(row):return False
    spec=EXPLICIT_COMPETITION_CATEGORIES[row['league_id']]
    observed=_identity_label(evidence)
    return any(re.search(rf'(?<![a-z0-9]){re.escape(_identity_label(alias))}(?![a-z0-9])',observed)
               for alias in spec['bwin_aliases'])


# Etiquetas inequivocas: no bloquear "Boca Juniors", "Young Boys", etc.
YOUTH_PATTERN = re.compile(
    r"(?i)(?:\bu\s*[- ]?\s*(?:1[0-9]|2[0-3])\b|"
    r"\bunder\s*[- ]?\s*(?:1[0-9]|2[0-3])\b|"
    r"\bsub\s*[- ]?\s*(?:1[0-9]|2[0-3])\b|"
    r"\b(?:youth|juvenil(?:es)?|juniores|primavera)\b)"
)
RESERVE_LEAGUE_PATTERN = re.compile(
    r"(?i)\b(?:reserves?|reservas?|reserve\s+league|"
    r"development\s+league|second\s+teams?)\b"
)
RESERVE_TEAM_PATTERN = re.compile(
    r"(?i)(?:\b(?:reserves?|reservas?|reserve\s+team)\b|"
    r"\s+(?:B|II|III|2)\s*$|\s+\(B\)\s*$)"
)
WOMEN_PATTERN = re.compile(
    r"(?i)(?:\bwomen(?:'s)?\b|\bwomens\b|\bfemenin[oa]\b|"
    r"\bfeminin[ea]\b|\bfrauen\b|\bfemminile\b|"
    r"\bdamen\b|\(W\)|\sW\s*$)"
)


VALID_POLICIES = ('adult-open', 'domestic-top-tier', 'top-tier-and-senior-cups')
CUP_OR_INTL_HINT_RE = re.compile(
    r'(?i)(?:\bcopa\b|\bcup\b|\bchampions\s*league\b|\beuropa\s*league\b|\bconference\s*league\b|\btrophy\b|\bpokal\b|\bta[cç]a\b)'
)


def classify_competition_category(
    row: dict[str, Any],
    registered_entry: dict[str, Any] | None = None,
) -> str:
    """Clasifica la categoría estructural usando únicamente evidencia verificable."""
    country = str(row.get('country') or '')
    league = str(row.get('league') or '')
    home = str(row.get('home') or '')
    away = str(row.get('away') or '')
    lid = row.get('league_id')
    if YOUTH_PATTERN.search(league) or YOUTH_PATTERN.search(home) or YOUTH_PATTERN.search(away):
        return 'YOUTH'
    if RESERVE_LEAGUE_PATTERN.search(league) or RESERVE_TEAM_PATTERN.search(home) or RESERVE_TEAM_PATTERN.search(away):
        return 'RESERVES'
    if registered_entry is None:
        from competition_registry import lookup
        registered_entry = lookup(row)
    if registered_entry:
        typ = registered_entry.get('type')
        lvl = registered_entry.get('level')
        tier = registered_entry.get('national_tier')
        gender = registered_entry.get('gender')
        if typ == 'CLUB_FRIENDLY':
            return 'CLUB_FRIENDLY'
        if typ == 'LEAGUE':
            if tier == 1 and lvl == 'SENIOR':
                return 'SENIOR_FEMALE_TOP_TIER' if gender == 'F' else 'SENIOR_MALE_TOP_TIER'
            if isinstance(tier, int) and tier >= 2:
                return 'LOWER_DIVISION'
        if typ in ('NATIONAL_CUP', 'NATIONAL_OFFICIAL') and lvl == 'SENIOR':
            return 'SENIOR_ELIGIBLE_CUP_OR_INTERNATIONAL'
        if typ == 'NATIONAL_FRIENDLY':
            from competition_registry import resolve_national_friendly_evidence
            if resolve_national_friendly_evidence(row):
                return 'SENIOR_ELIGIBLE_CUP_OR_INTERNATIONAL'
            return 'UNKNOWN_CATEGORY'
    if lid in KNOWN_EXCLUDED_IDS:
        return 'LOWER_DIVISION'
    if lid in CATALOG_BY_COUNTRY.get(country, ()):
        is_women = (lid in WOMENS_LEAGUE_IDS) or bool(WOMEN_PATTERN.search(league))
        return 'SENIOR_FEMALE_TOP_TIER' if is_women else 'SENIOR_MALE_TOP_TIER'
    return 'UNKNOWN_CATEGORY'


def classify(row: dict[str,Any], policy: str='adult-open') -> tuple[str,str]:
    """Decision conservadora: diferencia juveniles, reservas, primeras M/F, copas adultas y divisiones inferiores."""
    if policy not in VALID_POLICIES:
        raise ValueError(f'Politica no admitida: {policy}')
    if row.get('status') != 'NS':
        return 'EXCLUDED', 'NOT_PREMATCH'
    country = str(row.get('country') or '')
    league = str(row.get('league') or '')
    lid = row.get('league_id')
    if not isinstance(lid,int) or isinstance(lid,bool) or lid <= 0 or not league:
        return 'REVIEW','MISSING_LEAGUE_ID_OR_NAME'
    if not country:
        return 'REVIEW','MISSING_COUNTRY_FOR_IDENTITY'
    if YOUTH_PATTERN.search(league):
        return 'EXCLUDED','YOUTH_COMPETITION'
    if RESERVE_LEAGUE_PATTERN.search(league):
        return 'EXCLUDED','RESERVE_COMPETITION'
    home, away = str(row.get('home') or ''),str(row.get('away') or '')
    if not home or not away:
        return 'REVIEW','MISSING_TEAM_NAME'
    if YOUTH_PATTERN.search(home) or YOUTH_PATTERN.search(away):
        return 'EXCLUDED','YOUTH_TEAM'
    if RESERVE_TEAM_PATTERN.search(home) or RESERVE_TEAM_PATTERN.search(away):
        return 'EXCLUDED','RESERVE_TEAM'
    from competition_registry import admission, lookup, resolve_national_friendly_evidence
    entry = lookup(row)
    if policy == 'domestic-top-tier':
        if entry:
            typ = entry.get('type')
            if typ == 'CLUB_FRIENDLY':
                return 'EXCLUDED', 'CLUB_FRIENDLY'
            if typ == 'LEAGUE':
                tier = entry.get('national_tier')
                if tier == 1 and entry.get('level') == 'SENIOR':
                    return 'ELIGIBLE', 'CURATED_DOMESTIC_TOP_TIER'
                if isinstance(tier, int) and tier >= 2:
                    return 'EXCLUDED', 'LOWER_DIVISION'
            if typ in ('NATIONAL_CUP', 'NATIONAL_OFFICIAL', 'NATIONAL_FRIENDLY'):
                return 'EXCLUDED', 'CUP_OR_INTERNATIONAL_EXCLUDED_BY_DOMESTIC_TOP_TIER'
        if lid in KNOWN_EXCLUDED_IDS:
            return 'EXCLUDED', 'KNOWN_NON_TOP_TIER'
        if lid in CATALOG_BY_COUNTRY.get(country, ()):
            return 'ELIGIBLE', 'CURATED_DOMESTIC_TOP_TIER'
        if CUP_OR_INTL_HINT_RE.search(league):
            return 'EXCLUDED', 'CUP_OR_INTERNATIONAL_EXCLUDED_BY_DOMESTIC_TOP_TIER'
        return 'REVIEW', 'NOT_IN_CURATED_TOP_TIER_CATALOG'
    if policy == 'top-tier-and-senior-cups':
        if entry:
            typ = entry.get('type')
            if typ == 'CLUB_FRIENDLY':
                return 'EXCLUDED', 'CLUB_FRIENDLY'
            if typ == 'LEAGUE':
                tier = entry.get('national_tier')
                if tier == 1 and entry.get('level') == 'SENIOR':
                    return 'ELIGIBLE', 'CURATED_DOMESTIC_TOP_TIER'
                if isinstance(tier, int) and tier >= 2:
                    return 'EXCLUDED', 'LOWER_DIVISION'
                return 'REVIEW', 'TIER_UNKNOWN'
            if typ == 'NATIONAL_CUP':
                if entry.get('level') == 'SENIOR':
                    return 'ELIGIBLE', 'ADULT_NATIONAL_CUP'
                return 'REVIEW', 'TIER_UNKNOWN'
            if typ == 'NATIONAL_OFFICIAL':
                if entry.get('level') == 'SENIOR':
                    return 'ELIGIBLE', 'NATIONAL_OFFICIAL'
                return 'REVIEW', 'TIER_UNKNOWN'
            if typ == 'NATIONAL_FRIENDLY':
                if resolve_national_friendly_evidence(row):
                    return 'ELIGIBLE', 'NATIONAL_FRIENDLY'
                return 'REVIEW', 'SENIOR_NATIONAL_TEAMS_PENDING'
        if lid in KNOWN_EXCLUDED_IDS:
            return 'EXCLUDED', 'LOWER_DIVISION'
        if lid in CATALOG_BY_COUNTRY.get(country, ()):
            return 'ELIGIBLE', 'CURATED_DOMESTIC_TOP_TIER'
        return 'REVIEW', 'TIER_UNKNOWN'
    # No hay un campo universal de categoria de edad. La ausencia de indicios
    # no demuestra la adultez; conservar esa limitacion en la politica.
    return admission(row)


def matching_eligibility(row:dict[str,Any]) -> tuple[str,str]:
    """Admission to local candidate matching, never commercial certification."""
    for key in ('filter_status','category_status'):
        if str(row.get(key) or '').upper()=='EXCLUDED':
            return 'EXCLUDED',str(row.get('filter_reason') or row.get('category_reason') or 'EXPLICIT_CATEGORY_EXCLUSION')
    # Reuse the existing exclusion policy. Matching is also used on frozen
    # rows without NS status; temporal verification remains a separate gate.
    raw_status=str(row.get('status') or '')
    api_statuses={'NS','TBD','1H','HT','2H','ET','P','FT','AET','PEN','BT','SUSP','INT',
                  'PST','CANC','ABD','AWD','WO','LIVE'}
    policy_status=raw_status if raw_status in api_statuses else 'NS'
    status,reason=classify({**row,'status':policy_status},policy='adult-open')
    if status=='EXCLUDED':return status,reason
    if status=='REVIEW':return 'PENDING_CLASSIFICATION',reason
    from competition_registry import category
    registered=category(row)
    if (explicit_competition_category(row) or
        (registered.get('category_evidence_status')=='CORROBORATED' and registered.get('category_level')=='SENIOR') or
        (row.get('category_evidence_status')=='CORROBORATED' and row.get('category_level')=='SENIOR')):
        return 'ELIGIBLE','POSITIVE_SENIOR_CATEGORY_EVIDENCE'
    return 'PENDING_CLASSIFICATION','CATEGORY_EVIDENCE_NOT_CORROBORATED'


def gender_for(row:dict[str,Any]) -> str:
    """No inventar masculino si API-Football no publica el genero."""
    explicit=explicit_competition_category(row)
    if explicit:return explicit['gender']
    if WOMEN_PATTERN.search(str(row.get('league') or '')):
        return 'F'
    if any(WOMEN_PATTERN.search(str(row.get(key) or '')) for key in ('home','away')):
        return 'F'
    if row.get('league_id') in WOMENS_LEAGUE_IDS:
        return 'F'
    if row.get('league_id') in {lid for lids in CATALOG_BY_COUNTRY.values() for lid in lids}:
        return 'M'
    return 'UNKNOWN'


def filter_document(data:dict[str,Any],source_bytes:bytes,policy:str='adult-open') -> dict[str,Any]:
    if policy not in VALID_POLICIES:
        raise ValueError(f'Politica no admitida: {policy}')
    if data.get('schema_version')!='1.0-discovery' or not isinstance(data.get('fixtures'),list):
        raise ValueError('Entrada no es fixtures.json V1.0 valido')
    ids:set[int]=set()
    included=[]; excluded=[]; review=[]
    for row in data['fixtures']:
        if not isinstance(row,dict):raise ValueError('Entrada contiene fixture invalido')
        fid=row.get('fixture_id')
        if not isinstance(fid,int) or isinstance(fid,bool) or fid <= 0 or fid in ids:
            raise ValueError('Fixture ID invalido/duplicado')
        ids.add(fid)
        status,reason=classify(row,policy=policy)
        region=region_for(row.get('country') or '')
        explicit=explicit_competition_category(row)
        from competition_registry import category, lookup
        registered_entry=lookup(row)
        registered=category(row)
        comp_category=classify_competition_category(row,registered_entry=registered_entry)
        enriched={**row,'filter_status':status,'filter_reason':reason,
                  'competition_category':comp_category,
                  'gender':explicit['gender'] if explicit else gender_for(row),
                  'region':region,'region_priority':REGION_PRIORITY[region]}
        if explicit:
            enriched.update(explicit)
        enriched.update(registered)
        enriched['matching_eligibility']='EXCLUDED' if status=='EXCLUDED' else 'ELIGIBLE' if status=='ELIGIBLE' else 'PENDING_CLASSIFICATION'
        enriched['tier_status']=reason
        if reason in ('YOUTH_TEAM','RESERVE_TEAM'):
            enriched['participant_category_check']='EXCLUDED_YOUTH_OR_RESERVE_PARTICIPANT'
        elif comp_category=='SENIOR_ELIGIBLE_CUP_OR_INTERNATIONAL' and status=='ELIGIBLE':
            enriched['participant_category_check']='SENIOR_PARTICIPANTS_CHECKED'
        else:
            enriched['participant_category_check']='NO_YOUTH_OR_RESERVE_INDICATORS'
        if status=='ELIGIBLE':included.append(enriched)
        elif status=='REVIEW':review.append(enriched)
        else:excluded.append(enriched)
    policy_tier={'adult-open':'ADULT_OPEN','domestic-top-tier':'DOMESTIC_TOP_TIER','top-tier-and-senior-cups':'TOP_TIER_AND_SENIOR_CUPS'}[policy]
    return {'schema_version':VERSION,'filter_implementation':FILTER_IMPLEMENTATION,
      'generated_at_utc':datetime.now(timezone.utc).isoformat(),
      'source_discovery_generated_at_utc':data.get('generated_at_utc'),
      'source_window':data.get('window'),'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
      'policy':{'competition_tier':policy_tier,
                'age':'ADULT_EVIDENCE_REQUIRED','gender':['M','F','UNKNOWN'],
                'cups':policy in ('adult-open','top-tier-and-senior-cups'),
                'secondary_divisions':policy=='adult-open',
                'unknown_tiers':'REVIEW_NOT_INCLUDED',
                'youth_reserves':'EXCLUDED_WHEN_DETECTABLE',
                'region_priority':REGION_PRIORITY,'bwin_coverage':'NOT_CHECKED'},
      'counts':{'input':len(ids),'eligible':len(included),'review':len(review),'excluded':len(excluded),
                'eligible_competitions':len({(x['country'],x['league_id']) for x in included}),
                'eligible_male':sum(x['gender']=='M' for x in included),
                'eligible_female':sum(x['gender']=='F' for x in included),
                'eligible_gender_unknown':sum(x['gender']=='UNKNOWN' for x in included),
                'by_competition_category':dict(Counter(x['competition_category'] for x in included+review+excluded))},
      'fixtures':included,'review':review,'excluded':excluded,
      'notes':['Categorías juveniles, reservas y divisiones inferiores excluidas solo con evidencia verificable.',
               'Categorías desconocidas conservadas en review sin promoción automática.',
               'No equivale a disponibilidad en Bwin ni calidad de datos.']}


def save_outputs(result:dict[str,Any],folder:Path):
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'fixtures_major.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    fields=['fixture_id','kickoff_madrid','country','league','league_id','gender','region',
            'home','away','home_id','away_id','filter_status']
    with (folder/'fixtures_major.csv').open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(result['fixtures'])
    # Revisiones y descartes explícitos, auditables.
    (folder/'league_filter_review.json').write_text(json.dumps({
       'schema_version':VERSION,'source_sha256':result['source_sha256'],
       'review':result['review'],'excluded':result['excluded']},indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    lines=['# Competiciones adultas — filtro configurable', '',
     f"- Entrada: **{result['counts']['input']}**",
     f"- Elegibles: **{result['counts']['eligible']}**",
     f"- Revisar: **{result['counts']['review']}**",
     f"- Excluidos explícitos: **{result['counts']['excluded']}**",
     '- Sin evaluación estadística ni verificación Bwin.', '',
     '| Hora (Madrid) | País | Liga | Categoría | Partido | Fixture ID |','|---|---|---|---|---|---|']
    for x in result['fixtures']:
        esc=lambda v:str(v or '').replace('|','\\|')
        lines.append(f"| {esc(x['kickoff_madrid'])} | {esc(x['country'])} | {esc(x['league'])} | {x['gender']} | {esc(x['home'])}–{esc(x['away'])} | `{x['fixture_id']}` |")
    lines+=['','## Competiciones pendientes de catalogación','']
    counts=Counter((x['country'],x['league_id'],x['league']) for x in result['review'])
    for (country,lid,league),n in sorted(counts.items()):lines.append(f'- {country} / {league} (ID {lid}): {n} partido(s)')
    (folder/'fixtures_major.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True,help='fixtures.json generado por discover_fixtures.py')
    p.add_argument('--output-dir',type=Path,help='Por defecto misma carpeta que la entrada')
    p.add_argument('--policy',choices=VALID_POLICIES,
                   default=os.environ.get('PREMATCH_COMPETITION_SCOPE','adult-open'),
                   help='adult-open admite ligas/copas adultas; top-tier-and-senior-cups admite 1a div M/F y copas absolutas; domestic-top-tier solo 1a div nacional')
    a=p.parse_args(argv)
    try:
        data_bytes=a.input.read_bytes();result=filter_document(json.loads(data_bytes),data_bytes,policy=a.policy)
        folder=a.output_dir or a.input.parent
        save_outputs(result,folder)
    except (OSError,ValueError,KeyError,TypeError) as e:
        print(f'ERROR: {e}',file=sys.stderr);return 2
    print(f"Filtro: {result['counts']['input']} -> {result['counts']['eligible']} competiciones candidatas adultas (M/F/UNKNOWN)")
    print(f"Revisión: {result['counts']['review']} | Excluidos: {result['counts']['excluded']}")
    print(f'Archivos: {folder/"fixtures_major.json"}, {folder/"fixtures_major.csv"}, {folder/"fixtures_major.md"}, {folder/"league_filter_review.json"}')
    return 0

if __name__=='__main__':raise SystemExit(main())
