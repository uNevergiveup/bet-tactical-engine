"""Offline P0 regressions. Provider calls are mocks; no browser is started."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import generate_context as gc
import prematch_case as case
import prematch_single as single
import test_prematch_final_cycle as final_cycle
from test_prematch_case import CanonicalCaseTests, dump


class PinnedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = {'fixture_id':1570018, 'kickoff':datetime.fromisoformat('2026-11-15T15:15:00+00:00'),
                    'selection':{'home_api_id':715,'away_api_id':534,'league_id':141,'season':2026,'gender':'M'}}
        self.row = {'fixture':{'id':1570018,'date':'2026-11-15T15:15:00+00:00','status':{'short':'NS'}},
            'league':{'id':141,'name':'Segunda División','country':'Spain','season':2026},
            'teams':{'home':{'id':715,'name':'Granada CF'},'away':{'id':534,'name':'Las Palmas'}}}
        self.match = gc.BwinMatch(url='https://www.bwin.es/es/sports/eventos/granada-ud-las-palmas-2:7909230',
            event_id=7909230,home_name_raw='Granada LaLiga 2',away_name_raw='UD Las Palmas LaLiga 2',
            home_name='Granada LaLiga 2',away_name='Las Palmas LaLiga 2',home_gender='M',away_gender='M')
        self.api=Mock(); self.api.get.return_value={'response':[self.row]}

    def resolve(self):
        with patch.object(gc,'fixture_temporal_eligibility',return_value=(True,'FIXTURE_AND_BWIN_TIME_VERIFIED','OK',{})):
            return gc.resolve_selected_fixture(self.match,self.api,self.ctx)

    def test_granada_715_not_22018_even_with_contaminated_labels(self):
        home,away=self.resolve()
        self.assertEqual((home.id,away.id),(715,534))
        self.api.get.assert_called_once_with('fixtures',{'id':1570018},use_cache=False)
        self.assertEqual(self.match.verified_fixture_id,1570018)

    def test_wrong_provider_team_is_terminal(self):
        self.row['teams']['home']['id']=22018
        with self.assertRaisesRegex(ValueError,'TEAM_ORDER_CONFLICT'):self.resolve()

    def test_reversed_teams_are_rejected(self):
        self.row['teams']['home'],self.row['teams']['away']=self.row['teams']['away'],self.row['teams']['home']
        with self.assertRaisesRegex(ValueError,'TEAM_ORDER_CONFLICT'):self.resolve()

    def test_season_and_competition_conflicts(self):
        for field in ('id','season'):
            original=self.row['league'][field];self.row['league'][field]=999
            with self.assertRaisesRegex(ValueError,'COMPETITION_CONFLICT'):self.resolve()
            self.row['league'][field]=original

    def test_gender_conflict(self):
        self.ctx['selection']['gender']='F'
        with self.assertRaisesRegex(ValueError,'CATEGORY_CONFLICT'):self.resolve()

    def test_time_conflict(self):
        self.row['fixture']['date']='2026-11-15T18:15:00+00:00'
        with self.assertRaisesRegex(ValueError,'KICKOFF_CONFLICT'):self.resolve()

    def test_live_status_gate_is_not_bypassed(self):
        with patch.object(gc,'fixture_temporal_eligibility',return_value=(False,'BLOCK','ALREADY_STARTED',{})):
            with self.assertRaisesRegex(ValueError,'TEMPORAL_BLOCK'):
                gc.resolve_selected_fixture(self.match,self.api,self.ctx)


class ManualTransitionTests(CanonicalCaseTests):
    def test_real_manual_validation_chain_and_canonical_clock(self):
        from datetime import timedelta
        from prematch_multi import ensure_event_operation_window
        research,row=self.manual_inputs()
        kickoff=row['kickoff_madrid']; stamp=datetime.now(timezone.utc).isoformat()
        pre=Path(case.read(research)['sources']['preselection']['path'])
        fixture={**row,'home_id':1,'away_id':2,'league_id':239,'season':2026,
                 'details_checked':True,'gender':'M'}
        dump(pre,{'matches':[fixture]})
        doc=case.read(research);doc['sources']['preselection']=case.reference(pre);dump(research,doc)
        discovery=self.root/'fresh_discovery.json'
        match={**fixture,'status':'IDENTITY_VERIFIED_AUTOMATICALLY','identity_status':'NAME_AND_ABSOLUTE_TIME_MATCH',
            'inspection':{'title_teams_match':True,'event_id_from_url':self.event,
                         'event_start_machine_readable':kickoff,'observed_at_utc':stamp}}
        dump(discovery,{'generated_at_utc':stamp,'user_time_reference':'Europe/Madrid','matches':[match],'catalog_observed':[]})
        preflight=self.root/'fresh_preflight.json'
        dump(preflight,{'source_discovery':str(discovery),'source_sha256':case.sha(discovery),
            'source_preselection_sha256':case.sha(pre),'prematch_only':True,'min_odd_candidate':'1.20',
            'matches':[{**fixture,'status':'MARKET_TRIPLES_EXTRACTED',
             'time_evidence':{'status':'ABSOLUTE_TIME_VERIFIED','source':'INSPECTED_EVENT_JSON_LD','observed_at_utc':stamp,'difference_minutes':0.0},
             'snapshot':{'event_id_verified_in_txt':True,'status':'MARKET_TRIPLES_EXTRACTED',
                'snapshot_path':str(self.odds),'sha256':case.sha(self.odds),'snapshot_bytes':self.odds.stat().st_size,
                'capture_declared_madrid':stamp,'selection_triplets_verified':1},
             'quality_gate':{'join_status':'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF','minimum_candidate_odd':'1.20',
                'price_eligible':1,'researchable_after_data_gate':1,'market_kinds':{'total_goals':{'researchable':1}}}}]})
        selected=case.promote_research(self.root,research,discovery,preflight,self.event)
        ctx=single.load_context(__import__('types').SimpleNamespace(root=self.root,selected=selected,decision=None))
        manifest=single.match_manifest(ctx)
        ensure_event_operation_window(self.root,self.event,manifest,min_lead=45)
        self.assertEqual(single.validate_temporal_identity_evidence(ctx)['fixture_id'],self.fixture)
        self.assertEqual(manifest['status'],'ERROR_CONTEXTO')
        self.assertFalse(case.read(self.directory/'case.json')['actionable_bet'])

    def test_T45_failure_preserves_research_and_writes_no_case(self):
        research,row=self.manual_inputs();before=research.read_bytes()
        with patch('select_single_match.prepare_rows',return_value=('SELECTED',row,[row],{})), \
             patch.object(single,'validate_temporal_identity_evidence',return_value={}), \
             patch.object(single,'freshness',side_effect=ValueError('T45')):
            with self.assertRaisesRegex(ValueError,'T45'):
                case.promote_research(self.root,research,self.handoff,self.handoff,self.event)
        self.assertEqual(research.read_bytes(),before)
        self.assertFalse((self.directory/'case.json').exists())

    def manual_inputs(self):
        selection=json.loads(self.selected.read_text())
        pre=self.root/'pre.json'
        dump(pre,{'matches':[{'fixture_id':self.fixture,'home_id':1,'away_id':2,'league_id':239,'season':2026}]})
        research=self.root/'research_selection.json'
        dump(research,{'schema_version':'1.0-research-selection','status':'SELECTED',
             'selected':{'fixture_id':self.fixture,'bwin_event_id':self.event},
             'sources':{'preselection':case.reference(pre)}})
        selection['market_families_ready']=['goals'];selection['status']='QUALIFIED_FOR_COMPARISON'
        return research,selection

    def test_explicit_transition_adopts_without_synthetic_handoff(self):
        research,row=self.manual_inputs()
        with patch('select_single_match.prepare_rows',return_value=('SELECTED',row,[row],{})), \
             patch.object(single,'validate_temporal_identity_evidence',return_value={}):
            selected=case.promote_research(self.root,research,self.handoff,self.handoff,self.event)
        manifest=json.loads((self.directory/'manifest.json').read_text())
        self.assertEqual(manifest['status'],'ERROR_CONTEXTO')
        self.assertEqual(manifest['steps']['scraper'],0)
        self.assertTrue(selected.is_file())
        self.assertFalse((self.root/'handoff_prematch.json').exists())

    def test_wrong_explicit_event_never_creates_case(self):
        research,_=self.manual_inputs()
        with self.assertRaisesRegex(ValueError,'MANUAL_EVENT_RESEARCH_CONFLICT'):
            case.promote_research(self.root,research,self.handoff,self.handoff,'7909230')
        self.assertFalse((self.directory/'case.json').exists())

    def test_bad_temporal_contract_never_creates_case(self):
        research,row=self.manual_inputs()
        with patch('select_single_match.prepare_rows',return_value=('SELECTED',row,[row],{})), \
             patch.object(single,'validate_temporal_identity_evidence',side_effect=ValueError('BAD_CONTRACT')):
            with self.assertRaisesRegex(ValueError,'BAD_CONTRACT'):
                case.promote_research(self.root,research,self.handoff,self.handoff,self.event)
        self.assertFalse((self.directory/'case.json').exists())


class PriceDeltaTests(unittest.TestCase):
    def test_exact_contract_delta_does_not_infer_suspension(self):
        before=[{'market':'Total FT','selection':'Over 2.5','odd':2.0},
                {'market':'Total FT','selection':'Under 2.5','odd':1.8}]
        after=[{'market':'Total FT','selection':'Over 2.5','odd':2.1},
               {'market':'Total 2H','selection':'Over 2.5','odd':4.0}]
        with patch('prematch_opportunity_scanner.parse_bwin_txt',side_effect=[(before,{}),(after,{})]):
            result=single.odds_changes(Path('old'),Path('new'),'7909230')
        self.assertEqual(len(result['added']),1)
        self.assertEqual(len(result['not_observed_in_new_capture']),1)
        self.assertEqual(len(result['price_changes']),1)
        self.assertEqual(result['suspension_status'],'NOT_INFERRED_FROM_ABSENCE')


class LineupAcquisitionTests(CanonicalCaseTests):
    def test_absent_XI_keeps_context_and_archives_acquisition(self):
        from types import SimpleNamespace
        ctx={'directory':self.directory,'fixture_id':self.fixture,'event_id':self.event,
             'home':'Home','away':'Away','url':self.url,
             'kickoff':datetime.now(timezone.utc),
             'selection':{'home_api_id':1,'away_api_id':2}}
        payload={'get':'fixtures/lineups','parameters':{'fixture':self.fixture},'response':[]}
        api=Mock();api.get.return_value=payload
        with patch.object(single,'ensure_prematch'), \
             patch.object(single,'match_manifest',return_value={'inputs':{'context':{},'context_json':{},'identity':{}}}), \
             patch('generate_context.APIFootballClient',return_value=api), \
             patch('generate_context.API_KEY','offline-test'), \
             patch('generate_context.resolve_selected_fixture'), \
             patch.object(single,'update_state') as update, \
             patch.object(single,'cmd_refresh_context') as refresh:
            single.cmd_acquire_lineups(ctx,SimpleNamespace(min_refresh_lead_minutes=5))
        api.get.assert_called_once_with('fixtures/lineups',{'fixture':self.fixture},use_cache=False)
        self.assertIn(update.call_args.args[2], {'XI_ABSENT_OR_INCOMPLETE', 'LINEUPS_PENDING', 'LINEUPS_UNAVAILABLE_FINAL'})
        refresh.assert_not_called()
        self.assertEqual(len(list((self.directory/'context_acquisitions').glob('*.json'))),1)


class P0FinalCycleTests(final_cycle.FinalCycleTests):
    """Reuse the real offline cycle with the event-local executable path.

    The legacy fixture passes ../prematch_multi.py against a temporary root;
    that path points outside its own placeholders. Do not alter that test file.
    """
    def setUp(self):
        super().setUp()
        original = single.ensure_root
        def event_local_root(ctx, path):
            return original(ctx, Path('prematch_multi.py') if path == Path('../prematch_multi.py') else path)
        patcher = patch.object(single, 'ensure_root', side_effect=event_local_root)
        patcher.start(); self.addCleanup(patcher.stop)

    def finish(self):
        from argparse import Namespace
        return single.cmd_finish(self.ctx, Namespace(min_finish_lead_minutes=5,
            historical=False, multi=Path('prematch_multi.py'),
            max_odds_age_minutes=15, report=self.report))
