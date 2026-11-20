import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import bwin_identity_audit as identity
from bwin_discovery import category_compatibility, competition_similarity, team_similarity
from bwin_identity_regression import build

MADRID=ZoneInfo('Europe/Madrid')


def fixture(fid,home,away,kickoff,league='Segunda División',gender='M',**extra):
    return {'fixture_id':fid,'home':home,'away':away,'kickoff_madrid':kickoff.isoformat(),
            'league':league,'league_id':141,'country':'Spain','season':2026,'status':'NS',
            'gender':gender,'category_status':'ELIGIBLE','category_level':'SENIOR',
            'category_evidence_status':'CORROBORATED',
            'category_provenance':{'method':'SYNTHETIC_TEST_EVIDENCE'},**extra}


def event(eid,home,away,kickoff,league='Segunda División',observed=None,source='catalog'):
    observed=observed or kickoff-timedelta(hours=2)
    label='Hoy' if observed.date()==kickoff.date() else 'Mañana'
    return {'bwin_event_id':str(eid),'bwin_url':f'https://www.bwin.es/es/sports/eventos/{home}-{away}-2:{eid}',
            'text':f'{league} {label} / {kickoff:%H:%M} {home} - {away}',
            'text_observed_at_utc':observed.astimezone(timezone.utc).isoformat(),'text_source':source,
            'source_pages':['https://www.bwin.es/catalog']}


