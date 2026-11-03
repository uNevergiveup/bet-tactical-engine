#!/usr/bin/env python3
"""Un comando diario hasta la investigación manual PREMATCH; nunca ejecuta finish."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import shutil
import sys
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import prematch_prospective_trial as trial
from prematch_runtime import reusable_preparation, scan, stamp, verified_preflight


def automatic_day(now, start, end):
    return trial.automatic_day(now,start,end)


def final_odds_check(doc, now):
    kickoff=trial.dt(doc.get('kickoff_madrid'));capture=trial.dt(doc.get('capture_madrid'))
    refresh_before=doc.get('final_odds_policy',{}).get('capture_within_kickoff_minutes',60)
    age_limit=doc.get('final_odds_policy',{}).get('max_age_minutes',15)
    if not kickoff or not capture or not 0 < kickoff.timestamp()-now.timestamp() <= refresh_before*60:
        raise trial.TrialError('FINAL_ODDS_REFRESH_REQUIRED: revisión final solo cerca del kickoff')
    if capture.timestamp() < kickoff.timestamp()-refresh_before*60 or not 0 <= now.timestamp()-capture.timestamp() <= age_limit*60:
        raise trial.TrialError('FINAL_ODDS_REFRESH_REQUIRED: recapturar y revalidar cuotas antes del análisis final')


def review(path, root=None, check_final_odds=False):
    doc = trial.read(path)
    now = trial.now_madrid()
    kickoff = trial.dt(doc.get('kickoff_madrid'))
    capture = trial.dt(doc.get('capture_madrid'))
    if doc.get('status') != 'READY_FOR_MANUAL_RESEARCH':
        raise trial.TrialError(doc.get('status', 'HANDOFF_INVALID'))
    if doc.get('selection_tier'):
        rank=next((r for r in doc.get('tactical_selection_ranking',[])
                   if str(r.get('fixture_id'))==str(doc.get('fixture_id'))),None)
        if (not rank or rank.get('selection_tier')!=doc['selection_tier']
                or not rank.get('research_eligible') or rank.get('hard_block_reasons')
                or rank.get('research_missing_evidence')!=doc.get('research_missing_evidence')
                or bool(rank.get('eligible'))!=bool(doc.get('eligible'))):
            raise trial.TrialError('HANDOFF_SELECTION_TIER_CONFLICT')
    if not kickoff or (kickoff-now).total_seconds() < doc.get('minimum_lead_minutes',45)*60:
        raise trial.TrialError('HANDOFF_EXPIRED: partido iniciado o sin margen PREMATCH')
    if not capture or not timedelta(0) <= now-capture <= timedelta(minutes=doc.get('max_odds_age_minutes',120)):
        raise trial.TrialError('HANDOFF_EXPIRED: snapshot caducado')
    if trial.sha(Path(doc['odds_txt'])) != doc['odds_sha256']:
        raise trial.TrialError('HANDOFF_SNAPSHOT_SHA_MISMATCH')
    if trial.sha(Path(doc['context_paths'][0])) != doc['context_sha256']:
        raise trial.TrialError('HANDOFF_CONTEXT_SHA_MISMATCH')
    context = trial.read(Path(doc['context_paths'][0]))
    fixture = context['fixture']
    if str(fixture['fixture_id']) != str(doc['fixture_id']):
        raise trial.TrialError('HANDOFF_FIXTURE_IDENTITY_MISMATCH')
    project = root or Path(__file__).resolve().parent
    pf = trial.read(Path(doc['preflight']))
    verified = verified_preflight(project, Path(doc['preflight']), fixture,
                Path(pf['source_preselection']), SimpleNamespace(min_lead=doc['minimum_lead_minutes'],
                                                               max_odds_age=doc['max_odds_age_minutes']))
    if any(str(verified[k]) != str(doc[k]) for k in ('bwin_event_id','bwin_url','home','away','kickoff_madrid')):
        raise trial.TrialError('HANDOFF_EVENT_IDENTITY_MISMATCH')
    if check_final_odds:
        final_odds_check(doc,now)
    doc['actionable_bet'] = False
    doc['ready_to_bet'] = False
    doc['requires_pre_kickoff_odds_refresh'] = True
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',type=trial.parse_clock)
    parser.add_argument('--end',type=trial.parse_clock)
    parser.add_argument('--evaluation-budget',type=int,default=0,help='0=todos; límite explícito con filas pendientes')
    parser.add_argument('--discovery-budget',type=int,default=0,help='0=todos; límite explícito con filas pendientes')
    parser.add_argument('--date', help='Forzar fecha inicial Madrid: YYYY-MM-DD, hoy o mañana')
    parser.add_argument('--horizon-hours',type=float,default=3,help='Próximas N horas; 0 permite preparar toda la ventana con antelación')
    parser.add_argument('--bwin-top',type=int,default=5,help='Tamaño de lote Bwin; sin recorte global')
    parser.add_argument('--check-final-odds',action='store_true',help='Con --review: exigir una captura cercana al kickoff y fresca')
    parser.add_argument('--window', choices=['normal','ampliada'])
    parser.add_argument('--dry-run', '--show-config', dest='dry_run', action='store_true')
    parser.add_argument('--next-window', action='store_true', help='Preparar la siguiente franja si la actual ya no tiene candidatos útiles')
    parser.add_argument('--review', type=Path, help='Revalidar el handoff antes de continuar manualmente')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--max-matches', type=int, default=5,help='Tamaño del lote de evaluación API; todos los candidatos del horizonte se evalúan')
    parser.add_argument('--max-scrapes', type=int, default=0, help='Presupuesto de capturas verificadas; 0 = todos los candidatos del horizonte')
    parser.add_argument('--refresh-existing-odds',action='store_true',help='Autoriza recaptura con archivo SHA de snapshots caducados del mismo evento')
    parser.add_argument('--discovery-retries', type=int, default=2)
    parser.add_argument('--min-lead', type=int, default=45)
    parser.add_argument('--max-odds-age', type=int, default=120)
    args = parser.parse_args(argv)
    if args.evaluation_budget<0 or args.discovery_budget<0 or not __import__("math").isfinite(args.horizon_hours) or args.horizon_hours<0 or args.bwin_top<1 or args.max_scrapes<0 or min(args.max_matches,args.max_odds_age)<1 or args.min_lead<45 or args.discovery_retries<0:
        parser.error('Límites inválidos; margen mínimo 45 minutos')
    if args.date and args.next_window:
        parser.error('--date y --next-window son alternativas')
    if args.check_final_odds and not args.review:
        parser.error('--check-final-odds requiere --review')
    try:
        if args.review:
            return review(args.review, args.root.expanduser().resolve(),args.check_final_odds)
        root = args.root.expanduser().resolve()
        config, _, _ = trial.load_window_config(root)
        profile, args.start, args.end = trial.resolve_window(config, args.window,args.start,args.end)
        day = trial.parse_date_relative(args.date) if args.date else automatic_day(trial.now_madrid(), args.start, args.end)
        if args.next_window:
            day += timedelta(days=1)
        folder = trial.window_path(root, day, args.start, args.end)
        begin, finish = trial.local_window(day, args.start, args.end)
        print(f'PERFIL {profile}: {begin.isoformat()} → {finish.isoformat()} | HORIZONTE {args.horizon_hours} h (0=toda la ventana)\nSALIDA {folder}', flush=True)
        if args.dry_run:
            return 0
        args.team=[]; args.history=20; args.details=8
        # This lock is shared with trial scan and remains as an inert file after closing.
        lock_path = root / 'outputs/.prematch_daily.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise trial.TrialError('PREMATCH_ALREADY_RUNNING') from None
            try:
                reusable_preparation(root, folder, day, args)
                print('Preparación existente íntegra: reutilizada sin consultas API.', flush=True)
            except (trial.TrialError, OSError, ValueError, KeyError) as exc:
                print(f'Preparación necesaria: {exc}', flush=True)
                try:
                    transcript = folder / 'prospective_trial/logs' / f'daily_prepare_{stamp()}.log'
                    transcript.parent.mkdir(parents=True, exist_ok=True)
                    print(f'Preparando API, patrones y Bwin; registro: {transcript}', flush=True)
                    with transcript.open('w', encoding='utf-8') as output, contextlib.redirect_stdout(output):
                        trial.prepare(root, folder, day, args)
                except trial.TrialError as exc:
                    # An empty candidate set is a normal, explicit terminal state.
                    status = 'NO_VALID_CANDIDATE' if str(exc).startswith('No hay partidos futuros') else 'INSUFFICIENT_EVIDENCE'
                    path = folder / 'prospective_trial/handoff_prematch.json'
                    trial.dump(path, {'schema_version':'1.0-prematch-handoff','status':status,
                               'generated_madrid':trial.now_madrid().isoformat(), 'actionable_bet':False,
                               'discard_reasons':[{'phase':'prepare','reason':str(exc)}]})
                    print(f'{status}: {path}', flush=True)
                    return 0 if status=='NO_VALID_CANDIDATE' else 2
            transcript = folder / 'prospective_trial/logs' / f'daily_scan_{stamp()}.log'
            transcript.parent.mkdir(parents=True, exist_ok=True)
            print(f'Verificando capturas y scanner; registro: {transcript}', flush=True)
            with transcript.open('w', encoding='utf-8') as output, contextlib.redirect_stdout(output):
                doc = scan(root, folder, day, args)
            from prematch_daily_csv import export_daily
            rows=export_daily(folder/'prospective_trial')
            print(f"{doc['status']}: {folder / 'prospective_trial/handoff_prematch.json'}", flush=True)
            print(f'CSV: {folder / "prospective_trial/partidos_diarios.csv"} | filas={len(rows)}',flush=True)
            return 0 if doc['status'] in ('READY_FOR_MANUAL_RESEARCH','NO_VALID_CANDIDATE') else 2
    except (trial.TrialError, OSError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    finally:
        if 'folder' in locals() and not args.dry_run:
            from prematch_daily_csv import export_daily
            export_daily(folder/'prospective_trial')


if __name__ == '__main__':
    raise SystemExit(main())
