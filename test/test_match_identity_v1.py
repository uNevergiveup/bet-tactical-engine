import json
import tempfile
import unittest
from pathlib import Path
from match_identity import identity_from_payload, validate_identity, compare_report_name, write_identity, normalize_category_gender

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
    def test_browser_event_time_schema_supported(self):
        obj=sample()
        obj['match']['identity_status']='FIXTURE_AND_BWIN_TIME_VERIFIED'
        obj['match']['temporal_identity_evidence']={
            'schema_version':'1.0-browser-event-time',
            'source':'SINGLE_EVENT_TILE',
            'timezone':'Europe/Madrid',
            'kickoff_utc':'2026-10-31T17:30:00+00:00',
            'catalog_observations':[{'bwin_event_id':'7899092','bwin_url':URL}],
        }
        res=identity_from_payload(obj)
        self.assertEqual(res['identity_status'],'FIXTURE_AND_BWIN_TIME_VERIFIED')
        self.assertTrue(validate_identity(res,obj,'7899092',URL))
    def test_gender_normalization_helpers(self):
        for val in ('F', 'f', 'female', 'Female', 'women', 'Women', 'femenino', 'femenina'):
            self.assertEqual(normalize_category_gender(val), 'female')
        for val in ('M', 'm', 'male', 'Male', 'men', 'masculino', 'masculina', None, ''):
            self.assertEqual(normalize_category_gender(val), 'male')
    def test_valid_gender_equivalences(self):
        chelsea_f = {'api_name': 'Chelsea W', 'bwin_name': 'Chelsea W', 'category': 'F'}
        psg_f = {'api_name': 'Paris Saint Germain W', 'bwin_name': 'Paris Saint Germain W', 'category': 'F'}
        chelsea_female = {'api_name': 'Chelsea W', 'bwin_name': 'Chelsea W', 'category': 'female'}
        psg_female = {'api_name': 'Paris Saint Germain W', 'bwin_name': 'Paris Saint Germain W', 'category': 'female'}
        self.assertEqual(compare_report_name('Chelsea W', chelsea_f, psg_f), 'EXACT')
        self.assertEqual(compare_report_name('Paris Saint Germain W', psg_f, chelsea_f), 'EXACT')
        self.assertEqual(compare_report_name('Chelsea W', chelsea_female, psg_female), 'EXACT')
        self.assertEqual(compare_report_name('Paris Saint Germain W', psg_female, chelsea_female), 'EXACT')
        madrid_m = {'api_name': 'Real Madrid', 'bwin_name': 'Real Madrid', 'category': 'M'}
        barca_m = {'api_name': 'Barcelona', 'bwin_name': 'Barcelona', 'category': 'M'}
        madrid_male = {'api_name': 'Real Madrid', 'bwin_name': 'Real Madrid', 'category': 'male'}
        barca_male = {'api_name': 'Barcelona', 'bwin_name': 'Barcelona', 'category': 'male'}
        self.assertEqual(compare_report_name('Real Madrid', madrid_m, barca_m), 'EXACT')
        self.assertEqual(compare_report_name('Real Madrid', madrid_male, barca_male), 'EXACT')
    def test_validate_identity_gender_normalization(self):
        s = sample()
        s['match']['home_gender'] = 'F'
        s['match']['away_gender'] = 'F'
        x = identity_from_payload(s)
        self.assertTrue(validate_identity(x, s, '7899092', URL))
        x['home']['category'] = 'female'
        x['away']['category'] = 'female'
        self.assertTrue(validate_identity(x, s, '7899092', URL))
        s2 = sample()
        x2 = identity_from_payload(s2)
        x2['home']['category'] = 'M'
        x2['away']['category'] = 'M'
        self.assertTrue(validate_identity(x2, s2, '7899092', URL))
    def test_real_gender_conflicts(self):
        chelsea_f = {'api_name': 'Chelsea W', 'bwin_name': 'Chelsea W', 'category': 'F'}
        psg_f = {'api_name': 'Paris Saint Germain W', 'bwin_name': 'Paris Saint Germain W', 'category': 'F'}
        chelsea_female = {'api_name': 'Chelsea W', 'bwin_name': 'Chelsea W', 'category': 'female'}
        psg_female = {'api_name': 'Paris Saint Germain W', 'bwin_name': 'Paris Saint Germain W', 'category': 'female'}
        self.assertEqual(compare_report_name('Chelsea Men', chelsea_f, psg_f), 'MISMATCH')
        self.assertEqual(compare_report_name('Chelsea Male', chelsea_female, psg_female), 'MISMATCH')
        self.assertEqual(compare_report_name('Chelsea Masculino', chelsea_f, psg_f), 'MISMATCH')
        madrid_m = {'api_name': 'Real Madrid', 'bwin_name': 'Real Madrid', 'category': 'M'}
        barca_m = {'api_name': 'Barcelona', 'bwin_name': 'Barcelona', 'category': 'M'}
        madrid_male = {'api_name': 'Real Madrid', 'bwin_name': 'Real Madrid', 'category': 'male'}
        barca_male = {'api_name': 'Barcelona', 'bwin_name': 'Barcelona', 'category': 'male'}
        self.assertEqual(compare_report_name('Real Madrid Femenino', madrid_m, barca_m), 'MISMATCH')
        self.assertEqual(compare_report_name('Real Madrid W', madrid_male, barca_male), 'MISMATCH')
        self.assertEqual(compare_report_name('Real Madrid Women', madrid_male, barca_male), 'MISMATCH')
        self.assertEqual(compare_report_name('Real Madrid F', madrid_m, barca_m), 'MISMATCH')
    def test_validate_identity_rejects_category_mismatch(self):
        s = sample()
        s['match']['home_gender'] = 'F'
        s['match']['away_gender'] = 'F'
        x = identity_from_payload(s)
        x['home']['category'] = 'male'
        with self.assertRaises(ValueError):
            validate_identity(x, s, '7899092', URL)
if __name__=='__main__':unittest.main(verbosity=2)
