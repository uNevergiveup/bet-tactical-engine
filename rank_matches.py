#!/usr/bin/env python3
"""Selector PREMATCH V1.3 — ranking offline y plan de ampliación de evidencias.

NO consulta APIs, cuotas, ni genera picks. Diferencia partidas detalladas y pendientes.
Ejemplo:
 python rank_matches.py --input outputs/discovery/.../adaptive30/preselection.json \
   --next-detail 24 --output-dir outputs/discovery/.../ranking13
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = '1.3-slate-ranking'
REGIONS = ('EUROPE', 'SOUTH_AMERICA', 'NORTH_CENTRAL_AMERICA', 'AFRICA', 'ASIA', 'OTHER')
REGION_BONUS = {'EUROPE': 8, 'SOUTH_AMERICA': 8, 'NORTH_CENTRAL_AMERICA': 8,
                'AFRICA': 2, 'ASIA': 2, 'OTHER': 1}
REGION_LABEL = {'EUROPE':'Europa','SOUTH_AMERICA':'Sudamérica','NORTH_CENTRAL_AMERICA':'Norte/Centroamérica',
                'AFRICA':'África','ASIA':'Asia','OTHER':'Otra'}
# Preferencias OPERATIVAS de prospección: no probabilidad ni supuesta calidad intrínseca.
ALLOCATION = {'EUROPE': 0.43, 'SOUTH_AMERICA': 0.24,
              'NORTH_CENTRAL_AMERICA': 0.24, 'AFRICA': 0.06, 'ASIA': 0.03}
PROFILES = ('corners', 'shots', 'on_target', 'fouls', 'offsides', 'yellow', 'xg')
# Lista de competiciones PREFERIDAS operacionalmente por el usuario (no garantiza
# cuota Bwin ni calidad superior). IDs comprobados contra fixtures_major del slate.
PREFERRED_LEAGUE_IDS = {
    # Europa: grandes y primeras divisiones con amplia visibilidad
    39, 61, 78, 88, 94, 135, 140, 103, 113, 119, 144, 197, 203,
    139, 142, 91,
    # América: ligas principales, masculinas y femeninas
    128, 239, 242, 252, 265, 268, 281, 299, 344, 1229,
    253, 254, 262, 673, 479, 162, 304,
}
PREFERRED_LEAGUE_BONUS = 20.0


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def parse_date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f'Fecha inválida: {value}')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Fecha sin zona horaria explícita')
    return dt.astimezone(timezone.utc)


def basic_signal(m: dict[str, Any]) -> tuple[float, list[str]]:
    """Orden de investigación con información básica; NO es predicción de juego.

    Compara discrepancias descriptivas de goles entre producción y concesión en liga,
    con un término acotado para contrastes de clasificación; ninguno infiere EV.
    """
    pre = number(m.get('preliminary_coverage_score')) or 0.
    reg = m.get('region') or 'OTHER'
    home = (m.get('home_evidence') or {})
    away = (m.get('away_evidence') or {})
    hh, ah = home.get('history') or {}, away.get('history') or {}
    reasons = []
    ns = [hh.get('same_competition_total') or 0, ah.get('same_competition_total') or 0]
    recent = min(ns)
    if recent >= 14:
        reasons.append(f'Historial en la misma liga: {ns[0]}/{ns[1]}')
    elif recent < 6:
        reasons.append(f'Comparabilidad limitada en liga: {ns[0]}/{ns[1]}')
    mean = lambda x, k: number((x.get(k) or {}).get('mean'))
    x = mean(hh, 'same_competition_goals_for')
    y = mean(ah, 'same_competition_goals_against')
    z = mean(ah, 'same_competition_goals_for')
    t = mean(hh, 'same_competition_goals_against')
    contrast = 0.
    if all(q is not None for q in (x, y, z, t)) and recent >= 6:
        contrast = min(10., 4.0 * (abs(x-y) + abs(z-t)))
        if abs(x-y) + abs(z-t) >= 1.1:
            reasons.append('Contraste descriptivo de goles por equipo (por estudiar)')
    tables = [home.get('standings') or {}, away.get('standings') or {}]
    if all(v.get('status') == 'AVAILABLE' for v in tables):
        reasons.append('Clasificación disponible')
    # La posición no se transforma en "obligación" ni en pronóstico.
    h2h = (m.get('h2h') or {}).get('count') or 0
    if h2h:
        reasons.append(f'H2H reciente: {h2h} partido(s), no exhaustivo')
    bonus = REGION_BONUS.get(reg, 1)
    score = 0.78 * pre + bonus + contrast + min(2., h2h * 0.65) + (PREFERRED_LEAGUE_BONUS if m.get('league_id') in PREFERRED_LEAGUE_IDS else 0.)
    if m.get('league_id') in PREFERRED_LEAGUE_IDS:
        reasons.append('Competición de prioridad operativa (no cobertura Bwin confirmada)')
    return round(score, 2), reasons


def evidence_rank_key(m: dict[str, Any]):
    return (-(number(m.get('research_priority_score')) or -1),
            -(number(m.get('data_readiness_score')) or -1),
            -(number(m.get('stability_score')) or -1),
            int(m.get('region_priority') or 3),
            m.get('kickoff_madrid') or '', m.get('fixture_id') or 0)


def pending_rank_key(m: dict[str, Any]):
    return (-m['_planning_score'],
            -(number(m.get('preliminary_coverage_score')) or 0),
            m.get('kickoff_madrid') or '', m.get('fixture_id') or 0)


def region_plan(top: int) -> dict[str, int]:
    result = {k: int(top * w) for k, w in ALLOCATION.items()}
    available = top - sum(result.values())
    for k, _ in sorted(ALLOCATION.items(), key=lambda item: -(top*item[1] % 1))[:available]:
        result[k] += 1
    return result


def choose_expansion(pending: list[dict[str, Any]], top: int,
                     max_per_league: int) -> list[dict[str, Any]]:
    if top <= 0:
        return []
    ordered = sorted(pending, key=pending_rank_key)
    plan = region_plan(top)
    selected: list[dict[str, Any]] = []
    chosen = set()
    leagues: Counter[tuple] = Counter()
    def take(m: dict[str, Any]) -> bool:
        fid = m['fixture_id']
        key = (m.get('league_id'), m.get('season'))
        if fid in chosen or leagues[key] >= max_per_league:
            return False
        selected.append(m)
        chosen.add(fid)
        leagues[key] += 1
        return True
    # Primera pasada: diversificar regiones en proporciones configuradas,
    # pero sin obligar a usar partidos sin historial básico suficiente.
    for region, amount in plan.items():
        if not amount:
            continue
        taken = 0
        for m in ordered:
            if taken >= amount:
                break
            if m.get('region') != region:
                continue
            if (m.get('initial_data_gate') or '') not in ('APTO_SEGUN_DATOS_BASICOS', 'REVISION_COBERTURA_BASICA'):
                continue
            if take(m):
                taken += 1
    # Liberar cupos en regiones sin candidatos, priorizando señales y no ranking por fama.
    for m in ordered:
        if len(selected) >= top:
            break
        if m.get('initial_data_gate') not in ('APTO_SEGUN_DATOS_BASICOS', 'REVISION_COBERTURA_BASICA'):
            continue
        take(m)
    # El número puede ser menor al solicitado si no hay suficientes aptos.
    return sorted(selected, key=pending_rank_key)


def market_overview(m: dict[str, Any]) -> tuple[str, str]:
    p = m.get('market_profiles') or {}
    usable = []
    weak = []
    for key in PROFILES:
        v = p.get(key) or {}
        if v.get('coverage_class') == 'SUFICIENTE':
            usable.append(key)
        elif v.get('coverage_class') in ('ESCASA', 'PARCIAL'):
            weak.append(key)
    return ', '.join(usable) or 'ninguno', ', '.join(weak) or 'ninguno'


def rank_report(input_report: dict[str, Any], next_detail: int = 24,
                max_per_league: int = 2,
                now: datetime | None = None,
                exclude_started: bool = True) -> dict[str, Any]:
    if not str(input_report.get('schema_version', '')).endswith('-preselection'):
        raise ValueError('Se requiere preselection.json V1.1/V1.2/V1.3')
    if max_per_league < 1 or next_detail < 0:
        raise ValueError('next-detail >= 0 y max-per-league >= 1')
    matches = input_report.get('matches')
    if not isinstance(matches, list):
        raise ValueError('Falta matches[]')
    source_dt = parse_date(input_report['generated_at_utc'])
    when = now or source_dt
    eligible = []
    started = []
    for original in matches:
        m = dict(original)
        kickoff = parse_date(m['kickoff_madrid'])
        if kickoff <= when:
            started.append(m)
            if exclude_started:
                continue
        score, why = basic_signal(m)
        m['_planning_score'] = score
        m['_planning_reasons'] = why
        eligible.append(m)
    detailed = sorted([m for m in eligible if m.get('details_checked')], key=evidence_rank_key)
    # No se mezclan 100/100 de cobertura con preliminares sin detalles.
    validated = [m for m in detailed if m.get('research_priority_score') is not None
                 and m.get('data_readiness_status') == 'DATOS_APROVECHABLES']
    detail_limited = [m for m in detailed if m not in validated]
    pending = [m for m in eligible if not m.get('details_checked')]
    pending = sorted(pending, key=pending_rank_key)
    next_matches = choose_expansion(pending, next_detail, max_per_league)
    return {
        'schema_version': VERSION,
        'source_schema_version': input_report['schema_version'],
        'source_generated_at_utc': input_report['generated_at_utc'],
        'evaluation_as_of_utc': when.isoformat(),
        'methodology': {
            'scope': 'SLATE_SCREENING_NO_ODDS_NO_PICKS',
            'detailed_and_pending_ranking_separated': True,
            'next_detail_buckets': ALLOCATION,
            'max_per_league': max_per_league,
            'planning_score': '0.78*preliminary_coverage + region_operation_bonus + bounded_goals_contrast + recent_h2h + preferred_league_operational_bonus',
            'preferred_league_ids': sorted(PREFERRED_LEAGUE_IDS),
            'not_probabilities_or_expected_value': True,
            'bwin_event_ids': 'NOT_VERIFIED',
            'source_standings_temporal_status': 'CURRENT_SNAPSHOT_NOT_HISTORICALLY_POINT_IN_TIME_VERIFIED',
        },
        'counts': {'input': len(matches), 'eligible_at_cutoff': len(eligible),
                   'started_by_cutoff': len(started), 'detailed': len(detailed),
                   'detailed_usable': len(validated), 'detailed_limited': len(detail_limited),
                   'pending': len(pending), 'next_detail_selected': len(next_matches)},
        'evaluated_research_ranking': [compact(m) for m in validated],
        'detailed_for_review': [compact(m) for m in detail_limited],
        'pending_ranked': [compact(m) for m in pending],
        'next_detail_plan': [compact(m) for m in next_matches],
    }


def compact(m: dict[str, Any]) -> dict[str, Any]:
    usable, weak = market_overview(m)
    return {key: m.get(key) for key in ('fixture_id', 'kickoff_madrid', 'country', 'region',
       'league', 'league_id', 'season', 'home', 'away', 'gender', 'details_checked',
       'initial_data_gate', 'preliminary_coverage_score', 'data_readiness_score',
       'data_readiness_status', 'stability_score', 'research_priority_score',
       'bwin_market_availability')} | {
       'next_detail_planning_score': m.get('_planning_score'),
       'planning_reasons': m.get('_planning_reasons', []),
       'usable_statistical_families': usable,
       'limited_statistical_families': weak,
       'market_profiles': m.get('market_profiles') or {},
       'standings_local': (m.get('home_evidence') or {}).get('standings'),
       'standings_visitante': (m.get('away_evidence') or {}).get('standings'),
       'h2h_status': (m.get('h2h') or {}).get('status'),
       'h2h_count': (m.get('h2h') or {}).get('count'),
       'warnings': m.get('warnings', []),
    }


def save_report(report: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output/'slate_ranking.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if report.get('manual_selection'):
        (output/'selected_fixture.json').write_text(json.dumps(report['manual_selection'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'next_detail_ids.txt').write_text(
        ''.join(f'{m["fixture_id"]}\n' for m in report['next_detail_plan']), encoding='utf-8')
    tables = [('evaluated', report['evaluated_research_ranking']),
              ('review', report['detailed_for_review']),
              ('pending', report['pending_ranked']),
              ('next_detail', report['next_detail_plan'])]
    with (output/'slate_ranking.csv').open('w', newline='', encoding='utf-8-sig') as fp:
        keys = ['group', 'fixture_id', 'kickoff_madrid', 'country', 'region', 'league', 'home', 'away',
                'preliminary_coverage_score', 'data_readiness_score', 'stability_score',
                'research_priority_score', 'next_detail_planning_score', 'data_readiness_status',
                'usable_statistical_families', 'limited_statistical_families', 'bwin_market_availability']
        w = csv.DictWriter(fp, fieldnames=keys); w.writeheader()
        for group, rows in tables:
            for m in rows:
                w.writerow({'group':group, **{k:m.get(k) for k in keys[1:]}})
    with (output/'bwin_verification.csv').open('w', newline='', encoding='utf-8-sig') as fp:
        keys = ['fixture_id','kickoff_madrid','country','league','home','away',
                'bwin_event_id','bwin_url','identity_match','offered_market_count','notes']
        w = csv.DictWriter(fp,fieldnames=keys);w.writeheader()
        for m in report['evaluated_research_ranking'][:15]:
            w.writerow({k:m.get(k,'') for k in keys})
    c = report['counts']
    lines = ['# Auditoría y siguiente fase — selector PREMATCH V1.3', '',
             f"Fuente: V{report['source_schema_version']} | Corte: {report['evaluation_as_of_utc']}", '',
             f"- Entrada: **{c['input']}**; elegibles: **{c['eligible_at_cutoff']}**", 
             f"- Detalle: **{c['detailed']}**, de los cuales aprovechables: **{c['detailed_usable']}**", 
             f"- Revisión: **{c['detailed_limited']}**; pendientes: **{c['pending']}**",
             f"- Próxima tanda propuesta: **{c['next_detail_selected']}**", '',
             'No se predicen resultados, no se verifican cuotas Bwin y no se equiparan pendientes con malos partidos.', '',
             '## Ranking de encuentros YA detallados (investigación; no picks)', '',
             '| # | Hora Madrid | Partido | Liga | Cobertura | Estabilidad | Prioridad | Métricas utilizables |',
             '|---:|---|---|---|---:|---:|---:|---|']
    for i, m in enumerate(report['evaluated_research_ranking'], 1):
        lines.append(f"| {i} | {m['kickoff_madrid'][11:16]} | {m['home']}–{m['away']} | {m['league']} | {m['data_readiness_score']} | {m['stability_score']} | {m['research_priority_score']} | {m['usable_statistical_families']} |")
    lines += ['', '## Partidos detallados con limitaciones', '',
              '| Partido | Liga | Estado | Cobertura | Advertencias |', '|---|---|---|---:|---|']
    for m in report['detailed_for_review']:
        lines.append(f"| {m['home']}–{m['away']} | {m['league']} | {m['data_readiness_status']} | {m['data_readiness_score']} | {'; '.join(m['warnings'][:2])[:110]} |")
    lines += ['', '## Próxima tanda de ampliación — SIN extrapolar cobertura', '',
             '| # | Fixture ID | Hora Madrid | Partido | Competición | Región | Básicos | Planificación |',
             '|---:|---:|---|---|---|---|---:|---:|']
    for i,m in enumerate(report['next_detail_plan'],1):
        lines.append(f"| {i} | `{m['fixture_id']}` | {m['kickoff_madrid'][11:16]} | {m['home']}–{m['away']} | {m['league']} | {REGION_LABEL.get(m['region'],m['region'])} | {m['preliminary_coverage_score']} | {m['next_detail_planning_score']} |")
    lines += ['', 'La columna **Planificación** ordena la consulta de datos, no apuestas ni probabilidad.',
              'Las familias disponibles aún se desconocen para estos partidos. No asignarles cero.', '',
              '## Mercado Bwin y vínculo de identidad', '',
              'Bwin no se ha comprobado. `fixture_id` de API-Football NO es `bwin_event_id`.',
              'Utilizar `bwin_verification.csv` como checklist; completar únicamente tras verificar equipo, competición y kickoff.', '',
              '## Próxima ejecución', '',
              '```bash',
              'python preselect_matches.py \\',
              '  --input outputs/discovery/2026-11-01_1600_0200/fixtures_major.json \\',
              f'  --fixture-ids-file {output / "next_detail_ids.txt"} \\',
              '  --mode full \\',
              '  --merge-report outputs/discovery/2026-11-01_1600_0200/adaptive30/preselection.json \\',
              '  --output-dir outputs/discovery/2026-11-01_1600_0200/enriched_v13',
              '```', '',
              'Requiere el parche `preselect_matches.py` V1.3 incluido en el paquete.']
    (output/'slate_ranking.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--input',type=Path,required=True,help='preselection.json V1.2 o compatible')
    p.add_argument('--output-dir',type=Path,help='Directorio de salida (no sobrescribe la fuente)')
    p.add_argument('--next-detail',type=int,default=24,help='Número de nuevos partidos a consultar')
    p.add_argument('--max-per-league',type=int,default=2,help='Diversificación por liga/temporada')
    p.add_argument('--asof',choices=['source','now'],default='source',help='source usa el cutoff del archivo para reproducibilidad')
    p.add_argument('--choose',type=int,help='Selecciona manualmente un fixture_id, incluso pendiente de detalle')
    args=p.parse_args(argv)
    try:
        report=json.loads(args.input.read_text(encoding='utf-8'))
        ranked=rank_report(report,args.next_detail,args.max_per_league,
                           datetime.now(timezone.utc) if args.asof=='now' else None)
        if args.choose is not None:
            all_matches=ranked['evaluated_research_ranking']+ranked['detailed_for_review']+ranked['pending_ranked']
            hits=[m for m in all_matches if m['fixture_id']==args.choose]
            if len(hits)!=1:
                raise ValueError(f'Fixture {args.choose} no está disponible en este slate al corte seleccionado')
            m=hits[0]
            ranked['manual_selection']={
                'fixture_id':m['fixture_id'],
                'home':m['home'],'away':m['away'],'league':m['league'],
                'league_id':m['league_id'],'season':m['season'],
                'kickoff_madrid':m['kickoff_madrid'],
                'data_status':m['data_readiness_status'],
                'needs_detail':not m['details_checked'],
                'bwin_event_id':None,'bwin_identity_status':'PENDING_VERIFICATION',
                'note':'Identificador API-Football; NO equivale a Event ID Bwin.'}
        output=args.output_dir or args.input.parent/'ranking_v13'
        save_report(ranked,output)
        print(f"Entrada: {ranked['counts']['input']} | Detallados: {ranked['counts']['detailed']} | Aprovechables: {ranked['counts']['detailed_usable']} | Pendientes: {ranked['counts']['pending']}")
        print(f"Próxima tanda: {ranked['counts']['next_detail_selected']} | Resultados: {output / 'slate_ranking.md'}")
        return 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
