"""Run from repository root: .venv/bin/python -B tools/run_offline_tests.py."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from offline_test_guard import install


def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))
    protected = [str(root / 'outputs'), '/Users/antonio/Projects/scraper']
    with tempfile.TemporaryDirectory(prefix='prematch-test-guard-') as td:
        # Python children inherit the same boundary, including CLI regression tests.
        source = root / 'tools/offline_test_guard.py'
        (Path(td) / 'sitecustomize.py').write_text(
            'import runpy\nrunpy.run_path(' + repr(str(source)) + ')["install"](' + repr(protected) + ')\n')
        previous = os.environ.get('PYTHONPATH')
        os.environ['PYTHONPATH'] = td + (os.pathsep + previous if previous else '')
        install(protected)
        try:
            suite = unittest.defaultTestLoader.discover(str(root), pattern='test*.py')
            result = unittest.TextTestRunner(verbosity=1).run(suite)
            print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
                              'errors': len(result.errors), 'skipped': [(str(test), reason) for test, reason in result.skipped],
                              'original_outputs_protected': True}, ensure_ascii=False, indent=2))
            return 0 if result.wasSuccessful() else 1
        finally:
            if previous is None: os.environ.pop('PYTHONPATH', None)
            else: os.environ['PYTHONPATH'] = previous


if __name__ == '__main__':
    raise SystemExit(main())
