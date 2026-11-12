import json,tempfile,unittest
from pathlib import Path
import bwin_discovery as b
import bwin_offline_reconcile as r

class OfflineReconcileTests(unittest.TestCase):
    def fixture(self,**kw):
        value={'fixture_id':1,'home':'Albania','away':'Finland','home_id':10,'away_id':20,
            'kickoff_madrid':'2026-11-12T20:45:00+01:00','kickoff_utc':'2026-11-12T19:45:00Z',
            'league':'UEFA Nations League','league_id':5,'season':2026,'country':'World','gender':'UNKNOWN','category_status':'ELIGIBLE'}
        value.update(kw);return value
    def event(self,**kw):
        value={'bwin_event_id':'7909679','bwin_url':'https://www.bwin.es/es/sports/eventos/albania-finlandia-2:7909679',
            'text':'Albania Finlandia','text_observed_at_utc':'2026-11-12T02:45:03Z','source_pages':['today']}
        value.update(kw);return value
    def test_albania_finland_translation_generates_candidate_only(self):
        found=b.match_catalog(self.fixture(),[self.event()]);self.assertEqual(found[0]['bwin_event_id'],'7909679')
        row=r.reconcile_fixture(self.fixture(),[self.event()],{},[])
        self.assertEqual(row['post_status'],'CANDIDATE_TIME_UNVERIFIED');self.assertFalse(row['identity_verified'])
    def test_relative_tomorrow_uses_retained_capture_date(self):
        event=self.event(text='Llaneros FC Boyacá Chicó Mañana / 1:00',text_observed_at_utc='2026-11-12T02:45:07Z')
        self.assertEqual(b.catalog_relative_kickoff(event).isoformat(),'2026-11-13T01:00:00+01:00')
    def test_sao_bernardo_crosses_midnight(self):
        event=self.event(text='Sao Bernardo FC SP Athletic Club BR Mañana / 0:00')
        self.assertEqual(b.catalog_relative_kickoff(event).isoformat(),'2026-11-13T00:00:00+01:00')
        header={'prematch_header':True,'browser_timezone':'Europe/Madrid','date_label':'Mañana','time_label':'0:00',
            'capture_started_at_utc':'2026-11-12T02:45:42Z','captured_at_utc':'2026-11-12T02:45:44Z'}
        self.assertEqual(b.displayed_header_kickoff(header).isoformat(),'2026-11-13T00:00:00+01:00')
    def test_unknown_category_is_pending_not_male_or_conflict(self):
        f=self.fixture(home='Calvo Sotelo',away='Albacete',kickoff_madrid='2026-11-12T18:30:00+01:00',league='Copa del Rey')
        e=self.event(text='Calvo Sotelo Puertollano CF Albacete Balompié Hoy / 18:30',bwin_event_id='7910678',bwin_url='https://www.bwin.es/es/sports/eventos/calvo-sotelo-puertollano-cf-albacete-balompie-2:7910678')
        self.assertEqual(b.category_compatibility(f,e['text']),'CATEGORY_UNCONFIRMED')
    def test_time_conflict_remains_blocked(self):
        f=self.fixture(home='Real Potosí',away='Nacional Potosí',kickoff_madrid='2026-11-12T23:00:00+01:00',league='Copa Bolivia')
        e=self.event(text='Real Potosi CA Nacional Potosí Mañana / 0:00',bwin_event_id='7910426',bwin_url='https://www.bwin.es/es/sports/eventos/real-potosi-ca-nacional-potosi-2:7910426')
        self.assertEqual(r.reconcile_fixture(f,[e],{},[])['post_status'],'CANDIDATE_TIME_CONFLICT')
    def test_gender_conflict(self):
        self.assertEqual(b.category_compatibility(self.fixture(gender='M'),'Albania W Finland W'),'GENDER_CONFLICT')
    def test_reserve_conflict(self):
        self.assertEqual(b.category_compatibility(self.fixture(gender='M'),'Albania Reserves Finland Reserves'),'NON_ADULT_CONFLICT')
    def test_home_away_reverse_is_rejected(self):
        self.assertEqual(b.match_catalog(self.fixture(),[self.event(text='Finlandia Albania')]),[])
    def test_live_is_strictly_excluded(self):
        self.assertEqual(b.match_catalog(self.fixture(),[self.event(text='Albania Finlandia EN VIVO')]),[])
    def test_duplicate_candidates_are_ambiguous(self):
        events=[self.event(),self.event(bwin_event_id='2',bwin_url='https://www.bwin.es/es/sports/eventos/albania-finlandia-2:2')]
        self.assertEqual(r.reconcile_fixture(self.fixture(),events,{},[])['post_status'],'AMBIGUOUS_LOCAL_CANDIDATES')
    def test_same_teams_multiple_events_need_clock(self):
        e=self.event(text='Albania Finlandia Hoy / 20:45');e2=self.event(bwin_event_id='2',bwin_url='https://www.bwin.es/es/sports/eventos/albania-finlandia-2:2',text='Albania Finlandia Mañana / 20:45')
        self.assertEqual(r.reconcile_fixture(self.fixture(),[e,e2],{},[])['post_status'],'AMBIGUOUS_LOCAL_CANDIDATES')
    def test_absent_is_not_nonexistent(self):
        row=r.reconcile_fixture(self.fixture(),[],{},[])
        self.assertEqual(row['post_status'],'NOT_OBSERVED_IN_LOCAL_CATALOG');self.assertIn('DISCOVERY',row['recovery'])
    def test_full_inventory_includes_legacy_excluded_and_single_fixture_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);inv=root/'f.json';disc=root/'d.json'
            f1=self.fixture();f2=self.fixture(fixture_id=2,home='X',away='Y',home_id=30,away_id=40)
            inv.write_text(json.dumps({'eligible':[f1],'excluded':[f2],'fixtures':[f1,f2]}));disc.write_text(json.dumps({'catalog_observed':[],'matches':[],'pages':[]}))
            self.assertEqual(len(r.run(inv,disc)['fixtures']),2);self.assertEqual(r.run(inv,disc,[2])['fixtures'][0]['fixture_id'],2)
    def test_partial_catalog_and_event_without_markets_preserved(self):
        row=r.reconcile_fixture(self.fixture(),[self.event()],{},[{'url':'today','status':'OK'}])
        self.assertEqual(row['pages_scanned'][0]['url'],'today');self.assertEqual(row['event_id'],'7909679')
    def test_invalid_relative_clock_rejected(self):
        self.assertIsNone(b.catalog_relative_kickoff(self.event(text='Albania Finlandia Mañana / 25:90')))

    def test_frozen_real_cases_reproduced(self):
        root=Path(__file__).parent
        inv=root/'outputs/phase1/2026-11-12_1500_0300/fixtures_discovered.json'
        disc=root/'outputs/phase1/2026-11-12_1500_0300/browser_retry_02/bwin_scan/bwin_discovery.json'
        if not inv.is_file() or not disc.is_file():self.skipTest('Frozen 2026-11-12 catalog unavailable')
        rows={x['fixture_id']:x for x in r.run(inv,disc)['fixtures']}
        expected={1644370:('7910678','CANDIDATE_CATEGORY_UNKNOWN'),1520978:('7911881','CANDIDATE_CATEGORY_UNKNOWN'),
            1549870:('7912958','IDENTITY_VERIFIED_OFFLINE'),1528963:('7909679','CANDIDATE_TIME_UNVERIFIED'),
            1649147:('7910426','CANDIDATE_TIME_CONFLICT')}
        for fid,(eid,status) in expected.items():self.assertEqual((rows[fid]['event_id'],rows[fid]['post_status']),(eid,status))

if __name__=='__main__':unittest.main()
