import unittest
from datetime import timedelta
from prematch_tactical_signal_engine import coverage_summary, capture_age_score, market_link_counts, analyze_fixture
from test_prematch_tactical_signal_engine import NOW,fixture,observations

class RevisionTests(unittest.TestCase):
    def test_age_boundaries(self):
        for minutes in (0,1/60,1,15,120):
            self.assertEqual(capture_age_score(NOW-timedelta(minutes=minutes),NOW),10)
        for minutes in (-1/60,120+1/60):
            self.assertEqual(capture_age_score(NOW-timedelta(minutes=minutes),NOW),0)
    def test_reconciled_population(self):
        data=observations(9)
        for rows in data.values():
            for row in rows[7:]:row['statistics_requested']=False
        summary=analyze_fixture(fixture(),NOW,data)['coverage_summary']
        for row in [summary,*summary['by_metric_team_denominator']]:
            self.assertEqual(row['historical_matches_available'],row['historical_matches_detailed']+row['historical_matches_not_detailed'])
        self.assertNotEqual(summary['representative_series']['metric'],'xg')
    def test_deduplicated_supported_links(self):
        link={'market':'Total','selection':'Over','odd':2,'line':2.5,'relation':'INDIRECT_TEAM_TO_MATCH_TOTAL'}
        signals=[{'metric':'goals','status':'SUPPORTED_DESCRIPTIVE_SIGNAL','markets':[link]},
                 {'metric':'goals','status':'SUPPORTED_DESCRIPTIVE_SIGNAL','markets':[link]},
                 {'metric':'goals','status':'UNSTABLE_PATTERN','markets':[link]}]
        c=market_link_counts(signals)
        self.assertEqual(c['raw_signal_market_links'],3)
        self.assertEqual(c['supported_signal_market_links'],2)
        self.assertEqual(c['unique_supported_selections'],1)
        self.assertEqual(c['indirect_supported'],1)
        self.assertEqual(c['direct_supported'],0)

if __name__=='__main__':unittest.main()
