#!/usr/bin/env python3
"""Pruebas autónomas (sin TXT, sin Playwright, sin llamadas API)."""
import unittest
import market_quality_gate as g

D={'fixture_id':100,'home':'Local','away':'Visitante','gender':'M','kickoff_madrid':'2026-11-02T20:30:00+01:00'}
S={**D,'details_checked':True,
   'market_profiles':{'corners':{'coverage_class':'SUFICIENTE','n_series':{
       'home_for':8,'home_against':8,'away_for':8,'away_against':8}}},
   'home_evidence':{'history':{'same_competition_goals_for':{'n':15},'same_competition_goals_against':{'n':15}}},
   'away_evidence':{'history':{'same_competition_goals_for':{'n':17},'same_competition_goals_against':{'n':17}}}}

class MinimalTests(unittest.TestCase):
    def test_levels(self):
        rows=[{'market':'Total de córners | Tiempo reglamentario','selection':'Más de 8,5','odd':o} for o in [1.006,1.10,1.20,1.38]]
        s,items=g.analyze_rows(rows,D,S)
        self.assertEqual((s['below_observation'],s['low_odd_observation'],s['price_eligible']),(1,1,2))
        self.assertEqual(s['researchable_after_data_gate'],2)
    def test_time_splits_not_inferred(self):
        s,items=g.analyze_rows([{'market':'Total de córners | 2ª parte','selection':'Más de 4,5','odd':1.4}],D,S)
        self.assertEqual(items[0]['data_status'],'NEEDS_PERIOD_SPLIT')
    def test_player_requires_individual_evidence(self):
        s,items=g.analyze_rows([{'market':'Jugador - Total de tiros a portería (1+)','selection':'Jugador A','odd':1.4}],D,S)
        self.assertEqual(items[0]['data_status'],'PLAYER_DATA_AND_XI_REQUIRED')
    def test_identity_mismatch(self):
        ss=dict(S,away='Otro equipo')
        s,items=g.analyze_rows([{'market':'Total de córners','selection':'Más de 9,5','odd':1.5}],D,ss)
        self.assertEqual(s['researchable_after_data_gate'],0)
        self.assertEqual(items[0]['data_status'],'IDENTITY_MISMATCH')
    def test_history_goals_is_not_probability(self):
        s,items=g.analyze_rows([{'market':'Total de goles | Tiempo reglamentario','selection':'Más de 2,5','odd':1.5}],D,S)
        self.assertEqual(items[0]['decision'],'READY_FOR_RESEARCH')
        self.assertIn('no demuestra EV',items[0]['rationale'])

if __name__=='__main__':unittest.main(verbosity=2)
