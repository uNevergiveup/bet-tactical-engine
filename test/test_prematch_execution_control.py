import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import prematch_execution_control as control

ROOT=Path(__file__).resolve().parents[1]


class ManualControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.plan=self.root/'plan.json';self.output=self.root/'run'
        self.source=self.root/'fixtures.json'
        self.source.write_text(json.dumps({'schema':'prematch-phase1-foundation-v1','stage':'LIGHTWEIGHT_DISCOVERY',
            'generated_at_utc':'2026-11-12T00:00:00Z','window':{'inclusive_end':True},'fixtures':[{
            'fixture_id':9000001,'home_id':9000002,'away_id':9000003,'home':'Synthetic A','away':'Synthetic B',
            'league_id':9000004,'league':'Synthetic league','season':2099,'country':'Synthetic',
            'kickoff_utc':'2099-11-12T17:00:00Z','kickoff_madrid':'2099-11-12T18:00:00+01:00',
            'status':'NS','gender':'M','category_status':'ELIGIBLE','region':'UNKNOWN','region_priority':0}]}))
        self.args=['prepare','--date','2099-11-12','--plan',str(self.plan),'--output-dir',str(self.output),
                   '--fixtures-input',str(self.source),'--cache-root',str(self.root/'empty-cache')]

    def prepare(self):
        import prematch_daily
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(prematch_daily.main(self.args),0)
        return control.read_plan(self.plan)

    def test_prepare_and_dry_run_never_launch_a_child_or_network(self):
        with (patch('socket.socket.connect',side_effect=AssertionError('NETWORK')),
              patch.object(control.subprocess,'run',side_effect=AssertionError('CHILD'))):
            self.prepare()
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(control.main(['dry-run','--plan',str(self.plan)]),0)
        self.assertFalse(self.output.exists())

    def test_external_execution_requires_exact_plan_approval_before_writes(self):
        plan=self.prepare()
        with patch.object(control.subprocess,'run',side_effect=AssertionError('CHILD')):
            with self.assertRaises(ValueError):control.execute(plan,external=True,confirmation='yes')
        self.assertFalse(self.output.exists())

    def test_input_and_plan_tampering_rejected(self):
        self.prepare();self.source.write_text('{}')
        with self.assertRaises(ValueError):control.read_plan(self.plan)

    def test_full_offline_pipeline_and_resume_use_no_external_runtime(self):
        document=json.loads(self.source.read_text());document['fixtures'][0].update(
            kickoff_utc='2099-11-13T02:00:00Z',kickoff_madrid='2099-11-13T03:00:00+01:00')
        self.source.write_text(json.dumps(document))
        plan=self.prepare()
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
            result=control.execute(plan)
            self.assertEqual(result['status'],'OFFLINE_IMPLEMENTATION_VERIFIED')
            self.assertEqual(result['api_reserved_or_consumed'],0)
            with patch.object(control.subprocess,'run',side_effect=AssertionError('RESUME_REEXECUTED_CHILD')):
                resumed=control.execute(plan,resume=True)
        self.assertFalse(resumed['actionable_bet'])
        document=json.loads((self.output/'llm/ranking_enriched.json').read_text())
        self.assertEqual(document['status'],'PENDING_LLM_RESEARCH_NOT_FINAL')
        self.assertEqual(len(document['base_ranking']),1)
        self.assertFalse(json.loads((self.output/'llm/runtime_dependency.json').read_text())['runtime_executed'])
        explanations=json.loads((self.output/'explanations.json').read_text())
        self.assertEqual(len(explanations['fixtures']),1)
        self.assertFalse(explanations['value_validated'])
        for family in explanations['fixtures'][0]['familias']:
            self.assertIsNone(family['comportamiento_equipo']['mean'])
            self.assertFalse(family['mercados_capturados'])

    def test_prepare_rejects_a_fixture_from_another_window(self):
        self.args[self.args.index('2099-11-12')]='2099-11-13'
        with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(control.main(self.args),2)
        self.assertFalse(self.plan.exists())

    def test_legacy_dry_run_never_runs_online_even_when_confirmation_is_present(self):
        import prematch_daily
        with (patch.object(prematch_daily,'operational_research',side_effect=AssertionError('DRY_RUN_EXECUTED')),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(prematch_daily.main(['--rank','--online','--dry-run','--confirm-external']),0)

    def test_network_entrypoints_reject_missing_confirmation(self):
        import prematch_phase1,bwin_discovery,scraper_bwin,prematch_daily,prematch_llm_research,prematch_single,prematch_multi,preselect_matches
        cases=[(prematch_phase1.main,['--online','--max-api-calls','1']),
               (bwin_discovery.main,['--input',str(self.source)]),
               (scraper_bwin.main,[]),(prematch_daily.main,['--sync-bwin']),
               (prematch_single.main,['--selected','missing','prepare']),
               (prematch_multi.main,['prepare','--url','https://www.bwin.es/es/sports/eventos/a-b-2:1234567']),
               (preselect_matches.main,['--input','missing']),
               (prematch_llm_research.main,['--observations','missing','--base-ranking','missing',
                '--cutoff','2099-01-01T00:00:00Z','--output-dir','missing','--runtime-command-json','["missing"]'])]
        with patch.dict(os.environ,{},clear=True),patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
            for function,arguments in cases:
                with self.subTest(function=function.__module__),contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:function(arguments)
                    self.assertEqual(error.exception.code,2)

    def test_approval_environment_is_scoped_and_restored(self):
        @control.confirmed_cli
        def command(argv):
            control.require_external_confirmation()
            return os.environ['PREMATCH_EXTERNAL_CONFIRMATION']
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):command([])
            self.assertEqual(len(command(['--confirm-external'])),64)
            self.assertNotIn('PREMATCH_EXTERNAL_CONFIRMATION',os.environ)

    def test_interrupted_api_budget_is_not_replenished_by_resume(self):
        from types import SimpleNamespace
        self.args.extend(['--api-budget','2']);plan=self.prepare()
        with patch.object(control.subprocess,'run',return_value=SimpleNamespace(returncode=130)) as child:
            with self.assertRaises(ValueError):control.execute(plan,external=True,confirmation=plan['approval_sha256'])
            state=json.loads((self.output/'controller_state.json').read_text())
            self.assertEqual(state['api_reserved'],2)
            with self.assertRaises(ValueError):control.execute(plan,external=True,confirmation=plan['approval_sha256'],resume=True)
            self.assertEqual(child.call_count,1)

    def test_confirmed_api_budget_propagates_and_reconciles_only_completed_usage(self):
        from types import SimpleNamespace
        self.args.extend(['--api-budget','2']);plan=self.prepare();original=control.subprocess.run
        def child(command,**kwargs):
            if 'prematch_phase1.py' in str(command[2]):
                self.assertIn('--online',command)
                self.assertEqual(command[command.index('--max-api-calls')+1],'2')
                self.assertEqual(kwargs['env']['PREMATCH_EXTERNAL_CONFIRMATION'],plan['approval_sha256'])
                folder=Path(command[command.index('--output-dir')+1]);folder.mkdir(parents=True)
                control.write_once(folder/'fixtures_discovered.json',json.loads(self.source.read_text()))
                control.write_once(folder/'phase1_summary.json',{'api_usage':{'new_http_calls':1}})
                return SimpleNamespace(returncode=0)
            return original(command,**kwargs)
        with patch.object(control.subprocess,'run',side_effect=child),patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
            report=control.execute(plan,external=True,confirmation=plan['approval_sha256'])
        self.assertEqual(report['api_reserved_or_consumed'],1)

    def test_failed_quote_capture_remains_explicit_without_dropping_the_fixture(self):
        from types import SimpleNamespace
        from datetime import datetime,timezone
        self.args.extend(['--bwin-events','1']);plan=self.prepare();original=control.subprocess.run
        fixture=json.loads(self.source.read_text())['fixtures'][0]
        def child(command,**kwargs):
            name=Path(command[2]).name
            if name=='bwin_discovery.py':
                row={**fixture,'status':'IDENTITY_VERIFIED_AUTOMATICALLY','identity_status':'NAME_AND_ABSOLUTE_TIME_MATCH',
                     'bwin_event_id':'1234567','bwin_url':'https://www.bwin.es/es/sports/eventos/synthetic-a-synthetic-b-2:1234567',
                     'category_compatibility':'COMPATIBLE_OR_UNKNOWN','team_order_status':'HOME_AWAY','competition_similarity':1,
                     'inspection':{'event_id_from_url':'1234567','title_teams_match':True,'competition_match_score':1,
                     'event_start_machine_readable':fixture['kickoff_madrid'],'observed_at_utc':datetime.now(timezone.utc).isoformat()}}
                folder=Path(command[command.index('--output-dir')+1])
                control.write_once(folder/'bwin_discovery.json',{'matches':[row],'catalog_observed':[]})
                return SimpleNamespace(returncode=0)
            if name=='bwin_market_preflight.py':
                folder=Path(command[command.index('--output-dir')+1])
                control.write_once(folder/'market_preflight.json',{'matches':[{
                    'fixture_id':fixture['fixture_id'],'home':fixture['home'],'away':fixture['away'],
                    'status':'BLOCKED','reasons':['SYNTHETIC_CAPTURE_FAILURE']}]})
                return SimpleNamespace(returncode=2)
            return original(command,**kwargs)
        with patch.object(control.subprocess,'run',side_effect=child),patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
            control.execute(plan,external=True,confirmation=plan['approval_sha256'])
        pre=json.loads((self.output/'preselection.json').read_text())
        self.assertTrue(pre['matches'][0]['market_identity_ready'])
        self.assertEqual(len(json.loads((self.output/'base_ranking.json').read_text())),1)
        self.assertTrue((self.output/'markets/9000001/market_preflight.json').is_file())
        mapping=json.loads((self.output/'market_mapping/market_mapping.json').read_text())
        self.assertGreater(mapping['counts']['identity_blocked'],0)


if __name__=='__main__':unittest.main()
