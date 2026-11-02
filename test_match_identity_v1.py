import json
import tempfile
import unittest
from pathlib import Path
from match_identity import identity_from_payload, validate_identity, compare_report_name, write_identity

URL='https://www.bwin.es/es/sports/eventos/bar%C3%A7a-alav%C3%A9s-2:7899092?market=-1'
def sample():
    return {'generated_at':'2026-10-31T15:00:00+00:00','match':{'event_id':7899092,'url':URL,'home_name_raw':'Barça','away_name_raw':'Alavés','home_gender':'male','away_gender':'male'},'fixture':{'fixture_id':1570434,'date':'2026-10-31T17:30:00+00:00','home_id':529,'home_name':'Barcelona','away_id':542,'away_name':'Alaves','league_id':140,'season':2026,'league_name':'La Liga'}}
class IdentityTests(unittest.TestCase):
    def test_identity_fields(self):
        x=identity_from_payload(sample());self.assertEqual(x['away']['api_team_id'],542);self.assertEqual(x['bwin_event_id'],'7899092')
    def test_validated(self):self.assertTrue(validate_identity(identity_from_payload(sample()),sample(),'7899092',URL))
    def test_wrong_event(self):
        with self.assertRaises(ValueError):validate_identity(identity_from_payload(sample()),sample(),'7899093')
    def test_wrong_url(self):
        with self.assertRaises(ValueError):validate_identity(identity_from_payload(sample()),sample(),'7899092',URL+'wrong')
    def test_wrong_fixture(self):
        x=identity_from_payload(sample());x['api_fixture_id']=999
        with self.assertRaises(ValueError):validate_identity(x,sample())
    def test_wrong_team(self):
        x=identity_from_payload(sample());x['away']['api_team_id']=777
        with self.assertRaises(ValueError):validate_identity(x,sample())
    def test_correct_deportivo_alaves(self):
        x=identity_from_payload(sample());self.assertEqual(compare_report_name('Deportivo Alavés',x['away'],x['home']),'ALIAS')
    def test_different_deportivo(self):
        x=identity_from_payload(sample());self.assertEqual(compare_report_name('Deportivo La Coruña',x['away'],x['home']),'MISMATCH')
    def test_barca_is_not_barcelona_without_alias(self):
        x=identity_from_payload(sample());self.assertEqual(compare_report_name('Barcelona',x['home'],x['away']),'EXACT')
    def test_wrong_juvenile(self):
        x=identity_from_payload(sample());self.assertEqual(compare_report_name('Alavés U19',x['away'],x['home']),'MISMATCH')
    def test_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            inp=Path(d)/'match_engine_7899092.json';inp.write_text(json.dumps(sample()))
            path=write_identity(inp);self.assertEqual(path.name,'match_identity.json');self.assertTrue(validate_identity(json.loads(path.read_text()),sample()))
    def test_no_missing_fixture(self):
        obj=sample();obj['fixture']['fixture_id']=None
        with self.assertRaises(ValueError):identity_from_payload(obj)
    def test_wrong_match_id_in_url(self):
        obj=sample();obj['match']['event_id']=7899093
        with self.assertRaises(ValueError):identity_from_payload(obj)
if __name__=='__main__':unittest.main(verbosity=2)
