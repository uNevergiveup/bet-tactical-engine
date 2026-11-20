"""Offline regressions for daily-flow stabilization (Objective 7.2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import prematch_prospective_trial as trial
from bwin_discovery import candidate_possibilities, verified_identity_candidates
from prematch_daily import complete_daily_metrics
from prematch_daily_csv import export_daily
from prematch_tactical_signal_engine import run_engine
from test_prematch_tactical_signal_engine import fixture, observations, NOW


ROOT=Path(__file__).resolve().parents[1]


class Objective72Tests(unittest.TestCase):
    def evaluation_args(self):
        return SimpleNamespace(start=time(15),end=time(3),horizon_hours=4,
            evaluation_budget=0,discovery_budget=0,max_matches=5,bwin_top=5,
            history=20,details=8,min_lead=45,competition_scope='adult-open',team=[])

    def test_csv_export_resolves_isolated_evaluation_window(self):
        with tempfile.TemporaryDirectory() as td:
            window=Path(td)/'outputs/discovery/2026-11-12_1500_0300'
            evaluation,_=trial.resolve_evaluation(window,date(2026,11,12),self.evaluation_args(),
                datetime.fromisoformat('2026-11-12T18:00:00+01:00'))
            base=evaluation/'prospective_trial'
            rows=[{'fixture_id':1,'home':'A','away':'B','status':'NS','season':2026,
                   'kickoff_madrid':'2026-11-12T20:00:00+01:00'},
                  {'fixture_id':2,'home':'C','away':'D','status':'NS','season':2026,
                   'kickoff_madrid':'2026-11-13T01:00:00+01:00'}]
            trial.dump(evaluation/'fixtures.json',{'window':{
                'start_madrid':'2026-11-12T15:00:00 Europe/Madrid (hora civil)',
                'end_madrid':'2026-11-13T03:00:00 Europe/Madrid (hora civil)'},'fixtures':rows})
            exported=export_daily(base)
            self.assertEqual(len(exported),2)
            self.assertTrue((base/'partidos_diarios.csv').is_file())
            self.assertTrue((base/'partidos_diarios.json').is_file())

    def test_statistical_ranking_uses_local_evidence_without_6_1_dossier(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);base=root/'evaluation/prospective_trial';pre=base/'enriched/preselection.json'
            strong=fixture();strong['statistical_observations']=observations(12)
            weak=dict(fixture(),fixture_id=900002,home='Weak Home',away='Weak Away')
            weak['statistical_observations']=observations(3)
            trial.dump(pre,{'generated_at_utc':NOW.isoformat(),'matches':[weak,strong]})
            doc=run_engine(pre,None,base/'tactical',root,asof=NOW,historical=True,use_cache=False)
            ranking=doc['statistical_ranking']
            self.assertEqual(ranking[0]['fixture_id'],strong['fixture_id'])
            self.assertGreater(ranking[0]['statistical_research_score'],ranking[1]['statistical_research_score'])
            self.assertEqual(ranking[0]['expanded_6_1_evidence']['status'],'NOT_AVAILABLE_FOR_FIXTURE')
            self.assertEqual(doc['ranking_separation']['statistical'],'CURRENT_PRESELECTION_DESCRIPTIVE_EVIDENCE')

    @unittest.skipUnless((ROOT/'outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json').is_file(),
                         'Frozen Objective 5.1 catalog absent')
    def test_persisted_direct_binding_is_normalized_before_summary(self):
        catalog=ROOT/'outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json'
        fixture_row={'fixture_id':1520978,'home':'São Bernardo','away':'Athletic Club',
            'home_id':7865,'away_id':13975,'league_id':72,'season':2026,'gender':'UNKNOWN',
            'league':'Serie B','kickoff_madrid':'2026-11-13T00:00:00+01:00'}
        bindings,_=verified_identity_candidates(catalog,[fixture_row])
        possibilities=candidate_possibilities(fixture_row,list(bindings.values()),bindings)
        self.assertEqual(possibilities[0]['bwin_event_id'],'7911881')
        self.assertEqual(possibilities[0]['name_score'],1.0)
        wrong=dict(fixture_row,home='Unrelated Home',away='Unrelated Away')
        self.assertEqual(candidate_possibilities(wrong,list(bindings.values()),bindings),[])

    def test_complete_metrics_separate_daily_total_from_nested_breakdowns(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)/'prospective_trial';logs=base/'logs';logs.mkdir(parents=True)
            records=[{'script':'discover_fixtures.py','elapsed_seconds':2.0},
                     {'script':'preselect_matches.py','elapsed_seconds':3.0},
                     {'script':'bwin_discovery.py','elapsed_seconds':1.5},
                     {'script':'prematch_opportunity_scanner.py','elapsed_seconds':.5}]
            (logs/'pipeline_stage_timings.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in records))
            phase_path=base/'daily_runs/run/phase_metrics.json'
            doc={'phase_metrics':{'total':4.0,'commercial_scanner':.5},
                 'phase_metrics_path':str(phase_path),'run_handoff':str(base/'daily_runs/run/handoff_prematch.json')}
            metrics=complete_daily_metrics(base,doc,100.0,{
                'preparation_total':5.0,'scan_total':4.0,'csv_export':.25},ended=110.0)
            self.assertEqual(metrics['daily_total_elapsed_seconds'],10.0)
            self.assertEqual(metrics['exclusive_phase_sum_seconds'],10.0)
            self.assertEqual(metrics['scan_internal_phase_seconds']['total'],4.0)
            self.assertEqual(metrics['subprocess_component_seconds_nested']['api_fixture_and_history'],5.0)
            self.assertEqual(metrics['subprocess_component_seconds_nested']['bwin_discovery'],1.5)


if __name__=='__main__':
    unittest.main()
