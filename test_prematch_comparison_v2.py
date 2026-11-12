"""Offline V2 audit tests. They never call API-Football, Bwin or an LLM."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import prematch_prospective_trial as t
from prematch_runtime import write_comparison_audit
from prematch_tactical_signal_engine import analyze_fixture, market_links, selection_rows
from test_prematch_tactical_signal_engine import fixture, observations, NOW
import test_prematch_daily as helpers


class CoverageAndMarketV2Tests(unittest.TestCase):
    setUp=helpers.PipelineTests.setUp

    def test_partial_history_exposes_both_denominators(self):
        data=observations(9)
        for side in data:
            for row in data[side][7:]:
                row['statistics_requested']=False
                row['own']={'goals':row['own']['goals']}
                row['conceded']={'goals':row['conceded']['goals']}
        signal=next(s for s in analyze_fixture(fixture(),NOW,data)['signals'] if s['metric']=='corners' and s['team_side']=='home')
        self.assertEqual(signal['own']['historical_matches_available'],9)
        self.assertEqual(signal['own']['historical_matches_detailed'],7)
        self.assertEqual(signal['own']['historical_matches_not_detailed'],2)
        self.assertEqual(signal['own']['metric_observations_valid'],7)
        self.assertEqual(signal['own']['metric_observations_missing'],0)
        self.assertEqual(signal['own']['requested_data_coverage_pct'],100)
        self.assertAlmostEqual(signal['own']['historical_effective_coverage_pct'],77.78,places=2)
        self.assertLess(signal['components']['coverage'],1)

    def test_direct_indirect_and_player_categories_are_separate(self):
        f=self.fixtures[0]
        path,match=helpers.PipelineTests.preflight(self,f)
        snapshot=Path(match['snapshot']['snapshot_path'])
        snapshot.write_text(
            f"Event ID: {match['bwin_event_id']}\nCaptura: {(helpers.NOW-timedelta(minutes=10)).isoformat()}\nAcordeones: 3 | Selecciones: 3\n"
            "MERCADO: Home 1 - Total de goles | Tiempo reglamentario\nMás de 1,5 : 1.50\n"
            "MERCADO: Total de goles | Tiempo reglamentario\nMás de 2,5 : 1.80\n"
            "MERCADO: Jugador - Total de tiros | Tiempo reglamentario\nMás de 1,5 : 2.00\n")
        doc=t.read(path)
        doc['matches'][0]['snapshot'].update(sha256=t.sha(snapshot),selection_triplets_verified=3)
        t.dump(path,doc)
        x=analyze_fixture(f,helpers.NOW,{})
        result=market_links(f,x['signals'],self.base/'enriched/preselection.json',[path],helpers.NOW,120)
        self.assertGreater(result['direct_team_market_hypotheses'],0)
        self.assertGreater(result['indirect_team_to_match_total_hypotheses'],0)
        self.assertEqual(result['player_market_requires_exposure_and_xi'],1)
        links=[m for signal in x['signals'] for m in signal['markets']]
        self.assertIn('DIRECT_TEAM_MARKET',{link['association_category'] for link in links})
        self.assertIn('INDIRECT_TEAM_TO_MATCH_TOTAL',{link['association_category'] for link in links})
        self.assertTrue(all(link['execution_status']=='MARKET_VERIFIED_NO_CALIBRATED_MODEL' for link in links))

    def test_strong_candidate_without_market_stays_promising_not_discarded(self):
        f=fixture();x=analyze_fixture(f,NOW,observations())
        x['market_validation']={'verified_preflights':0,'block_reasons':[]}
        rows=selection_rows([f],{str(f['fixture_id']):x},{},NOW)
        self.assertFalse(rows[0]['eligible'])
        self.assertEqual(rows[0]['evaluation_completeness'],'CANDIDATO_PROMETEDOR_NO_INVESTIGADO')
        self.assertGreater(rows[0]['tactical_research_score'],0)
        self.assertEqual(rows[0]['market_execution_readiness'],10)  # only time is knowable

    def test_three_candidates_are_ranked_without_quote_count(self):
        fs=[];payload={};audits=[]
        for i in range(3):
            f=dict(fixture(),fixture_id=910000+i);x=analyze_fixture(f,NOW,observations())
            x['market_validation']={'verified_preflights':1,'block_reasons':[],
                'direct_team_market_hypotheses':1,'market_verified_no_calibrated_model':1,
                'usable_verified_market_selections':1}
            for signal in x['signals']:
                if signal['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL':
                    signal['markets']=[{'association_category':'DIRECT_TEAM_MARKET','capture_madrid':NOW.isoformat()}]
            fs.append(f);payload[str(f['fixture_id'])]=x;audits.append({'fixture_id':f['fixture_id'],'status':'VERIFIED'})
        rows=selection_rows(fs,payload,{'audits':audits},NOW)
        self.assertEqual(len(rows),3);self.assertTrue(all(row['eligible'] for row in rows))
        self.assertTrue(all(row['market_execution_readiness']==100 for row in rows))


class AuditPackageV2Tests(unittest.TestCase):
    def test_audit_package_manifest_and_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);base=root/'window/prospective_trial';run=base/'daily_runs/run';run.mkdir(parents=True)
            f={'fixture_id':1,'home':'Home','away':'Away','kickoff_madrid':(NOW+timedelta(hours=2)).isoformat()}
            detail=run/'tactical/fixtures/1.json';t.dump(detail,{'fixture_id':1,'home':'Home','away':'Away','signals':[],
                'signal_count':0,'signal_families':[],'contradictions':[],'coverage_summary':{},'detail_json':str(detail)})
            preflight=run/'preflight/1/market_preflight.json';discovery=run/'discovery.json';t.dump(discovery,{'matches':[]})
            snapshot=run/'cuotas.txt';snapshot.write_text('Event ID: 7\n')
            match={'fixture_id':1,'bwin_event_id':'7','snapshot':{'snapshot_path':str(snapshot),'capture_declared_madrid':NOW.isoformat(),'selection_triplets_verified':1}}
            t.dump(preflight,{'source_discovery':str(discovery),'matches':[match]})
            t.dump(run/'handoff_prematch.json',{'status':'NO_VALID_CANDIDATE'})
            t.dump(run/'candidate_ledger.json',{'fixtures':[]})
            tactical={'fixtures':{'1':t.read(detail)},'ranking':[{'fixture_id':1,'research_priority':50,'tactical_research_score':60,
                'market_execution_readiness':40,'direct_market_hypotheses':0,'indirect_market_hypotheses':0,'unmodelled_market_count':0,
                'player_market_requires_exposure_and_xi':0}]}
            scanner=run/'opportunity_scan.json';t.dump(scanner,{'audits':[{'fixture_id':1,'status':'VERIFIED'}]})
            args=SimpleNamespace(max_scrapes=3,evaluation_budget=15,discovery_budget=15,bwin_top=5)
            out,status,zip_path=write_comparison_audit(root,base,run,tactical,[(f,match,preflight)],
                {'comparison_status':'COMPARISON_INCOMPLETE'},scanner,args)
            self.assertEqual(status,'COMPARISON_INCOMPLETE')
            self.assertTrue((out/'MANIFEST.json').is_file());self.assertTrue(zip_path.is_file())
            manifest=json.loads((out/'MANIFEST.json').read_text())
            self.assertTrue(any(row['path']=='COMPARATIVA_PREMATCH_3_PARTIDOS.md' for row in manifest['artifacts']))

    def test_daily_csv_retains_162_unique_rows_with_duplicate_discovery(self):
        from prematch_daily_csv import export_daily
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'2026-11-12_1500_0300';base=folder/'prospective_trial'
            fixtures=[{'fixture_id': i, 'home': f'Synthetic {i}', 'away':'Synthetic Away',
                       'kickoff_madrid':'2026-11-12T18:00:00+01:00','status':'NS','season':2026} for i in range(162)]
            t.dump(folder/'fixtures.json',{'fixtures':fixtures+fixtures[:5]})
            export_daily(base)
            with (base/'partidos_diarios.csv').open(encoding='utf-8-sig',newline='') as handle:
                rows=list(csv.DictReader(handle,delimiter=';'))
        self.assertEqual(len(rows),162)
        self.assertEqual(len({row['fixture_id'] for row in rows}),162)


if __name__=='__main__':
    unittest.main()
