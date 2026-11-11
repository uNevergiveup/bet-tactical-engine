import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prematch_market_mapping_v1 as mapping
import prematch_predictive_evidence_ranking_v1 as ranking
import prematch_candidate_selector_v1 as selector
import test_prematch_market_mapping_v1 as fixtures

ROOT=Path(__file__).resolve().parent
DAY9=ROOT/'outputs/discovery/2026-11-09_1500_0300/prospective_trial'
SOURCE9=next((DAY9/'predictive').glob('**/predictive_patterns.json'))
SOURCE8=ROOT/'outputs/discovery/2026-11-08_1500_0300/prospective_trial/predictive_stability/1.0-mvp/20261107T235531+0100_2cf2b84cc40b_code-810f03a2ea4f/predictive_patterns.json'


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.net=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.net.start(); self.addCleanup(self.net.stop)

    def inputs(self,**kw):
        td,p,pf=fixtures.MarketMappingTests().make_inputs(**kw)
        self.addCleanup(td.cleanup)
        return p,pf

    def test_ft_does_not_match_half(self):
        for period in ('1ª parte','2ª parte'):
            p,pf=self.inputs(rows=[['1','99',f'Home - Total de goles | {period}','Más de 1,5','1.8','total_goals','','']])
            r=mapping.map_markets(p,[pf])['mappings'][0]
            self.assertEqual(r['reason'],'ONLY_INCOMPATIBLE_PERIOD_OBSERVED')
            self.assertEqual(r['market_links'],[])

    def test_raw_price_tampering_is_blocked(self):
        p,pf=self.inputs()
        review=pf.parent/'market_selection_review.csv'
        review.write_text(review.read_text().replace('1.80','3.20'))
        r=mapping.map_markets(p,[pf])['mappings'][0]
        self.assertEqual(r['reason'],'MARKET_REVIEW_NOT_IN_ORIGINAL_TXT')

    def test_live_event_never_matches(self):
        p,pf=self.inputs();doc=json.loads(pf.read_text());doc['matches'][0]['is_live']=True
        pf.write_text(json.dumps(doc))
        self.assertEqual(mapping.map_markets(p,[pf])['mappings'][0]['reason'],'LIVE_EVENT_REJECTED')
        self.assertEqual(mapping.market_period('Home - Total de goles LIVE | Tiempo reglamentario'),'LIVE')

    def test_observed_market_without_usable_price_is_distinct(self):
        p,pf=self.inputs(rows=[['1','99','Home - Total de goles | Tiempo reglamentario','Más de 1,5','0','total_goals','','']])
        row=mapping.map_markets(p,[pf])['mappings'][0]
        self.assertEqual(row['observation_status'],'MARKET_OBSERVED_WITHOUT_USABLE_PRICE')

    def test_reused_event_and_stale_evidence_block(self):
        p,pf=self.inputs()
        original=json.loads(pf.read_text());bad=json.loads(pf.read_text())
        bad['matches'].append(dict(bad['matches'][0],fixture_id=2))
        pf.write_text(json.dumps(bad))
        self.assertEqual(mapping.map_markets(p,[pf])['mappings'][0]['reason'],'BWIN_EVENT_REUSED_BY_MULTIPLE_FIXTURES')
        original['matches'][0]['time_evidence']['observed_at_utc']='2026-11-08T00:00:00+00:00'
        pf.write_text(json.dumps(original))
        self.assertEqual(mapping.map_markets(p,[pf])['mappings'][0]['reason'],'BWIN_TIME_EVIDENCE_UNDATED_OR_STALE')

    def test_verified_txt_modified_after_preflight_blocks(self):
        p,pf=self.inputs();d=json.loads(pf.read_text());txt=Path(d['matches'][0]['snapshot']['snapshot_path'])
        txt.write_text(txt.read_text()+'altered')
        self.assertEqual(mapping.map_markets(p,[pf])['mappings'][0]['reason'],'SNAPSHOT_SHA256_MISMATCH_OR_MISSING')

    def test_floor_and_trace(self):
        p,pf=self.inputs()
        row=mapping.map_markets(p,[pf],min_odd=2)['mappings'][0]
        self.assertEqual(row['mapping_status'],'DISCARDED_CANDIDATE')
        self.assertFalse(row['market_links'][0]['price_eligible'])
        good=mapping.map_markets(p,[pf],min_odd=1.8)['mappings'][0]
        self.assertEqual(good['mapping_status'],'MARKET_MATCHED')
        for key in ('period','snapshot_sha256','snapshot_path','captured_at','event_id','review_sha256'):
            self.assertTrue(good['market_links'][0][key])

    def test_offsides_xg_are_not_commercial(self):
        for metric in ('offsides','xg'):
            p,pf=self.inputs(metric=metric)
            self.assertEqual(mapping.map_markets(p,[pf])['mappings'][0]['mapping_status'],'UNSUPPORTED_MARKET')

    def test_real_pending_causes_exactly_853(self):
        ledger=json.loads((DAY9/'candidate_ledger.json').read_text())
        pats=json.loads(SOURCE9.read_text())['descriptive_discovery']['patterns']
        from collections import Counter
        captured={1549860,1565299,1632059,1632060,1645541}
        causes=Counter(mapping.pending_evidence(p['fixture_id'],ledger)['cause'] for p in pats if p['fixture_id'] not in captured)
        self.assertEqual(causes,{'CAPTURE_LIMIT_REACHED':707,'EVENT_NOT_FOUND_IN_SCANNED_PAGES':146})

    def test_generalized_both_days_idempotent_no_input_change(self):
        for source,n,day in ((SOURCE8,290,'2026-11-08'),(SOURCE9,45,'2026-11-09')):
            original=ranking.sha(source)
            with tempfile.TemporaryDirectory() as td:
                out=Path(td)/'rank'; rows,m=ranking.build(source,out,day=day)
                self.assertEqual(len(rows),n)
                rank_bytes={p.name:p.read_bytes() for p in out.iterdir()}
                ranking.build(source,out,day=day)
                self.assertEqual(rank_bytes,{p.name:p.read_bytes() for p in out.iterdir()})
                args={'asof':m['source_cutoff']}
                one=selector.select(out,Path(td)/'selection',**args)
                two=selector.select(out,Path(td)/'selection',**args)
                self.assertEqual(one,two)
                self.assertEqual(one['selection_policy']['statistical_universe'],n)
                with self.assertRaisesRegex(ValueError,'BEFORE_SOURCE_CUTOFF'):
                    selector.select(out,Path(td)/'invalid',asof='2020-01-01T00:00:00+00:00')
            self.assertEqual(original,ranking.sha(source))

    def test_duplicate_patterns_not_scored_twice_and_conflicts_fail(self):
        p={'pattern_id':'a','fixture_id':1}
        self.assertEqual(ranking.unique_patterns([p,p]),[p])
        with self.assertRaisesRegex(ValueError,'PATTERN_ID_CONFLICT'):
            ranking.unique_patterns([p,dict(p,fixture_id=2)])

    def test_other_date_dst_and_half_open_window(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'predictive.json'
            doc=json.loads(SOURCE9.read_text()); row=dict(doc['descriptive_discovery']['patterns'][0])
            row.update(pattern_id='synthetic',fixture_id=999999,kickoff_madrid='2027-03-28T22:00:00+02:00')
            doc.update(asof_madrid='2027-03-28T12:00:00+02:00');doc['descriptive_discovery']['patterns']=[row]
            p.write_text(json.dumps(doc)); rows,m=ranking.build(p,Path(td)/'new',day='2027-03-28')
            self.assertEqual(len(rows),1);self.assertEqual(m['date_tag'],'20270328')

if __name__=='__main__':unittest.main()
