"""Reproduce V2.1.3 from packaged frozen inputs, without network."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import prematch_pattern_discovery as discovery

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / 'validation_v213/sources'
NAMES = ('coverage_matrix', 'pattern_discovery', 'research_queue_sport',
         'commercial_link_report', 'selector_order_comparison')
TESTS = ('test_prematch_pattern_discovery_v2', 'test_prematch_selection_audit',
         'test_prematch_tactical_signal_engine', 'test_prematch_daily',
         'test_preflight_timing_v0_3_1')

def main():
    base = ROOT / 'validation_v213'
    result = subprocess.run([sys.executable, '-m', 'unittest', '-v', *TESTS],
                            cwd=ROOT, capture_output=True, text=True)
    (base / 'tests.log').write_text(result.stdout + result.stderr)
    if result.returncode: return result.returncode
    subprocess.run([sys.executable, '-m', 'compileall', '-q',
                    'prematch_pattern_discovery.py', 'test_prematch_pattern_discovery_v2.py'],
                   cwd=ROOT, check=True)
    hashes = {}
    for run in ('run1', 'run2'):
        out = base / run
        discovery.generate(SOURCES / 'enriched/preselection.json', out)
        discovery.commercial_link(out / 'research_queue_sport.json',
                                  SOURCES / 'opportunity_scan.json',
                                  SOURCES / 'bwin_discovery.json', out)
        hashes[run] = {name: discovery.sha(out / (name + '.json')) for name in NAMES}
    assert hashes['run1'] == hashes['run2'], hashes
    comparison = discovery.read(base / 'run1/selector_order_comparison.json')
    budget = discovery.read(SOURCES / 'budget_assignment.json')
    assert comparison['original_bwin_order_fixture_ids'] == budget.get('bwin_order_fixture_ids', [])
    manifest = {'deterministic_json_sha256': hashes,
                'source_sha256': {str(p.relative_to(SOURCES)): discovery.sha(p)
                                  for p in sorted(SOURCES.rglob('*.json'))}}
    discovery.dump(base / 'verification.json', manifest)
    print('93 tests; five deterministic JSON; budget order preserved')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
