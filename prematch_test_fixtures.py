"""Synthetic, temporary fixtures. Never evidence for a real PREMATCH selection."""
import json
import shutil
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone


def integral_fixture(root):
    import prematch_integral_analysis_v1046 as engine
    from test_prematch_integral_analysis_v1046 import row, performance
    deep = root / 'outputs/matches/7902458/analysis/deep_v1042'
    deep.mkdir(parents=True)
    cutoff = datetime(2026, 11, 6, 14, tzinfo=timezone.utc)
    teams = {}
    context = {'context': {}}
    for side, tid, venue in [('home', 1, 'HOME'), ('away', 2, 'AWAY')]:
        rows, details = [], []
        for i in range(20):
            at = (cutoff - timedelta(days=i + 1)).isoformat()
            r = row(100 + tid * 100 + i, at, venue, xg=1.0 if i < 5 else .5)
            r.update(goals_for=2 if i < 5 else 1, league_name='La Liga')
            rows.append(r)
            stats = lambda xg: [{'type': 'expected_goals', 'value': xg}, {'type': 'Total Shots', 'value': 4}]
            details.append({'fixture': {'id': r['fixture_id'], 'date': at},
                            'statistics_halves': [{'team': {'id': tid}, 'statistics_1h': stats(.4), 'statistics_2h': stats(.8)}]})
        teams[side] = rows
        context['context'][side + '_history_deep'] = {'detailed_fixtures': details}
    perf = performance(teams['home'], teams['away'])
    perf['target_fixture_id'] = 1570450
    context['context']['injuries'] = {'response': [{'fixture': {'id': 1570450}, 'team': {'id': 1},
        'player': {'id': 7, 'name': 'Synthetic Player', 'reason': 'Synthetic absence', 'type': 'Questionable'}}]}
    players = {'teams': {'home': {'players': [{'player_id': 7, 'name': 'Synthetic Player', 'played_n': 5,
        'metrics': {'shots_total': {'observed_n': 5, 'total': 10, 'eligible_minutes': None, 'per90': 99}}}]}}}
    for name, data in [('rendimiento_profundo', perf), ('contexto_ampliado', context),
                       ('jugadores_historicos', players), ('ajuste_rivales_v1045', {'schema': 'prematch-sos-1.0.4.4', 'teams': {}})]:
        (deep / f'{name}_7902458.json').write_text(json.dumps(data))
    engine.run('7902458', root)
    return root


def restored_selection_fixture(root):
    """Replay the archived quote bound to the historical selection, never today's canonical quote."""
    project = Path(__file__).resolve().parent
    legacy = Path('/Users/antonio/Projects/scraper')
    selected_rel = Path('outputs/phase3/bwin_reuse_integrity/selection/selected_match.json')
    selected = json.loads((project / selected_rel).read_text())
    decision_rel = selected_rel.with_name('selection_decision.json')
    decision = json.loads((project / decision_rel).read_text())
    preflight_rel = Path(selected['preflight_path']).relative_to(legacy)
    preflight = json.loads((project / preflight_rel).read_text())
    files = {selected_rel, decision_rel, preflight_rel}
    files.update(p.relative_to(project) for p in (project / preflight_rel.parent).glob('*.csv'))
    files.update(Path(name) for name in ('scraper_bwin.py', 'generate_context.py', 'config/constants.py'))
    files.update(Path(decision['sources'][key]).relative_to(legacy) for key in ('preselection', 'discovery'))
    files.update(Path(preflight[key]).relative_to(legacy) if Path(preflight[key]).is_absolute() else Path(preflight[key])
                 for key in ('source_discovery', 'source_preselection'))
    for rel in files:
        target = root / rel; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / rel, target)
    archive = project / 'outputs/matches/7903872/archives/force_prepare_20261107T160543068693Z/inputs/cuotas_bwin_7903872.txt'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != selected['odds_sha256']:
        raise AssertionError('ARCHIVED_TEST_FIXTURE_SHA_MISMATCH')
    odds = root / Path(selected['odds_txt']).relative_to(legacy)
    odds.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(archive, odds)
    return root / selected_rel
