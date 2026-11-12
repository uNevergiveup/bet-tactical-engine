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
        expected={1644370:('7910678','IDENTITY_VERIFIED_OFFLINE'),1520978:('7911881','CANDIDATE_CATEGORY_UNKNOWN'),
            1549870:('7912958','IDENTITY_VERIFIED_OFFLINE'),1528963:('7909679','CANDIDATE_TIME_UNVERIFIED'),
            1649147:('7910426','CANDIDATE_TIME_CONFLICT')}
        for fid,(eid,status) in expected.items():self.assertEqual((rows[fid]['event_id'],rows[fid]['post_status']),(eid,status))

    def test_navalcarnero_senior_cup_category_and_armenia_translation(self):
        root=Path(__file__).parent;inv=root/'outputs/phase1/2026-11-12_1500_0300/fixtures_discovered.json';disc=root/'outputs/phase1/2026-11-12_1500_0300/browser_retry_02/bwin_scan/bwin_discovery.json'
        if not inv.is_file() or not disc.is_file():self.skipTest('Frozen catalog unavailable')
        rows={x['fixture_id']:x for x in r.run(inv,disc,[1644363,1528959])['fixtures']}
        self.assertEqual(rows[1644363]['post_status'],'IDENTITY_VERIFIED_OFFLINE')
        self.assertEqual(rows[1644363]['category']['status'],'SENIOR_COMPETITION_CORROBORATED')
        self.assertEqual(rows[1528959]['event_id'],'7909685')
        self.assertEqual(rows[1528959]['post_status'],'CANDIDATE_TIME_UNVERIFIED')

    def test_senior_category_gate_requires_exact_competition_and_no_female_signal(self):
        f=self.fixture(league_id=143,league='Copa del Rey',category_status='ELIGIBLE')
        self.assertTrue(b.senior_category_corroborated(f,'CATEGORY_UNCONFIRMED',1.0))
        self.assertFalse(b.senior_category_corroborated({**f,'league':'Other'},'CATEGORY_UNCONFIRMED',1.0))
        self.assertFalse(b.senior_category_corroborated(f,'CATEGORY_UNCONFIRMED',.8))
        self.assertFalse(b.senior_category_corroborated(f,'OBSERVED_FEMALE_TARGET_UNKNOWN',1.0))

    def test_multilingual_national_team_candidates_remain_unverified(self):
        cases=[('Türkiye','Belgium','Turquía Bélgica','7905470'),('Italy','France','Italia Francia','7905471'),
            ('Czechia','Spain','Chequia España','7905479'),('England','Croatia','Inglaterra Croacia','7905480')]
        for home,away,text,eid in cases:
            f=self.fixture(home=home,away=away)
            e=self.event(text=text+' Hoy / 20:45',bwin_event_id=eid,bwin_url=f'https://www.bwin.es/es/sports/eventos/{eid}-2:{eid}')
            found=b.match_catalog(f,[e]);self.assertEqual(found[0]['bwin_event_id'],eid)
            result=r.reconcile_fixture(f,[e],{},[]);self.assertEqual(result['post_status'],'CANDIDATE_CATEGORY_UNKNOWN')
            self.assertFalse(result['identity_verified'])
        self.assertEqual(b.team_similarity('Belgium','Turquía Bélgica'),0)
        self.assertEqual(b.match_catalog(self.fixture(home='Italy U20',away='France U20',category_status='EXCLUDED'),
            [self.event(text='Italia Francia Hoy / 20:45')]),[])

    def test_benfica_austria_female_institutional_candidate_is_not_male_alias(self):
        event=self.event(text='Benfica F FK Austria Viena F Hoy / 21:00',bwin_event_id='7910156',
            bwin_url='https://www.bwin.es/es/sports/eventos/benfica-f-fk-austria-viena-f-2:7910156')
        female=self.fixture(home='SL Benfica W',away='Austria Wien W',gender='F')
        self.assertEqual(b.match_catalog(female,[event])[0]['category_compatibility'],'FEMALE_CORROBORATED')
        self.assertEqual(b.match_catalog({**female,'home':'SL Benfica','away':'Austria Wien','gender':'M'},[event]),[])
        self.assertEqual(b.match_catalog({**female,'home':'SL Benfica W U19'},[event]),[])

if __name__=='__main__':unittest.main()
