import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import prematch_daily as d

ROOT=Path(__file__).resolve().parent
REAL=ROOT/'outputs/phase3/2026-11-07_1500_0300/runs/20261107T164129549308/bwin_catalog/bwin_discovery.json'
PRE=ROOT/'outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json'


class BwinPhase3Tests(unittest.TestCase):
    def sample(self):
        catalog=json.loads(REAL.read_text())
        catalog['matches']=[r for r in catalog['matches'] if r['fixture_id']==1570448]
        original=json.loads((PRE.parent.parent/'fixtures_major_test.json').read_text())
        canonical={'fixtures':[r for r in original['fixtures'] if r['fixture_id']==1570448]}
        when=datetime.fromisoformat(catalog['generated_at_utc'])+timedelta(seconds=30)
        return canonical,catalog,when

    def link(self,catalog=None,canonical=None,when=None):
        a,b,c=self.sample()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'catalog.json';path.write_text(json.dumps(catalog or b))
            return d.bwin_ranking_links(canonical or a,catalog or b,when or c,path)[0]['1570448']

    def test_real_catalog_verified_original_display_time(self):
        link=self.link()
        self.assertEqual(link['status'],'VERIFIED')
        self.assertEqual(link['event_id'],'7903872')
        self.assertEqual(link['evidence']['time_evidence']['difference_minutes'],0)

    def test_name_variant_keeps_identity_with_other_checks(self):
        a,b,c=self.sample();b['matches'][0]['inspection']['page_title']='CA Osasuna - Athletic Club LaLiga Fútbol | bwin'
        self.assertEqual(self.link(b,a,c)['status'],'VERIFIED')

    def test_incompatible_competition_date_and_category(self):
        for field,value in [('league_id',999),('season',2025),('kickoff_madrid','2026-11-09T18:30:00+01:00')]:
            a,b,c=self.sample();b['matches'][0][field]=value
            self.assertEqual(self.link(b,a,c)['status'],'CONFLICT')
        a,b,c=self.sample();a['fixtures'][0]['gender']='F'
        self.assertNotEqual(self.link(b,a,c)['status'],'VERIFIED')

    def test_ambiguity_duplicate_and_no_event(self):
        a,b,c=self.sample();b['matches'][0]['status']='AMBIGUOUS_MULTIPLE_EVENTS'
        self.assertEqual(self.link(b,a,c)['status'],'AMBIGUOUS')
        a,b,c=self.sample();b['matches'].append(copy.deepcopy(b['matches'][0]))
        self.assertEqual(self.link(b,a,c)['status'],'CONFLICT')
        a,b,c=self.sample();b['matches'][0].update(status='NOT_FOUND_IN_SCANNED_PAGES',bwin_event_id=None)
        self.assertEqual(self.link(b,a,c)['status'],'NOT_FOUND')

    def test_old_capture_and_started_match_cannot_verify(self):
        a,b,c=self.sample()
        for row in b['catalog_observed']:row['text_observed_at_utc']=(c-timedelta(hours=4)).isoformat()
        self.assertNotEqual(self.link(b,a,c)['status'],'VERIFIED')
        self.assertNotEqual(self.link(when=c+timedelta(days=1))['status'],'VERIFIED')
        a,b,c=self.sample();b.pop('user_time_reference',None)
        self.assertNotEqual(self.link(b,a,c)['status'],'VERIFIED')

    def test_austin_candidate_real_event_not_promoted_on_names(self):
        path=ROOT/'outputs/phase3/bwin_integration_austin_real/bwin_discovery.json'
        catalog=json.loads(path.read_text());a,b,c=self.sample()
        original=json.loads((PRE.parent.parent/'fixtures_major_test.json').read_text())
        can={'fixtures':[r for r in original['fixtures'] if r['fixture_id']==1490611]}
        links,_=d.bwin_ranking_links(can,catalog,c,path)
        self.assertEqual(links['1490611']['event_id'],'7903341')
        self.assertNotEqual(links['1490611']['status'],'VERIFIED')

    def test_archived_preflight_selector_check_and_reuse_no_capture(self):
        from prematch_single import main
        from prematch_test_fixtures import restored_selection_fixture
        from prematch_paths import local_reference
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();selected=restored_selection_fixture(root)
            saved=json.loads(selected.read_text());when=datetime.fromisoformat(saved['selection_asof_madrid'])+timedelta(seconds=5)
            with patch('prematch_single.local_now',return_value=when),patch('socket.socket.connect',side_effect=AssertionError('network')):
                self.assertEqual(main(['--root',str(root),'--selected',str(selected),'check']),0)
            pf=local_reference(root,saved['preflight_path'],saved['preflight_sha256'])
            document=json.loads(pf.read_text())
            pre=local_reference(root,document['source_preselection'],document['source_preselection_sha256'])
            catalog=local_reference(root,document['source_discovery'],document['source_sha256'])
            ranked=[{'fixture_id':1570448,'bwin':self.link()}]
            args=SimpleNamespace(asof=None,fixture_id=1570448,preflight=pf,min_lead=45,max_odds_age=120)
            def fake_cli(command, **kwargs):
                if 'select_single_match.py' in command[1]:
                    destination=Path(command[command.index('--output-dir')+1])
                    (destination/'selected_match.json').write_text(selected.read_text())
                return SimpleNamespace(returncode=0)
            out=root/'run'
            with patch('socket.socket.connect',side_effect=AssertionError('network')),patch.object(d.trial,'now_madrid',return_value=when),patch('subprocess.run',side_effect=fake_cli) as runner:
                result=d.select_verified_bwin(args,root,out,ranked,pre,catalog)
                self.assertEqual(result['status'],'SELECTED_AND_CHECKED_FOR_RESEARCH')
                self.assertEqual(result['preflight'],str(pf))
                self.assertFalse((out/'fixtures/1570448/selection_pipeline/preflight.log').exists())
                self.assertEqual(runner.call_count,2)


if __name__=='__main__':unittest.main()
