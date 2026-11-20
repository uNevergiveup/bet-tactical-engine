import copy
import unittest
from prematch_phase2 import build_player_duels


class PlayerDuelTests(unittest.TestCase):
    def rows(self):
        return [{'team_id':tid,'player_id':pid,'fixture_id':fid,'fixture_status':'FT','season':2026,
            'league_id':9,'position':position,'performance_eligible':True,'minutes':minutes,
            'player_name':str(pid),'metrics':{'fouls_drawn':value if tid==1 else None,
            'fouls_committed':value if tid==2 else None},'provenance':[]}
            for tid,pid,position in ((1,7,'F'),(2,8,'D'))
            for fid,minutes,value in ((10,30,0),(11,60,2),(12,90,4))]

    def test_exposure_weighted_rates_do_not_claim_direct_duels_or_target_xi(self):
        doc=build_player_duels({'fixture_id':99,'home_id':1,'away_id':2,'season':2026},self.rows())
        profile=next(p for p in doc['profiles'] if p['team_id']==1)
        self.assertEqual(profile['metrics']['fouls_drawn']['per90'],3)
        hypothesis=next(h for h in doc['hypotheses'] if h['status'].startswith('EXPLORATORY'))
        self.assertEqual(hypothesis['historical_coappearance_fixture_ids'],[10,11,12])
        self.assertFalse(hypothesis['direct_foul_interaction_observed'])
        self.assertFalse(doc['current_lineup_confirmed']);self.assertFalse(hypothesis['value_validated'])

    def test_missing_and_other_seasons_do_not_become_zero_or_pooled_support(self):
        rows=self.rows();rows[0]['metrics']['fouls_drawn']=None;rows[1]['season']=2025
        doc=build_player_duels({'fixture_id':99,'home_id':1,'away_id':2,'season':2026},rows)
        profile=next(p for p in doc['profiles'] if p['team_id']==1)['metrics']['fouls_drawn']
        self.assertEqual(profile['n'],1);self.assertEqual(profile['missing_n'],1)
        self.assertFalse(profile['exposure_sufficient'])
        self.assertEqual(doc['excluded_observations']['OTHER_SEASON_NOT_POOLED'],1)

    def test_duplicate_or_wrong_team_evidence_rejected(self):
        match={'fixture_id':99,'home_id':1,'away_id':2,'season':2026};rows=self.rows()
        with self.assertRaises(ValueError):build_player_duels(match,rows+[copy.deepcopy(rows[0])])
        rows[0]['team_id']=3
        with self.assertRaises(ValueError):build_player_duels(match,rows)

    def test_player_seen_for_both_clubs_requires_affiliation_review(self):
        rows=self.rows()
        for row in rows:row['player_id']=7
        doc=build_player_duels({'fixture_id':99,'home_id':1,'away_id':2,'season':2026},rows)
        self.assertEqual(doc['current_affiliation_requires_review_player_ids'],[7])
        self.assertEqual(len(doc['profiles']),2)
        self.assertEqual(doc['hypotheses'],[])


if __name__=='__main__':unittest.main()
