import tempfile
import unittest
import json
from pathlib import Path
from datetime import timedelta

import prematch_sos_pool_v1044 as pool
from test_prematch_sos_v1044 import perf, performance_row, BASE, TARGET


def put(root, event, doc, good_quality=True):
    folder = root/f'outputs/matches/{event}/analysis/deep_v1042'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/f'rendimiento_profundo_{event}.json').write_text(json.dumps(doc))
    if good_quality:
        (folder/f'auditoria_calidad_{event}.json').write_text(json.dumps({
            'quality_status':'REVIEW_REQUIRED','target_fixture_id':doc['target_fixture_id'],
            'issues':[{'severity':'WARNING'}]}))


class PoolTests(unittest.TestCase):
    def test_old_snapshot_is_reused_offline(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            put(root, '123', perf([performance_row(11)]))
            src=perf([performance_row(24, date=4, tid=1, oid=5)])
            src['event_id_bwin']='777'
            src['target_fixture_id']=888
            src['snapshot_generated_at_utc']=(TARGET-timedelta(hours=3)).isoformat()
            put(root,'777',src)
            res=pool.collect(root,'123')
            self.assertEqual(len(res['games']),1)
            self.assertEqual(res['games'][0]['fixture_id'],24)
            self.assertEqual(len(res['included_sources']),1)

    def test_future_capture_skipped_even_if_history_is_old(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            put(root,'123',perf([performance_row(11)]))
            src=perf([performance_row(24,date=4)])
            src['event_id_bwin']='777';src['target_fixture_id']=888
            src['snapshot_generated_at_utc']=(TARGET+timedelta(hours=2)).isoformat()
            put(root,'777',src)
            res=pool.collect(root,'123')
            self.assertFalse(res['games'])
            self.assertEqual(res['excluded_sources'][0]['reason'],'INVALID_OR_FUTURE_SNAPSHOT')

    def test_unverified_quality_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            put(root,'123',perf([performance_row(11)]))
            src=perf([performance_row(24,date=4)])
            src['event_id_bwin']='777';src['target_fixture_id']=888
            src['snapshot_generated_at_utc']=(TARGET-timedelta(hours=3)).isoformat()
            put(root,'777',src,good_quality=False)
            res=pool.collect(root,'123')
            self.assertFalse(res['games'])
            self.assertEqual(res['excluded_sources'][0]['reason'],'QUALITY_AUDIT_MISSING')

if __name__=='__main__':
    unittest.main()
