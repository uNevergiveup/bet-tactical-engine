import unittest,copy,json,tempfile
from pathlib import Path
from datetime import datetime,timezone
from prematch_phase1 import reconcile_fixture_schedules,merge_bwin_snapshots
import bwin_discovery as b
class ReconciliationTests(unittest.TestCase):
 def setUp(self):
  self.begin=datetime.fromisoformat('2026-11-11T15:00:00+01:00');self.end=datetime.fromisoformat('2026-11-12T03:00:00+01:00');self.now=datetime.fromisoformat('2026-11-11T16:00:00+00:00')
  self.f=dict(fixture_id=1638339,home_id=15495,away_id=17575,home='SL Benfica W',away='Austria Wien W',gender='F',league_id=525,season=2026,kickoff_madrid='2026-11-11T21:00:00+01:00',kickoff_utc='2026-11-11T20:00:00+00:00',status='NS')
  self.r=dict(fixture=dict(id=1638339,date='2026-11-12T20:00:00Z',status={'short':'NS'}),teams=dict(home={'id':15495,'name':'SL Benfica W'},away={'id':17575,'name':'Austria Wien W'}),league=dict(id=525,season=2026))
 def run_case(self,rows):return reconcile_fixture_schedules({'fixtures':[self.f]},rows,self.begin,self.end,self.now)
 def test_reschedule_preserves_identity_original_and_histories(self):
  old=copy.deepcopy(self.f);d,a=self.run_case([self.r]);self.assertEqual(a[0]['reason'],'RESCHEDULED_OUT_OF_WINDOW');self.assertEqual(d['fixtures'][0]['kickoff_madrid'],'2026-11-12T21:00:00+01:00');self.assertEqual(old,self.f);self.assertEqual(d['fixtures'][0]['home_id'],old['home_id'])
 def test_ambiguous_response_blocks(self):self.assertEqual(self.run_case([self.r,self.r])[1][0]['status'],'SCHEDULE_UNVERIFIED')
 def test_wrong_gender_and_teams_block(self):
  for field,value in [('name','SL Benfica'),('name','SL Benfica W U19'),('name','SL Benfica W Reserves'),('id',1)]:
   r=copy.deepcopy(self.r);r['teams']['home'][field]=value;self.assertEqual(self.run_case([r])[1][0]['status'],'IDENTITY_CONFLICT')
 def test_unchanged_and_resumed_no_change(self):
  self.r['fixture']['date']=self.f['kickoff_utc'];a=self.run_case([self.r]);self.assertEqual(a,self.run_case([self.r]));self.assertEqual(a[1][0]['reason'],'UNCHANGED')
 def test_catalog_newer_clock_preferred_over_long_old_text(self):
  with tempfile.TemporaryDirectory() as tmp:
   paths=[]
   for i,(text,stamp) in enumerate([('x'*100,'2026-11-10T12:00:00Z'),('Mañana / 21:00','2026-11-11T12:00:00Z')]):
    p=Path(tmp)/f'{i}.json';p.write_text(json.dumps({'catalog_observed':[dict(bwin_event_id='7910156',bwin_url='https://www.bwin.es/es/sports/eventos/a-b-2:7910156',text=text,text_observed_at_utc=stamp)],'matches':[]}));paths.append(p)
   m=merge_bwin_snapshots(paths);self.assertEqual(m['events'][0]['text'],'Mañana / 21:00')
 def test_safe_alias_candidates_and_ambiguous_not_selected(self):
  f={'home':'Juventus W','away':'Lyon W','gender':'F'};event=dict(bwin_event_id='7910157',bwin_url='https://www.bwin.es/es/sports/eventos/juventus-f-ol-lyonnes-f-2:7910157',text='Juventus F OL Lyonnes F')
  self.assertEqual(len(b.match_catalog(f,[event])),1);self.assertEqual(len(b.match_catalog({**f,'gender':'M'},[event])),0)

class AdditionalScheduleTests(unittest.TestCase):
 setUp=ReconciliationTests.setUp
 run_case=ReconciliationTests.run_case
 def test_unknown_category_does_not_become_identity_conflict_or_gender_verified(self):
  self.f['gender']='UNKNOWN';d,a=self.run_case([self.r]);self.assertEqual(a[0]['status'],'SCHEDULE_VERIFIED');self.assertEqual(d['fixtures'][0]['gender'],'UNKNOWN')
 def test_wrong_competition_or_season_blocks(self):
  for key in ['id','season']:
   r=copy.deepcopy(self.r);r['league'][key]=999;self.assertEqual(self.run_case([r])[1][0]['status'],'IDENTITY_CONFLICT')
 def test_no_response_keeps_original_schedule_and_pending(self):
  d,a=self.run_case([]);self.assertEqual(d['fixtures'][0]['kickoff_madrid'],self.f['kickoff_madrid']);self.assertEqual(a[0]['status'],'SCHEDULE_UNVERIFIED')

