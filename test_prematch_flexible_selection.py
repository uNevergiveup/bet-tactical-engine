"""A/B/C research routing with real local TXT/SHA gates; no external services."""
import copy
import csv
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import prematch_daily as daily
import prematch_release as release
import prematch_runtime as runtime
import prematch_single as single
import prematch_prospective_trial as trial
from prematch_tactical_signal_engine import analyze_fixture, selection_rows
import test_prematch_daily as helpers
from test_prematch_tactical_signal_engine import fixture, observations, NOW


class TierTests(unittest.TestCase):
    def ranked(self, kinds):
        fixtures=[];payload={};audits=[]
        for i,kind in enumerate(kinds):
            f=dict(fixture(),fixture_id=900100+i)
            x=analyze_fixture(f,NOW,observations(12 if kind!='C' else 2))
            x['market_validation']={'verified_preflights':1,'block_reasons':[],
                'usable_verified_market_selections':1,'direct_team_market_hypotheses':1}
            if kind.startswith('B'):
                for s in x['signals']:
                    s['markets']=[]
                    if kind=='B_low':s['priority_score']=max(0,s['priority_score']-20)
            elif kind=='A':
                for s in x['signals']:
                    if s['status']=='SUPPORTED_DESCRIPTIVE_SIGNAL':
                        s['markets']=[{'association_category':'DIRECT_TEAM_MARKET','capture_madrid':NOW.isoformat()}]
            fixtures.append(f);payload[str(f['fixture_id'])]=x
            audits.append({'fixture_id':f['fixture_id'],'status':'VERIFIED'})
        return selection_rows(fixtures,payload,{'audits':audits},NOW)

    def test_a_wins_over_b(self):
        rows=self.ranked(['B','A'])
        self.assertEqual([r['selection_tier'] for r in rows],['A','B'])
        self.assertTrue(rows[0]['eligible'])

    def test_best_b_selected_without_a_and_no_bet(self):
        rows=self.ranked(['B','B'])
        self.assertEqual(rows[0]['selection_tier'],'B')
        self.assertTrue(rows[0]['research_eligible'])
        self.assertFalse(rows[0]['eligible'])
        self.assertIn('NO_VERIFIED_MARKET_FOR_SUPPORTED_SIGNAL',rows[0]['research_missing_evidence'])
        self.assertEqual(rows[0]['hard_block_reasons'],[])
        self.assertEqual([r['fixture_id'] for r in rows],[900100,900101])

    def test_higher_conditional_evidence_wins_within_b(self):
        rows=self.ranked(['B_low','B'])
        self.assertEqual(rows[0]['fixture_id'],900101)
        self.assertGreater(rows[0]['conditional_research_score'],rows[1]['conditional_research_score'])

    def test_without_a_or_b_all_c(self):
        rows=self.ranked(['C'])
        self.assertEqual(rows[0]['selection_tier'],'C')
        self.assertFalse(rows[0]['research_eligible'])
        self.assertIn('INSUFFICIENT_HISTORICAL_RESEARCH_EVIDENCE',rows[0]['hard_block_reasons'])

    def test_started_and_invalid_cutoff_are_c(self):
        f=fixture();f['kickoff_madrid']=(NOW-timedelta(minutes=1)).isoformat()
        x=analyze_fixture(f,NOW,observations())
        x['market_validation']={'verified_preflights':1,'usable_verified_market_selections':1,'block_reasons':[]}
        row=selection_rows([f],{str(f['fixture_id']):x},{'audits':[{'fixture_id':f['fixture_id'],'status':'VERIFIED'}]},NOW)[0]
        self.assertEqual(row['selection_tier'],'C')
        self.assertIn('MATCH_STARTED',row['hard_block_reasons'])
        f=fixture();f['analysis_cutoff_utc']=(NOW+timedelta(minutes=1)).isoformat()
        x=analyze_fixture(f,NOW,observations())
        x['market_validation']={'verified_preflights':1,'usable_verified_market_selections':1,'block_reasons':[]}
        row=selection_rows([f],{str(f['fixture_id']):x},{'audits':[{'fixture_id':f['fixture_id'],'status':'VERIFIED'}]},NOW)[0]
        self.assertIn('INVALID_ANALYSIS_CUTOFF',row['hard_block_reasons'])

    def test_unverified_snapshot_and_no_usable_market_are_c(self):
        f=fixture();x=analyze_fixture(f,NOW,observations())
        x['market_validation']={'verified_preflights':0,'usable_verified_market_selections':0,'block_reasons':['SNAPSHOT_SHA256_MISMATCH']}
        row=selection_rows([f],{str(f['fixture_id']):x},{'audits':[{'fixture_id':f['fixture_id'],'status':'VERIFIED'}]},NOW)[0]
        self.assertEqual(row['selection_tier'],'C')
        self.assertIn('MARKET_SOURCE_INTEGRITY_ERROR',row['hard_block_reasons'])
        self.assertIn('NO_USABLE_VERIFIED_MARKET',row['hard_block_reasons'])


