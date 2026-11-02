#!/usr/bin/env python3
"""Filtro conservador de primeras divisiones ADULTAS nacionales, M/F.

Consume un fixtures.json de discover_fixtures.py y conserva un nuevo fichero.
No llama a API, no reescribe el descubrimiento ni presupone cuotas Bwin.
Un league_id no incluido en el catálogo queda en REVIEW, nunca aceptado por nombre.

Ejemplo:
 python filter_major_leagues.py --input outputs/discovery/2026-11-01_1600_0200/fixtures.json
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = '1.0-major-leagues'
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


def classify(row: dict[str,Any]) -> tuple[str,str]:
    country = row.get('country') or ''
    league = str(row.get('league') or '')
    lid = row.get('league_id')
    if not isinstance(lid,int) or isinstance(lid,bool) or lid <= 0 or not country or not league:
        return 'REVIEW','MISSING_LEAGUE_ID_OR_NAME'
    if HARD_STOP.search(league):
        return 'EXCLUDED','YOUTH_RESERVES_OR_CUP'
    if lid in KNOWN_EXCLUDED_IDS:
        return 'EXCLUDED','KNOWN_NON_TOP_TIER_OR_CUP'
    if lid not in CATALOG_BY_COUNTRY.get(country,()):
        return 'REVIEW','NOT_IN_CURATED_TOP_TIER_CATALOG'
    if row.get('status') != 'NS':
        return 'EXCLUDED','NOT_PREMATCH'
    return 'ELIGIBLE','CURATED_DOMESTIC_TOP_TIER'


def filter_document(data:dict[str,Any],source_bytes:bytes) -> dict[str,Any]:
    if data.get('schema_version')!='1.0-discovery' or not isinstance(data.get('fixtures'),list):
        raise ValueError('Entrada no es fixtures.json V1.0 válido')
    ids:set[int]=set()
    included=[]; excluded=[]; review=[]
    for row in data['fixtures']:
        if not isinstance(row,dict): raise ValueError('Entrada contiene fixture inválido')
        fid=row.get('fixture_id')
        if not isinstance(fid,int) or fid in ids: raise ValueError('Fixture ID inválido/duplicado')
        ids.add(fid)
        status, reason=classify(row)
        enriched={**row,'filter_status':status,'filter_reason':reason,
                  'gender': 'F' if row['league_id'] in WOMENS_LEAGUE_IDS else ('M' if status=='ELIGIBLE' else 'UNKNOWN'),
                  'region':region_for(row.get('country') or ''),
                  'region_priority':REGION_PRIORITY[region_for(row.get('country') or '')]}
        if status=='ELIGIBLE': included.append(enriched)
        elif status=='REVIEW': review.append(enriched)
        else: excluded.append(enriched)
    return {'schema_version':VERSION,'generated_at_utc':datetime.now(timezone.utc).isoformat(),
      'source_discovery_generated_at_utc':data.get('generated_at_utc'),
      'source_window':data.get('window'), 'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
      'policy':{'competition_tier':'DOMESTIC_TOP_TIER','age':'ADULT','gender':['M','F'],
                'cups':False,'secondary_divisions':False,'unknown_tiers':'REVIEW_NOT_INCLUDED',
                'region_priority':REGION_PRIORITY,'bwin_coverage':'NOT_CHECKED'},
      'counts':{'input':len(ids),'eligible':len(included),'review':len(review),'excluded':len(excluded),
                'eligible_competitions':len({(x['country'],x['league_id']) for x in included}),
                'eligible_male':sum(x['gender']=='M' for x in included),
                'eligible_female':sum(x['gender']=='F' for x in included)},
      'fixtures':included,'review':review,'excluded':excluded,
      'notes':['Aprobación por catálogo ID+país, NO por suposición de nombre de liga.',
               'REVIEW no significa segunda división; requiere clasificar explícitamente.',
               'No presupone estadísticas disponibles, cuotas Bwin ni patrones.']}


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
    lines=['# Primeras divisiones adultas — filtro', '',
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
    a=p.parse_args(argv)
    try:
        data_bytes=a.input.read_bytes();result=filter_document(json.loads(data_bytes),data_bytes)
        folder=a.output_dir or a.input.parent
        save_outputs(result,folder)
    except (OSError,ValueError,KeyError,TypeError) as e:
        print(f'ERROR: {e}',file=sys.stderr);return 2
    print(f"Filtro: {result['counts']['input']} -> {result['counts']['eligible']} primeras divisiones adultas (M/F)")
    print(f"Revisión: {result['counts']['review']} | Excluidos: {result['counts']['excluded']}")
    print(f'Archivos: {folder/"fixtures_major.json"}, {folder/"fixtures_major.csv"}, {folder/"fixtures_major.md"}, {folder/"league_filter_review.json"}')
    return 0

if __name__=='__main__':raise SystemExit(main())
