import json
import tempfile
import unittest
import io
from datetime import datetime
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import prematch_daily as daily
from test_prematch_phase2 import fixture


class Phase3Tests(unittest.TestCase):
    def test_official_profiles_and_midnight_day(self):
        root=Path(__file__).resolve().parent
        config,_,_=daily.trial.load_window_config(root)
        for profile in ('normal','ampliada'):
            _,start,end=daily.trial.resolve_window(config,profile)
            self.assertEqual((start.isoformat(),end.isoformat()),('15:00:00','03:00:00'))
            for at,expected in [('2026-11-08T00:00:00+01:00','2026-11-07'),('2026-11-08T02:59:59+01:00','2026-11-07'),('2026-11-08T03:00:00+01:00','2026-11-08')]:
                self.assertEqual(str(daily.automatic_day(datetime.fromisoformat(at),start,end)),expected)

    def test_rank_cli_resolves_real_official_folder_without_preselection(self):
        with tempfile.TemporaryDirectory() as td, patch('socket.socket.connect',side_effect=AssertionError('network')):
            output=io.StringIO()
            with patch.object(daily.trial,'now_madrid',return_value=datetime.fromisoformat('2026-11-07T12:00:00+01:00')),redirect_stdout(output):
                code=daily.main(['--rank','--output-dir',td])
            self.assertEqual(code,0)
            result=json.loads(output.getvalue())
            self.assertTrue(result['source_preselection'].endswith('2026-11-07_1500_0300/prospective_trial/enriched/preselection.json'))
            ranking=json.loads((Path(result['path'])/'ranking.json').read_text())
            self.assertEqual(ranking['window']['start'],'2026-11-07T15:00:00+01:00')
            self.assertEqual(ranking['window']['end'],'2026-11-08T03:00:00+01:00')
            self.assertTrue(any('OLDER_THAN' in x for x in ranking['warnings']))

    def test_explicit_other_matchday_rejected_and_missing_day_no_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            a=self.setup(td);a.rank=True;a.date='2026-11-08'
            a.preselection=a.root/'outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json'
            with self.assertRaisesRegex(ValueError,'PRESELECTION_WINDOW_MISMATCH'):daily.operational_research(a)
            a.root=Path(td);a.preselection=None
            with self.assertRaisesRegex(daily.trial.TrialError,'2026-11-08_1500_0300'):daily.operational_research(a)

    def setup(self, td):
        root = Path(__file__).resolve().parent; out = Path(td)
        m = fixture(5); m['kickoff_madrid'] = '2026-11-07T20:00:00+01:00'
        canonical = {k:v for k,v in m.items() if k != 'statistical_observations'}
        canonical.update(status='NS', category_status='ELIGIBLE')
        pre=out/'pre.json'; can=out/'fixtures.json'
        pre.write_text(json.dumps({'generated_at_utc':'2026-11-07T10:00:00Z','matches':[m]}))
        can.write_text(json.dumps({'fixtures':[canonical]}))
        return SimpleNamespace(root=root,asof='2026-11-07T12:00:00Z',online=False,max_api_calls=0,
            window=None,start=None,end=None,date='2026-11-07',preselection=pre,fixtures_input=can,
            fixture_id=None,min_lead=45,rank=False,output_dir=out/'results',preflight=None,max_odds_age=120)

    def test_manual_and_auto_are_isolated_and_no_network(self):
        with tempfile.TemporaryDirectory() as td, patch('socket.socket.connect',side_effect=AssertionError('network')):
            a=self.setup(td)
            auto=daily.operational_research(a);a.fixture_id=1550188;manual=daily.operational_research(a)
            self.assertEqual(auto['fixture_id'],manual['fixture_id'])
            self.assertNotEqual(auto['path'],manual['path'])
            doc=json.loads((Path(auto['path'])/'research_package.json').read_text())
            self.assertFalse(doc['market_identity_ready']);self.assertFalse(doc['actionable_bet'])
            self.assertEqual(doc['acquisition']['api_calls'],0)
            self.assertEqual(len(list(Path(auto['path']).parent.iterdir())),1)

    def test_ranking_is_reproducible(self):
        with tempfile.TemporaryDirectory() as td:
            a=self.setup(td);a.rank=True
            first=daily.operational_research(a);second=daily.operational_research(a)
            self.assertEqual(first['shortlist'],second['shortlist'])

    def test_live_identity_and_temporal_conflicts_block(self):
        for field,value in [('status','1H'),('home_id',8),('gender','UNKNOWN')]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as td:
                a=self.setup(td); d=json.loads(a.fixtures_input.read_text());d['fixtures'][0][field]=value
                a.fixtures_input.write_text(json.dumps(d))
                with self.assertRaisesRegex(ValueError,'NO_ELIGIBLE_FIXTURE'):daily.operational_research(a)
        with tempfile.TemporaryDirectory() as td:
            a=self.setup(td);a.asof='2026-11-07T20:01:00+01:00'
            with self.assertRaisesRegex(ValueError,'NO_ELIGIBLE_FIXTURE'):daily.operational_research(a)

    def test_budget_and_simulated_online_block(self):
        with tempfile.TemporaryDirectory() as td:
            a=self.setup(td);a.max_api_calls=26
            with self.assertRaises(ValueError):daily.operational_research(a)
            a.max_api_calls=1;a.online=True
            with self.assertRaises(ValueError):daily.operational_research(a)

    def test_missing_manual_fixture_and_corrupt_identity_sha(self):
        with tempfile.TemporaryDirectory() as td:
            a=self.setup(td);a.fixture_id=99
            with self.assertRaisesRegex(ValueError,'NO_ELIGIBLE_FIXTURE'):daily.operational_research(a)
            d=json.loads(a.preselection.read_text());d['source_filter_sha256']='0'*64;a.preselection.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError,'SHA_MISMATCH'):daily.operational_research(a)


if __name__=='__main__':unittest.main()
