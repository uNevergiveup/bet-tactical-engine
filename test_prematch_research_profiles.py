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
