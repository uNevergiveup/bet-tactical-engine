import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import prematch_daily as daily
import prematch_pattern_discovery as discovery
import prematch_prospective_trial as trial
from preselect_matches import select_detail_games


ASOF=datetime.fromisoformat('2026-11-03T10:00:00+01:00')


def observation(fid,days,venue='HOME',requested=True,missing=False,league=140,season=2026,post=False):
    own={} if missing else {'goals':1+(fid%2),'corners':2+fid,'shots':4+2*fid,
                            'on_target':2+fid/2,'fouls':8+fid,'yellow':1+fid/5,'offsides':fid%3,'xg':1+fid/10,
                            'blocked_shots':1+fid}
    conceded={} if missing else {'goals':1,'corners':3+fid/2,'shots':6+fid,
                                 'on_target':2,'fouls':9+fid/2,'yellow':1+fid/6,'offsides':1,'xg':1.1}
    at=ASOF+timedelta(hours=1) if post else ASOF-timedelta(days=days)
    return {'fixture_id':fid,'team_id':10,'opponent_id':20,'kickoff_utc':at.astimezone(timezone.utc).isoformat(),
            'league_id':league,'season':season,'venue':venue,'status':'FT',
            'statistics_requested':requested,
            'statistics_request_status':'REQUESTED_MISSING' if requested and missing else 'VALID' if requested else 'NOT_REQUESTED',
            'own':own,'conceded':conceded}


def fixture(rows,away_rows=None,fid=100,gender='M'):
    away=[]
    for row in (away_rows if away_rows is not None else rows):
        copy=dict(row);copy['team_id']=20;copy['opponent_id']=10
        copy['venue']='AWAY' if row['venue']=='HOME' else 'HOME'
        away.append(copy)
    return {'fixture_id':fid,'home':'Home','away':'Away','home_id':10,'away_id':20,
            'league_id':140,'league':'League','season':2026,'gender':gender,
            'kickoff_madrid':'2026-11-04T20:00:00+01:00','analysis_cutoff_utc':ASOF.isoformat(),
            'statistical_observations':{'home':rows,'away':away}}


