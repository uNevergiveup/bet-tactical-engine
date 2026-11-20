import unittest,tempfile,json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from prematch_tactical_signal_engine import research_profiles
from test_prematch_tactical_signal_engine import fixture,observations,NOW
class ResearchProfiles(unittest.TestCase):
 def test_offline_idempotent_and_bound(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'pre.json';f=fixture();f['statistical_observations']=observations();p.write_text(json.dumps({'matches':[f]}));original=p.read_bytes()
   with patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
    out,d=research_profiles(p,Path(td)/'out',NOW.isoformat());out2,d2=research_profiles(p,Path(td)/'out',NOW.isoformat())
   self.assertEqual(out,out2);self.assertEqual(d,d2);self.assertEqual(original,p.read_bytes());self.assertFalse(d['matches'][0]['actionable_bet'])
   q=Path(td)/'llm.json';q.write_text(json.dumps({'source_sha256':d['source_sha256'],'claims':[{'fixture_id':900001,'status':'CONFIRMADO','fixture_ids':[999999]}]}))
   with self.assertRaises(ValueError):research_profiles(p,Path(td)/'out',NOW.isoformat(),q)
 def test_cutoff(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();f['analysis_cutoff_utc']='2099-01-01T00:00:00Z';p.write_text(json.dumps({'matches':[f]}))
   with self.assertRaises(ValueError):research_profiles(p,td,NOW.isoformat())

class Longitudinal(unittest.TestCase):
 def test_metric_specific_minutes_and_null(self):
  from prematch_tactical_history import longitudinal_profile
  raw=[]
  for i in range(4):
   raw.append({'fixture':{'id':i+1,'date':f'2026-10-0{i+1}T00:00:00Z'},'players':[{'team':{'id':1},'players':[{'player':{'id':3,'name':'P'},'statistics':[{'games':{'minutes':90},'shots':{'total':None if i==0 else 2}}]}]}]})
  m=longitudinal_profile(1,[],raw)['players'][0]['metrics']['shots']
  self.assertEqual(m['eligible_minutes'],270);self.assertEqual(m['observed_matches'],3);self.assertEqual(m['per90'],2)
 def test_resume_corruption_and_interrupt(self):
  import os
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();f['statistical_observations']=observations();p.write_text(json.dumps({'matches':[f]}));base=Path(td)/'out';original=os.link;calls=[]
   def interrupt(a,b):
    calls.append(b)
    if 'team_store' in str(b) and sum('team_store' in str(p) for p in calls)==2:raise OSError('simulated interruption')
    return original(a,b)
   with patch('os.link',side_effect=interrupt):
    with self.assertRaises(OSError):research_profiles(p,base,NOW.isoformat())
   existing=list((base/'team_store').rglob('*.json'));self.assertEqual(len(existing),1);before=existing[0].read_bytes()
   out,d=research_profiles(p,base,NOW.isoformat());self.assertEqual(before,existing[0].read_bytes())
   existing[0].write_text('{}')
   with self.assertRaisesRegex(ValueError,'IMMUTABLE'):research_profiles(p,base,NOW.isoformat())
 def test_no_promotion_and_source_binding(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();f['statistical_observations']=observations();p.write_text(json.dumps({'matches':[f]}));out,d=research_profiles(p,td,NOW.isoformat());q=Path(td)/'llm.json'
   claim={'fixture_id':f['fixture_id'],'status':'HIPÓTESIS','fixture_ids':[1000],'mechanism':'possible','counter_scenario':'opposite','falsifier':'video'}
   llm={'source_sha256':d['source_sha256'],'context_sources':[],'claims':[claim]};q.write_text(json.dumps(llm));out,withllm=research_profiles(p,td,NOW.isoformat(),q)
   self.assertEqual(d['ranking'],withllm['ranking'])
   claim['quantitative_checks']=[{'team_id':1,'metric':'goals','n':999,'mean':9}];q.write_text(json.dumps(llm))
   with self.assertRaisesRegex(ValueError,'QUANTITATIVE'):research_profiles(p,td,NOW.isoformat(),q)
 def test_daily_window_offline(self):
  import prematch_daily as daily
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();f['kickoff_madrid']='2026-11-02T18:00:00+01:00';f['statistical_observations']=observations();p.write_text(json.dumps({'matches':[f]}))
   with patch('socket.socket.connect',side_effect=AssertionError('NETWORK')):
    self.assertEqual(daily.main(['--tactical-research','--date','2026-11-02','--asof',NOW.isoformat(),'--preselection',str(p),'--output-dir',str(Path(td)/'out'),'--pattern-library',str(Path(td)/'library')]),0)
 def test_context_future_and_identity(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();p.write_text(json.dumps({'matches':[f]}));q=Path(td)/'c.json';q.write_text(json.dumps({'generated_at':'2099-01-01T00:00:00Z'}))
   with self.assertRaisesRegex(ValueError,'CONTEXT_AFTER'):research_profiles(p,td,NOW.isoformat(),contexts=[q])
 def test_context_contradiction(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();p.write_text(json.dumps({'matches':[f]}));q=Path(td)/'c.json'
   q.write_text(json.dumps({'generated_at':NOW.isoformat(),'fixture':{'fixture_id':900001,'home_id':999,'away_id':2,'league_id':100,'season':2026,'date':f['kickoff_madrid']},'match':{'verified_fixture_id':900001}}))
   with self.assertRaisesRegex(ValueError,'CONTEXT_IDENTITY'):research_profiles(p,td,NOW.isoformat(),contexts=[q])
 def test_confirmed_requires_real_source_and_duplicate_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();f['statistical_observations']=observations();p.write_text(json.dumps({'matches':[f]}));out,d=research_profiles(p,td,NOW.isoformat());q=Path(td)/'llm.json'
   c={'fixture_id':900001,'status':'CONFIRMADO','fixture_ids':[1000],'mechanism':'possible','counter_scenario':'opposite','falsifier':'video','verified_observation_source':'fake'}
   # A bare source label is never accepted as verified evidence.
   llm={'source_sha256':d['source_sha256'],'context_sources':[],'claims':[c]};c['verified_observation_source']={};q.write_text(json.dumps(llm))
   with self.assertRaisesRegex(ValueError,'OBSERVATION'):research_profiles(p,td,NOW.isoformat(),q)
   c['status']='HIPÓTESIS';llm['claims']=[c,c];q.write_text(json.dumps(llm))
   with self.assertRaisesRegex(ValueError,'DUPLICATE'):research_profiles(p,td,NOW.isoformat(),q)
 def test_operational_llm_requires_timestamp(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'p.json';f=fixture();f['statistical_observations']=observations();p.write_text(json.dumps({'matches':[f]}));out,d=research_profiles(p,td,NOW.isoformat());q=Path(td)/'llm.json'
   q.write_text(json.dumps({'source_sha256':d['source_sha256'],'context_sources':[],'claims':[{'fixture_id':900001,'status':'INFERIDO','fixture_ids':[1000],'mechanism':'possible','counter_scenario':'opposite','falsifier':'video'}]}))
   with self.assertRaisesRegex(ValueError,'GENERATION_TIME'):research_profiles(p,td,NOW.isoformat(),q,retrospective=False)

class DynamicLibrary(unittest.TestCase):
 def profile(self,n=8):
  return {'team_id':42,'season':2026,'fixture_ids':list(range(1,n+1)),
   'observations':[{'fixture_id':i,'kickoff_utc':f'2026-10-{i:02}T00:00:00Z','league_id':1,'season':2026,'venue':'HOME','own':{'corners':7 if i<7 else 1},'conceded':{'goals':1}} for i in range(1,n+1)]}
 def proposal(self):
  return {'team_id':42,'season':2026,'family':'unexpected_new_family','title':'New free discovery','status':'HIPÓTESIS','fixture_ids':[1,2],
   'condition':{'field':'venue','op':'eq','value':'HOME'},'outcome':{'field':'own.corners','op':'ge','value':6},
   'mechanism':'possible second actions','counter_scenario':'clean finishing','falsifier':'video sequence'}
 def persist(self,path,obj):
  path.parent.mkdir(parents=True,exist_ok=True);raw=json.dumps(obj,sort_keys=True)
  if path.exists():self.assertEqual(path.read_text(),raw)
  else:path.write_text(raw)
 def test_discover_persist_reuse_update_idempotence(self):
  from prematch_tactical_history import evolve_tactical_library
  with tempfile.TemporaryDirectory() as td:
   a=evolve_tactical_library([self.profile(6)],[self.proposal()],td,NOW,self.persist)
   self.assertEqual(a['evaluations'][0]['state'],'DESCRIPTIVE_REGULARITY');self.assertEqual(a['evaluations'][0]['successes'],6)
   b=evolve_tactical_library([self.profile(8)],[],td,NOW+timedelta(seconds=1),self.persist)
   self.assertEqual(a['evaluations'][0]['pattern_id'],b['evaluations'][0]['pattern_id']);self.assertEqual(b['evaluations'][0]['evolution'],'WEAKENED')
   original={str(p):p.read_bytes() for p in Path(td).rglob('*.json')}
   c=evolve_tactical_library([self.profile(8)],[],td,NOW+timedelta(seconds=1),self.persist)
   self.assertEqual(b,c);self.assertTrue(all(Path(p).read_bytes()==v for p,v in original.items()))
 def test_equivalence_false_statistics_and_missing(self):
  from prematch_tactical_history import evolve_tactical_library,tactical_rule_value
  self.assertIsNone(tactical_rule_value({'field':'own.corners','op':'gt','value':5},{'own':{}}))
  with tempfile.TemporaryDirectory() as td:
   p=self.proposal();p['claimed_statistics']={'n':999,'successes':999}
   with self.assertRaisesRegex(ValueError,'COUNTS'):evolve_tactical_library([self.profile()],[p],td,NOW,self.persist)
   p=self.proposal();p['equivalent_to']='wrong'
   with self.assertRaisesRegex(ValueError,'EQUIVALENCE'):evolve_tactical_library([self.profile()],[p],td,NOW,self.persist)
 def test_no_future_library_and_no_strength_from_result(self):
  from prematch_tactical_history import evolve_tactical_library,contextual_segments
  with tempfile.TemporaryDirectory() as td:
   evolve_tactical_library([self.profile()],[self.proposal()],td,NOW+timedelta(days=1),self.persist)
   result=evolve_tactical_library([self.profile()],[],td,NOW,self.persist);self.assertEqual(result['evaluations'],[])
  segments=contextual_segments(self.profile());self.assertIn('favorite_status',segments['unavailable'])
 def test_equivalent_integer_rules_share_identity(self):
  from prematch_tactical_history import normalize_tactical_rule
  self.assertEqual(normalize_tactical_rule({'all':[{'field':'own.corners','op':'gt','value':5}]}),{'field':'own.corners','op':'ge','value':6})
 def test_season_change_marks_obsolete_without_erasing_history(self):
  from prematch_tactical_history import evolve_tactical_library
  with tempfile.TemporaryDirectory() as td:
   first=evolve_tactical_library([self.profile()],[self.proposal()],td,NOW,self.persist)
   changed=self.profile();changed['season']=2027
   result=evolve_tactical_library([changed],[],td,NOW+timedelta(days=1),self.persist)
   self.assertEqual(result['evaluations'][0]['state'],'OBSOLETE_SEASON');self.assertTrue(list((Path(td)/'patterns').rglob('*.json')))
 def test_dynamic_relational_pattern_not_predefined(self):
  from prematch_tactical_history import tactical_rule_value
  expr={'field':'own.shots','op':'lt','value':{'field':'conceded.shots'}}
  self.assertTrue(tactical_rule_value(expr,{'own':{'shots':9},'conceded':{'shots':10}}))
  self.assertIsNone(tactical_rule_value(expr,{'own':{'shots':9}}))


class TacticalRepairGranadaLasPalmasRegression(unittest.TestCase):
 """Offline regression suite for Fixture 1570018 (Granada CF vs Las Palmas, Event 7909230)."""

 def _build_granada_las_palmas_fixture_and_cache(self, root: Path):
  cutoff = NOW
  kickoff = (NOW + timedelta(hours=6)).isoformat()
  cache_dir = root / 'outputs/data/cache/preselect_api'
  cache_dir.mkdir(parents=True, exist_ok=True)

  # Granada (715): 12 FT league matches (8 detailed, 4 non-detailed) + 1 Copa (143:2026) + 1 prior season (141:2025)
  granada_goals_own = [1, 2, 1, 0, 2, 3, 1, 2, 1, 1, 2, 1]
  granada_goals_con = [0, 1, 1, 2, 1, 1, 0, 2, 2, 1, 1, 2]
  granada_h1_own = [0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 1, 0]
  granada_h1_con = [0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 1]

  # Las Palmas (534): 13 FT league matches (8 detailed, 5 non-detailed) + 1 Copa (143:2026) + 1 Friendly (667:2026)
  # Conceded goals in Las Palmas has high variance (CV > 0.80) while Granada own goals has CV <= 0.80
  lp_goals_own = [1, 2, 0, 1, 3, 1, 2, 0, 1, 2, 1, 0, 1]
  lp_goals_con = [0, 0, 3, 0, 1, 4, 0, 2, 0, 0, 3, 0, 1]
  lp_h1_own = [0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 0, 0]
  lp_h1_con = [0, 0, 1, 0, 0, 2, 0, 1, 0, 0, 1, 0, 0]

  home_obs = []
  away_obs = []
  cache_responses = []

  for i in range(12):
   fid = 1569900 + i
   dt_str = (cutoff - timedelta(days=i + 2)).isoformat()
   is_detailed = i < 8
   venue = 'HOME' if i % 2 == 0 else 'AWAY'
   own_g = granada_goals_own[i]
   con_g = granada_goals_con[i]
   h1_o = granada_h1_own[i]
   h1_c = granada_h1_con[i]
   own_block = {'goals': own_g, 'goals_h1': h1_o, 'goals_h2': own_g - h1_o}
   con_block = {'goals': con_g, 'goals_h1': h1_c, 'goals_h2': con_g - h1_c}
   if is_detailed:
    own_block.update({
     'shots': 15 if i < 5 else 9,
     'on_target': 4 + (i % 2),
     'corners': 3 + (i % 2),
     'fouls': 12 + (i % 3),
     'yellow': 0 if i == 0 else (2 + (i % 2)),
     'offsides': 2,
     'xg': None if i == 7 else round(1.25 + 0.1 * (i % 3), 2),
     'blocked_shots': 3 + (i % 2),
     'possession_pct': 48 + (i % 5),
    })
    con_block.update({
     'shots': 11 + (i % 2),
     'on_target': 3 + (i % 2),
     'corners': 5 + (i % 2),
     'fouls': 13 + (i % 2),
     'yellow': 2 + (i % 2),
     'offsides': 1 + (i % 2),
     'xg': None if i == 7 else 1.15,
     'blocked_shots': 2 + (i % 2),
     'possession_pct': 52 - (i % 5),
    })
    # Coach id=0 unlinked alias in fixture i==1, positive coach_id=26029 Pacheta in the other 7
    cid = 0 if i == 1 else 26029
    cname = 'José Rojo Martín' if i == 1 else 'Pacheta'
    cache_responses.append({
     'fixture': {'id': fid, 'date': dt_str, 'status': {'short': 'FT'}},
     'league': {'id': 141, 'season': 2026},
     'teams': {
      'home': {'id': 715 if venue == 'HOME' else 800 + i},
      'away': {'id': 800 + i if venue == 'HOME' else 715},
     },
     'goals': {'home': own_g if venue == 'HOME' else con_g, 'away': con_g if venue == 'HOME' else own_g},
     'score': {'halftime': {'home': h1_o if venue == 'HOME' else h1_c, 'away': h1_c if venue == 'HOME' else h1_o}},
     'lineups': [{
      'team': {'id': 715},
      'formation': '4-3-3' if i != 2 else '4-4-2',
      'coach': {'id': cid, 'name': cname},
      'startXI': [{'player': {'id': 71501, 'name': 'Jorge Pascual'}}],
     }],
     'players': [{
      'team': {'id': 715},
      'players': [{
       'player': {'id': 71501, 'name': 'Jorge Pascual'},
       'statistics': [{
        'games': {'minutes': 90, 'position': 'F'},
        'shots': {'total': None if i == 0 else (0 if i == 1 else 2), 'on': 0 if i in (0, 1) else 1},
        'passes': {'key': 1},
        'tackles': {'total': None if i < 2 else 0},
        'goals': {'total': 0 if i > 0 else 1, 'assists': 0},
       }],
      }],
     }],
     'statistics': [{'team': {'id': 715}, 'statistics': []}],
    })
   home_obs.append({
    'fixture_id': fid,
    'team_id': 715,
    'opponent_id': 800 + i,
    'kickoff_utc': dt_str,
    'league_id': 141,
    'season': 2026,
    'venue': venue,
    'status': 'FT',
    'statistics_requested': is_detailed,
    'own': own_block,
    'conceded': con_block,
   })

  # Extra non-target competition observations for Granada (Copa 143:2026 & prior season 141:2025)
  home_obs.append({
   'fixture_id': 1569801, 'team_id': 715, 'opponent_id': 901,
   'kickoff_utc': (cutoff - timedelta(days=5)).isoformat(),
   'league_id': 143, 'season': 2026, 'venue': 'AWAY', 'status': 'FT',
   'statistics_requested': False, 'own': {'goals': 2}, 'conceded': {'goals': 0},
  })
  home_obs.append({
   'fixture_id': 1569802, 'team_id': 715, 'opponent_id': 902,
   'kickoff_utc': (cutoff - timedelta(days=180)).isoformat(),
   'league_id': 141, 'season': 2025, 'venue': 'HOME', 'status': 'FT',
   'statistics_requested': False, 'own': {'goals': 1}, 'conceded': {'goals': 1},
  })

  for i in range(13):
   fid = 1570100 + i
   dt_str = (cutoff - timedelta(days=i + 2)).isoformat()
   is_detailed = i < 8
   venue = 'AWAY' if i % 2 == 0 else 'HOME'
   own_g = lp_goals_own[i]
   con_g = lp_goals_con[i]
   h1_o = lp_h1_own[i]
   h1_c = lp_h1_con[i]
   own_block = {'goals': own_g, 'goals_h1': h1_o, 'goals_h2': own_g - h1_o}
   con_block = {'goals': con_g, 'goals_h1': h1_c, 'goals_h2': con_g - h1_c}
   if is_detailed:
    own_block.update({
     'shots': 11 + (i % 2),
     'on_target': 4 + (i % 2),
     'corners': 2 if i % 2 == 0 else 8,
     'fouls': 12 + (i % 2),
     'yellow': 4 if venue == 'AWAY' else 1,
     'offsides': 2 + (i % 2),
     'xg': round(1.10 + 0.1 * (i % 3), 2),
     'blocked_shots': 2 + (i % 3),
     'possession_pct': 56 + (i % 4),
    })
    con_block.update({
     'shots': 8 if i < 5 else 14,
     'on_target': 4 + (i % 2),
     'corners': 7 + (i % 2),
     'fouls': 13 + (i % 2),
     'yellow': 2 + (i % 2),
     'offsides': 1 + (i % 2),
     'xg': 1.20,
     'blocked_shots': 3 + (i % 2),
     'possession_pct': 44 - (i % 4),
    })
    if i < 2:
     cid, cname = 23107, 'Albert Sánchez'
    elif i < 4:
     cid, cname = 29780, 'Manuel Rodriguez Juan'
    else:
     cid, cname = 27702, 'Ruben de la Barrera'
    cache_responses.append({
     'fixture': {'id': fid, 'date': dt_str, 'status': {'short': 'FT'}},
     'league': {'id': 141, 'season': 2026},
     'teams': {
      'home': {'id': 534 if venue == 'HOME' else 850 + i},
      'away': {'id': 850 + i if venue == 'HOME' else 534},
     },
     'goals': {'home': own_g if venue == 'HOME' else con_g, 'away': con_g if venue == 'HOME' else own_g},
     'score': {'halftime': {'home': h1_o if venue == 'HOME' else h1_c, 'away': h1_c if venue == 'HOME' else h1_o}},
     'lineups': [{
      'team': {'id': 534},
      'formation': '4-2-3-1' if i < 2 else '4-4-2',
      'coach': {'id': cid, 'name': cname},
      'startXI': [{'player': {'id': 53401, 'name': 'Jesé'}}],
     }],
     'players': [{
      'team': {'id': 534},
      'players': [{
       'player': {'id': 53401, 'name': 'Jesé'},
       'statistics': [{
        'games': {'minutes': 75, 'position': 'F'},
        'shots': {'total': 2 if i < 4 else 0, 'on': 1 if i < 3 else None},
        'passes': {'key': 0 if i < 4 else None},
        'tackles': {'total': None},
        'goals': {'total': 1 if i == 0 else 0, 'assists': 0},
       }],
      }],
     }],
     'statistics': [{'team': {'id': 534}, 'statistics': []}],
    })
   away_obs.append({
    'fixture_id': fid,
    'team_id': 534,
    'opponent_id': 850 + i,
    'kickoff_utc': dt_str,
    'league_id': 141,
    'season': 2026,
    'venue': venue,
    'status': 'FT',
    'statistics_requested': is_detailed,
    'own': own_block,
    'conceded': con_block,
   })

  away_obs.append({
   'fixture_id': 1570050, 'team_id': 534, 'opponent_id': 950,
   'kickoff_utc': (cutoff - timedelta(days=4)).isoformat(),
   'league_id': 143, 'season': 2026, 'venue': 'AWAY', 'status': 'FT',
   'statistics_requested': False, 'own': {'goals': 3}, 'conceded': {'goals': 1},
  })

  cache_file = cache_dir / 'batch_1570018.json'
  cache_file.write_text(json.dumps({
   'endpoint': 'fixtures',
   'retrieved_at_utc': (cutoff - timedelta(minutes=30)).isoformat(),
   'response': cache_responses,
  }))

  match_row = {
   'fixture_id': 1570018,
   'home': 'Granada CF',
   'away': 'Las Palmas',
   'home_id': 715,
   'away_id': 534,
   'league': 'Segunda División',
   'league_id': 141,
   'season': 2026,
   'kickoff_madrid': kickoff,
   'analysis_cutoff_utc': cutoff.isoformat(),
   'statistical_observations': {'home': home_obs, 'away': away_obs},
  }
  pre_path = root / 'outputs/discovery/manual/preselection_1570018.json'
  pre_path.parent.mkdir(parents=True, exist_ok=True)
  pre_path.write_text(json.dumps({'generated_at_utc': cutoff.isoformat(), 'matches': [match_row]}))
  return pre_path

 def test_granada_las_palmas_all_ten_repairs(self):
  with tempfile.TemporaryDirectory() as td:
   root = Path(td)
   pre_path = self._build_granada_las_palmas_fixture_and_cache(root)
   out_dir = root / 'outputs/discovery/manual/tactical_research'
   with patch('socket.socket.connect', side_effect=AssertionError('NETWORK_FORBIDDEN')):
    out1, doc1 = research_profiles(pre_path, out_dir, NOW.isoformat())
    out2, doc2 = research_profiles(pre_path, out_dir, NOW.isoformat())

   # Idempotence and non-commercial guardrails (Reqs 9 & 10)
   self.assertEqual(out1, out2)
   self.assertEqual(doc1, doc2)
   self.assertEqual(doc1['api_calls'], 0)
   match = doc1['matches'][0]
   self.assertFalse(match['actionable_bet'])
   self.assertEqual(match['fixture_id'], 1570018)

   home_prof = doc1['teams'][match['profile_ids'][0]]
   away_prof = doc1['teams'][match['profile_ids'][1]]
   self.assertFalse(home_prof['actionable_bet'])
   self.assertFalse(away_prof['actionable_bet'])

   # Req 3: Unambiguous denominators and Missing (null) vs Zero (0)
   h_yel = home_prof['metrics']['yellow']['own']
   self.assertEqual(h_yel['history_n'], 12)
   self.assertEqual(h_yel['expected_n'], 8)
   self.assertEqual(h_yel['history_not_detailed_n'], 4)
   self.assertEqual(h_yel['n'], 8)
   self.assertEqual(h_yel['missing_n'], 0)
   self.assertEqual(h_yel['zero_n'], 1)
   self.assertEqual(h_yel['positive_n'], 7)

   h_xg = home_prof['metrics']['xg']['own']
   self.assertEqual(h_xg['history_n'], 12)
   self.assertEqual(h_xg['expected_n'], 8)
   self.assertEqual(h_xg['n'], 7)
   self.assertEqual(h_xg['missing_n'], 1)
   self.assertEqual(h_xg['zero_n'], 0)

   # Window denominator disambiguation & mixed-competition detection
   global_l10 = home_prof['windows']['GLOBAL']['10']
   self.assertEqual(global_l10['window_fixtures_n'], 10)
   self.assertTrue(global_l10['mixed_competitions'])
   self.assertIn('143:2026', global_l10['competitions_included'])
   pure_l10 = home_prof['windows']['141:2026']['10']
   self.assertFalse(pure_l10['mixed_competitions'])
   self.assertEqual(pure_l10['metrics']['shots']['n'], 8)
   self.assertEqual(pure_l10['metrics']['shots']['not_detailed_n'], 2)
   self.assertIn('141:2026:HOME', home_prof['windows'])
   self.assertIn('141:2026:AWAY', away_prof['windows'])

   # Player null vs zero distinction
   h_player = home_prof['longitudinal']['players'][0]
   self.assertEqual(h_player['name'], 'Jorge Pascual')
   p_shots = h_player['metrics']['shots']
   self.assertEqual(p_shots['appearances_n'], 8)
   self.assertEqual(p_shots['observed_matches'], 7)
   self.assertEqual(p_shots['missing_matches'], 1)
   self.assertEqual(p_shots['zero_matches'], 1)
   self.assertEqual(p_shots['positive_matches'], 6)

   # Req 2: Period (1H vs 2H) and support metrics (blocked_shots, possession_pct)
   self.assertIn('goals_h1', home_prof['period_metrics'])
   self.assertIn('goals_h2', home_prof['period_metrics'])
   self.assertGreater(
    home_prof['period_metrics']['half_comparison']['match_total']['total_h2_mean'],
    home_prof['period_metrics']['half_comparison']['match_total']['total_h1_mean'],
   )
   self.assertEqual(home_prof['support_metrics']['blocked_shots']['own']['n'], 8)
   self.assertEqual(away_prof['support_metrics']['possession_pct']['own']['n'], 8)

   # Req 4: Coach regime separation & unlinked id=0 handling
   h_crs = home_prof['coach_regime_summary']
   self.assertEqual(h_crs['regime_state'], 'SINGLE_POSITIVE_COACH_ID_PLUS_UNLINKED_ID_ZERO_ENTRY')
   self.assertEqual(h_crs['distinct_positive_coach_ids'], [26029])
   self.assertTrue(h_crs['has_unlinked_zero_coach_id'])
   self.assertFalse(h_crs['multiple_positive_coaches_detected'])

   a_crs = away_prof['coach_regime_summary']
   self.assertEqual(a_crs['regime_state'], 'MULTIPLE_POSITIVE_COACH_IDS_RECORDED_REGIME_SPLIT_REQUIRED')
   self.assertTrue(a_crs['multiple_positive_coaches_detected'])
   self.assertEqual(set(a_crs['distinct_positive_coach_ids']), {23107, 29780, 27702})
   self.assertEqual(a_crs['most_recent_recorded_coach']['coach_id'], 23107)
   self.assertIsNotNone(a_crs['recent_vs_prior_coach_contrast'])
   self.assertEqual(
    a_crs['recent_vs_prior_coach_contrast']['warning'],
    'RECENT_COACH_SAMPLE_SMALL_DO_NOT_PROJECT_PRIOR_REGIME_AS_STABLE',
   )

   # Req 5: Stable, unstable, and contradictory signals explanation (Poisson-aware CV gate for discrete low-mean goals)
   sigs = {s['signal_id']: s for s in match['signals']}
   g_sig = sigs['1570018:home:goals']
   self.assertEqual(g_sig['status'], 'SUPPORTED_DESCRIPTIVE_SIGNAL')
   self.assertLessEqual(g_sig['own']['cv'], 0.80)
   self.assertGreater(g_sig['opponent_conceded']['cv'], 0.80)
   self.assertLessEqual(g_sig['opponent_conceded']['cv'], g_sig['opponent_conceded'].get('cv_limit', 1.65))

   sh_sig = sigs['1570018:home:shots']
   self.assertIn('OPPOSITE_RECENT_TRENDS', sh_sig['contradictions'])
   self.assertTrue(
    any(d['code'] == 'OPPOSITE_RECENT_TRENDS' for d in sh_sig['status_explanation']['contradiction_details'])
   )

   cor_sig = sigs['1570018:home:corners']
   self.assertIn('OWN_PRODUCTION_VS_RIVAL_CONCESSION_DISAGREE', cor_sig['contradictions'])

   yel_sig = sigs['1570018:away:yellow']
   self.assertIn('VENUE_VS_GLOBAL_DISAGREE', yel_sig['contradictions'])

   fouls_sig = sigs['1570018:home:fouls']
   self.assertEqual(fouls_sig['status'], 'SUPPORTED_DESCRIPTIVE_SIGNAL')
   self.assertEqual(fouls_sig['status_explanation']['stability_class'], 'STABLE')

   # Req 6: Cross own production with rival concession
   cm = match['matchup_cross_matrix']
   self.assertIn('goals', cm['metrics'])
   self.assertIn('goals_h1', cm['metrics'])
   self.assertIn('goals_h2', cm['metrics'])
   self.assertIn('blocked_shots', cm['metrics'])
   self.assertIsNotNone(
    cm['metrics']['on_target']['combined_match_totals_descriptive']['sum_of_global_midpoints_descriptive']
   )

   # Req 7: Opposing tactical scenarios & falsifiers strictly when evidence exists
   ts = match['tactical_scenarios']
   self.assertGreaterEqual(ts['scenarios_count'], 4)
   for sc in ts['scenarios']:
    self.assertTrue(sc['primary_hypothesis'])
    self.assertTrue(sc['counter_scenario'])
    self.assertTrue(sc['falsifier'])
    self.assertFalse(sc['actionable_bet'])

   # Req 8: Empty states of pattern_library and pattern_matchup explained without inventing patterns
   self.assertEqual(doc1['pattern_library']['evaluations'], [])
   lib_diag = doc1['pattern_library']['diagnostics']
   self.assertEqual(lib_diag['library_state'], 'EMPTY_NO_VALIDATED_PATTERNS_PERSISTED')
   self.assertGreaterEqual(len(lib_diag['empty_state_causes']), 2)
   self.assertTrue(
    all(not c['predictive_use_authorized'] for c in lib_diag['descriptive_candidate_regularities_not_persisted'])
   )
   pm_diag = match['pattern_matchup']['diagnostics']
   self.assertEqual(
    pm_diag['matchup_state'],
    'EMPTY_LIBRARY_EVALUATIONS_USE_EMPIRICAL_CROSS_MATRIX_AND_SCENARIOS',
   )

   # Req 1: research_profiles.md and pattern_library_summary.md rich detail
   md_text = (out1 / 'research_profiles.md').read_text(encoding='utf-8')
   summary_md_text = (out1 / 'pattern_library_summary.md').read_text(encoding='utf-8')
   self.assertGreater(len(md_text.splitlines()), 120)
   self.assertIn('Granada CF', md_text)
   self.assertIn('Las Palmas', md_text)
   self.assertIn('Albert Sánchez', md_text)
   self.assertIn('Pacheta', md_text)
   self.assertIn('José Rojo Martín', md_text)
   self.assertIn('Jorge Pascual', md_text)
   self.assertIn('Jesé', md_text)
   self.assertIn('matchup_cross_matrix', md_text)
   self.assertIn('tactical_scenarios', md_text)
   self.assertIn('EMPTY_NO_VALIDATED_PATTERNS_PERSISTED', summary_md_text)
   for forbidden in ('p_model', 'cuota justa', 'stake recomendado'):
    self.assertNotIn(forbidden, md_text.lower().replace('ninguna frecuencia, media, punto medio ni `priority_score` en este documento representa una probabilidad calibrada (`p_model`), cuota justa', ''))

 def test_scenarios_omitted_when_sample_below_four(self):
  with tempfile.TemporaryDirectory() as td:
   p = Path(td) / 'small.json'
   f = fixture()
   f['statistical_observations'] = observations(3)
   p.write_text(json.dumps({'matches': [f]}))
   out, doc = research_profiles(p, Path(td) / 'out', NOW.isoformat())
   ts = doc['matches'][0]['tactical_scenarios']
   self.assertEqual(ts['scenarios'], [])
   self.assertEqual(len(ts['omitted_due_to_insufficient_evidence']), 4)
