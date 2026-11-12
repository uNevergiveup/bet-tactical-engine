import unittest,csv,json,tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
import prematch_prospective_trial as t
from prematch_audit import CandidateLedger
from prematch_daily_csv import export_daily,FIELDS

class CSVTests(unittest.TestCase):
 def test_complete_unique_atomic_and_pending_rows(self):
  with tempfile.TemporaryDirectory() as td:
   folder=Path(td)/'2026-11-02_1500_0300';base=folder/'prospective_trial'
   fs=[{'fixture_id':i,'home':f'Home {i}','away':'Away','kickoff_madrid':'2026-11-02T18:00:00+01:00','status':'NS','season':2026} for i in range(7)]
   t.dump(folder/'fixtures.json',{'fixtures':fs+fs[:1]})
   ledger=CandidateLedger(base/'candidate_ledger.json');ledger.record(fs[5],'DIFERIDO_POR_PRESUPUESTO','preselection','BUDGET');ledger.write()
   rows=export_daily(base);self.assertEqual(len(rows),7);self.assertEqual(rows[5]['estado_final'],'NO_EVALUADO_POR_LIMITE')
   self.assertEqual(rows[5]['preselection_evaluada'],'false');self.assertEqual(rows[5]['mejor_hipotesis_cuota'],'')
   with (base/'partidos_diarios.csv').open(encoding='utf-8-sig',newline='') as f:parsed=list(csv.DictReader(f,delimiter=';'))
   self.assertEqual(len(parsed),7);self.assertEqual(set(parsed[0]),set(FIELDS));self.assertEqual(parsed[0]['temporada'],'2026')
   self.assertFalse(list(base.glob('*.tmp')))
 @unittest.skipUnless((Path(__file__).parent/'outputs/discovery/2026-11-01_1600_0300/fixtures.json').is_file(), 'Historical snapshot absent; synthetic CSV regression remains enabled')
 def test_lafc_historical_not_lost(self):
  root=Path(__file__).parent;source=root/'outputs/discovery/2026-11-01_1600_0300'
  with tempfile.TemporaryDirectory() as td:
   folder=Path(td)/source.name;base=folder/'prospective_trial'
   t.dump(folder/'fixtures.json',t.read(source/'fixtures.json'))
   f=next(f for f in t.read(source/'fixtures.json')['fixtures'] if f['fixture_id']==1490590)
   ledger=CandidateLedger(base/'candidate_ledger.json');ledger.record(f,'EXCLUIDO_POR_LIMITE_ANTERIOR','select_fixtures','LEGACY_TOP5');ledger.write()
   row=next(r for r in export_daily(base) if r['fixture_id']==1490590)
   self.assertEqual(row['estado_final'],'NO_EVALUADO_POR_LIMITE');self.assertEqual(row['motivo_exclusion_o_pendiente'],'LEGACY_TOP5')
 def test_legacy_limit_row_preserved_without_historical_dependency(self):
  with tempfile.TemporaryDirectory() as td:
   folder=Path(td)/'2026-11-01_1600_0300';base=folder/'prospective_trial'
   fixtures=[{'fixture_id':i,'home':'Synthetic Home','away':'Synthetic Away','kickoff_madrid':'2026-11-02T02:30:00+01:00'} for i in range(1,8)]
   t.dump(folder/'fixtures.json',{'fixtures':fixtures})
   ledger=CandidateLedger(base/'candidate_ledger.json');ledger.record(fixtures[5],'EXCLUIDO_POR_LIMITE_ANTERIOR','select_fixtures','LEGACY_TOP5');ledger.write()
   rows=export_daily(base);row=next(r for r in rows if r['fixture_id']==6)
   self.assertEqual(len(rows),7);self.assertEqual(row['estado_final'],'NO_EVALUADO_POR_LIMITE');self.assertEqual(row['motivo_exclusion_o_pendiente'],'LEGACY_TOP5')
 def test_default_config_profiles(self):
  c,_,_=t.load_window_config(Path(__file__).parent)
  expected={'normal':('15:00:00','03:00:00'),'ampliada':('15:00:00','03:00:00')}
  for name,hours in expected.items():
   _,start,end=t.resolve_window(c,name);self.assertEqual((str(start),str(end)),hours)

if __name__=='__main__':unittest.main()
