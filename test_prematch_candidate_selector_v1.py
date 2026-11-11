import csv,json,tempfile,unittest
from pathlib import Path
from prematch_candidate_selector_v1 import select

SRC=Path('outputs/discovery/2026-11-08_1500_0300/prospective_trial/predictive_evidence_ranking_v1')
class CandidateSelectorTests(unittest.TestCase):
    def test_union_deduplicated_and_prematch_control(self):
        with tempfile.TemporaryDirectory() as td:
            d=select(SRC,Path(td),asof='2026-11-08T16:00:00+01:00')
            self.assertEqual(d['counts']['candidate_unique'],len({x['fixture_id'] for x in d['candidates']}))
            self.assertGreater(d['counts']['candidate_unique'],20)
            self.assertEqual(d['counts']['bwin_event_verified'],0)
            self.assertTrue(all(x['estado_identidad']=='PENDING_BWIN_IDENTITY' for x in d['candidates']))
            self.assertEqual(d['counts']['eligible_for_scraping'], 0)
            self.assertTrue(all(x['elegible_para_scraping']=='false' for x in d['candidates']))
    def test_event_started_is_not_scrapable(self):
        with tempfile.TemporaryDirectory() as td:
            d=select(SRC,Path(td),asof='2026-11-08T23:59:00+01:00')
            self.assertEqual(d['counts']['prematch_available'],0)
            self.assertTrue(all(x['elegible_para_scraping']=='false' for x in d['candidates']))
    def test_idempotent_and_budget_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            x=select(SRC,Path(a),asof='2026-11-08T16:00:00+01:00',max_bwin=5)
            y=select(SRC,Path(b),asof='2026-11-08T16:00:00+01:00',max_bwin=5)
            self.assertEqual(x['counts'],y['counts'])
            self.assertEqual((Path(a)/'candidatos_prematch_20261108.csv').read_bytes(),(Path(b)/'candidatos_prematch_20261108.csv').read_bytes())
            self.assertEqual(x['selection_policy']['max_bwin_captures'],5)
if __name__=='__main__':unittest.main()
