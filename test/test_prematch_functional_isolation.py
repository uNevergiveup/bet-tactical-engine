import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from prematch_paths import local_reference
from prematch_cache_record import acquisition_record, unpack_record
import prematch_daily as daily
import prematch_single as single


class IsolationTests(unittest.TestCase):
    def test_manifest_legacy_keys_verify_local_copy_and_conflicts_block(self):
        from prematch_paths import manifest_reference
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();path=root/'outputs/discovery/source.json'
            path.parent.mkdir(parents=True);path.write_text('{}')
            old='/Users/antonio/Projects/scraper/outputs/discovery/source.json'
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(manifest_reference(root,old,{old:digest}),path)
            with self.assertRaises(ValueError):manifest_reference(root,old,{old:digest,str(path):'0'*64})
            path.write_text('{"tampered":true}')
            with self.assertRaises(ValueError):manifest_reference(root,old,{old:digest})

    def test_operational_readers_reject_global_and_legacy_controls_before_reading(self):
        from select_single_match import read_json
        from prematch_opportunity_scanner import load_json
        from prematch_market_mapping_v1 import read
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);latest=root/'outputs/latest';latest.mkdir(parents=True)
            (latest/'ACTIVE_EVENT.json').write_text('{}')
            for reader in (read_json,load_json,read):
                with self.subTest(reader=reader.__module__):
                    with self.assertRaises(ValueError):reader(latest/'ACTIVE_EVENT.json')
                    with self.assertRaises(ValueError):reader(Path('/Users/antonio/Projects/scraper/outputs/control.json'))

    def test_all_old_global_pointers_are_rejected_without_changing_them(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            root = Path(td)
            latest = root / 'outputs/latest'
            latest.mkdir(parents=True)
            target = Path(other) / 'selected.json'
            target.write_text('{}')
            for name in ('selected_match.json', 'selection_decision.json', 'release_state.json'):
                link = latest / name
                link.symlink_to(target)
                with self.assertRaisesRegex(ValueError, 'ACTIVE_STATE'):
                    local_reference(root, link)
                self.assertEqual(link.readlink(), target)
            (latest / 'ACTIVE_EVENT.json').write_text('{"bwin_event_id":"1234567"}')
            with self.assertRaises(ValueError):
                local_reference(root, latest / 'ACTIVE_EVENT.json')

    def test_remap_requires_local_copy_and_matching_hash_even_if_old_path_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            file = root / 'outputs/matches/1234567/inputs/identity.json'
            file.parent.mkdir(parents=True)
            file.write_text('{"event_id":"1234567"}')
            old = '/Users/antonio/Projects/scraper/outputs/matches/1234567/inputs/identity.json'
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            self.assertEqual(local_reference(root, old, digest), file.resolve())
            for h in (None, '0' * 64, 'invalid'):
                with self.assertRaises(ValueError):
                    local_reference(root, old, h)

    def test_traversal_and_external_symlinks_block(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            root = Path(td)
            (root / 'alias').symlink_to(other)
            for p in ('../escape', 'alias/input.json', other,
                      '/Users/antonio/Projects/scraper/../../escape'):
                with self.assertRaises(ValueError):
                    local_reference(root, p, '0' * 64)

    def test_new_day_never_reads_previous_handoff_or_active_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            previous = root / 'outputs/discovery/2026-11-11_1500_0300/prospective_trial'
            previous.mkdir(parents=True)
            (previous / 'handoff_prematch.json').write_text('{"status":"READY_FOR_MANUAL_RESEARCH"}')
            state = daily.discovery_status(root, '2026-11-12')
            self.assertEqual(state['status'], 'NO_DAILY_HANDOFF')
            self.assertFalse(state['actionable_bet'])


class CacheTests(unittest.TestCase):
    def test_acquisition_hash_and_timezone_required(self):
        value = acquisition_record({'response': []})
        self.assertEqual(unpack_record(value)[0], {'response': []})
        value['raw_payload']['response'] = [1]
        with self.assertRaises(ValueError):
            unpack_record(value)
        with self.assertRaises(ValueError):
            unpack_record({'response': []})

    def test_restored_mtime_cannot_refresh_cache(self):
        from generate_context import JSONCache
        with tempfile.TemporaryDirectory() as td:
            cache = JSONCache(Path(td))
            payload = {'response': [{'id': 1}]}
            path = cache._path('injuries', {'fixture': 1})
            path.write_text(json.dumps(acquisition_record(payload, datetime.now(timezone.utc) - timedelta(days=30))))
            os.utime(path, None)
            self.assertIsNone(cache.get('injuries', {'fixture': 1}))
            path.write_text(json.dumps(payload))
            self.assertIsNone(cache.get('injuries', {'fixture': 1}))
            cache.set('injuries', {'fixture': 1}, payload)
            self.assertEqual(cache.get('injuries', {'fixture': 1}), payload)


if __name__ == '__main__':
    unittest.main()
