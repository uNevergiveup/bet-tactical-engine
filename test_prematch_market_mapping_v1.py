import json
import csv
import tempfile
import unittest
from pathlib import Path

from prematch_market_mapping_v1 import MappingError, map_markets


def pattern(fid=1, metric='goals', status='MUESTRA_DESCRIPTIVA_OK'):
    return {'fixture_id': fid, 'home': 'Home', 'away': 'Away', 'kickoff_madrid': '2026-11-09T20:00:00+01:00',
            'team': 'Home', 'opponent': 'Away', 'team_side': 'home', 'metric': metric,
            'status': status, 'pattern_flag': 'ASIMETRIA_RELATIVA_ALTA'}


class MarketMappingTests(unittest.TestCase):
    def make_inputs(self, *, rows=None, metric='goals', status='MUESTRA_DESCRIPTIVA_OK', event='7000099'):
        td = tempfile.TemporaryDirectory(); root = Path(td.name)
        actual_rows=[list(row) for row in rows] if rows else [['1',event,'Home - Total de goles | Tiempo reglamentario','Más de 1,5','1.80','total_goals','PRICE_ELIGIBLE','DATA_READY_FOR_RESEARCH']]
        for row in actual_rows:
            if row[1]=='99': row[1]=event
        url=f'https://www.bwin.es/es/sports/eventos/home-away-2:{event}'
        snapshot = root / 'odds.txt'; snapshot.write_text(f'Event ID: {event}\nURL: {url}\nCaptura: 2026-11-09T16:00:00+01:00\n'+''.join(f'MERCADO: {r[2]}\n{r[3]} : {r[4]}\n' for r in actual_rows), encoding='utf-8')
        import hashlib
        preflight = {'generated_at_utc': '2026-11-09T15:00:00+00:00', 'matches': [{
            'fixture_id': 1, 'home': 'Home', 'away': 'Away', 'kickoff_madrid': '2026-11-09T20:00:00+01:00',
            'bwin_event_id': event, 'bwin_url': url,
            'status': 'MARKET_TRIPLES_EXTRACTED', 'time_evidence': {'status': 'DISPLAY_TIME_CORROBORATED','observed_at_utc':'2026-11-09T14:55:00+00:00'},
            'snapshot': {'event_id_verified_in_txt': True, 'snapshot_path': str(snapshot),
                         'sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),'source_url':url,'capture_declared_madrid':'2026-11-09T16:00:00+01:00'},
            'quality_gate': {'join_status': 'MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF'},
        }]}
        pf = root / 'market_preflight.json'; pf.write_text(json.dumps(preflight), encoding='utf-8')
        review = root / 'market_selection_review.csv'
        with review.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['fixture_id', 'bwin_event_id', 'market', 'selection', 'odd', 'market_kind', 'price_tier', 'data_status'])
            writer.writerows(actual_rows)
        patterns = root / 'patterns.json'; patterns.write_text(json.dumps({'asof_madrid': '2026-11-09T15:00:00+01:00', 'patterns': [pattern(metric=metric, status=status)]}), encoding='utf-8')
        return td, patterns, pf

    def test_exact_team_line_matches_without_authorizing_bet(self):
        td, patterns, pf = self.make_inputs()
        try:
            result = map_markets(patterns, [pf])
            row = result['mappings'][0]
            self.assertEqual(row['mapping_status'], 'MARKET_MATCHED')
            self.assertEqual(row['market_links'][0]['line'], 1.5)
            self.assertFalse(row['actionable_bet'])
            self.assertEqual(result['counts']['market_matched'], 1)
        finally: td.cleanup()

    def test_pending_validation_and_unsupported_are_preserved(self):
        td, patterns, pf = self.make_inputs(metric='offsides', status='SIN_DATOS')
        try:
            result = map_markets(patterns, [pf])
            self.assertEqual(result['mappings'][0]['mapping_status'], 'UNSUPPORTED_MARKET')
        finally: td.cleanup()
        td, patterns, pf = self.make_inputs(metric='goals', status='MUESTRA_PARCIAL')
        try:
            self.assertEqual(map_markets(patterns, [pf])['mappings'][0]['mapping_status'], 'PENDING_VALIDATION')
        finally: td.cleanup()

    def test_no_capture_and_identity_conflict_are_distinct(self):
        td, patterns, pf = self.make_inputs()
        try:
            result = map_markets(patterns, [])
            self.assertEqual(result['mappings'][0]['mapping_status'], 'PENDING_ODDS')
            bad = json.loads(pf.read_text()); bad['matches'][0]['bwin_event_id'] = '100'; pf.write_text(json.dumps(bad))
            self.assertEqual(map_markets(patterns, [pf])['mappings'][0]['mapping_status'], 'IDENTITY_BLOCKED')
        finally: td.cleanup()

    def test_preflight_without_statistical_join_is_security_block_not_market_match(self):
        td, patterns, pf = self.make_inputs()
        try:
            doc = json.loads(pf.read_text())
            doc['matches'][0]['quality_gate']['join_status'] = 'NO_PRESELECTION_SUPPLIED'
            pf.write_text(json.dumps(doc), encoding='utf-8')
            row = map_markets(patterns, [pf])['mappings'][0]
            self.assertEqual(row['mapping_status'], 'IDENTITY_BLOCKED')
            self.assertEqual(row['reason'], 'API_BWIN_JOIN_NOT_VERIFIED')
        finally: td.cleanup()

    def test_missing_team_market_and_duplicate_rows(self):
        csv_rows = [['1', '99', 'Total de córners | Tiempo reglamentario', 'Más de 6,5', '1.80', 'corners', 'PRICE_ELIGIBLE', 'DATA_READY_FOR_RESEARCH']] * 2
        td, patterns, pf = self.make_inputs(rows=csv_rows)
        try:
            result = map_markets(patterns, [pf])
            self.assertEqual(result['mappings'][0]['mapping_status'], 'MARKET_NOT_AVAILABLE')
            self.assertEqual(result['counts']['duplicate_market_rows_removed'], 1)
        finally: td.cleanup()

    def test_ambiguous_same_line_with_different_prices_is_not_matched(self):
        rows = [
            ['1', '99', 'Home - Total de goles | Tiempo reglamentario', 'Más de 1,5', '1.80', 'total_goals', 'PRICE_ELIGIBLE', 'DATA_READY_FOR_RESEARCH'],
            ['1', '99', 'Home - Total de goles | Tiempo reglamentario', 'Más de 1,5', '2.00', 'total_goals', 'PRICE_ELIGIBLE', 'DATA_READY_FOR_RESEARCH'],
        ]
        td, patterns, pf = self.make_inputs(rows=rows)
        try:
            result = map_markets(patterns, [pf])
            self.assertEqual(result['mappings'][0]['mapping_status'], 'MARKET_NOT_AVAILABLE')
            self.assertEqual(result['mappings'][0]['reason'], 'AMBIGUOUS_MARKET_SELECTION')
        finally: td.cleanup()

    def test_output_is_idempotent_and_conflicts_are_blocked(self):
        td, patterns, pf = self.make_inputs()
        try:
            out = Path(td.name) / 'mapping'
            map_markets(patterns, [pf], output_dir=out)
            first = (out / 'market_mapping.json').read_bytes()
            mtimes = {p: (out / p).stat().st_mtime_ns for p in ('market_mapping.json', 'market_mapping.md')}
            map_markets(patterns, [pf], output_dir=out)
            self.assertEqual(first, (out / 'market_mapping.json').read_bytes())
            self.assertEqual(mtimes, {p: (out / p).stat().st_mtime_ns for p in mtimes})
            patterns.write_text(patterns.read_text().replace('MUESTRA_DESCRIPTIVA_OK', 'SIN_DATOS'))
            with self.assertRaisesRegex(MappingError, 'OUTPUT_EXISTS_CONFLICT'):
                map_markets(patterns, [pf], output_dir=out)
        finally: td.cleanup()

    def test_real_osасuna_artifacts_are_auditable(self):
        root = Path(__file__).resolve().parent
        patterns = root / 'outputs/discovery/2026-11-07_1500_0300/prospective_trial/patterns/pattern_engine.json'
        pf = root / 'outputs/phase3/bwin_integration_selected/preflight/market_preflight.json'
        result = map_markets(patterns, [pf])
        rows = [x for x in result['mappings'] if str(x['fixture_id']) == '1570448']
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(x['bwin_event_id'] == '7903872' for x in rows))
        self.assertFalse(any(x['actionable_bet'] for x in rows))

    def test_daily_optional_stage_uses_only_local_inputs(self):
        import prematch_daily
        from types import SimpleNamespace
        td, patterns, pf = self.make_inputs()
        try:
            args = SimpleNamespace(market_mapping=True, market_mapping_patterns=patterns,
                                   market_mapping_preflight=[pf])
            output, counts = prematch_daily.run_optional_market_mapping(Path(td.name), Path(td.name) / 'window', args)
            self.assertTrue((output / 'market_mapping.json').is_file())
            self.assertEqual(counts['market_matched'], 1)
        finally: td.cleanup()


