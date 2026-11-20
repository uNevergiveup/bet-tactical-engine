import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
from prematch_tactical_history import phase1_preselection
from test_prematch_phase2_player_integrity import write_cache

CUT = datetime(2026,11,12,3,tzinfo=timezone.utc)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cache=self.root/'cache';self.cache.mkdir()
        self.source=self.root/'fixtures.json'
        target={'fixture_id':99,'home_id':1,'away_id':2,'league_id':3,'season':2026,
                'home':'Synthetic A','away':'Synthetic B','gender':'UNKNOWN','category_status':'EXCLUDED',
                'kickoff_madrid':'2026-11-12T18:00:00+01:00','kickoff_utc':'2026-11-12T17:00:00Z'}
        self.doc={'generated_at_utc':'2026-11-12T02:00:00Z','fixtures':[target,dict(target,fixture_id=100)]}
        self.source.write_text(json.dumps(self.doc))
        self.raw={'fixture':{'id':10,'date':'2026-11-05T12:00:00Z','status':{'short':'FT'}},
                  'league':{'id':3,'season':2026},'teams':{'home':{'id':1},'away':{'id':2}},
                  'goals':{'home':0,'away':1},'score':{'halftime':{'home':0,'away':0}}}
        write_cache(self.cache,'fixtures','id=10',[self.raw],fetched='2026-11-12T02:30:00Z')

    def test_all_targets_provenance_zero_and_missing_preserved_without_network(self):
        stats=[{'team':{'id':1},'statistics':[{'type':'Total Shots','value':0},{'type':'Fouls','value':None}]}]
        write_cache(self.cache,'fixtures/statistics','fixture=10',stats,fetched='2026-11-12T02:35:00Z')
        with patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK')):
            d=phase1_preselection(self.source,self.cache,CUT)
        self.assertEqual(len(d['matches']),2);self.assertEqual(d['api_calls'],0)
        from prematch_predictive_engine import validate_source
        validate_source(d,CUT)
        row=d['matches'][0];self.assertEqual(row['category_status'],'EXCLUDED')
        obs=row['statistical_observations']['home'][0]
        self.assertEqual(obs['own']['shots'],0);self.assertIsNone(obs['own'].get('fouls'))
        self.assertTrue(obs['provenance']);self.assertEqual(obs['league_id'],3)

    def test_postcutoff_source_and_conflicting_history_excluded(self):
        stats=[{'team':{'id':1},'statistics':[{'type':'Total Shots','value':999}]}]
        write_cache(self.cache,'fixtures/statistics','fixture=10',stats,fetched='2026-11-12T04:00:00Z')
        d=phase1_preselection(self.source,self.cache,CUT)
        self.assertIsNone(d['matches'][0]['statistical_observations']['home'][0]['own'].get('shots'))
        other=copy.deepcopy(self.raw);other['league']['id']=4
        write_cache(self.cache,'fixtures','id=10',[other],fetched='2026-11-12T02:35:00Z',suffix='conflict')
        d=phase1_preselection(self.source,self.cache,CUT)
        self.assertFalse(d['matches'][0]['statistical_observations']['home'])
        self.assertTrue(any(r['reason']=='HISTORICAL_IDENTITY_CONFLICT' for r in d['audit']['rejected']))

    def test_embedded_detail_before_cutoff_beats_direct_response_after_cutoff(self):
        from prematch_phase2 import PlayerCache
        raw=copy.deepcopy(self.raw);raw['players']=[{'team':{'id':1},'players':[]}]
        write_cache(self.cache,'fixtures','ids=10',[raw],fetched='2026-11-12T02:35:00Z')
        write_cache(self.cache,'fixtures/players','fixture=10',[{'team':{'id':999}}],fetched='2026-11-12T04:00:00Z')
        rows,ref=PlayerCache(self.cache).get('fixtures/players',10,CUT)
        self.assertEqual(rows[0]['team']['id'],1)
        self.assertEqual(ref['embedded_field'],'players')
        self.assertEqual(ref['endpoint'],'fixtures')


if __name__=='__main__':unittest.main()
