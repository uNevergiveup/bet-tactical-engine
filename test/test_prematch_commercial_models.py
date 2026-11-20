import unittest
from prematch_commercial_models import contract,estimate,eligible,validate,prematch_features

class CommercialTests(unittest.TestCase):
    target={'home':'Alpha','away':'Beta','home_id':1,'away_id':2,'league_id':3,'season':2026,'gender':'UNKNOWN','kickoff_madrid':'2026-11-12T15:00:00+01:00'}
    def c(self,name,selection='Más de 2,5'):
        return contract({'market':name,'selection':selection,'odd':'1.8'},self.target)
    def test_match_total_not_team(self):
        self.assertEqual(self.c('Total de goles | Tiempo reglamentario')['scope'],'MATCH')
        c=self.c('Alpha - Total de goles | Tiempo reglamentario');self.assertEqual((c['scope'],c['team_id']),('TEAM',1))
    def test_halves_and_direction(self):
        self.assertEqual(self.c('Total de goles | 1ª parte','Menos de 1,5')['line'],{'direction':'UNDER','line':1.5})
        self.assertEqual(self.c('Total de goles | 2ª parte')['period'],'SECOND_HALF')
    def test_handicap_and_combination_not_total(self):
        for name in ['Hándicap 1X2 (-1) - Tiempo reglamentario','Ambos equipos marcan y total de goles 2,5 | Tiempo reglamentario','Resultado del partido | Tiempo reglamentario']:
            self.assertEqual(self.c(name)['status'],'UNSUPPORTED_CONTRACT')
    def test_unknown_player_owner_blocked(self):
        self.assertEqual(self.c('Player X - Total de remates | Tiempo reglamentario')['status'],'OWNER_UNRESOLVED')
    def test_verified_female_fc_owner_alias(self):
        from prematch_market_mapping_v1 import exact_owner_alias
        self.assertTrue(exact_owner_alias('FC Barcelona (F)','Barcelona W'))
        self.assertFalse(exact_owner_alias('FC Barcelona (F)','Barcelona'))
        self.assertFalse(exact_owner_alias('FC Barcelona (F)','Arsenal W'))
    def test_missing_line_and_metric(self):
        self.assertEqual(self.c('Total de goles | Tiempo reglamentario','Más de')['status'],'UNSUPPORTED_CONTRACT')
        self.assertIsNone(self.c('Total xG | Tiempo reglamentario')['metric'])
    def test_sample_uncalibrated_and_settlement(self):
        self.assertIsNone(estimate([1,2],{'line':2.5,'direction':'OVER'})['probability'])
        r=estimate([0,1,4,5],{'line':2.5,'direction':'OVER'})
        self.assertFalse(r['calibrated']);self.assertIsNone(r['probability'])
        self.assertEqual(estimate([0,1,4],{'line':2,'direction':'OVER'})['status'],'SETTLEMENT_MODEL_UNAVAILABLE')
    def test_joint_total_deduplicates_and_rejects_future(self):
        rows=[]
        for team,a,b in [(1,2,1),(2,1,2)]:
            rows.append({'entity_type':'TEAM','fixture_id':9,'team_id':team,'league_id':3,'season':2026,'gender':'UNKNOWN','kickoff_utc':'2026-10-01T12:00:00Z','available_at_utc':'2026-10-01T15:00:00Z','metrics':{'own.goals':a,'conceded.goals':b}})
        h=eligible(rows,self.target,'goals','FULL_TIME');self.assertEqual(len(h),1);self.assertEqual(h[0]['value'],3)
        rows[0]['available_at_utc']='2026-12-01T00:00:00Z';rows.pop()
        self.assertEqual(eligible(rows,self.target,'goals','FULL_TIME'),[])
    def test_no_data_and_temporal_evaluation(self):
        self.assertEqual(validate([],{'line':2.5,'direction':'OVER'})['n'],0)
        h=[{'fixture_id':i,'kickoff':f'2026-10-{i+1:02}T12:00:00Z','available':f'2026-10-{i+1:02}T15:00:00Z','value':i%4} for i in range(10)]
        r=validate(h,{'line':2.5,'direction':'OVER'});self.assertEqual(r['n'],7);self.assertFalse(r['calibrated'])
    def test_postmatch_and_xi_never_authorized(self):
        for field,kind in [('own.shots','POSTMATCH_ONLY'),('starter','LINEUP_DEPENDENT'),('venue','PREMATCH_OBSERVABLE')]:
            r=prematch_features({'variables':{'condition':{'field':field}},'counterevidence_fixture_ids':[3]})
            self.assertEqual(r['classification'],kind);self.assertFalse(r['authorized_predictive_use'])
