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


class ResearchKitV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.directory=self.root/'outputs/matches/7903872'
        inputs=self.directory/'inputs';inputs.mkdir(parents=True)
        self.identity=inputs/'match_identity.json'
        t.dump(self.identity,{
            'identity_status':'FIXTURE_AND_BWIN_TIME_VERIFIED','bwin_event_id':'7903872',
            'bwin_url':'https://www.bwin.es/es/sports/eventos/osasuna-athletic-club-2:7903872',
            'api_fixture_id':1570448,'league_id':140,'league_name':'La Liga','season':2026,
            'kickoff_utc':(NOW+timedelta(hours=1)).isoformat(),
            'home':{'api_name':'Osasuna','api_team_id':727},
            'away':{'api_name':'Athletic Club','api_team_id':531}})
        self.context_file=inputs/'match_engine_7903872.json'
        t.dump(self.context_file,{'match':{'event_id':7903872},'fixture':{'fixture_id':1570448},
            'context':{'history':[{'fixture_id':111}],'api_odds':{'secret_price':9.99},
                       'predictions':{'winner':'forbidden'},'shots':12}})
        self.manifest={'status':'PREPARADO','inputs':{
            'identity':release.reference(self.identity),'context_json':release.reference(self.context_file)}}
        selected=self.directory/'selection/selected_match.json';selected.parent.mkdir()
        selected.write_text('{}');decision=selected.parent/'selection_decision.json';decision.write_text('{}')
        self.ctx={'root':self.root,'directory':self.directory,'event_id':'7903872','fixture_id':1570448,
            'url':'https://www.bwin.es/es/sports/eventos/osasuna-athletic-club-2:7903872',
            'home':'Osasuna','away':'Athletic Club','kickoff':NOW+timedelta(hours=1),
            'selection_time':NOW,'selection':{},'selected_path':selected,'decision_path':decision}
        active=self.directory/'research/external';active.mkdir(parents=True)
        self.active={}
        for name in ('COPIAR_GEMINI_7903872.md','COPIAR_GROK_7903872.md',
                     'ENTRADA_GEMINI_7903872.md','ENTRADA_GROK_7903872.md','ENTRADA_ARGON_7903872.md',
                     'gemini_7903872.md','grok_7903872.md','fusion_7903872.md','reporte_cualitativo_7903872.md'):
            path=active/name;path.write_text('ACTIVE '+name)
            self.active[path]=t.sha(path)
        for target,name,value in ((release,'selected_context',self.ctx),(single,'match_manifest',self.manifest)):
            mocked=patch.object(target,name,return_value=value);mocked.start();self.addCleanup(mocked.stop)
        allowed=patch.object(single,'ensure_prematch',return_value=30);allowed.start();self.addCleanup(allowed.stop)

    def test_three_specialized_prompts_correct_metadata_and_no_active_overwrite(self):
        folder=release.research_kit(self.root,Path('selected.json'))
        prompts=sorted(folder.glob('PROMPT_*.txt'))
        self.assertEqual([p.name for p in prompts],[
            'PROMPT_ARGON_7903872.txt','PROMPT_GEMINI_7903872.txt','PROMPT_GROK_7903872.txt'])
        bodies={p.name:p.read_text() for p in prompts}
        for body in bodies.values():
            self.assertIn('La Liga (League ID 140)',body)
            self.assertIn('Temporada API-Football: 2026',body)
            self.assertIn('cutoff_utc estricto:',body)
            self.assertNotIn('9.99',body);self.assertNotIn('api_odds',body)
        self.assertIn('mecanismos colectivos',bodies['PROMPT_GEMINI_7903872.txt'])
        self.assertIn('cuentas oficiales en X',bodies['PROMPT_GROK_7903872.txt'])
        self.assertIn('auditoría cruzada',bodies['PROMPT_ARGON_7903872.txt'])
        self.assertIn(str(self.directory/'research/external/reporte_cualitativo_7903872.md'),
                      bodies['PROMPT_ARGON_7903872.txt'])
        projection=t.read(folder/'contexto_cualitativo_7903872.json')
        self.assertNotIn('api_odds',projection['data']['context'])
        self.assertNotIn('predictions',projection['data']['context'])
        self.assertEqual({p:t.sha(p) for p in self.active},self.active)

    def test_idempotent_and_different_existing_prompt_blocks_transaction(self):
        folder=release.research_kit(self.root,Path('selected.json'))
        before={p:t.sha(p) for p in folder.iterdir()}
        self.assertEqual(release.research_kit(self.root,Path('selected.json')),folder)
        self.assertEqual({p:t.sha(p) for p in folder.iterdir()},before)
        target=folder/'PROMPT_GROK_7903872.txt';target.write_text('DO NOT OVERWRITE')
        protected=folder/'PROMPT_GEMINI_7903872.txt';digest=t.sha(protected)
        with self.assertRaisesRegex(ValueError,'WOULD_OVERWRITE'):
            release.research_kit(self.root,Path('selected.json'))
        self.assertEqual(t.sha(protected),digest)
        self.assertEqual(target.read_text(),'DO NOT OVERWRITE')


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
