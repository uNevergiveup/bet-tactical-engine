#!/usr/bin/env python3
"""Genera COPIAR_GROK_<EVENT_ID>.md a partir de selected_match.json.

python generar_prompt_grok.py --selected outputs/matches/<event_id>/selection/<run>/selected_match.json
"""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MADRID = ZoneInfo('Europe/Madrid')


def generar(selected_path: Path, output_dir: Path | None = None) -> Path:
    from prematch_paths import explicit_selection
    selected_path = explicit_selection(selected_path)
    data = json.loads(selected_path.read_text(encoding='utf-8'))
    eid = str(data.get('bwin_event_id') or '').strip()
    fid = str(data.get('fixture_id') or '').strip()
    home = str(data.get('home') or '').strip()
    away = str(data.get('away') or '').strip()
    if not eid.isdigit() or not fid.isdigit() or not home or not away:
        raise ValueError('Falta identidad verificada de Bwin/API/equipos')
    kickoff = datetime.fromisoformat(str(data.get('kickoff_madrid') or '').replace('Z', '+00:00'))
    if kickoff.tzinfo is None or kickoff.utcoffset() is None:
        raise ValueError('Kickoff requiere offset de UTC explícito')
    kickoff = kickoff.astimezone(MADRID)
    competition = str(data.get('league') or 'COMPETICIÓN POR VERIFICAR').strip()
    country = str(data.get('country') or data.get('league_country') or '').strip()
    if country and country.casefold() not in competition.casefold():
        competition += f' ({country})'
    event_dir = next((p for p in selected_path.parents if p.name == eid and p.parent.name == 'matches'), None)
    if event_dir is None and output_dir is None:
        raise ValueError('No se encontró carpeta de evento. Usa --output-dir')
    out = Path(output_dir).expanduser().resolve() if output_dir else event_dir / 'research' / 'external'
    out.mkdir(parents=True, exist_ok=True)
    target = out / f'COPIAR_GROK_{eid}.md'
    content = (
        'Utiliza Grok Expert y aplica íntegramente las instrucciones permanentes y el Prompt Maestro Grok PREMATCH V5.0.\n\n'
        f'Partido: {home} vs {away}\n'
        f'Competición: {competition}\n'
        f'Fecha/hora: {kickoff:%d/%m/%Y, %H:%M} Europe/Madrid ({kickoff:%z})\n'
        f'EVENT_ID_BWIN: {eid}\n'
        f'Fixture API-Football: {fid}\n\n'
        'Investiga exhaustivamente la actualidad PREMATCH: noticias y X de ambos equipos, prensa local, comunicados oficiales, convocatorias, bajas, XI y banquillo, declaraciones, ajustes tácticos recientes, roles, duelos, balón parado, árbitro y VAR.\n\n'
        'Investiga obligatoriamente la meteorología horaria en el estadio (viento, rachas, dirección, lluvia, temperatura, humedad, alertas y césped) y las posibles adaptaciones de ambos entrenadores, separando hechos de inferencias.\n\n'
        'Verifica las fuentes originales, la fecha de publicación, los posts individuales y las contradicciones. Excluye toda información posterior al corte PREMATCH y prepara un registro de evidencias contrastable con Gemini por Argon.\n\n'
        f'Genera el informe completo grok_{eid}.md.\n\n'
        'No generes picks, cuotas, probabilidades, EV ni pronósticos.\n'
    )
    if target.exists():
        if target.read_text(encoding='utf-8') != content:
            raise FileExistsError(f'Ya existe un archivo distinto; no se sobrescribe: {target}')
    else:
        temp = target.with_name(target.name + '.tmp')
        try:
            temp.write_text(content, encoding='utf-8')
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selected', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    try:
        print(generar(args.selected, args.output_dir))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f'ERROR: {exc}\n')


if __name__ == '__main__':
    main()
