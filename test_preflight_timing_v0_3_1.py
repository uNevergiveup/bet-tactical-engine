import unittest
from datetime import datetime,timedelta,timezone
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

path=Path(__file__).with_name('bwin_market_preflight.py')
spec=spec_from_file_location('preflight_new', path)
mod=module_from_spec(spec); spec.loader.exec_module(mod)
T0=datetime.fromisoformat('2026-11-01T23:58:00+01:00')
KO='2026-11-02T01:00:00+01:00'
OBS='2026-11-01T22:58:00+00:00'
EV='7898001'
URL='https://www.bwin.es/es/sports/eventos/new-england-revolution-inter-miami-2:7898001'

def records():
 m={'fixture_id':1490598,'home':'New England Revolution','away':'Inter Miami','status':'EVENT_FOUND_TIME_CORROBORATED',
    'identity_status':'NAME_AND_RELATIVE_TIME_CORROBORATED','bwin_event_id':EV,'bwin_url':URL,'kickoff_madrid':KO,
    'inspection': {'title_teams_match': True,'event_id_from_url':EV,
       'time_evidence_type':'RELATIVE_CATALOG_COUNTDOWN','countdown_observed_at_utc':OBS,
       'event_start_from_countdown':'2026-11-02T01:00:00+01:00','countdown_minutes':62,
       'countdown_difference_minutes':0}}
 cat=[{'bwin_event_id':EV,'bwin_url':URL,'text':'New England Revolution Inter Miami 02:00 restante',
       'text_observed_at_utc':OBS}]
 return m,cat

class PreflightTest(unittest.TestCase):
 def test_accepts_tied_countdown(self):
  m,c=records(); r=mod.validate_match(m,c,T0,45,180)
  self.assertEqual(r['status'],'READY_FOR_SCRAPER')
  self.assertEqual(r['time_evidence']['status'],'COUNTDOWN_TIME_CORROBORATED')
 def test_rejects_other_catalog_event(self):
  m,c=records();c[0]['bwin_event_id']='0000000';r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('NO_TIMESTAMPED_CATALOG_EVIDENCE',r['reasons'])
 def test_rejects_stale_countdown(self):
  m,c=records();r=mod.validate_match(m,c,T0+timedelta(hours=4),45,180)
  self.assertIn('DISCOVERY_SNAPSHOT_TOO_OLD',r['reasons'])
 def test_rejects_false_title(self):
  m,c=records();m['inspection']['title_teams_match']=False
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('PAGE_TITLE_TEAMS_NOT_MATCHED',r['reasons'])
  self.assertIn('KICKOFF_NOT_CORROBORATED',r['reasons'])
 def test_rejects_wrong_id(self):
  m,c=records();m['inspection']['event_id_from_url']='0000'
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('KICKOFF_NOT_CORROBORATED',r['reasons'])
 def test_rejects_wrong_minute_diff(self):
  m,c=records();m['inspection']['event_start_from_countdown']='2026-11-02T01:09:00+01:00'
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('KICKOFF_NOT_CORROBORATED',r['reasons'])
 def test_rejects_future_capture(self):
  m,c=records();m['inspection']['countdown_observed_at_utc']='2026-11-01T23:10:00+00:00'
  c[0]['text_observed_at_utc']=m['inspection']['countdown_observed_at_utc']
  m['inspection']['countdown_minutes']=50
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('CATALOG_TIMESTAMP_FROM_FUTURE',r['reasons'])
 def test_rejects_not_corrob_identity(self):
  m,c=records();m['identity_status']='NAME_MATCH_TIME_NOT_MACHINE_VERIFIED'
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('KICKOFF_NOT_CORROBORATED',r['reasons'])
 def test_rejects_conflicting_catalog_visible_time(self):
  m,c=records();c[0]['text']='New England Revolution Inter Miami Hoy / 02:00'
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('BWIN_API_KICKOFF_CONFLICT',r['reasons'])
 def test_accepts_existing_absolute_timestamp(self):
  m,c=records();m['identity_status']='NAME_AND_ABSOLUTE_TIME_MATCH';m['status']='IDENTITY_VERIFIED_AUTOMATICALLY'
  m['inspection']['event_start_machine_readable']='2026-11-02T01:00:00+01:00'
  m['inspection']['observed_at_utc']=OBS
  r=mod.validate_match(m,[],T0,45,180)
  self.assertEqual(r['status'],'READY_FOR_SCRAPER')
 def test_existing_visible_catalog(self):
  m,c=records();m['identity_status']='NAME_MATCH_TIME_NOT_MACHINE_VERIFIED';m['status']='EVENT_FOUND_NEEDS_TIME_CONFIRMATION'
  c[0]['text']='New England Revolution Inter Miami Mañana / 01:00'
  r=mod.validate_match(m,c,T0,45,180)
  self.assertEqual(r['status'],'READY_FOR_SCRAPER')
 def test_rejects_inconsistent_countdown_arithmetic(self):
  m,c=records();m['inspection']['countdown_minutes']=1
  r=mod.validate_match(m,c,T0,45,180)
  self.assertIn('KICKOFF_NOT_CORROBORATED',r['reasons'])
 def test_absolute_label_without_evidence_is_rejected(self):
  m,c=records();m['identity_status']='NAME_AND_ABSOLUTE_TIME_MATCH'
  r=mod.validate_match(m,[],T0,45,180)
  self.assertIn('KICKOFF_NOT_CORROBORATED',r['reasons'])
 def test_rejects_too_close_kickoff(self):
  m,c=records();r=mod.validate_match(m,c,datetime.fromisoformat('2026-11-02T00:45:01+01:00'),45,180)
  self.assertIn('ALREADY_STARTED_OR_INSUFFICIENT_PREMATCH_MARGIN',r['reasons'])

if __name__=='__main__':unittest.main()