class FemaleMarketOwnerTests(unittest.TestCase):
    def test_verified_female_aliases_map_exact_owner(self):
        from prematch_market_mapping_v1 import _compatible
        for team, opponent, owner in [('Juventus W','Lyon W','Juventus (F)'),('Lyon W','Juventus W','OL Lyonnes (F)')]:
            p={'team':team,'opponent':opponent,'metric':'goals','period':'FULL_TIME'}
            row={'market':owner+' - Total de goles | Tiempo reglamentario','market_kind':'total_goals','period':'FULL_TIME'}
            self.assertTrue(_compatible(row,p))
            self.assertFalse(_compatible(row,dict(p,team=opponent,opponent=team)))
            self.assertFalse(_compatible(dict(row,period='FIRST_HALF'),p))

    def test_alias_fallback_rejects_category_and_unowned_total(self):
        from prematch_market_mapping_v1 import _compatible
        p={'team':'Lyon W','opponent':'Juventus W','metric':'goals','period':'FULL_TIME'}
        for market in ['Lyon - Total de goles | Tiempo reglamentario','Lyon U19 (F) - Total de goles | Tiempo reglamentario','Total de goles | Tiempo reglamentario']:
            self.assertFalse(_compatible({'market':market,'market_kind':'total_goals','period':'FULL_TIME'},p))


if __name__ == '__main__':
    unittest.main()
