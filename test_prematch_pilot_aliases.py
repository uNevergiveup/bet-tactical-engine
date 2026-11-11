import unittest,json
from pathlib import Path
import bwin_discovery as b
class AliasTests(unittest.TestCase):
 def test_real_catalog_recovers_only_candidates(self):
  catalog=[{'bwin_event_id':eid,'bwin_url':f'https://www.bwin.es/es/sports/eventos/{slug}-2:{eid}','text':text} for eid,slug,text in [('7910157','juventus-f-ol-lyonnes-f','Juventus F OL Lyonnes F'),('7910155','oud-heverlee-leuven-f-hb-koge-f','Oud-Heverlee Leuven F HB Koge F')]]
  for home,away,eid in [('Juventus W','Lyon W','7910157'),('OH Leuven W','Køge W','7910155')]:
   rows=b.match_catalog({'home':home,'away':away,'gender':'F'},catalog);self.assertEqual(rows[0]['bwin_event_id'],eid)
   self.assertNotIn('identity_status',rows[0])
 def test_gender_and_reserves_still_blocked(self):
  event={'text':'Juventus F OL Lyonnes F','bwin_url':'https://www.bwin.es/es/sports/eventos/juventus-f-ol-lyonnes-f-2:7910157','bwin_event_id':'7910157'}
  f={'home':'Juventus','away':'Lyon','gender':'M'};self.assertEqual(b.match_catalog(f,[event]),[])
  f.update(home='Juventus W',away='Lyon W',gender='F');event['text']+=' U19';self.assertEqual(b.match_catalog(f,[event]),[])
 def test_event_tile_time_never_uses_other_event(self):
  from playwright.sync_api import sync_playwright
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True);page=browser.new_page();page.route('**/*',lambda route:route.abort())
   page.set_content('<div>Hoy / 01:00</div><ms-tree-event><a href="https://www.bwin.es/es/sports/eventos/a-b-2:7910157">Juventus F OL Lyonnes F</a><span>Hoy / 21:00</span></ms-tree-event><ms-tree-event><a href="https://www.bwin.es/es/sports/eventos/c-d-2:7910155">Leuven F Koge F</a><span>Hoy / 18:45</span></ms-tree-event>')
   rows=b.browser_collect_anchors(page,0);byid={r['bwin_event_id']:r for r in rows};self.assertIn('21:00',byid['7910157']['text']);self.assertNotIn('18:45',byid['7910157']['text']);self.assertNotIn('01:00',byid['7910157']['text']);browser.close()

class BrowserKickoffTests(unittest.TestCase):
 def row(self, text='Hoy / 21:00', observed='2026-11-11T01:49:28+00:00'):
  return dict(bwin_event_id='7910157',bwin_url='https://www.bwin.es/es/sports/eventos/juventus-f-ol-lyonnes-f-2:7910157',text_source='SINGLE_EVENT_TILE',browser_timezone='Europe/Madrid',text=text,text_observed_at_utc=observed)
 def test_specific_time_and_other_event(self):
  import generate_context as g
  other={**self.row('Hoy / 18:45'),'bwin_event_id':'7910155'}
  self.assertEqual(g.browser_event_datetime_utc([self.row(),other],7910157).isoformat(),'2026-11-11T20:00:00+00:00')
 def test_unknown_timezone_unscoped_live_and_multiple_times_rejected(self):
  import generate_context as g
  for changes in [dict(browser_timezone=None),dict(text_source='EVENT_ANCHOR'),dict(text='LIVE Hoy / 21:00'),dict(text='Hoy / 21:00 Hoy / 18:00')]:
   self.assertIsNone(g.browser_event_datetime_utc([{**self.row(),**changes}],7910157))
 def test_midnight_and_dst(self):
  import generate_context as g
  cases=[('Mañana / 00:30','2026-11-10T22:59:00+00:00','2026-11-10T23:30:00+00:00'),('Hoy / 21:00','2026-07-11T01:00:00+00:00','2026-07-11T19:00:00+00:00')]
  for text,observed,want in cases:self.assertEqual(g.browser_event_datetime_utc([self.row(text,observed)],7910157).isoformat(),want)
 def test_structured_contradiction_blocks(self):
  import generate_context as g
  html='<title>Juventus (F) - OL Lyonnes (F) UEFA Champions League (F) Fútbol | bwin</title><script type="application/json">{"eventId":7910157,"startDate":"2026-11-11T19:00:00Z"}</script>'
  with self.assertRaisesRegex(ValueError,'BWIN_KICKOFF_CONFLICT'):g.BwinParser().parse(self.row()['bwin_url'],html,browser_catalog=[self.row()])

class ContextAliasTests(unittest.TestCase):
 def test_female_alias_hints_and_category(self):
  import generate_context as g
  for a,c in [('OL Lyonnes','Lyon W'),('HB Koge','Køge W'),('PSV Eindhoven','PSV/Eindhoven W')]:
   self.assertEqual(g.team_name_similarity(a,c,'female'),100)
   self.assertEqual(g.team_name_similarity(a,c,'male'),0)
   self.assertEqual(g.team_name_similarity(a,c+' U19','female'),0)
 def test_unknown_competition_preserves_female_parenthesis(self):
  import generate_context as g
  m=g.BwinParser().parse('https://www.bwin.es/es/sports/eventos/eintracht-frankfurt-f-psv-eindhoven-f-2:7910004','<title>Eintracht Frankfurt (F) - PSV Eindhoven (F) UEFA Copa Europa (F) Fútbol | bwin</title>')
  self.assertEqual(m.away_name,'PSV Eindhoven');self.assertEqual(m.away_gender,'female');self.assertEqual(m.competition_display,'UEFA Copa Europa (F)')

class CompetitionAliasTests(unittest.TestCase):
 def test_europa_cup_women_label_is_exact_and_distinct(self):
  import generate_context as g
  self.assertEqual(b.competition_similarity('UEFA Europa Cup - Women','Eintracht Frankfurt (F) - PSV Eindhoven (F) UEFA Copa Europa (F) Fútbol | bwin'),1)
  self.assertEqual(b.competition_similarity('UEFA Europa Cup - Women','UEFA Champions League (F)'),0)
  self.assertEqual(b.competition_similarity('UEFA Europa Cup - Women','UEFA Europa League'),0)
  self.assertTrue(g.bwin_league_compatible('UEFA Copa Europa (F)',{'name':'UEFA Europa Cup - Women'}))
  self.assertFalse(g.bwin_league_compatible('UEFA Copa Europa (F)',{'name':'UEFA Champions League Women'}))

class StructuredAmbiguityTests(unittest.TestCase):
 row = BrowserKickoffTests.row
 def test_visible_clock_cannot_override_structured_conflict(self):
  import generate_context as g
  html='<title>Juventus (F) - OL Lyonnes (F) UEFA Champions League (F) Fútbol | bwin</title><script type="application/json">[{"eventId":7910157,"startDate":"2026-11-11T20:00:00Z"},{"eventId":7910157,"startDate":"2026-11-11T19:00:00Z"}]</script>'
  with self.assertRaisesRegex(ValueError,'BWIN_KICKOFF_CONFLICT'):g.BwinParser().parse(self.row()['bwin_url'],html,browser_catalog=[self.row()])
