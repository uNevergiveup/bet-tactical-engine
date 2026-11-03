#!/usr/bin/env python3
"""Limpieza conservadora de capturas PREMATCH no seleccionadas.

Por defecto NO modifica archivos. Solo elimina capturas huérfanas cuando existe
una correspondencia Bwin/API verificada Y un resultado final explícito aportado
mediante --results-json. Archiva y verifica cada captura antes de eliminarla.

Uso:
  python prematch_cleanup.py --dry-run
  python prematch_cleanup.py --results-json resultados_finales.json --dry-run
  python prematch_cleanup.py --results-json resultados_finales.json --apply

Formato de resultados_finales.json (datos verificados, NO estimaciones):
{"events": [{"fixture_id": 12345, "status": "FT"}]}

Los códigos FT, AET y PEN deben proceder de una fuente de resultados real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FINAL_STATUSES = {'FT', 'AET', 'PEN'}
EVENT_ID = re.compile(r'^\d+$')


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return None


def date(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def within(path: Path, parent: Path):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def collect_mapping(root: Path):
    """Only exact IDs from market preflight, cross-checked against the ledger."""
    ledger = {}
    for p in (root / 'outputs/discovery').glob('*/prospective_trial/candidate_ledger.json'):
        doc = read_json(p)
        if not isinstance(doc, dict):
            continue
        for item in doc.get('fixtures', []):
            if isinstance(item, dict):
                fid = str(item.get('fixture_id', ''))
                if fid.isdigit():
                    ledger.setdefault(fid, set()).add((str(item.get('kickoff_madrid', '')), str(item.get('home', '')), str(item.get('away', ''))))
    mapping = {}
    for p in (root / 'outputs/discovery').glob('*/prospective_trial/daily_runs/*/preflight/*/market_preflight.json'):
        doc = read_json(p)
        if not isinstance(doc, dict):
            continue
        for item in doc.get('matches', []):
            if not isinstance(item, dict):
                continue
            eid = str(item.get('bwin_event_id', ''))
            fid = str(item.get('fixture_id', ''))
            kick = str(item.get('kickoff_madrid', ''))
            home, away = str(item.get('home', '')), str(item.get('away', ''))
            if not (eid.isdigit() and fid.isdigit() and date(kick)):
                continue
            if (kick, home, away) not in ledger.get(fid, set()):
                continue
            mapping.setdefault(eid, set()).add((fid, kick, home, away))
    return mapping


def finished_fixtures(path: Path | None):
    if path is None:
        return set()
    doc = read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get('events'), list):
        raise ValueError('--results-json debe contener {"events": [...]}')
    statuses = {}
    for item in doc['events']:
        if not isinstance(item, dict) or not str(item.get('fixture_id', '')).isdigit():
            raise ValueError('Cada resultado requiere fixture_id numérico')
        fid = str(item['fixture_id'])
        status = str(item.get('status', '')).upper()
        if fid in statuses and statuses[fid] != status:
            raise ValueError(f'Contradicción en resultado fixture_id={fid}')
        statuses[fid] = status
    return {fid for fid, st in statuses.items() if st in FINAL_STATUSES}


def protected_ids(root: Path):
    protected = set()
    latest = root / 'outputs/latest'
    active = read_json(latest / 'ACTIVE_EVENT.json')
    if isinstance(active, dict) and str(active.get('bwin_event_id', '')).isdigit():
        protected.add(str(active['bwin_event_id']))
    # All selected events remain protected; a previous selected event may have
    # external research or downstream references even when latest changed.
    for p in (root / 'outputs/matches').glob('*/selection/*/selected_match.json'):
        protected.add(p.parents[2].name)
    for p in (root / 'outputs/discovery').glob('*/prospective_trial/handoff_prematch.json'):
        doc = read_json(p)
        if isinstance(doc, dict) and str(doc.get('bwin_event_id', '')).isdigit():
            protected.add(str(doc['bwin_event_id']))
    return protected


def safe_capture_folder(folder: Path):
    """Only the precise two-file shape observed for unselected snapshots."""
    if folder.is_symlink():
        return False
    files = []
    for base, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs + names:
            if (Path(base) / name).is_symlink():
                return False
        for name in names:
            files.append((Path(base) / name).relative_to(folder).as_posix())
    eid = folder.name
    return sorted(files) == sorted([f'inputs/cuotas_bwin_{eid}.txt', f'scraper/cuotas_bwin_{eid}.json'])


def scan(root: Path, results: set[str], retention_hours: int, now: datetime):
    mapping = collect_mapping(root)
    protected = protected_ids(root)
    folder = root / 'outputs/matches'
    rows = []
    if not folder.exists():
        return rows
    for p in sorted(folder.iterdir()):
        if not p.is_dir() or not EVENT_ID.fullmatch(p.name):
            continue
        event = p.name
        entries = mapping.get(event, set())
        detail = next(iter(entries)) if len(entries) == 1 else None
        fixture_id, kickoff, home, away = detail if detail else ('?', '?', '?', '?')
        reason = ''
        if event in protected:
            reason = 'PROTEGIDO_SELECCIONADO_O_HANDOFF'
        elif len(entries) != 1:
            reason = 'PROTEGIDO_IDENTIDAD_AUSENTE_O_AMBIGUA'
        elif not safe_capture_folder(p):
            reason = 'PROTEGIDO_EVIDENCIA_ADICIONAL_O_ESTRUCTURA_DESCONOCIDA'
        elif fixture_id not in results:
            reason = 'PROTEGIDO_SIN_RESULTADO_FINAL_VERIFICADO'
        elif now < date(kickoff).astimezone(timezone.utc) + timedelta(hours=retention_hours):
            reason = 'PROTEGIDO_PERIODO_DE_RETENCION'
        else:
            reason = 'ELEGIBLE_ARCHIVAR_Y_LIMPIAR'
        size = sum(x.stat().st_size for x in p.rglob('*') if x.is_file() and not x.is_symlink())
        rows.append(dict(event_id=event, fixture_id=fixture_id, partido=f'{home} vs {away}',
                         kickoff=kickoff, ruta=str(p), bytes=size, estado=reason))
    return rows


def archive_and_remove(root: Path, row: dict, *, dry_run: bool):
    if dry_run:
        return 'SIMULACION'
    source = root / 'outputs/matches' / row['event_id']
    archive_dir = root / 'outputs/archive/matches'
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f'{row["event_id"]}_{row["fixture_id"]}.zip'
    if target.exists():
        return 'BLOQUEADO_ARCHIVO_YA_EXISTE'
    if not safe_capture_folder(source):
        return 'BLOQUEADO_CONTENIDO_CAMBIO'
    with tempfile.NamedTemporaryFile(prefix='.prematch_', suffix='.zip', dir=archive_dir, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        original = {}
        with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in sorted(source.rglob('*')):
                if p.is_file():
                    relative = p.relative_to(source).as_posix()
                    data = p.read_bytes()
                    original[relative] = sha(data)
                    z.writestr(relative, data)
        with zipfile.ZipFile(temp_path) as z:
            if set(z.namelist()) != set(original):
                raise ValueError('Archivos del ZIP incompletos')
            for name, digest in original.items():
                if sha(z.read(name)) != digest:
                    raise ValueError(f'Checksum distinto: {name}')
        if not safe_capture_folder(source):
            raise ValueError('El origen cambió antes de borrar; no eliminado')
        for name, digest in original.items():
            if sha((source / name).read_bytes()) != digest:
                raise ValueError('El origen cambió antes de borrar; no eliminado')
        temp_path.replace(target)
        shutil.rmtree(source)
        return 'ARCHIVADO_Y_ELIMINADO'
    finally:
        temp_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Predeterminado: no escribe ni borra')
    mode.add_argument('--apply', action='store_true', help='Archiva y elimina solo capturas verificadas')
    parser.add_argument('--results-json', type=Path, help='JSON de estados finales FT/AET/PEN obtenidos de fuente fiable')
    parser.add_argument('--retention-hours', type=int, default=24, help='Margen desde kickoff (mínimo 24, predeterminado 24)')
    parser.add_argument('--yes', action='store_true', help='Confirmación no interactiva para --apply')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.retention_hours < 24:
        parser.error('--retention-hours debe ser >= 24')
    if args.yes and not args.apply:
        parser.error('--yes solo puede utilizarse con --apply')
    try:
        results = finished_fixtures(args.results_json)
        rows = scan(root, results, args.retention_hours, datetime.now(timezone.utc))
    except ValueError as exc:
        parser.error(str(exc))
    print(f'PREMATCH CLEANUP | {"APPLY" if args.apply else "DRY RUN"} | raíz: {root}')
    for r in rows:
        print(f'[{r["estado"]}] Bwin {r["event_id"]} | fixture {r["fixture_id"]} | {r["partido"]} | kickoff {r["kickoff"]} | {r["bytes"]} bytes')
    candidates = [r for r in rows if r['estado'] == 'ELEGIBLE_ARCHIVAR_Y_LIMPIAR']
    print(f'Eventos: {len(rows)} | candidatos a archivar y eliminar: {len(candidates)}')
    if not args.apply:
        print('Sin cambios. La limpieza real requiere --results-json, --apply y confirmación.')
        return 0
    if not candidates:
        print('Sin candidatos. No se modificó ningún evento.')
        return 0
    if not args.yes:
        try:
            response = input(f'Para archivar y eliminar {len(candidates)} capturas escribe ELIMINAR: ')
        except EOFError:
            response = ''
        if response != 'ELIMINAR':
            print('Cancelado.')
            return 1
    log_dir = root / 'outputs/cleanup'
    log_dir.mkdir(parents=True, exist_ok=True)
    log = []
    failures = 0
    for row in candidates:
        # Re-evaluate to protect against changes during the command.
        current = {r['event_id']: r for r in scan(root, results, args.retention_hours, datetime.now(timezone.utc))}.get(row['event_id'])
        if not current or current['estado'] != 'ELEGIBLE_ARCHIVAR_Y_LIMPIAR':
            outcome = 'BLOQUEADO_ESTADO_CAMBIO'
        else:
            try:
                outcome = archive_and_remove(root, current, dry_run=False)
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                outcome = f'ERROR: {exc}'
        failures += int(outcome != 'ARCHIVADO_Y_ELIMINADO')
        print(f'{row["event_id"]}: {outcome}')
        log.append({'event_id': row['event_id'], 'fixture_id': row['fixture_id'], 'result': outcome})
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    log_path = log_dir / f'cleanup_{stamp}.json'
    log_path.write_text(json.dumps({'generated_utc': datetime.now(timezone.utc).isoformat(), 'operations': log}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Registro:', log_path)
    return 2 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
