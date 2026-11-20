import unittest
from pathlib import Path
import prematch_ai_packager as packager
from prematch_tactical_signal_engine import research_context_category_matches as matches


class IntegratedCategoryTests(unittest.TestCase):
    def test_enriched_report_is_scoped_to_its_event(self):
        name='research_profiles_7911900.md'
        family=packager.classify_family(Path(name),'research/'+name)
        self.assertEqual(family,'perfil_tactico_evento')
        self.assertEqual(packager.bind_event(name,'',None,'7911900',family),'ESTE_EVENTO')
        self.assertEqual(packager.bind_event(name,'',None,'7909230',family),'EVENTO_AJENO')
        for provider in ('gemini','deepseek','codex'):
            self.assertIn(family,packager.PROVIDER_FAMILIES[provider])

    def test_explicit_codes_and_labels(self):
        for expected, actual in [('M','M'),('M','male'),('F','F'),('F','female')]:
            self.assertTrue(matches(expected,actual))

    def test_conflicts_and_unknowns_stay_blocked(self):
        for expected, actual in [('M','F'),('M','female'),('F','M'),('F','male'),('M',None),('M','UNKNOWN'),('UNKNOWN','M')]:
            self.assertFalse(matches(expected,actual))
