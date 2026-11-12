import csv, json, tempfile, unittest
from pathlib import Path
from prematch_predictive_evidence_ranking_v1 import build

SOURCE=Path('outputs/discovery/2026-11-08_1500_0300/prospective_trial/predictive_stability/1.0-mvp/20261107T235531+0100_2cf2b84cc40b_code-810f03a2ea4f/predictive_patterns.json')

class EvidenceRankingTests(unittest.TestCase):
    def test_canonical_counts_and_all_fixtures(self):
        with tempfile.TemporaryDirectory() as td:
            rows,m=build(SOURCE,Path(td))
            self.assertEqual(len(rows),290); self.assertEqual(m['patterns'],8021)
            with (Path(td)/'ranking_predictibilidad_20261108.csv').open(encoding='utf-8-sig') as f:
                data=list(csv.DictReader(f))
            self.assertEqual(len(data),290); self.assertEqual(len({x['fixture_id'] for x in data}),290)

    def test_atletico_barcelona_is_canonical_and_not_odds_ranked(self):
        with tempfile.TemporaryDirectory() as td:
            rows,_=build(SOURCE,Path(td)); row=next(x for x in rows if str(x['fixture_id'])=='1570443')
            self.assertEqual(row['patrones_respaldados'],2)
            self.assertEqual(row['nivel_confianza'],'EVIDENCE_PRIORITY_UNCALIBRATED')
            self.assertNotIn('Bwin',row['motivos_ranking'])

    def test_family_files_and_determinism(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            build(SOURCE,Path(a)); build(SOURCE,Path(b))
            for name in ('ranking_goals_20261108.csv','ranking_corners_20261108.csv','ranking_offsides_20261108.csv','manifest.json'):
                self.assertEqual((Path(a)/name).read_bytes(),(Path(b)/name).read_bytes())

    def test_metric_categories_do_not_collapse_on_target_or_xg(self):
        with tempfile.TemporaryDirectory() as td:
            build(SOURCE,Path(td))
            def count(name):
                with (Path(td)/name).open(encoding='utf-8-sig') as f:
                    return sum(1 for _ in csv.DictReader(f))
            # Canonical counts: on_target is a distinct metric under family=shots;
            # yellow is family=cards; xg remains in the goals view.
            self.assertEqual(count('ranking_on_target_20261108.csv'), 290)
            self.assertEqual(count('ranking_yellow_20261108.csv'), 290)
            self.assertEqual(count('ranking_goals_20261108.csv'), 290)
            self.assertEqual(count('ranking_shots_20261108.csv'), 290)

if __name__=='__main__': unittest.main()
