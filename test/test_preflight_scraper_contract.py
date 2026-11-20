"""Exercise the real scraper path contract without starting a browser."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bwin_market_preflight as preflight
import scraper_bwin as scraper


class ScraperContractTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.eid='7909688'
        self.url=f'https://www.bwin.es/es/sports/eventos/letonia-armenia-2:{self.eid}'
        self.canonical=self.root/f'outputs/matches/{self.eid}/inputs/cuotas_bwin_{self.eid}.txt'
        self.canonical.parent.mkdir(parents=True);self.canonical.write_bytes(b'original retained bytes')
        self.old=self.canonical.read_bytes();self.out=self.root/'capture'

    def process(self,command,*,code=0,old=False,wrong_url=False,no_file=False,wrong_declaration=False,**kw):
        env=kw['env']
        with patch.object(scraper,'OUTPUT_DIR_SCRAPER',self.root/'outputs/scraper'),patch.dict(scraper.os.environ,env):
            inputs,diagnostics,_=scraper.rutas_salida_evento({'event_id':self.eid})
        self.assertIn('/outputs/matches/7909688/staging/',str(inputs))
        self.assertEqual(inputs.name,'inputs');self.assertEqual(inputs.parent.name,self.eid)
        self.assertFalse(inputs.is_relative_to(self.out))
        self.assertEqual(command[-2:],['--intentos','1'])
        (diagnostics/'diagnostic.json').write_text('{"test":true}')
        target=inputs/self.canonical.name
        if not no_file:
            now=datetime.now(timezone.utc)-(timedelta(days=1) if old else timedelta())
            target.write_text(f'Event ID: {self.eid}\nURL: {"https://wrong.invalid" if wrong_url else self.url}\n'
                              f'Captura: {now.isoformat()}\nAcordeones: 1 | Selecciones: 1\n'
                              'MERCADO: Total de goles | tiempo reglamentario\nMás de 2,5 : 1.80\n')
            kw['stdout'].write(f'TXT: {self.canonical if wrong_declaration else target}\n')
        if code:kw['stdout'].write('ValueError: controlled subprocess failure\n')
        return SimpleNamespace(returncode=code)

    def run_capture(self,**kwargs):
        with patch.object(preflight.subprocess,'run',side_effect=lambda cmd,**kw:self.process(cmd,**kw,**kwargs)):
            return preflight.run_scraper(self.root,self.out,self.eid,self.url,refresh=True,immutable=True)

    def test_actual_path_contract_then_verified_immutable_archive(self):
        result=self.run_capture()
        self.assertEqual(result['returncode'],0);self.assertNotIn('error',result)
        archive=Path(result['fresh_snapshot_path']);operational=Path(result['operational_inputs'])/self.canonical.name
        self.assertTrue(archive.is_relative_to(self.out));self.assertNotEqual(archive,operational)
        self.assertEqual(archive.read_bytes(),operational.read_bytes())
        self.assertEqual(result['snapshot_sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(self.canonical.read_bytes(),self.old)
        self.assertTrue((archive.parent/'scraper_diagnostics/diagnostic.json').is_file())
        provenance=json.loads((archive.parent/'provenance.json').read_text())
        self.assertEqual(provenance['returncode'],0);self.assertIn('finished_at_utc',provenance)

    def test_nonzero_exit_is_preserved_even_when_a_txt_exists(self):
        result=self.run_capture(code=73)
        self.assertEqual(result['returncode'],73)
        self.assertIn('controlled subprocess failure',result['error'])
        self.assertNotIn('fresh_snapshot_path',result)
        self.assertEqual(self.canonical.read_bytes(),self.old)

    def test_zero_exit_requires_new_valid_declared_output(self):
        for failure in ({'no_file':True},{'old':True},{'wrong_url':True},{'wrong_declaration':True}):
            with self.subTest(failure=failure):
                result=self.run_capture(**failure)
                self.assertEqual(result['returncode'],0)
                self.assertIn('error',result);self.assertNotIn('fresh_snapshot_path',result)
                self.assertEqual(self.canonical.read_bytes(),self.old)

    def test_timeout_retains_log_and_original_without_success(self):
        with patch.object(preflight.subprocess,'run',side_effect=subprocess.TimeoutExpired('controlled',1)):
            result=preflight.run_scraper(self.root,self.out,self.eid,self.url,refresh=True,immutable=True,timeout_seconds=1)
        self.assertTrue(result['timed_out']);self.assertIsNone(result['returncode'])
        self.assertNotIn('fresh_snapshot_path',result);self.assertTrue(Path(result['log']).is_file())
        self.assertEqual(self.canonical.read_bytes(),self.old)

    def test_historical_directory_still_rejected_by_unchanged_scraper(self):
        invalid=self.out/'captures'/self.eid/'20261114T220000000001Z'
        env={'PREMATCH_EVENT_ID':self.eid,'PREMATCH_EVENT_DATA_DIR':str(invalid),
             'PREMATCH_IMMUTABLE_CAPTURE':'1','PREMATCH_BATCH_MODE':'1'}
        with patch.object(scraper,'OUTPUT_DIR_SCRAPER',self.root/'outputs/scraper'),patch.dict(scraper.os.environ,env):
            with self.assertRaises(ValueError):scraper.rutas_salida_evento({'event_id':self.eid})


if __name__=='__main__':unittest.main()
