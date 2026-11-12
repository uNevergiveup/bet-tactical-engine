import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import prematch_single as single


ROOT=Path(__file__).resolve().parent
SELECTED=ROOT/'outputs/phase3/bwin_reuse_integrity/selection/selected_match.json'


class TemporalIdentityContractTests(unittest.TestCase):
    def context(self):
        return single.load_context(SimpleNamespace(root=ROOT,selected=SELECTED,decision=None))

    def test_real_complete_event_specific_evidence_is_verified(self):
        ctx=self.context()
        result=single.validate_temporal_identity_evidence(
            ctx,reference_now=ctx['selection_time'])
        self.assertEqual(result['bwin_event_id'],'7903872')
        self.assertEqual(result['fixture_id'],1570448)
        self.assertEqual(result['kickoff_utc'],'2026-11-07T17:30:00+00:00')
        self.assertEqual(result['difference_minutes'],0.0)

    def staged(self, folder):
        ctx=copy.deepcopy(self.context())
        original_preflight=Path(ctx['selection']['preflight_path'])
        preflight=json.loads(original_preflight.read_text())
        original_discovery=single._source_path(ROOT,preflight['source_discovery'],preflight['source_sha256'])
        discovery=json.loads(original_discovery.read_text())
        discovery_path=folder/'discovery.json';preflight_path=folder/'preflight.json'
        discovery_path.write_text(json.dumps(discovery),encoding='utf-8')
        preflight['source_discovery']=str(discovery_path)
        preflight['source_sha256']=single.sha(discovery_path)
        preflight_path.write_text(json.dumps(preflight),encoding='utf-8')
        selected=ctx['selection'];contract=selected['temporal_identity_evidence']
        selected['preflight_path']=str(preflight_path)
        selected['preflight_sha256']=single.sha(preflight_path)
        contract['discovery_sha256']=single.sha(discovery_path)
        contract['preflight_sha256']=single.sha(preflight_path)
        ctx['root']=folder.resolve()
        return ctx,discovery,preflight,discovery_path,preflight_path

    def persist(self, ctx, discovery, preflight, discovery_path, preflight_path):
        discovery_path.write_text(json.dumps(discovery),encoding='utf-8')
        preflight['source_sha256']=single.sha(discovery_path)
        preflight_path.write_text(json.dumps(preflight),encoding='utf-8')
        ctx['selection']['preflight_sha256']=single.sha(preflight_path)
        contract=ctx['selection']['temporal_identity_evidence']
        contract['discovery_sha256']=single.sha(discovery_path)
        contract['preflight_sha256']=single.sha(preflight_path)

    def test_unknown_timezone_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);ctx,d,p,dp,pp=self.staged(folder)
            d['user_time_reference']=None
            ctx['selection']['temporal_identity_evidence']['timezone']=None
            self.persist(ctx,d,p,dp,pp)
            with self.assertRaisesRegex(ValueError,'TIMEZONE_UNKNOWN'):
                single.validate_temporal_identity_evidence(ctx,reference_now=ctx['selection_time'])

    def test_conflicting_published_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);ctx,d,p,dp,pp=self.staged(folder)
            event=next(x for x in d['catalog_observed'] if str(x.get('bwin_event_id'))=='7903872')
            event['text']='LaLiga Hoy / 19:30 Osasuna - Athletic Club'
            self.persist(ctx,d,p,dp,pp)
            with self.assertRaisesRegex(ValueError,'SOURCE_REVALIDATION_FAILED'):
                single.validate_temporal_identity_evidence(ctx,reference_now=ctx['selection_time'])

    def test_stale_capture_is_rejected(self):
        ctx=self.context();observed=datetime.fromisoformat(
            ctx['selection']['temporal_identity_evidence']['observed_at_utc'])
        with self.assertRaisesRegex(ValueError,'EVIDENCE_STALE'):
            single.validate_temporal_identity_evidence(
                ctx,reference_now=observed+timedelta(minutes=181))

    def test_wrong_event_contract_is_rejected(self):
        ctx=copy.deepcopy(self.context())
        ctx['selection']['temporal_identity_evidence']['bwin_event_id']='7900000'
        with self.assertRaisesRegex(ValueError,'WRONG_EVENT'):
            single.validate_temporal_identity_evidence(ctx,reference_now=ctx['selection_time'])


if __name__=='__main__':
    unittest.main()
