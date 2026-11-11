import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import prematch_market_mapping_v1 as m
ROOT=Path(__file__).parent/'outputs/reconciliation_real_20261111'
class ContractTests(unittest.TestCase):
 def signal(self,**kw):return dict(metric='goals',team_side='home',status='SUPPORTED_DESCRIPTIVE_SIGNAL',contradictions=[],**kw)
 def test_direct_team_ft_structural_match(self):self.assertEqual(m.classify_signal_market([self.signal()],'goals','home','FULL_TIME',{'line':1.5})[0],'SUPPORTED_MARKET_MATCH')
 def test_total_cannot_inherit_team_model(self):self.assertEqual(m.classify_signal_market([self.signal()],'goals',None,'FULL_TIME',{'line':2.5})[0],'DESCRIPTIVE_ONLY')
 def test_period_opposite_side_offside_and_contradiction(self):
  s=self.signal()
  self.assertEqual(m.classify_signal_market([s],'goals','home','FIRST_HALF',{'line':1.5})[0],'MARKET_WITHOUT_EVIDENCE')
  self.assertNotEqual(m.classify_signal_market([s],'goals','away','FULL_TIME',{'line':1.5})[0],'SUPPORTED_MARKET_MATCH')
  self.assertEqual(m.classify_signal_market([s],'offsides','home','FULL_TIME',{'line':1.5})[0],'DESCRIPTIVE_ONLY')
  s['contradictions']=['OPPOSITE_TRENDS'];self.assertEqual(m.classify_signal_market([s],'goals','home','FULL_TIME',{'line':1.5})[0],'CONTRADICTED')
 def test_integer_settlement_and_unbacked_not_promoted(self):
  s=self.signal();self.assertEqual(m.classify_signal_market([s],'goals','home','FULL_TIME',None)[0],'DATA_INSUFFICIENT')
  s['status']='INSUFFICIENT_DATA';self.assertEqual(m.classify_signal_market([s],'goals','home','FULL_TIME',{'line':1.5})[0],'DATA_INSUFFICIENT')
class RealIntegrationTests(unittest.TestCase):
 def run_offline(self,out,**kw):
  with patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')):return m.integrate_reconciliation(ROOT,out,**kw)
 def test_twelve_real_captures_all_quotes_isolated_and_idempotent(self):
  with tempfile.TemporaryDirectory() as tmp:
   out=Path(tmp);a=self.run_offline(out);hashes={p.name:m.sha(p) for p in out.iterdir()};b=self.run_offline(out);self.assertEqual(a,b);self.assertEqual(hashes,{p.name:m.sha(p) for p in out.iterdir()})
   self.assertEqual(a['counts']['scanner_verified_captures'],12);self.assertEqual(a['counts']['selections'],1772);self.assertEqual(len(a['commercial_ranking']),64)
   quotes=json.loads((out/'market_pattern_matches.json').read_text())['quotes'];self.assertEqual(len({q['quote_id'] for q in quotes}),1772)
   byid={r['fixture_id']:r for r in a['commercial_ranking']}
   for fid,n in [(1638311,448),(1557232,152),(1649465,89),(1600656,104)]:self.assertEqual(byid[fid]['captured_selections'],n)
   self.assertEqual(byid[1638311]['tier'],'C');self.assertEqual(byid[1638311]['supported_patterns'],0)
   self.assertFalse(byid[1645267]['operational_research_queue_eligible_at_evaluation']);self.assertEqual(byid[1638339]['original_binding_state'],'RESCHEDULED_OUT_OF_WINDOW')
 def test_stale_quotes_block_operational_research_queue(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=self.run_offline(Path(tmp),asof='2026-11-11T18:44:00+01:00');self.assertFalse(any(r['operational_research_queue_eligible_at_evaluation'] for r in d['commercial_ranking']));self.assertFalse(d['actionable_bet'])
 def test_bad_snapshot_is_not_trusted_despite_scanner_label(self):
  real_sha=m.sha
  def digest(p):return 'bad' if Path(p).name=='cuotas_bwin_7909989.txt' else real_sha(p)
  with tempfile.TemporaryDirectory() as tmp,patch.object(m,'sha',side_effect=digest):
   d=self.run_offline(Path(tmp));r=next(r for r in d['commercial_ranking'] if r['fixture_id']==1557232);self.assertEqual(r['market_state'],'MARKET_NOT_VERIFIED');self.assertEqual(d['counts']['scanner_verified_captures'],11)
 def test_input_sha_conflict_blocks_join(self):
  real=m.read
  def read(p):
   d=real(p)
   if Path(p).name=='opportunity_scan.json':d=copy.deepcopy(d);d['sources']['preselection_sha256']='bad'
   return d
  with tempfile.TemporaryDirectory() as tmp,patch.object(m,'read',side_effect=read):
   with self.assertRaisesRegex(m.MappingError,'SCANNER_PRESELECTION_SHA_MISMATCH'):self.run_offline(Path(tmp))
 def test_scanner_event_id_cannot_be_reassigned(self):
  real=m.read
  def read(p):
   d=real(p)
   if Path(p).name=='opportunity_scan.json':
    d=copy.deepcopy(d)
    next(a for a in d['audits'] if a['fixture_id']==1557232)['bwin_event_id']='9999999'
   return d
  with tempfile.TemporaryDirectory() as tmp,patch.object(m,'read',side_effect=read):
   d=self.run_offline(Path(tmp));r=next(r for r in d['commercial_ranking'] if r['fixture_id']==1557232)
   self.assertEqual(r['market_state'],'MARKET_NOT_VERIFIED');self.assertFalse(r['operational_research_queue_eligible_at_evaluation'])