class HybridDiscoveryV2Tests(unittest.TestCase):
    def test_v213_temporal_and_identity_regressions(self):
        cases=['positive','utc_equivalent','missing_audit','missing_discovery','conflict_kickoff','post_kickoff','post_asof','invalid_capture','offsetless_capture','event_conflict','no_bwin','missing_sha','different_sha','mixed_captures']
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                q,s,b=self._commercial_fixture();p=Path(tmp)
                if case=='utc_equivalent':b['matches'][0]['kickoff_madrid']='2026-11-02T22:00:00+00:00'
                if case=='missing_audit':s['audits'][0].pop('kickoff_madrid')
                if case=='missing_discovery':b['matches'][0].pop('kickoff_madrid')
                if case=='conflict_kickoff':b['matches'][0]['kickoff_madrid']='2026-11-03T00:00:00+01:00'
                if case=='post_kickoff':s['selection_reviews'][0]['capture_madrid']='2026-11-02T23:01:00+01:00'
                if case=='post_asof':s['selection_reviews'][0]['capture_madrid']='2026-11-02T22:01:00+01:00'
                if case=='invalid_capture':s['selection_reviews'][0]['capture_madrid']='invalid'
                if case=='offsetless_capture':s['selection_reviews'][0]['capture_madrid']='2026-11-02T21:00:00'
                if case=='event_conflict':b['matches'][0]['bwin_event_id']='999'
                if case=='no_bwin':b['matches']=[];s['audits']=[];s['selection_reviews']=[]
                if case=='missing_sha':s['sources'].clear()
                if case=='different_sha':b['source_input_sha256']='different'
                if case=='mixed_captures':s['selection_reviews'].append({**s['selection_reviews'][0],'capture_madrid':'invalid'})
                for name,doc in (('q.json',q),('s.json',s),('b.json',b)):discovery.dump(p/name,doc)
                row=discovery.commercial_link(p/'q.json',p/'s.json',p/'b.json',p/'out')['links'][0]
                self.assertEqual(row['verified_quote_link'],case in ('positive','utc_equivalent','mixed_captures'))
                self.assertFalse(row['actionable_bet']);self.assertEqual(row['betting_value_status'],'NOT_ESTABLISHED')
                if case.startswith('missing_') and case!='missing_sha' or case=='conflict_kickoff':self.assertIn('KICKOFF_MISSING_OR_CONFLICT',row['commercial_validation_reasons'])
                if case in ('post_kickoff','post_asof','invalid_capture','offsetless_capture'):self.assertIn('CAPTURE_TIME_INVALID',row['commercial_validation_reasons'])
                if case=='event_conflict':self.assertIn('EVENT_ID_CONFLICT',row['commercial_validation_reasons'])
                if case=='no_bwin':self.assertNotIn('EVENT_ID_CONFLICT',row['commercial_validation_reasons'])

    def _commercial_fixture(self, review_event='123', discovery_event='123', scanner_sha='sha', bwin_sha='sha', capture='2026-11-02T21:00:00+01:00', scanner_asof='2026-11-02T22:00:00+01:00', kickoff='2026-11-02T23:00:00+01:00'):
        q={'preselection_sha256':'sha','items':[{'pattern_id':'p','fixture_id':1,'team_side':'home','metric':'corners','family':'corners','validation_status':'DESCRIPTIVE_SUPPORTED','sport_research_priority':1}]}
        s={'asof_madrid':scanner_asof,'sources':{'preselection_sha256':scanner_sha},'audits':[{'fixture_id':1,'bwin_event_id':'123','status':'VERIFIED','kickoff_madrid':kickoff}],
           'selection_reviews':[{'fixture_id':1,'bwin_event_id':review_event,'metric':'corners','team_side':'home','market':'Corners','selection':'Over 1.5','odd':2.0,'capture_madrid':capture}]}
        b={'source_input_sha256':bwin_sha,'matches':[{'fixture_id':1,'bwin_event_id':discovery_event,'kickoff_madrid':kickoff}]}
        return q,s,b

    def test_commercial_integrity_rejects_conflict_and_post_kickoff(self):
        for kwargs in ({'discovery_event':'FORGED'}, {'capture':'2026-11-02T23:01:00+01:00'}):
            with tempfile.TemporaryDirectory() as tmp:
                q,s,b=self._commercial_fixture(**kwargs);p=Path(tmp)
                for name,doc in (('q.json',q),('s.json',s),('b.json',b)):discovery.dump(p/name,doc)
                out=discovery.commercial_link(p/'q.json',p/'s.json',p/'b.json',p/'out')
                self.assertFalse(out['links'][0]['verified_quote_link']);self.assertEqual(out['links'][0]['commercial_status'],'COMMERCIAL_SOURCE_NOT_VERIFIABLE')

    def test_commercial_integrity_requires_all_shas_and_accepts_positive_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);q,s,b=self._commercial_fixture()
            for name,doc in (('q.json',q),('s.json',s),('b.json',b)):discovery.dump(p/name,doc)
            out=discovery.commercial_link(p/'q.json',p/'s.json',p/'b.json',p/'out')
            self.assertTrue(out['links'][0]['verified_quote_link']);self.assertFalse(out['links'][0]['actionable_bet'])
            s['sources'].pop('preselection_sha256');discovery.dump(p/'s.json',s)
            bad=discovery.commercial_link(p/'q.json',p/'s.json',p/'b.json',p/'out2')
            self.assertFalse(bad['links'][0]['verified_quote_link']);self.assertIn('SHA_MISSING_OR_CONFLICT',bad['links'][0]['commercial_validation_reasons'])
    def test_daily_cli_exposes_depth_defaults_and_rejects_invalid(self):
        output=StringIO()
        with self.assertRaises(SystemExit),redirect_stdout(output):daily.main(['--help'])
        self.assertIn('--history',output.getvalue());self.assertIn('--details',output.getvalue())
        for args in (['--dry-run','--history','3'],['--dry-run','--history','51'],['--dry-run','--history','20','--details','21']):
            with self.assertRaises(SystemExit),redirect_stderr(StringIO()):daily.main(args)
        self.assertEqual(daily.main(['--dry-run','--history','20','--details','8']),0)

    def test_detail_selection_prefers_exact_league_season_without_reordering_history(self):
        games=[{'id':1,'date_utc':'2026-11-02T09:00:00+00:00','status':'FT','league_id':999,'season':2026},
               {'id':2,'date_utc':'2026-11-01T09:00:00+00:00','status':'FT','league_id':140,'season':2025},
               {'id':3,'date_utc':'2026-10-31T09:00:00+00:00','status':'FT','league_id':140,'season':2026},
               {'id':4,'date_utc':'2026-10-30T09:00:00+00:00','status':'FT','league_id':140,'season':2026}]
        before=[x['id'] for x in games]
        selected,meta=select_detail_games(games,140,2026,ASOF,2)
        self.assertEqual([x['id'] for x in selected],[3,4]);self.assertEqual([x['id'] for x in games],before)
        self.assertEqual(meta['alternative_sources_used'],[]);self.assertEqual(meta['rejected_reasons']['DIFFERENT_COMPETITION'],1)

    def test_max_matches_remains_batch_size_not_global_cap(self):
        rows=[{'fixture_id':i,'home':f'H{i}','away':f'A{i}','status':'NS','region':'EUROPE',
               'kickoff_madrid':'2026-11-04T20:00:00+01:00'} for i in range(1,8)]
        selected=trial.select_fixtures({'schema_version':'1.0-major-leagues','fixtures':rows},[],0,ASOF,45,0)
        batches=[selected[i:i+2] for i in range(0,len(selected),2)]
        self.assertEqual(len(selected),7);self.assertEqual([len(x) for x in batches],[2,2,2,1])

    def test_coverage_20_history_8_valid_and_missing_are_not_zero(self):
        rows=[observation(i,i,requested=i<=8) for i in range(1,21)]
        rows[7]['own'].pop('shots');rows[7]['conceded'].pop('shots')
        pre={'matches':[fixture(rows)]};matrix=discovery.build_coverage(pre,'abc',ASOF)
        corner=next(x for x in matrix['rows'] if x['metric']=='corners' and x['source_type']=='TEAM_PRODUCTION')
        self.assertEqual(corner['history_available_n'],40);self.assertEqual(corner['details_requested_n'],16)
        self.assertEqual(corner['valid_observations_n'],16);self.assertEqual(corner['missing_provider_n'],0)
        self.assertEqual(corner['not_requested_n'],24);self.assertEqual(corner['requested_coverage_pct'],100.0)
        self.assertEqual(corner['effective_coverage_pct'],40.0)
        self.assertEqual(corner['history_available_n'], corner['valid_observations_n'] + corner['missing_provider_n'] + corner['not_requested_n'] + corner['request_status_unverifiable_n'])
        shots=next(x for x in matrix['rows'] if x['metric']=='shots' and x['source_type']=='TEAM_PRODUCTION')
        self.assertEqual(shots['valid_observations_n'],14);self.assertEqual(shots['missing_provider_n'],2)
        self.assertEqual(shots['details_requested_n'],16);self.assertEqual(shots['requested_coverage_pct'],87.5)

    def test_zero_details_requested_has_null_requested_coverage(self):
        rows=[observation(i,i,requested=False) for i in range(1,5)]
        matrix=discovery.build_coverage({'matches':[fixture(rows)]},'abc',ASOF)
        corner=next(x for x in matrix['rows'] if x['metric']=='corners' and x['source_type']=='TEAM_PRODUCTION')
        self.assertEqual(corner['details_requested_n'],0);self.assertIsNone(corner['requested_coverage_pct'])

    def test_exploration_finds_venue_recency_and_paired_association_without_post_cutoff(self):
        rows=[]
        for i in range(1,11):
            row=observation(i,i,'HOME' if i<=5 else 'AWAY')
            # Exact paired relation creates an observable association, not causality.
            row['own']['corners']=2*row['own']['shots']
            rows.append(row)
        rows.append(observation(999,1,post=True))
        doc=discovery.build_discovery({'matches':[fixture(rows)]},'abc',ASOF)
        segments={x['segment'] for x in doc['patterns'] if x['discovery_mode']=='EXPLORATORY'}
        self.assertIn('HOME_VS_AWAY',segments);self.assertIn('RECENT_VS_PREVIOUS',segments)
        self.assertIn('PAIRED_FIXTURE_ASSOCIATION',segments)
        self.assertTrue(all(999 not in ids for p in doc['patterns'] for ids in p['evidence_fixture_ids'].values()))
        paired=next(x for x in doc['patterns'] if x['metric']=='shots__corners')
        self.assertEqual(paired['validation_status'],'EXPLORATORY_UNVALIDATED')
        self.assertIn('CORRELATION_NOT_CAUSALITY',paired['validation_reasons'])

    def test_no_bwin_preserves_sport_queue_without_actionable_handoff(self):
        rows=[observation(i,i) for i in range(1,9)]
        disc=discovery.build_discovery({'matches':[fixture(rows)]},'abc',ASOF);queue=discovery.build_queue(disc)
        with tempfile.TemporaryDirectory() as tmp:
            q=Path(tmp)/'queue.json';discovery.dump(q,queue)
            link=discovery.commercial_link(q,None,None,Path(tmp)/'out')
        self.assertTrue(queue['items']);self.assertEqual(set(link['counts']),{'BWIN_NOT_FOUND'})
        self.assertFalse(link['actionable_bet']);self.assertTrue(all(x['sport_signal_preserved'] for x in link['links']))
        self.assertFalse(any('player' in str(x.get('metric','')).casefold() or 'period' in str(x.get('metric','')).casefold() for x in link['links']))

    def test_gender_and_fixture_identity_remain_separate(self):
        rows=[observation(i,i) for i in range(1,5)]
        a=fixture(rows,fid=100,gender='M');b=fixture(rows,fid=101,gender='F');b['home']='Home Women'
        matrix=discovery.build_coverage({'matches':[a,b]},'abc',ASOF)
        self.assertEqual({x['gender'] for x in matrix['rows']},{'M','F'})
        disc=discovery.build_discovery({'matches':[a,b]},'abc',ASOF)
        self.assertEqual({x['fixture_id'] for x in disc['patterns']},{100,101})

    def test_reproducible_and_duplicate_history_deduplicated(self):
        rows=[observation(i,i) for i in range(1,6)];rows.append(dict(rows[0]))
        pre={'matches':[fixture(rows)]}
        first=discovery.build_discovery(pre,'abc',ASOF);second=discovery.build_discovery(pre,'abc',ASOF)
        self.assertEqual(json.dumps(first,sort_keys=True),json.dumps(second,sort_keys=True))
        explicit=next(x for x in first['patterns'] if x['discovery_mode']=='EXPLICIT' and x['metric']=='goals' and x['team_side']=='home')
        self.assertLessEqual(explicit['history_n']['own'],5)

    def test_unsupported_dimensions_are_explicit(self):
        doc=discovery.build_discovery({'matches':[]},'abc',ASOF)
        self.assertEqual(doc['unsupported_dimensions']['players']['status'],'NOT_IMPLEMENTED')
        self.assertEqual(doc['unsupported_dimensions']['coaches']['status'],'NOT_AVAILABLE')


if __name__=='__main__':unittest.main()
