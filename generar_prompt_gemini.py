#!/usr/bin/env python3
"""Genera un mensaje corto para una Gem con instrucciones PREMATCH V5.0 precargadas.

Uso:
 python generar_prompt_gemini.py --selected outputs/matches/<event_id>/selection/<run>/selected_match.json
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
    with selected_path.open(encoding='utf-8') as f:
        data = json.load(f)
    eid = str(data.get('bwin_event_id') or '').strip()
    fixture_id = str(data.get('fixture_id') or '').strip()
    home = str(data.get('home') or '').strip()
    away = str(data.get('away') or '').strip()
    kickoff_raw = data.get('kickoff_madrid')
    if not eid.isdigit() or not fixture_id.isdigit() or not home or not away or not kickoff_raw:
        raise ValueError('Faltan identificadores, equipos o kickoff válidos en selected_match.json')
    kickoff = datetime.fromisoformat(str(kickoff_raw).replace('Z', '+00:00'))
    if kickoff.tzinfo is None or kickoff.utcoffset() is None:
        raise ValueError('kickoff_madrid debe incluir offset UTC explícito')
    kickoff = kickoff.astimezone(MADRID)
    league = str(data.get('league') or '').strip()
    country = str(data.get('country') or data.get('league_country') or '').strip()
    competition = league or 'POR VERIFICAR'
    if country and country.casefold() not in competition.casefold():
        competition = f'{competition} ({country})'
    # Verificar destino en carpeta de evento, sin depender de selección activa global.
    event_dir = selected_path.parent.parent.parent if selected_path.parent.parent.name == 'selection' else None
    if event_dir is None or event_dir.name != eid:
        # Busca un padre llamado <event_id> sin asumir nombres de snapshots.
        event_dir = next((p for p in selected_path.parents if p.name == eid and p.parent.name == 'matches'), None)
    if event_dir is None and output_dir is None:
        raise ValueError('No se pudo deducir el directorio del evento; indica --output-dir')
    target_dir = output_dir.expanduser().resolve() if output_dir else event_dir / 'research' / 'external'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f'COPIAR_GEMINI_{eid}.md'
    content = (f'Utiliza Deep Research y aplica íntegramente las instrucciones permanentes y el Prompt Maestro V5.0.\n\n'
               f'Partido: {home} vs {away}\n'
               f'Competición: {competition}\n'
               f'Fecha/hora: {kickoff:%d/%m/%Y, %H:%M} Europe/Madrid\n'
               f'EVENT_ID_BWIN: {eid}\n'
               f'Fixture API-Football: {fixture_id}\n\n'
               'Realiza la investigación profunda PREMATCH de ambos equipos, sin omitir scouting táctico, entrenadores, roles, duelos, balón parado, meteorología ni escenarios contrapuestos.\n\n'
               'Verifica la identidad y toda la evidencia antes de utilizarla.\n\n'
               f'Genera el informe completo gemini_{eid}.md.\n\n'
               'No generes picks ni pronósticos.\n')
    if target.exists():
        if target.read_text(encoding='utf-8') != content:
            raise FileExistsError(f'Ya existe un mensaje diferente: {target}; no se sobrescribe')
    else:
        temporary = target.with_suffix('.md.tmp')
        try:
            temporary.write_text(content, encoding='utf-8')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
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
