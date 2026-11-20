import tempfile
import unittest
from pathlib import Path

from select_single_match import temporal_identity_contract


class TemporalContractTransportTests(unittest.TestCase):
    def contract(self, evidence):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preflight.json'
            path.write_text('{}')
            return temporal_identity_contract({
                'fixture_id': 1570018, 'bwin_event_id': '7909230',
                'bwin_url': 'https://www.bwin.es/es/sports/eventos/granada-ud-las-palmas-2:7909230',
                'home': 'Granada CF', 'away': 'Las Palmas',
                'kickoff_madrid': '2026-11-15T16:15:00+01:00',
                'time_evidence': evidence,
            }, {'user_time_reference': 'Europe/Madrid'}, 'source-hash', path)

    def test_absolute_evidence_transports_api_fixture_clock(self):
        result = self.contract({'status': 'ABSOLUTE_TIME_VERIFIED'})
        self.assertEqual(result['api_kickoff_madrid'], '2026-11-15T16:15:00+01:00')

    def test_explicit_conflicting_clock_is_preserved_for_consumer_rejection(self):
        result = self.contract({'api_kickoff_madrid': '2026-11-15T17:15:00+01:00'})
        self.assertEqual(result['api_kickoff_madrid'], '2026-11-15T17:15:00+01:00')

    def test_missing_observation_is_not_invented(self):
        result = self.contract({})
        self.assertIsNone(result['observed_at_utc'])
        self.assertIsNone(result['evidence_status'])


if __name__ == '__main__':
    unittest.main()