class ClockRebuildTests(unittest.TestCase):
 def test_clock_uses_retained_event_observation_and_live_blocks(self):
  row=dict(bwin_event_id='7777777',kickoff_madrid='2026-11-11T19:00:00+01:00',inspection={'title_teams_match':True})
  cat=[dict(bwin_event_id='7777777',text='Comienza en 56 min',text_observed_at_utc='2026-11-11T17:03:44+00:00')]
  b.reconcile_catalog_clocks([row],cat);self.assertEqual(row['inspection']['countdown_observed_at_utc'],cat[0]['text_observed_at_utc']);self.assertLess(row['inspection']['countdown_difference_minutes'],1)
  cat[0]['text']='LIVE Descanso';b.reconcile_catalog_clocks([row],cat);self.assertTrue(row['inspection']['is_live'])
 def test_observed_aliases_are_hints_only(self):
  self.assertEqual(b.competition_similarity('WSL Cup','FA League Cup (F) Fútbol'),1)
  self.assertEqual(b.competition_similarity('WSL Cup','FA League Cup Fútbol'),0)
  self.assertEqual(b.team_similarity('Nautico Recife','Clube Náutico Capibaribe PE'),1)

class ImmutableCaptureTests(unittest.TestCase):
 def test_immutable_capture_keeps_original_and_separate_snapshot(self):
  from test_prematch_release import RefreshTests
  from unittest.mock import patch
  from types import SimpleNamespace
  import bwin_market_preflight as preflight
  import prematch_prospective_trial as t
  from test_prematch_release import NOW
  helper=RefreshTests();helper.setUp();self.addCleanup(helper.doCleanups);_,m=helper.preflight(helper.fixtures[0]);p=Path(m['snapshot']['snapshot_path']);before=t.sha(p)
  def scrape(cmd,**kwargs):
   dest=Path(kwargs['env']['PREMATCH_EVENT_DATA_DIR'])/p.name;dest.write_text(p.read_text().replace((NOW-__import__('datetime').timedelta(minutes=10)).isoformat(),NOW.isoformat()));kwargs['stdout'].write('TXT: '+str(dest)+'\n');kwargs['stdout'].flush();return SimpleNamespace(returncode=0)
  with patch.object(preflight.subprocess,'run',side_effect=scrape),patch.object(preflight,'utcnow',return_value=NOW):r=preflight.run_scraper(helper.root,helper.root/'immutable','7000001',m['bwin_url'],refresh=True,immutable=True)
  self.assertEqual(t.sha(p),before);self.assertTrue(r['canonical_unchanged']);self.assertNotEqual(t.sha(Path(r['fresh_snapshot_path'])),before)

class ActualScraperPathTests(unittest.TestCase):
 def test_staging_contract_and_wrong_event_are_checked_without_browser(self):
  import scraper_bwin as s
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);stage=root/'outputs/matches/7777777/staging/20261111T171000123456Z/7777777/inputs'
   env=dict(PREMATCH_EVENT_ID='7777777',PREMATCH_EVENT_DATA_DIR=str(stage),PREMATCH_IMMUTABLE_CAPTURE='1',PREMATCH_BATCH_MODE='1')
   with patch.object(s,'OUTPUT_DIR_SCRAPER',root/'outputs/scraper'),patch.dict(s.os.environ,env):
    self.assertEqual(s.rutas_salida_evento({'event_id':'7777777'})[0],stage.resolve())
    with self.assertRaises(ValueError):s.rutas_salida_evento({'event_id':'8888888'})
   env['PREMATCH_EVENT_DATA_DIR']=str(root/'foreign/7777777/inputs')
   with patch.object(s,'OUTPUT_DIR_SCRAPER',root/'outputs/scraper'),patch.dict(s.os.environ,env):
    with self.assertRaises(ValueError):s.rutas_salida_evento({'event_id':'7777777'})

class DailyResumeRouteTests(unittest.TestCase):
 def test_sync_bwin_routes_to_existing_preselection_not_preparation(self):
  import prematch_daily as daily
  from unittest.mock import patch
  with patch.object(daily,'operational_research',return_value={'status':'TEST'}) as op,patch.object(daily,'reusable_preparation',side_effect=AssertionError('Must not rebuild history')):
   self.assertEqual(daily.main(['--sync-bwin','--date','2026-11-11']),0);op.assert_called_once()
