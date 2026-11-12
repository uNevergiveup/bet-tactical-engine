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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = '1.0-major-leagues'  # Esquema compatible con preselect_matches y trial
FILTER_IMPLEMENTATION = '2.0-adult-open'
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


# Etiquetas inequivocas: no bloquear "Boca Juniors", "Young Boys", etc.
YOUTH_PATTERN = re.compile(
    r"(?i)(?:\bu\s*[- ]?\s*(?:1[0-9]|2[0-3])\b|"
    r"\bunder\s*[- ]?\s*(?:1[0-9]|2[0-3])\b|"
    r"\bsub\s*[- ]?\s*(?:1[0-9]|2[0-3])\b|"
    r"\b(?:youth|juvenil(?:es)?|juniores|primavera|academy|academia)\b)"
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


def classify(row: dict[str,Any], policy: str='adult-open') -> tuple[str,str]:
    """Decision conservadora: adultos por defecto, duda explicita en REVIEW."""
    if policy not in ('adult-open', 'domestic-top-tier'):
        raise ValueError(f'Politica no admitida: {policy}')
    if row.get('status') != 'NS':
        return 'EXCLUDED', 'NOT_PREMATCH'
    country = str(row.get('country') or '')
    league = str(row.get('league') or '')
    lid = row.get('league_id')
    if not isinstance(lid,int) or isinstance(lid,bool) or lid <= 0 or not league:
        return 'REVIEW','MISSING_LEAGUE_ID_OR_NAME'
    if policy == 'domestic-top-tier':
        if not country:
            return 'REVIEW','MISSING_LEAGUE_ID_OR_NAME'
        if HARD_STOP.search(league):
            return 'EXCLUDED','YOUTH_RESERVES_OR_CUP'
        if lid in KNOWN_EXCLUDED_IDS:
            return 'EXCLUDED','KNOWN_NON_TOP_TIER_OR_CUP'
        if lid not in CATALOG_BY_COUNTRY.get(country,()):
            return 'REVIEW','NOT_IN_CURATED_TOP_TIER_CATALOG'
        return 'ELIGIBLE','CURATED_DOMESTIC_TOP_TIER'
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
    # No hay un campo universal de categoria de edad. La ausencia de indicios
    # no demuestra la adultez; conservar esa limitacion en la politica.
    return 'ELIGIBLE','NO_YOUTH_OR_RESERVE_SIGNALS'


def gender_for(row:dict[str,Any]) -> str:
    """No inventar masculino si API-Football no publica el genero."""
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
    if policy not in ('adult-open','domestic-top-tier'):
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
        enriched={**row,'filter_status':status,'filter_reason':reason,
                  'gender':gender_for(row),
                  'region':region,'region_priority':REGION_PRIORITY[region]}
        if status=='ELIGIBLE':included.append(enriched)
        elif status=='REVIEW':review.append(enriched)
        else:excluded.append(enriched)
    return {'schema_version':VERSION,'filter_implementation':FILTER_IMPLEMENTATION,
      'generated_at_utc':datetime.now(timezone.utc).isoformat(),
      'source_discovery_generated_at_utc':data.get('generated_at_utc'),
      'source_window':data.get('window'),'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
      'policy':{'competition_tier':'ADULT_OPEN' if policy=='adult-open' else 'DOMESTIC_TOP_TIER',
                'age':'ADULT_HEURISTIC','gender':['M','F','UNKNOWN'],
                'cups':policy=='adult-open','secondary_divisions':policy=='adult-open',
                'unknown_tiers':'INCLUDE_IF_NO_YOUTH_SIGNALS' if policy=='adult-open' else 'REVIEW_NOT_INCLUDED',
                'youth_reserves':'EXCLUDED_WHEN_DETECTABLE',
                'region_priority':REGION_PRIORITY,'bwin_coverage':'NOT_CHECKED'},
      'counts':{'input':len(ids),'eligible':len(included),'review':len(review),'excluded':len(excluded),
                'eligible_competitions':len({(x['country'],x['league_id']) for x in included}),
                'eligible_male':sum(x['gender']=='M' for x in included),
                'eligible_female':sum(x['gender']=='F' for x in included),
                'eligible_gender_unknown':sum(x['gender']=='UNKNOWN' for x in included)},
      'fixtures':included,'review':review,'excluded':excluded,
      'notes':['Adulto inferido por ausencia de indicadores juveniles; NO garantia de categoria.',
               'No equivale a disponibilidad en Bwin ni calidad de datos.',
               'Los casos ambiguos requieren revision humana o catalogo de categorias.']}


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
    p.add_argument('--policy',choices=('adult-open','domestic-top-tier'),
                   default=os.environ.get('PREMATCH_COMPETITION_SCOPE','adult-open'),
                   help='adult-open admite ligas/copas internacionales; domestic-top-tier restaura filtro historico')
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
