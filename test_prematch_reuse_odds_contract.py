import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timedelta
from unittest.mock import patch
import prematch_single as s

ROOT=Path(__file__).resolve().parent
SELECTED=ROOT/'outputs/phase3/bwin_reuse_integrity/selection/selected_match.json'


class ReuseTests(unittest.TestCase):
    def setUp(self):
        from prematch_test_fixtures import restored_selection_fixture
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.selected=restored_selection_fixture(self.root)

    def test_producer_exports_verified_snapshot_binding(self):
        import select_single_match as selector
        from decimal import Decimal
        ctx=self.context();decision=json.loads((self.selected.parent/'selection_decision.json').read_text())
        from prematch_paths import local_reference
        pre=local_reference(self.root,decision['sources']['preselection'],decision['sources']['preselection_sha256']);disc=local_reference(self.root,decision['sources']['discovery'],decision['sources']['discovery_sha256']);pf=local_reference(self.root,decision['sources']['preflights'][0],ctx['selection']['preflight_sha256'])
        now=ctx['selection_time'];args=SimpleNamespace(asof=None,min_candidate_odd=Decimal('1.20'),min_lead_minutes=45,
            max_snapshot_age_minutes=120,max_discovery_age_minutes=180,min_comparable_matches=1,
            preselection=pre,discovery=disc,preflight_files_resolved=[pf])
        pre_doc=selector.read_json(pre);pre_doc['_path']=pre
        disc_doc=selector.read_json(disc);disc_doc['_path']=disc
        status,winner,rows,meta=selector.prepare_rows(pre_doc,disc_doc,[(pf,selector.read_json(pf))],now,45,120,180,1)
        self.assertEqual(status,'SELECTED')
        with tempfile.TemporaryDirectory() as td:
            selector.export(Path(td),status,winner,rows,meta,None,None,now,args)
            doc=json.loads((Path(td)/'selected_match.json').read_text())
            self.assertEqual(doc['odds_sha256'],ctx['selection']['odds_sha256'])
            self.assertEqual(doc['preflight_sha256'],s.sha(pf))

    def context(self):
        return s.load_context(SimpleNamespace(root=self.root,selected=self.selected,decision=None))

    def test_real_origin_and_hash(self):
        ctx=self.context();when=datetime.fromisoformat(ctx['selection']['selection_asof_madrid'])+timedelta(seconds=5)
        with patch.object(s,'local_now',return_value=when):
            info=s.validate_reuse_odds(ctx)
        self.assertEqual(info['sha256'],'1d0a71c22ee4100accb896042b4e6744981a0fd47ea5cf1bf4a26e61bb8d73c2')

    def test_absent_wrong_hash_other_event_and_modified_file(self):
        original=self.context()
        for change in ('missing','wrong','other_event','modified'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as td:
                ctx=copy.deepcopy(original);selection=ctx['selection']
                when=datetime.fromisoformat(selection['selection_asof_madrid'])
                if change=='missing':selection.pop('odds_sha256')
                elif change=='wrong':selection['odds_sha256']='0'*64
                elif change=='other_event':ctx['event_id']='7900000'
                else:
                    ctx['directory']=Path(td);folder=Path(td)/'inputs';folder.mkdir()
                    target=folder/'cuotas_bwin_7903872.txt'
                    target.write_bytes((original['directory']/'inputs/cuotas_bwin_7903872.txt').read_bytes()+b'\nchanged\n')
                    selection['odds_txt']=str(target)
                with patch.object(s,'local_now',return_value=when),self.assertRaises(ValueError):s.validate_reuse_odds(ctx)

    def test_prepare_checks_before_launch_and_normal_mode_is_unchanged(self):
        ctx=self.context();ctx['selection'].pop('odds_sha256')
        args=SimpleNamespace(min_lead_minutes=45,max_selection_age_minutes=0,multi=ROOT/'prematch_multi.py',reuse_odds=True,max_odds_age_minutes=60)
        with patch.object(s,'local_now',return_value=ctx['selection_time']),patch.object(s,'run') as launch:
            with self.assertRaisesRegex(ValueError,'BINDING_MISSING'):s.cmd_prepare(ctx,args)
            launch.assert_not_called()


if __name__=='__main__':unittest.main()
