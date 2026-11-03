"""Release gates: network boundaries are mocked; real JSON/TXT and SHA checks."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import prematch_release as release
import prematch_single as single
import prematch_runtime as runtime
import prematch_prospective_trial as t
import bwin_market_preflight as preflight
import test_prematch_daily as helpers
NOW=helpers.NOW


class BridgeTests(unittest.TestCase):
    setUp=helpers.PipelineTests.setUp
    preflight=helpers.PipelineTests.preflight
    fake_run=helpers.PipelineTests.fake_run
    def handoff(self):
        f=self.fixtures[0]
        f.update(home_id=10,away_id=20,league_id=100,season=2026,league='Test league')
        t.dump(self.base/'fixtures_major_test.json',{'fixtures':[f]})
        t.dump(self.base/'enriched/preselection.json',{'generated_at_utc':NOW.isoformat(),'matches':self.fixtures,
            'source_filter_sha256':t.sha(self.base/'fixtures_major_test.json')})
        self.preflight(f)
        with patch.object(t,'run',side_effect=self.fake_run):runtime.scan(self.root,self.folder,NOW.date(),self.args)
        return self.base/'handoff_prematch.json'
    def test_bridge_retains_single_identity_and_hashes(self):
        path=self.handoff();selected=release.bridge(self.root,path)
        doc=t.read(selected);decision=t.read(selected.parent/'selection_decision.json')
        ctx=single.validate_selected(doc,decision)
        self.assertEqual(ctx['fixture_id'],1);self.assertEqual(doc['home_api_id'],10)
        self.assertEqual(doc['league_id'],100)
        self.assertEqual(doc['odds_sha256'],t.sha(Path(doc['odds_txt'])))
    def test_bridge_rejects_context_team_change(self):
        path=self.handoff();doc=t.read(path);p=Path(doc['context_paths'][0])
        x=t.read(p);x['fixture']['home']='Wrong';t.dump(p,x)
        with self.assertRaisesRegex(t.TrialError,'CONTEXT_SHA'):release.bridge(self.root,path)
    def test_bridge_rejects_wrong_event(self):
        path=self.handoff();doc=t.read(path);doc['bwin_event_id']='7999999';t.dump(path,doc)
        with self.assertRaisesRegex(ValueError,'IMMUTABLE_HANDOFF_MISMATCH'):release.bridge(self.root,path)
    def test_history_bridge_never_loads_as_actionable(self):
        path=self.handoff();selected=release.bridge(self.root,path,historical=True,asof=NOW)
        with self.assertRaises(ValueError):single.validate_selected(t.read(selected),t.read(selected.parent/'selection_decision.json'))
    def test_simulated_time_cannot_generate_live_selection(self):
        with self.assertRaisesRegex(ValueError,'Simulated'):release.bridge(self.root,self.handoff(),asof=NOW)
    def test_expired_handoff_is_rejected(self):
        path=self.handoff()
        with patch.object(t,'now_madrid',return_value=NOW+timedelta(hours=4)):
            with self.assertRaises(t.TrialError):release.bridge(self.root,path)
    def test_competition_mismatch_between_selected_and_decision(self):
        selected=release.bridge(self.root,self.handoff())
        doc=t.read(selected);doc['league_id']=999
        with self.assertRaisesRegex(ValueError,'league_id'):
            single.validate_selected(doc,t.read(selected.parent/'selection_decision.json'))


class RefreshTests(unittest.TestCase):
    setUp=helpers.PipelineTests.setUp
    preflight=helpers.PipelineTests.preflight
    def test_failed_refresh_preserves_exact_previous_bytes(self):
        _,m=self.preflight(self.fixtures[0]);p=Path(m['snapshot']['snapshot_path']);before=t.sha(p)
        with patch.object(preflight.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            result=preflight.run_scraper(self.root,self.root/'refresh','7000001',m['bwin_url'],refresh=True)
        self.assertNotIn('fresh_snapshot_path',result);self.assertEqual(t.sha(p),before)
    def test_success_archives_old_snapshot_by_sha(self):
        _,m=self.preflight(self.fixtures[0]);p=Path(m['snapshot']['snapshot_path']);before=t.sha(p)
        def scrape(cmd,**kwargs):
            dest=Path(kwargs['env']['PREMATCH_EVENT_DATA_DIR'])/p.name
            dest.write_text(p.read_text().replace((NOW-timedelta(minutes=10)).isoformat(),NOW.isoformat()))
            kwargs['stdout'].write('TXT: '+str(dest)+'\n');kwargs['stdout'].flush()
            return SimpleNamespace(returncode=0)
        with patch.object(preflight.subprocess,'run',side_effect=scrape),patch.object(preflight,'utcnow',return_value=NOW):
            result=preflight.run_scraper(self.root,self.root/'refresh','7000001',m['bwin_url'],refresh=True)
        self.assertEqual(t.sha(Path(result['archived_previous'])),before)
        self.assertNotEqual(t.sha(p),before)
    def test_prepared_event_requires_manifest_aware_refresh(self):
        _,m=self.preflight(self.fixtures[0]);p=Path(m['snapshot']['snapshot_path'])
        t.dump(p.parent.parent/'manifest.json',{'status':'PREPARADO'})
        with patch.object(preflight.subprocess,'run',side_effect=AssertionError('No network')):
            result=preflight.run_scraper(self.root,self.root/'refresh','7000001',m['bwin_url'],refresh=True)
        self.assertIn('prematch_single',result['error'])


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.directory=self.root/'outputs/matches/7000001';self.directory.mkdir(parents=True)
        self.ctx={'root':self.root,'directory':self.directory,'event_id':'7000001','fixture_id':1,
                  'kickoff':NOW+timedelta(minutes=40),'url':'https://www.bwin.es/es/sports/eventos/home-away-2:7000001'}
        self.odds=self.directory/'odds.txt'
        self.odds.write_text('Event ID: 7000001\nURL: '+self.ctx['url']+'\nCaptura: '+NOW.isoformat()+'\n')
        self.manifest={'status':'FINALIZADO','inputs':{'odds':release.reference(self.odds)}}
        t.dump(self.directory/'analysis_input_binding.json',{'source_refs':{'odds':release.reference(self.odds)},'odds_sha256':t.sha(self.odds)})
        self.analysis=self.directory/'analysis/analisis_7000001.md';self.analysis.parent.mkdir()
        self.analysis.write_text('DECISIÓN: NO BET\nEvidencia insuficiente para una decisión.')
        for target,value in ((single,'local_now'),(t,'now_madrid')):
            clock=patch.object(target,value,return_value=NOW);clock.start();self.addCleanup(clock.stop)
        load=patch.object(single,'load_context',return_value=self.ctx);load.start();self.addCleanup(load.stop)
        manifest=patch.object(single,'match_manifest',side_effect=lambda _:self.manifest);manifest.start();self.addCleanup(manifest.stop)
    def test_no_bet_final_decision_documented(self):
        result=release.final_review(self.root,Path('selected.json'),self.analysis)
        self.assertEqual(result['status'],'NO_BET');self.assertFalse(result['actionable_bet'])
    def test_changed_price_or_line_invalidates_analysis(self):
        self.odds.write_text(self.odds.read_text()+'Changed quote\n')
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):release.final_review(self.root,Path('selected.json'),self.analysis)
    def test_post_kickoff_blocked(self):
        with patch.object(single,'local_now',return_value=NOW+timedelta(hours=1)):
            with self.assertRaisesRegex(ValueError,'empezó'):release.final_review(self.root,Path('selected.json'),self.analysis)
    def test_final_age_exceeds_fifteen_blocked(self):
        with patch.object(single,'local_now',return_value=NOW+timedelta(minutes=16)):
            with self.assertRaisesRegex(ValueError,'antiguas'):release.final_review(self.root,Path('selected.json'),self.analysis)
    def test_analysis_of_other_event_rejected(self):
        with self.assertRaisesRegex(ValueError,'selected event'):release.final_review(self.root,Path('selected.json'),self.root/'other.md')
    def test_external_placeholders_cannot_be_evidence(self):
        folder=self.directory/'research/external';folder.mkdir(parents=True)
        (folder/'grok_7000001.md').write_text('STATUS: PENDIENTE_MANUAL\n')
        with self.assertRaisesRegex(ValueError,'pendiente'):
            release.validate_external_inputs(self.ctx,self.analysis)
    def test_postkickoff_external_cutoff_is_blocked(self):
        folder=self.directory/'research/external';folder.mkdir(parents=True)
        (folder/'grok_7000001.md').write_text('Event ID: 7000001\nFixture ID: 1\ncutoff_utc: '+(NOW+timedelta(hours=2)).isoformat()+'\n'+'Evidence text '*30)
        with self.assertRaisesRegex(ValueError,'cutoff_utc'):
            release.validate_external_inputs(self.ctx,self.analysis)


if __name__=='__main__':unittest.main()
