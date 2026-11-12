import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from prematch_llm_research import research_packet,validate_hypotheses,write_pending_runtime,run_iterative_research,SEGMENT


class LLMResearchTests(unittest.TestCase):
    def setUp(self):
        self.cut=datetime(2026,11,12,tzinfo=timezone.utc)
        segment=dict(zip(SEGMENT,['TEAM',1,None,3,2026,'M','CLUB','FULL_TIME']))
        self.rows=[dict(segment,fixture_id=i+1,kickoff_utc=(self.cut-timedelta(days=60-i)).isoformat(),
            available_at_utc=(self.cut-timedelta(days=59-i)).isoformat(),
            provenance=[{'fetched_at_utc':(self.cut-timedelta(days=1)).isoformat()}],
            metrics={'own.corners':8 if i%2 else 1},conditions={'venue':'HOME' if i%2 else 'AWAY'}) for i in range(40)]
        self.targets=[{'fixture_id':99,'kickoff_madrid':(self.cut+timedelta(days=1)).isoformat()}]
        self.packet,self.sealed=research_packet(self.rows,self.targets,self.cut)
        self.h={'hypothesis_id':'synthetic-test','segment':segment,'mechanism':'Synthetic venue association',
                'falsifier':'Holdout reverses or fails to reproduce association','condition':{'field':'venue','operator':'EQ','value':'HOME'},
                'outcome':'own.corners','threshold':5,'evidence_fixture_ids':[2]}

    def test_chronological_holdout_not_in_llm_packet_and_python_tests_both(self):
        train=self.packet['training_observations'];hold=self.sealed['holdout']
        self.assertLess(max(r['kickoff_utc'] for r in train),min(r['kickoff_utc'] for r in hold))
        self.assertNotIn('holdout',self.packet)
        d=validate_hypotheses(self.packet,self.sealed,[self.h])
        self.assertEqual(d['results'][0]['status'],'ACCEPTED_TEMPORAL_ASSOCIATION')
        self.assertFalse(d['results'][0]['predictive_use_authorized'])
        self.assertFalse(d['runtime_execution_verified_by_this_validator'])

    def test_unknown_variables_and_holdout_evidence_rejected_but_retained(self):
        a=copy.deepcopy(self.h);a['hypothesis_id']='bad-variable';a['outcome']='invented.pressure'
        b=copy.deepcopy(self.h);b['hypothesis_id']='leak';b['evidence_fixture_ids']=[40]
        d=validate_hypotheses(self.packet,self.sealed,[a,b])
        self.assertEqual(len(d['results']),2)
        self.assertTrue(all(r['status']=='REJECTED' for r in d['results']))

    def test_postcutoff_sources_and_packet_tampering_block(self):
        rows=copy.deepcopy(self.rows);rows[0]['provenance'][0]['fetched_at_utc']=(self.cut+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):research_packet(rows,self.targets,self.cut)
        self.packet['training_observations'][0]['metrics']['own.corners']=999
        with self.assertRaises(ValueError):validate_hypotheses(self.packet,self.sealed,[self.h])

    def test_unavailable_runtime_never_fabricates_hypotheses_or_final_ranking(self):
        with tempfile.TemporaryDirectory() as td:
            write_pending_runtime(td,self.packet,[{'fixture_id':99}])
            hypotheses=json.loads((Path(td)/'llm_pattern_hypotheses.json').read_text())
            ranking=json.loads((Path(td)/'ranking_enriched.json').read_text())
            self.assertEqual(hypotheses['hypotheses'],[])
            self.assertEqual(ranking['status'],'PENDING_LLM_RESEARCH_NOT_FINAL')
            self.assertEqual(ranking['base_ranking'][0]['fixture_id'],99)

    def test_shared_fixture_never_leaks_across_entities(self):
        extra=copy.deepcopy(self.rows[0]);extra.update(entity_type='PLAYER',player_id=23)
        latest=copy.deepcopy(self.rows[-1]);latest.update(entity_type='PLAYER',player_id=23)
        packet,sealed=research_packet(self.rows+[extra,latest],self.targets,self.cut)
        self.assertFalse({r['fixture_id'] for r in packet['training_observations']} &
                         {r['fixture_id'] for r in sealed['holdout']})

    def test_loop_keeps_duplicate_and_stops_without_exposing_holdout(self):
        calls=[]
        def adapter(request):
            calls.append(request)
            self.assertNotIn('holdout',request['packet'])
            self.assertNotIn('validation',request)
            proposal=copy.deepcopy(self.h);proposal['hypothesis_id']=str(len(calls))
            return {'hypotheses':[proposal],'runtime_receipt':{'test_adapter':True},'continue_research':True}
        artifacts=run_iterative_research(self.packet,self.sealed,[{'fixture_id':99}],adapter,'UNIT_TEST_ONLY')
        registry=artifacts['pattern_registry.json']
        self.assertEqual(registry['stop_reason'],'NO_NEW_RELATION')
        self.assertEqual(registry['iterations_executed'],2)
        self.assertEqual(len(registry['rules']),2)
        self.assertEqual(registry['rules'][1]['reason'],'DUPLICATE_VARIABLES')
        self.assertFalse(artifacts['ranking_enriched.json']['ranking'][0]['actionable_bet'])

    def test_loop_requires_actual_adapter_and_preserves_all_targets(self):
        with self.assertRaises(ValueError):run_iterative_research(self.packet,self.sealed,[],None,None)
        with self.assertRaises(ValueError):run_iterative_research(self.packet,self.sealed,[],lambda r: {},'TEST')

    def test_iteration_limit_is_enforced(self):
        packet,sealed=research_packet(self.rows,self.targets,self.cut,max_iterations=1)
        response={'hypotheses':[self.h],'runtime_receipt':{'test_adapter':True},'continue_research':True}
        result=run_iterative_research(packet,sealed,[{'fixture_id':99}],lambda r: response,'UNIT_TEST_ONLY')
        self.assertEqual(result['pattern_registry.json']['iterations_executed'],1)
        self.assertEqual(result['pattern_registry.json']['stop_reason'],'ITERATION_LIMIT')

    def test_conjunctions_preserve_missing_values_and_reject_outcome_leakage(self):
        from prematch_llm_research import condition_rows
        rows=copy.deepcopy(self.rows)
        for row in rows:row['metrics']['own.shots']=10
        rows[0]['metrics']['own.shots']=None
        condition={'all':[{'field':'venue','operator':'EQ','value':'HOME'},
                          {'field':'own.shots','operator':'GT','value':5}]}
        transformed,compiled=condition_rows(rows,condition,'own.corners')
        self.assertIsNone(transformed[0]['conditions'][compiled['field']])
        self.assertTrue(transformed[1]['conditions'][compiled['field']])
        condition['all'][1]['field']='own.corners'
        with self.assertRaises(ValueError):condition_rows(rows,condition,'own.corners')


if __name__=='__main__':unittest.main()
