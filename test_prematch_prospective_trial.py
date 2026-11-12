"""Pruebas offline del lanzador. No consulta Bwin/API-Football."""
import unittest
import argparse
import json
from datetime import datetime, date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from prematch_prospective_trial import (
    allowed_window, kickoff_valid, parse_date, select_fixtures, window_path,
    read, dump, sha, TrialError, parse_clock, local_window,
    load_window_config, resolve_window, parse_date_relative, main, DEFAULT_WINDOW_CONFIG
    , enrichment_resume_plan, merge_enrichment_batches
)

TZ=ZoneInfo('Europe/Madrid')
NOW=datetime(2026,11,1,23,8,tzinfo=TZ)

def fixture(fid,home,away,kickoff,region='EUROPE',status='NS'):
    return {'fixture_id':fid,'home':home,'away':away,
            'kickoff_madrid':kickoff,'region':region,'status':status}

class TrialTests(unittest.TestCase):
    def setUp(self):
        self.doc={'schema_version':'1.0-major-leagues','fixtures':[
            fixture(1,'Toronto','Atlanta','2026-11-01T22:30:00+01:00'),
            fixture(2,'Sassuolo','Fiorentina','2026-11-02T18:30:00+01:00'),
            fixture(3,'Atalanta','Parma','2026-11-02T20:45:00+01:00'),
            fixture(4,'Newcastle','Everton','2026-11-02T21:00:00+01:00'),
            fixture(5,'Other','Other2','2026-11-02T17:00:00+01:00','ASIA'),
            fixture(6,'Lorient','Brest','2026-11-02T20:45:00+01:00',status='1H')]}
    def test_past_rejected(self):
        self.assertFalse(kickoff_valid(self.doc['fixtures'][0],NOW,60))
    def test_future_ok(self):
        self.assertTrue(kickoff_valid(self.doc['fixtures'][1],NOW,60))
    def test_region_priority(self):
        self.assertEqual([x['fixture_id'] for x in select_fixtures(self.doc,[],5,NOW,60)],[2,3,4,5])
    def test_team_filter(self):
        self.assertEqual([x['fixture_id'] for x in select_fixtures(self.doc,['parma'],5,NOW,60)],[3])
    def test_no_old_match_with_keyword(self):
        self.assertEqual(select_fixtures(self.doc,['Atlanta'],5,NOW,60),[])
    def test_top_limit(self):
        self.assertEqual(len(select_fixtures(self.doc,[],2,NOW,60)),2)
    def test_bad_schema(self):
        with self.assertRaises(TrialError):select_fixtures({'fixtures':[]},[],1,NOW,60)
    def test_dead_window(self):
        with self.assertRaises(TrialError):allowed_window(date(2026,10,31),NOW)
    def test_live_window_open(self):
        allowed_window(date(2026,11,2),NOW)
    def test_day_parse(self):
        self.assertEqual(parse_date('2026-11-02'),date(2026,11,2))
    def test_output_folder(self):
        self.assertEqual(str(window_path(Path('/tmp/root'),date(2026,11,2))), '/tmp/root/outputs/discovery/2026-11-02_1500_0300')
    def test_custom_window_midnight(self):
        begin,end=local_window(date(2026,11,1),time(16),time(3))
        self.assertEqual(begin.isoformat(),'2026-11-01T16:00:00+01:00')
        self.assertEqual(end.isoformat(),'2026-11-02T03:00:00+01:00')
    def test_custom_folder_matching_discover(self):
        p=window_path(Path('/tmp/root'),date(2026,11,1),time(16),time(3))
        self.assertEqual(p.name,'2026-11-01_1600_0300')
    def test_night_window_still_open(self):
        allowed_window(date(2026,11,1),NOW,time(16),time(3))
    def test_night_window_closed_monday_at_three(self):
        with self.assertRaises(TrialError):
            allowed_window(date(2026,11,1),datetime(2026,11,2,3,tzinfo=TZ),time(16),time(3))
    def test_clock_parser(self):
        self.assertEqual(parse_clock('03:00'),time(3))
        with self.assertRaises(argparse.ArgumentTypeError):parse_clock('25:10')
        with self.assertRaises(argparse.ArgumentTypeError):parse_clock('3:00')
    def test_window_stays_on_same_day_if_end_later(self):
        _begin,end=local_window(date(2026,11,2),time(16),time(19))
        self.assertEqual(end.isoformat(),'2026-11-02T19:00:00+01:00')
    def test_roundtrip(self):
        with TemporaryDirectory() as td:
            p=Path(td)/'a.json';dump(p,self.doc)
            self.assertEqual(read(p),self.doc)
            self.assertEqual(len(sha(p)),64)


