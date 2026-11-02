#!/usr/bin/env python3
"""Pruebas offline con fixtures sintéticos, sin API, navegador ni scraping."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime
from pathlib import Path

from select_single_match import (audit_preflight_match, prepare_rows, read_json,
                                 sha256, bwin_id_from_url, invoke_preflight_missing)

NOW=datetime.fromisoformat('2026-11-01T19:00:00+01:00')
KICK='2026-11-01T22:30:00+01:00'


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stat={'schema_version':'1.3-preselection', 'matches':[
          {'fixture_id':101,'home':'Atlanta United FC','away':'Toronto FC','gender':'M',
           'kickoff_madrid':KICK,'details_checked':True,'research_priority_score':91,
           'data_readiness_score':100,'stability_score':71,'league':'MLS'},
          {'fixture_id':102,'home':'Nashville SC','away':'New York Red Bulls','gender':'M',
           'kickoff_madrid':KICK,'details_checked':True,'research_priority_score':89,
           'data_readiness_score':99,'stability_score':67,'league':'MLS'}]}
        self.disc={'generated_at_utc':'2026-11-01T18:00:00+00:00', 'matches':[
          {'fixture_id':101,'home':'Atlanta United FC','away':'Toronto FC','gender':'M',
           'kickoff_madrid':KICK,'status':'EVENT_FOUND_NEEDS_TIME_CONFIRMATION',
           'bwin_event_id':'1111111','bwin_url':'https://www.bwin.es/es/sports/eventos/atlanta-toronto-2:1111111'},
          {'fixture_id':102,'home':'Nashville SC','away':'New York Red Bulls','gender':'M',
           'kickoff_madrid':KICK,'status':'EVENT_FOUND_NEEDS_TIME_CONFIRMATION',
           'bwin_event_id':'2222222','bwin_url':'https://www.bwin.es/es/sports/eventos/nashville-new-york-2:2222222'}]}
        self.ps=self.root/'preselection.json';self.ds=self.root/'discovery.json'
        self.ps.write_text(json.dumps(self.stat)); self.ds.write_text(json.dumps(self.disc))
        self.stat['_path']=self.ps;self.disc['_path']=self.ds
        self.pf1=self.make_preflight(101,1111111)
        self.pf2=self.make_preflight(102,2222222)

    def make_preflight(self,fixture,event):
        s=next(x for x in self.stat['matches'] if x['fixture_id']==fixture)
        d=next(x for x in self.disc['matches'] if x['fixture_id']==fixture)
        p=self.root/f'odds_{fixture}.txt';p.write_text(f'Event ID: {event}\nMERCADO: Total de córners\nMás de 8,5 : 1.40\n')
        pf={'source_sha256':sha256(self.ds),'source_preselection_sha256':sha256(self.ps),
          'prematch_only':True,'min_odd_candidate':'1.20', 'matches':[{**{k:s[k] for k in ['fixture_id','home','away','kickoff_madrid']},
            'bwin_event_id':str(event),'bwin_url':d['bwin_url'],
            'status':'HISTORICAL_MARKET_TRIPLES_EXTRACTED',
            'time_evidence':{'status':'DISPLAY_TIME_CORROBORATED','difference_minutes':0.0},
            'snapshot':{'event_id_verified_in_txt':True,'status':'MARKET_TRIPLES_EXTRACTED',
              'capture_declared_madrid':'2026-11-01T18:44:13+01:00',
              'snapshot_path':str(p),'sha256':sha256(p),'snapshot_bytes':p.stat().st_size,
              'selection_triplets_verified':25},
            'quality_gate':{'join_status':'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF','minimum_candidate_odd':'1.20',
              'researchable_after_data_gate':10,'price_eligible':20,
              'market_kinds':{'corners':{'researchable':4},'total_goals':{'researchable':6}}}
           }]}
        pp=self.root/f'preflight_{fixture}.json';pp.write_text(json.dumps(pf))
        return (pp,pf)

    def evaluate(self,*reports,now=NOW,max_age=120,lead=45):
        return prepare_rows(self.stat,self.disc,list(reports),now,lead,max_age,180,2)

    def test_url_extract(self):
        self.assertEqual(bwin_id_from_url('https://www.bwin.es/es/sports/eventos/mi-partido-2:12345?market=-1'),'12345')
        self.assertIsNone(bwin_id_from_url('https://evil.example/es/sports/eventos/mi-partido-2:12345'))

    def test_one_report_is_only_provisional(self):
        status,winner,rows,meta=self.evaluate(self.pf1)
        self.assertEqual(status,'PROVISIONAL_INSUFFICIENT_COMPARISON')
        self.assertEqual(winner['fixture_id'],101)
        self.assertEqual(meta['qualified_matches'],1)
        self.assertEqual(next(x for x in rows if x['fixture_id']==102)['status'],'NEEDS_MARKET_PREFLIGHT')

    def test_two_reports_select_one(self):
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2)
        self.assertEqual(status,'SELECTED');self.assertEqual(winner['fixture_id'],101)
        self.assertEqual(len([r for r in rows if r['status']=='QUALIFIED_FOR_COMPARISON']),2)

    def test_stale_snapshot_is_blocked(self):
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2,now=datetime.fromisoformat('2026-11-01T21:00:00+01:00'))
        self.assertEqual(status,'NO_SELECTION')
        self.assertIn('SNAPSHOT_TOO_OLD',rows[0]['block_reasons'])

    def test_wrong_sha_is_blocked(self):
        self.pf2[1]['matches'][0]['snapshot']['sha256']='0'*64
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2)
        self.assertEqual(status,'PROVISIONAL_INSUFFICIENT_COMPARISON')
        self.assertIn('SNAPSHOT_SHA_MISMATCH',next(x for x in rows if x['fixture_id']==102)['block_reasons'])

    def test_conflicting_ids_is_blocked(self):
        conflicting=copy.deepcopy(self.pf1[1]);conflicting['matches'][0]['bwin_event_id']='9999999'
        status,winner,rows,meta=self.evaluate(self.pf1,(self.root/'other.json',conflicting),self.pf2)
        self.assertEqual(status,'PROVISIONAL_INSUFFICIENT_COMPARISON')
        self.assertIn('CONFLICTING_PREFLIGHT_EVENT_IDS',next(x for x in rows if x['fixture_id']==101)['block_reasons'])

    def test_wrong_preselection_hash_is_blocked(self):
        self.pf2[1]['source_preselection_sha256']='bad'
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2)
        self.assertIn('PRESELECTION_SHA_MISMATCH',next(x for x in rows if x['fixture_id']==102)['block_reasons'])

    def test_time_conflict_is_blocked(self):
        self.pf2[1]['matches'][0]['time_evidence']['difference_minutes']=30
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2)
        self.assertIn('TIME_NOT_CORROBORATED',next(x for x in rows if x['fixture_id']==102)['block_reasons'])

    def test_no_price_quality_is_blocked(self):
        self.pf2[1]['matches'][0]['quality_gate']['market_kinds']={}
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2)
        self.assertIn('NO_RESEARCHABLE_MARKET_FAMILY',next(x for x in rows if x['fixture_id']==102)['block_reasons'])

    def test_past_kickoff_never_selected(self):
        status,winner,rows,meta=self.evaluate(self.pf1,self.pf2,now=datetime.fromisoformat('2026-11-02T01:00:00+01:00'))
        self.assertEqual(status,'NO_SELECTION')
        self.assertEqual(len(rows),0)

    def test_auto_capture_only_missing_and_explicit(self):
        fake=self.root/'fake_preflight.py'
        fake.write_text(
            'import argparse, shutil\n'
            'from pathlib import Path\n'
            'p=argparse.ArgumentParser()\n'
            "p.add_argument('--fixture-id')\n"
            "p.add_argument('--output-dir')\n"
            "p.add_argument('--run-scraper',action='store_true')\n"
            'a,_=p.parse_known_args()\n'
            "assert a.run_scraper and a.fixture_id=='102'\n"
            "folder=Path(a.output_dir);folder.mkdir(parents=True,exist_ok=True)\n"
            f"shutil.copyfile({str(self.pf2[0])!r},folder/'market_preflight.json')\n"
        )
        args=SimpleNamespace(auto_preflight_missing=1,preflight_script=fake,
            preselection=self.ps,discovery=self.ds,root=self.root,
            output_dir=self.root/'auto',min_lead_minutes=45,max_snapshot_age_minutes=120,
            max_discovery_age_minutes=180,scraper_timeout_seconds=10,
            min_candidate_odd=__import__('decimal').Decimal('1.20'))
        files=invoke_preflight_missing(args,self.stat,self.disc,NOW,[self.pf1])
        self.assertEqual(len(files),1)
        self.assertIn('102',str(files[0]))
        self.assertTrue(files[0].is_file())

    def test_cli_replay_cannot_create_actionable_file(self):
        out=self.root/'out'
        command=[sys.executable,str(Path(__file__).with_name('select_single_match.py')),
                 '--preselection',str(self.ps),'--discovery',str(self.ds),
                 '--preflight',str(self.pf1[0]),'--preflight',str(self.pf2[0]),
                 '--asof','2026-11-01T19:00:00+01:00','--output-dir',str(out)]
        proc=subprocess.run(command, capture_output=True,text=True)
        self.assertEqual(proc.returncode,3,proc.stderr)
        self.assertFalse((out/'selected_match.json').exists())
        payload=json.loads((out/'selection_decision.json').read_text())
        self.assertEqual(payload['status'],'HISTORICAL_REPLAY_SELECTED_NOT_ACTIONABLE')

    def test_auto_scrape_rejects_simulated_time(self):
        command=[sys.executable,str(Path(__file__).with_name('select_single_match.py')),
                 '--preselection',str(self.ps),'--discovery',str(self.ds),
                 '--asof','2026-11-01T19:00:00+01:00','--auto-preflight-missing','1']
        proc=subprocess.run(command,capture_output=True,text=True)
        self.assertNotEqual(proc.returncode,0)
        self.assertIn('no admite --asof',proc.stderr)

if __name__=='__main__':unittest.main()
