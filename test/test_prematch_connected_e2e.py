"""One connected offline replay; only acquisition boundaries are substituted."""
import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import bwin_discovery as discovery
import prematch_case as case
import prematch_multi as multi
import prematch_pattern_engine as patterns
import prematch_release as release
import prematch_single as single
import test_p0_repair as repair


class ConnectedReplay(repair.P0FinalCycleTests):
    def finish(self):
        result=super().finish()
        cache=json.loads((self.event/'probabilistic/evidence_cache_7000001.json').read_text())
        self.cache_runs=getattr(self,'cache_runs',[])+[cache]
        return result

    def test_connected_replay(self):
        # The inherited synthetic fixture supplies provider response bytes, not
        # an accepted context. Remove its bootstrap manifest before adoption.
        self.now -= timedelta(hours=1)
        self.event.joinpath('manifest.json').unlink()
        provider_txt=(self.event/'inputs/match_engine_7000001.txt').read_bytes()
        provider_json=(self.event/'inputs/match_engine_7000001.json').read_bytes()
        trace=[]
        owner=self
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return owner.now.astimezone(tz) if tz else owner.now.replace(tzinfo=None)
        clock=patch.object(case,'datetime',Clock);clock.start();self.addCleanup(clock.stop)
        stamp=self.now.isoformat();kick=self.kickoff.astimezone(single.TZ).isoformat()
        fixture={'fixture_id':99,'home':'Synthetic Home','away':'Synthetic Away','home_id':10,'away_id':20,
            'league_id':3,'league':'UEFA Europa League','country':'World','season':2026,'gender':'M',
            'kickoff_madrid':kick,'details_checked':True,'research_priority_score':80,
            'data_readiness_score':90,'stability_score':70}
        history={'same_competition_goals_for':{'n':8,'mean':2,'values':[2]*8},
                 'same_competition_goals_against':{'n':8,'mean':1,'values':[1]*8}}
        fixture.update(home_evidence={'history':history},away_evidence={'history':history})
        pre=self.root/'outputs/discovery/run/statistics/preselection.json';pre.parent.mkdir(parents=True)
        pre.write_text(json.dumps({'generated_at_utc':stamp,'matches':[fixture]}))
        chosen,skipped=discovery.read_candidates(pre,[99],0,False,self.now)
        self.assertEqual(len(chosen),1);self.assertFalse(skipped);trace.append('discovery_snapshot_replayed')
        historical_hash=case.sha(pre)
        rank=patterns.analyze(pre,[],[99],0,self.now,45)
        self.assertEqual(rank['counts']['fixtures_processed'],1);trace.append('ranking')
        self.write_odds(2.0,self.now)
        d=self.root/'outputs/discovery/run/bwin.json'
        match={**fixture,'status':'IDENTITY_VERIFIED_AUTOMATICALLY','identity_status':'NAME_AND_ABSOLUTE_TIME_MATCH',
            'bwin_event_id':'7000001','bwin_url':self.url,'inspection':{'title_teams_match':True,
            'event_id_from_url':'7000001','event_start_machine_readable':kick,'observed_at_utc':stamp}}
        d.write_text(json.dumps({'generated_at_utc':stamp,'user_time_reference':'Europe/Madrid','matches':[match],'catalog_observed':[]}))
        pf=self.root/'outputs/discovery/run/preflight.json'
        pf.write_text(json.dumps({'source_discovery':str(d),'source_sha256':case.sha(d),
            'source_preselection_sha256':case.sha(pre),'prematch_only':True,'min_odd_candidate':'1.20',
            'matches':[{**match,'status':'MARKET_TRIPLES_EXTRACTED','time_evidence':{
                'status':'ABSOLUTE_TIME_VERIFIED','source':'INSPECTED_EVENT_JSON_LD','observed_at_utc':stamp,'difference_minutes':0.0},
                'snapshot':{'event_id_verified_in_txt':True,'status':'MARKET_TRIPLES_EXTRACTED',
                    'snapshot_path':str(self.odds),'sha256':case.sha(self.odds),'snapshot_bytes':self.odds.stat().st_size,
                    'capture_declared_madrid':stamp,'selection_triplets_verified':2},
                'quality_gate':{'join_status':'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF','minimum_candidate_odd':'1.20',
                    'price_eligible':2,'researchable_after_data_gate':1,'market_kinds':{'total_goals':{'researchable':1}}}}]}))
        research=self.root/'outputs/discovery/run/research_selection.json'
        research.write_text(json.dumps({'schema_version':'1.0-research-selection','status':'SELECTED',
            'selected':{'fixture_id':99,'bwin_event_id':'7000001'},'sources':{'preselection':case.reference(pre)}}))
        with self.assertRaisesRegex(ValueError,'MANUAL_EVENT_RESEARCH_CONFLICT'):
            case.promote_research(self.root,research,d,pf,'7999999')
        with self.assertRaises(FileNotFoundError):
            case.promote_research(self.root,research,d,self.root/'missing.json','7000001')
        before=self.now;self.now=self.kickoff-timedelta(minutes=44)
        with self.assertRaisesRegex(ValueError,'MANUAL_TRANSITION_BLOCKED'):
            case.promote_research(self.root,research,d,pf,'7000001')
        self.now=before
        trace += ['wrong_event_blocked','missing_file_blocked','T45_blocked']
        self.selected=case.promote_research(self.root,research,d,pf,'7000001')
        self.ctx=single.load_context(argparse.Namespace(root=self.root,selected=self.selected,decision=None))
        trace += ['explicit_selection','promote_research','canonical_case_context_pending']
        self.assertEqual(single.match_manifest(self.ctx)['status'],'ERROR_CONTEXTO')
        def acquisition(root,event,stage,command,env=None):
            if stage!='contexto': return self.execute(root,event,stage,command)
            self.assertEqual(event,'7000001')
            self.assertEqual(env['PREMATCH_TEMPORAL_IDENTITY_SELECTED'],str(self.selected))
            stagefile=self.event/'provider_replay/match_engine_7000001.txt'
            stagefile.parent.mkdir();stagefile.write_bytes(provider_txt)
            stagefile.with_suffix('.json').write_bytes(provider_json)
            logs=self.event/'logs';logs.mkdir(exist_ok=True)
            (logs/'contexto.log').write_text('[OK] TXT generado: '+str(stagefile)+'\n')
            return 0
        with patch.object(multi,'execute',side_effect=acquisition):
            self.assertTrue(multi.retry_context_one(self.root,'7000001','unused',self.root/'budget.json',180,3,
                                                   temporal_identity_selected=self.selected))
        trace.append('context_acquired_and_identity_bound')
        # Run the actual validator/motor, XI delta, quote refresh and evidence
        # cache assertions from the same instance and the same canonical case.
        self.now += timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, 'Cuotas antiguas'):
            single.snapshot_info(self.odds,self.ctx,max_age=15)
        single.cmd_refresh(self.ctx,argparse.Namespace(min_refresh_lead_minutes=5,
            multi=Path('prematch_multi.py'),max_odds_age_minutes=15))
        trace.append('stale_price_block_then_permitted_refresh')
        self.test_real_finish_context_delta_odds_refresh_and_final_review()
        trace += ['validation','codex_input_binding','XI_delta','selective_recalculation',
                  'odds_refresh','price_only_evidence_reuse','revalidation','codex_preparation']
        self.assertEqual(case.sha(pre),historical_hash)
        self.assertEqual(self.executed,['validacion','motor']*3)
        cache=json.loads((self.event/'probabilistic/evidence_cache_7000001.json').read_text())
        self.assertEqual(cache['recalculated'],0);self.assertGreater(cache['reused'],0)
        self.assertEqual(self.cache_runs[1]['recalculated'],1)
        self.assertEqual(self.cache_runs[1]['reused'],1)
        before=self.now;self.now=self.kickoff-timedelta(minutes=4)
        with self.assertRaises(ValueError):
            single.ensure_prematch(self.ctx,min_lead=5)
        self.now=self.kickoff+timedelta(seconds=1)
        with self.assertRaises(ValueError): single.ensure_prematch(self.ctx,min_lead=0)
        self.now=before
        trace += ['T5_blocked','postkickoff_blocked']
        trace.append('historical_source_hash_unchanged')
        out=os.environ.get('PREMATCH_E2E_EVIDENCE_DIR')
        if out:
            folder=Path(out);folder.mkdir(parents=True,exist_ok=True)
            import shutil
            for src,name in [(self.event/'CODEX_7000001.md','synthetic_codex_prompt.md'),
                             (self.event/'validation/validacion_7000001.json','synthetic_validation.json')]:
                shutil.copy2(src,folder/name)
            (folder/'connected_replay.json').write_text(json.dumps({'status':'PASS','synthetic':True,
                'trace':trace,'external_calls':0,'discovery_replays':1,'cache_after_price_refresh':cache,
                'cache_runs':self.cache_runs,'artifact_note':'Copied evidence only; source temp paths expire and are not an operational case',
                'history_hash':historical_hash,'limitations':['Provider acquisition is replayed, not online verified',
                'General injuries/coach delta not covered'],'actionable_bet':False},indent=2))