class ConfigProfileTests(unittest.TestCase):
    def test_default_profile(self):
        name,start,end=resolve_window(DEFAULT_WINDOW_CONFIG)
        self.assertEqual((name,start,end),('normal',time(15),time(3)))
    def test_extended_profile(self):
        name,start,end=resolve_window(DEFAULT_WINDOW_CONFIG,'ampliada')
        self.assertEqual((name,start,end),('ampliada',time(15),time(3)))
    def test_override_end(self):
        name,start,end=resolve_window(DEFAULT_WINDOW_CONFIG,'normal',end_override=time(4))
        self.assertEqual((name,start,end),('normal',time(15),time(4)))
    def test_override_start(self):
        _,start,end=resolve_window(DEFAULT_WINDOW_CONFIG,'ampliada',start_override=time(19))
        self.assertEqual((start,end),(time(19),time(3)))
    def test_unknown_profile(self):
        with self.assertRaises(TrialError): resolve_window(DEFAULT_WINDOW_CONFIG,'inexistente')
    def test_relative_date(self):
        d=date(2026,11,1)
        self.assertEqual(parse_date_relative('hoy',d),d)
        self.assertEqual(parse_date_relative('mañana',d),date(2026,11,2))
        self.assertEqual(parse_date_relative('manana',d),date(2026,11,2))
        self.assertEqual(parse_date_relative('2026-11-03',d),date(2026,11,3))
    def test_config_file(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            cfg=root/'config/prematch_windows.json'
            dump(cfg,DEFAULT_WINDOW_CONFIG)
            x,used,path=load_window_config(root)
            self.assertTrue(used)
            self.assertEqual(path,cfg)
            self.assertEqual(resolve_window(x,'ampliada')[2],time(3))
    def test_no_file_uses_bundled_default(self):
        with TemporaryDirectory() as td:
            x,used,_=load_window_config(Path(td))
            self.assertFalse(used)
            self.assertEqual(resolve_window(x)[2],time(3))
    def test_missing_explicit_config_fails(self):
        with TemporaryDirectory() as td:
            with self.assertRaises(TrialError):load_window_config(Path(td),Path('config/noexiste.json'))
    def test_bad_schema_fails(self):
        with TemporaryDirectory() as td:
            root=Path(td);dump(root/'config/prematch_windows.json',{'schema_version':'wrong'})
            with self.assertRaises(TrialError):load_window_config(root)
    def test_invalid_clock_in_config_fails(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            x=dict(DEFAULT_WINDOW_CONFIG,profiles={'normal':{'start':'16:00','end':'99:00'}})
            dump(root/'config/prematch_windows.json',x)
            with self.assertRaises(TrialError):load_window_config(root)
    def test_unlisted_default_fails(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            x=dict(DEFAULT_WINDOW_CONFIG,default_profile='no_existe')
            dump(root/'config/prematch_windows.json',x)
            with self.assertRaises(TrialError):load_window_config(root)
    def test_invalid_timezone_fails(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            x=dict(DEFAULT_WINDOW_CONFIG,timezone='UTC')
            dump(root/'config/prematch_windows.json',x)
            with self.assertRaises(TrialError):load_window_config(root)
    def test_show_config_no_producers_required(self):
        with TemporaryDirectory() as td:
            self.assertEqual(main(['--root',td,'--date','2026-11-01','--window','ampliada','--show-config']),0)
    def test_show_config_from_installed_file(self):
        with TemporaryDirectory() as td:
            root=Path(td);dump(root/'config/prematch_windows.json',DEFAULT_WINDOW_CONFIG)
            self.assertEqual(main(['--root',td,'--date','2026-11-01','--window','normal','--show-config']),0)
    def test_profile_ends_night_next_day(self):
        _,start,end=resolve_window(DEFAULT_WINDOW_CONFIG,'ampliada')
        _,finish=local_window(date(2026,11,1),start,end)
        self.assertEqual(finish.isoformat(),'2026-11-02T03:00:00+01:00')
    def test_prepared_scan_window_different_folders(self):
        root=Path('/tmp/root')
        a=window_path(root,date(2026,11,1),time(16),time(2))
        b=window_path(root,date(2026,11,1),time(16),time(3))
        self.assertNotEqual(a,b)


class EnrichmentResumeTests(unittest.TestCase):
    def args(self, **changes):
        values=dict(max_matches=5,history=20,details=8,team=[],evaluation_budget=0,
                    horizon_hours=0,min_lead=45)
        values.update(changes)
        return SimpleNamespace(**values)

    def fixture_rows(self, count=10):
        return [{'fixture_id':i,'home':f'H{i}','away':f'A{i}',
                 'kickoff_madrid':'2026-11-08T18:00:00+01:00'} for i in range(1,count+1)]

    def write_batch_result(self, pending, args, *, api_errors=None, empty=False):
        rows=[] if empty else [{**row,'data_readiness_score':1.0,'research_priority_score':1.0,
            'preliminary_coverage_score':1.0,'region_priority':0} for row in pending['fixtures']]
        doc={'schema_version':'1.3-preselection','generated_at_utc':'2026-11-08T00:00:00+00:00',
             'source_filter_sha256':sha(pending['input']),
             'counts':{'input_filter':len(pending['fixture_ids'])},
             'methodology':{'sample_history_cap':args.history,'sample_details_cap':args.details},
             'api_errors':api_errors or {},'matches':rows}
        dump(pending['dest']/'preselection.json',doc)

    def test_interrupted_batches_resume_without_repeating_completed(self):
        with TemporaryDirectory() as td:
            base=Path(td);subset={'schema_version':'1.0-major-leagues','fixtures':self.fixture_rows()}
            dump(base/'fixtures_major_test.json',subset);args=self.args()
            first=enrichment_resume_plan(base,subset,args)
            self.assertEqual((len(first['pending']),first['checkpoint']['counts']['pending_fixtures']),(2,10))
            self.write_batch_result(first['pending'][0],args)
            second=enrichment_resume_plan(base,subset,args)
            completed=base/'enrichment_batches/1/preselection.json'
            before=(sha(completed),completed.stat().st_mtime_ns)
            self.assertEqual((second['checkpoint']['counts']['completed_batches'],
                              second['checkpoint']['counts']['pending_fixtures']),(1,5))
            checkpoint=base/'enrichment_resume.json'
            checkpoint_mtime=checkpoint.stat().st_mtime_ns
            third=enrichment_resume_plan(base,subset,args)
            self.assertEqual(before,(sha(completed),completed.stat().st_mtime_ns))
            self.assertEqual(checkpoint.stat().st_mtime_ns,checkpoint_mtime)
            self.assertEqual([x['index'] for x in third['pending']],[2])

    def test_checkpoint_blocks_parameter_change_and_tampering(self):
        with TemporaryDirectory() as td:
            base=Path(td);subset={'schema_version':'1.0-major-leagues','fixtures':self.fixture_rows(5)}
            dump(base/'fixtures_major_test.json',subset);args=self.args()
            plan=enrichment_resume_plan(base,subset,args)
            self.write_batch_result(plan['pending'][0],args)
            enrichment_resume_plan(base,subset,args)
            with self.assertRaisesRegex(TrialError,'PARAMETERS_MISMATCH'):
                enrichment_resume_plan(base,subset,self.args(details=7))
            result=base/'enrichment_batches/1/preselection.json'
            result.write_text(result.read_text()+'\n')
            with self.assertRaisesRegex(TrialError,'BATCH_RESULT_SHA_MISMATCH'):
                enrichment_resume_plan(base,subset,args)

    def test_incomplete_json_is_archived_and_batch_becomes_resumable(self):
        with TemporaryDirectory() as td:
            base=Path(td);subset={'schema_version':'1.0-major-leagues','fixtures':self.fixture_rows(5)}
            dump(base/'fixtures_major_test.json',subset);args=self.args()
            pending=enrichment_resume_plan(base,subset,args)['pending'][0]
            target=pending['dest']/'preselection.json';target.write_text('{')
            before=target.read_bytes()
            plan=enrichment_resume_plan(base,subset,args)
            self.assertFalse(target.exists())
            self.assertEqual(plan['checkpoint']['counts']['pending_batches'],1)
            archived=plan['checkpoint']['batches'][0]['interrupted_artifacts'][0]
            self.assertEqual(Path(archived['path']).read_bytes(),before)
            self.assertEqual(sha(Path(archived['path'])),archived['sha256'])
            self.write_batch_result(plan['pending'][0],args)
            recovered=enrichment_resume_plan(base,subset,args)
            self.assertEqual(recovered['checkpoint']['counts']['completed_batches'],1)
            self.assertEqual(recovered['checkpoint']['counts']['pending_batches'],0)

    def test_api_error_result_is_reused_but_flagged(self):
        with TemporaryDirectory() as td:
            base=Path(td);subset={'schema_version':'1.0-major-leagues','fixtures':self.fixture_rows(5)}
            dump(base/'fixtures_major_test.json',subset);args=self.args()
            pending=enrichment_resume_plan(base,subset,args)['pending'][0]
            self.write_batch_result(pending,args,api_errors={'detail_batch_0':'EMPTY_RESPONSE'})
            plan=enrichment_resume_plan(base,subset,args)
            self.assertEqual(plan['checkpoint']['batches'][0]['status'],'COMPLETED_WITH_API_ERRORS')
            self.assertEqual(plan['checkpoint']['counts']['pending_batches'],0)

    def test_candidate_ledger_repeated_state_is_idempotent(self):
        from prematch_audit import CandidateLedger
        with TemporaryDirectory() as td:
            path=Path(td)/'candidate_ledger.json';row=self.fixture_rows(1)[0]
            ledger=CandidateLedger(path,{'test':True})
            ledger.record(row,'PENDIENTE_BWIN','preselection','READY')
            ledger.record(row,'PENDIENTE_BWIN','preselection','READY')
            ledger.write()
            loaded=CandidateLedger.load(path)
            self.assertEqual(len(loaded.rows['1']['history']),1)

    def test_real_scale_482_case_recovers_58_batches_then_completes(self):
        with TemporaryDirectory() as td:
            base=Path(td);subset={'schema_version':'1.0-major-leagues','fixtures':self.fixture_rows(482)}
            source=base/'fixtures_major_test.json';dump(source,subset);args=self.args()
            first=enrichment_resume_plan(base,subset,args)
            for pending in first['pending'][:58]:self.write_batch_result(pending,args)
            interrupted=enrichment_resume_plan(base,subset,args)
            self.assertEqual(interrupted['checkpoint']['counts'],{
                'fixtures':482,'batches':97,'completed_batches':58,'completed_fixtures':290,
                'pending_batches':39,'pending_fixtures':192})
            completed_hashes={row['index']:row['result_sha256'] for row in interrupted['checkpoint']['batches'][:58]}
            for pending in interrupted['pending']:self.write_batch_result(pending,args)
            finished=enrichment_resume_plan(base,subset,args)
            self.assertEqual((finished['checkpoint']['counts']['completed_fixtures'],
                              finished['checkpoint']['counts']['pending_fixtures']),(482,0))
            self.assertEqual(completed_hashes,{row['index']:row['result_sha256'] for row in finished['checkpoint']['batches'][:58]})
            merged=merge_enrichment_batches(finished['completed_docs'],finished['completed_sources'],source)
            self.assertEqual(len(merged['matches']),482)
            self.assertEqual(len({row['fixture_id'] for row in merged['matches']}),482)

if __name__=='__main__':unittest.main()
