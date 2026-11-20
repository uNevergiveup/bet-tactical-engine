"""Regression cases for null detail blocks and additional observed API fields."""
import unittest
from preselect_matches import read_stat_block
from prematch_tactical_history import observations_from_games

class CoverageRegression(unittest.TestCase):
    def game(self):
        return {'id':1,'home_id':2,'away_id':3,'status':'FT','team_goals':1,
                'opponent_goals':0,'date_utc':'2026-10-01T12:00:00+00:00',
                'league_id':5,'season':2026,'venue':'HOME'}
    def test_null_blocks_do_not_become_valid_from_history_goals(self):
        raw={'statistics':[{'team':{'id':2},'statistics':[{'type':'Total Shots','value':None}]}]}
        row=observations_from_games([self.game()],2,{1:raw},{1})[0]
        self.assertEqual(row['statistics_request_status'],'REQUESTED_MISSING')
        self.assertEqual(row['own'],{'goals':1})
    def test_observed_zero_is_detail_and_missing_stays_absent(self):
        raw={'statistics':[{'team':{'id':2},'statistics':[{'type':'Red Cards','value':0}]}]}
        row=observations_from_games([self.game()],2,{1:raw},{1})[0]
        self.assertEqual(row['statistics_request_status'],'VALID')
        self.assertEqual(row['own']['red'],0)
        self.assertNotIn('shots',row['own'])
    def test_additional_fields_and_team_identity(self):
        raw={'statistics':[{'team':{'id':2},'statistics':[
            {'type':'Shots off Goal','value':6},{'type':'Passes %','value':'81%'},
            {'type':'Goalkeeper Saves','value':None}]}]}
        self.assertEqual(read_stat_block(raw,2),{'shots_off_target':6.0,'pass_accuracy_pct':81.0})
        self.assertEqual(read_stat_block(raw,3),{})