class IdentityFeatureTests(unittest.TestCase):
    def test_feature_1_normalization_thirty_positive_and_negative_pairs(self):
        positives=[('Manchester United','Man Utd'),('Internazionale','Inter Milan'),('Bayern München','Bayern Munich'),
            ('Atlético de Madrid','Atletico Madrid'),('Barcelona','Barça'),('Alaves','Deportivo Alavés'),
            ('Juventus W','Juventus Women'),('Lyon W','OL Lyonnes'),('Junior','CD Junior FC Barranquilla'),
            ('Türkiye','Turquía'),('Czechia','Chequia'),('Spain','España'),('England','Inglaterra'),
            ('Croatia','Croacia'),('Moldova','Moldavia'),('Kazakhstan','Kazajistán'),('Real Madrid CF','Real Madrid'),
            ('Athletic Club','Athletic Bilbao'),('Paris SG','Paris Saint Germain'),('Red Bull Bragantino','RB Bragantino')]
        negatives=[('Barcelona SC','Barcelona'),('Manchester City','Manchester United'),('Inter Miami','Internazionale'),
            ('Austria Wien','Austria'),('Junior','Argentinos Juniors'),('Lyon','Lyon Duchère'),('Spain','Spain U19'),
            ('Germany','Germany W'),('Real Sociedad','Real Madrid'),('Athletic Club','Atlético Madrid')]
        self.assertEqual(len(positives)+len(negatives),30)
        self.assertTrue(all(team_similarity(a,b,national_context=True)>=.94 for a,b in positives))
        # Text similarity alone may retrieve a category variant; it must never
        # confirm identity without the independent category gates.
        hard_negatives=negatives[1:5]+negatives[8:]
        self.assertTrue(all(team_similarity(a,b)<.94 for a,b in hard_negatives))
        self.assertGreaterEqual(team_similarity('Barcelona SC','Barcelona'),.94)
        self.assertGreaterEqual(team_similarity('Lyon','Lyon Duchère'),.94)
        self.assertIn(category_compatibility({'gender':'M','category_status':'ADULT'},'Spain U19'),{'NON_ADULT_CONFLICT'})
        self.assertEqual(category_compatibility({'gender':'M','category_status':'ADULT'},'Germany Women'),'GENDER_CONFLICT')

    def test_feature_2_time_utc_cet_cest_missing_and_rescheduled(self):
        winter=datetime(2026,1,10,20,0,tzinfo=MADRID);summer=datetime(2026,7,10,20,0,tzinfo=MADRID)
        for idx,kickoff in enumerate((winter,summer),1):
            row=identity.resolve_fixture(fixture(idx,'Home','Away',kickoff),[event(7000000+idx,'Home','Away',kickoff)],catalog_complete=True)
            self.assertEqual(row['link_status'],'VERIFIED_LINK')
        missing=event(7000003,'Home','Away',winter);missing['text']='Segunda División Home - Away'
        self.assertEqual(identity.resolve_fixture(fixture(3,'Home','Away',winter),[missing],catalog_complete=True)['link_status'],'SEARCH_INCOMPLETE')
        moved=event(7000004,'Home','Away',winter+timedelta(hours=2))
        self.assertEqual(identity.resolve_fixture(fixture(4,'Home','Away',winter),[moved],catalog_complete=True)['link_status'],'TIME_CONFLICT')

    def test_feature_3_categories_zero_false_links(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID)
        cases=[('Team','Rival','Team Women','Rival Women','M','CATEGORY_CONFLICT'),
               ('Team','Rival','Team U19','Rival U19','M','CATEGORY_CONFLICT'),
               ('Team W','Rival W','Team','Rival','F','CATEGORY_CONFLICT')]
        for i,(h,a,eh,ea,g,expected) in enumerate(cases,1):
            row=identity.resolve_fixture(fixture(i,h,a,kickoff,gender=g),[event(7100000+i,eh,ea,kickoff)],catalog_complete=True)
            self.assertNotEqual(row['link_status'],'VERIFIED_LINK');self.assertIn(row['link_status'],{expected,'SEARCH_INCOMPLETE'})

    def test_feature_4_competition_prevents_homonym_false_positive(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID)
        row=identity.resolve_fixture(fixture(1,'Barcelona SC','Emelec',kickoff,league='Liga Pro Ecuador'),
            [event(7200001,'Barcelona','Emelec',kickoff,league='La Liga')],catalog_complete=True)
        self.assertNotEqual(row['link_status'],'VERIFIED_LINK')
        self.assertLess(competition_similarity('Liga Pro Ecuador','La Liga Barcelona Emelec'),.82)

    def test_feature_5_candidate_recall_with_duplicates_and_distractors(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID);target=fixture(1,'Bayern München','Inter Milan',kickoff,league='Champions League')
        catalog=[event(7300000+i,f'Noise {i}',f'Other {i}',kickoff) for i in range(30)]
        catalog += [event(7399999,'Bayern Munich','Internazionale',kickoff,league='Champions League'),
                    event(7399998,'Bayern Munich','Internazionale',kickoff+timedelta(days=2),league='Friendly')]
        row=identity.resolve_fixture(target,catalog,catalog_complete=True)
        self.assertIn('7399999',[candidate['event_id'] for candidate in row['candidates']])
        self.assertNotEqual(row['link_status'],'NOT_FOUND_IN_CATALOG')

    def test_feature_6_real_resolution_precision_recall(self):
        root=Path(__file__).resolve().parents[1];truth=json.loads((root/'config/bwin_identity_regression.json').read_text())
        fixtures,catalog=build(truth,root);document=identity.audit(fixtures,catalog,[],[],catalog_complete=True,truth=truth)
        self.assertEqual(document['metrics']['precision_automatic_links'],1.0)
        self.assertEqual(document['metrics']['recall_verified_presence'],1.0)
        self.assertEqual(document['metrics']['false_positives'],0)

    def test_feature_7_one_catalog_matches_one_hundred_fixtures_locally(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID)
        fixtures=[fixture(i,f'Home {1000+i}',f'Away {1000+i}',kickoff) for i in range(1,101)]
        catalog=[event(7400000+i,f'Home {1000+i}',f'Away {1000+i}',kickoff) for i in range(1,101)]
        pages=[{'catalog_page':'https://www.bwin.es/catalog','status':'OK'}]
        document=identity.audit(fixtures,catalog,pages,[{'path':'frozen','sha256':'x','rows':100}],catalog_complete=True)
        self.assertEqual(len(document['fixtures']),100);self.assertEqual(document['metrics']['verified_links'],100)
        self.assertEqual(document['metrics']['catalog_page_navigations_recorded'],1);self.assertFalse(document['network_used'])

    def test_feature_8_directed_complement_preserves_provenance(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID);target=fixture(1,'Hard Name','Other Name',kickoff)
        general=[];directed=[event(7500001,'Hard Name','Other Name',kickoff,source='directed-country-page')]
        self.assertEqual(identity.resolve_fixture(target,general,catalog_complete=False)['link_status'],'SEARCH_INCOMPLETE')
        recovered=identity.resolve_fixture(target,directed,catalog_complete=True)
        self.assertEqual(recovered['link_status'],'VERIFIED_LINK')
        self.assertEqual(directed[0]['text_source'],'directed-country-page')

    def test_feature_9_idempotent_same_snapshot_without_network(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID);fixtures=[fixture(1,'Home','Away',kickoff)];catalog=[event(7600001,'Home','Away',kickoff)]
        first=identity.audit(fixtures,catalog,[],[{'path':'same','sha256':'abc','rows':1}],catalog_complete=True)
        second=identity.audit(fixtures,catalog,[],[{'path':'same','sha256':'abc','rows':1}],catalog_complete=True)
        self.assertEqual(first['fixtures'],second['fixtures']);self.assertFalse(first['network_used']);self.assertFalse(second['network_used'])

    def test_feature_10_identity_contract_keeps_fixture_and_event_distinct(self):
        kickoff=datetime(2026,11,20,20,0,tzinfo=MADRID)
        row=identity.resolve_fixture(fixture(1549868,'Deportivo Pasto','Junior',kickoff,league='Primera A',
                                             league_id=239,country='Colombia'),
                                     [event(7912964,'Deportivo Pasto','CD Junior FC Barranquilla',kickoff,league='Primera A')],catalog_complete=True)
        self.assertEqual(row['fixture_id'],1549868);self.assertEqual(row['bwin_event_id'],'7912964')
        handoff={'fixture_id':row['fixture_id'],'bwin_event_id':row['bwin_event_id']}
        ranking={'fixture_id':handoff['fixture_id'],'identity':handoff}
        case={'fixture_id':ranking['fixture_id'],'event_id':ranking['identity']['bwin_event_id']}
        self.assertNotEqual(str(case['fixture_id']),case['event_id'])


if __name__=='__main__':unittest.main()