class IntegrationTests(unittest.TestCase):
    setUp=helpers.PipelineTests.setUp
    preflight=helpers.PipelineTests.preflight
    fake_run=helpers.PipelineTests.fake_run

    def setup_b(self, fixture_row):
        fixture_row.update(home_id=10,away_id=20,league_id=100,season=2026,league='Test league')
        for side in ('home','away'):
            hist=fixture_row[side+'_evidence']['history']
            hist['same_competition_goals_for']['cv']=1.2
            hist['same_competition_goals_against']['cv']=1.2
        trial.dump(self.base/'fixtures_major_test.json',{'fixtures':[fixture_row]})
        trial.dump(self.base/'enriched/preselection.json',{'generated_at_utc':helpers.NOW.isoformat(),
            'matches':[fixture_row],'source_filter_sha256':trial.sha(self.base/'fixtures_major_test.json')})
        pf,match=self.preflight(fixture_row)
        doc=trial.read(pf);doc['matches'][0]['quality_gate']['researchable_after_data_gate']=0
        trial.dump(pf,doc)
        with (pf.parent/'market_research_shortlist.csv').open('w',newline='') as h:
            csv.writer(h).writerow(['fixture_id','bwin_event_id','market','selection','odd','market_kind','data_status'])
        return pf,match

    def scan_b(self):
        with patch.object(trial,'run',side_effect=self.fake_run):
            return runtime.scan(self.root,self.folder,date(2026,11,1),self.args)

    def test_b_handoff_bridge_and_single_check_same_ids(self):
        self.setup_b(self.fixtures[0])
        doc=self.scan_b()
        self.assertEqual(doc['status'],'READY_FOR_MANUAL_RESEARCH')
        self.assertEqual(doc['selection_tier'],'B')
        self.assertFalse(doc['eligible']);self.assertFalse(doc['actionable_bet'])
        path=self.base/'handoff_prematch.json'
        self.assertEqual(daily.review(path,self.root),0)
        selected=release.bridge(self.root,path)
        choice=trial.read(selected);decision=trial.read(selected.parent/'selection_decision.json')
        self.assertEqual(choice['selection_tier'],'B')
        self.assertEqual(choice['fixture_id'],doc['fixture_id'])
        self.assertEqual(choice['bwin_event_id'],doc['bwin_event_id'])
        self.assertEqual(choice['odds_sha256'],trial.sha(Path(choice['odds_txt'])))
        self.assertEqual(single.validate_selected(choice,decision)['selection_tier'],'B')
        with patch.object(single,'local_now',return_value=helpers.NOW):
            self.assertEqual(single.main(['--root',str(self.root),'--selected',str(selected),'check']),0)
        wrong=copy.deepcopy(choice);wrong['bwin_event_id']='7999999'
        with self.assertRaisesRegex(ValueError,'contradice|Inconsistencia'):
            single.validate_selected(wrong,decision)

    def test_b_corrupt_snapshot_blocks_before_handoff(self):
        pf,match=self.setup_b(self.fixtures[0])
        Path(match['snapshot']['snapshot_path']).write_text('corrupted')
        with self.assertRaises(trial.TrialError):
            runtime.verified_preflight(self.root,pf,self.fixtures[0],self.base/'enriched/preselection.json',self.args)

    def test_b_identity_conflict_blocks_before_handoff(self):
        pf,_=self.setup_b(self.fixtures[0])
        doc=trial.read(pf);doc['matches'][0]['away']='Other team';trial.dump(pf,doc)
        with self.assertRaises(trial.TrialError):
            runtime.verified_preflight(self.root,pf,self.fixtures[0],self.base/'enriched/preselection.json',self.args)

    def test_old_selection_does_not_contaminate_new_event(self):
        with patch.object(runtime,'stamp',side_effect=['first_run','second_run']):
            self.setup_b(self.fixtures[0]);first=self.scan_b()
            first_path=self.base/'handoff_prematch.json'
            first_choice=release.bridge(self.root,first_path)
            self.assertEqual(trial.read(first_choice)['bwin_event_id'],first['bwin_event_id'])
            second=self.fixtures[1]
            self.setup_b(second)
            self.args.max_scrapes=1
            fresh=self.scan_b()
            self.assertEqual(fresh['fixture_id'],second['fixture_id'])
            second_choice=release.bridge(self.root,self.base/'handoff_prematch.json')
        self.assertNotEqual(trial.read(second_choice)['bwin_event_id'],trial.read(first_choice)['bwin_event_id'])
        self.assertNotEqual(first_choice.parent,second_choice.parent)
        self.assertEqual(release.selected_context(self.root,first_choice)['event_id'],first['bwin_event_id'])
        self.assertEqual(release.selected_context(self.root,second_choice)['event_id'],fresh['bwin_event_id'])

    def test_no_researchable_candidate_keeps_insufficient_evidence(self):
        self.setup_b(self.fixtures[0])
        for side in ('home','away'):
            self.fixtures[0][side+'_evidence']['history']['same_competition_goals_for']['n']=2
            self.fixtures[0][side+'_evidence']['history']['same_competition_goals_against']['n']=2
        trial.dump(self.base/'fixtures_major_test.json',{'fixtures':[self.fixtures[0]]})
        trial.dump(self.base/'enriched/preselection.json',{'generated_at_utc':helpers.NOW.isoformat(),
            'matches':[self.fixtures[0]],'source_filter_sha256':trial.sha(self.base/'fixtures_major_test.json')})
        doc=self.scan_b()
        self.assertEqual(doc['status'],'INSUFFICIENT_EVIDENCE')
        self.assertNotIn('fixture_id',doc)

    def test_single_cli_preserves_operation_safety_floors(self):
        for option,value in (('--min-lead-minutes','44'),('--min-refresh-lead-minutes','4'),
                             ('--min-finish-lead-minutes','4')):
            with self.assertRaises(SystemExit) as caught:
                single.main(['--root',str(self.root),'--selected','missing.json',option,value,'check'])
            self.assertEqual(caught.exception.code,2)


if __name__=='__main__':unittest.main()
