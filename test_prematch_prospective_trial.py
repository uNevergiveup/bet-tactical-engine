"""Pruebas offline del lanzador. No consulta Bwin/API-Football."""
import unittest
import argparse
from datetime import datetime, date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
from prematch_prospective_trial import (
    allowed_window, kickoff_valid, parse_date, select_fixtures, window_path,
    read, dump, sha, TrialError, parse_clock, local_window,
    load_window_config, resolve_window, parse_date_relative, main, DEFAULT_WINDOW_CONFIG
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
        self.assertEqual(str(window_path(Path('/tmp/root'),date(2026,11,2))), '/tmp/root/outputs/discovery/2026-11-02_1600_0200')
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
    def test_no_file_preserves_previous_default(self):
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

if __name__=='__main__':unittest.main()
