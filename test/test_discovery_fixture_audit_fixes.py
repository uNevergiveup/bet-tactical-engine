"""Pruebas offline de auditoría discovery por fixture, aliases generalizados, immutable refresh y --diagnose-fixture."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bwin_discovery as disc
import bwin_identity_audit as audit
import prematch_audit as pmaudit
import prematch_daily as daily
import prematch_prospective_trial as trial
import prematch_runtime as runtime

ROOT = Path(__file__).resolve().parent.parent
REAL_EVAL3 = ROOT / "outputs/discovery/2026-11-16_1500_0300/evaluations/20261116T140316019822Z_39af34fb2084"


class TestGeneralizedAliasesWithoutRelaxation(unittest.TestCase):
    def test_institutional_and_national_aliases_recover_catalog_without_relaxing_gates(self):
        catalog = [
            {
                "bwin_event_id": "7905476",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/alemania-pa%C3%ADses-bajos-2:7905476",
                "text": "Alemania Países Bajos Hoy / 20:45 CA 1 1.80 X 4.33 2 3.25",
            },
            {
                "bwin_event_id": "7912772",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/cusco-fc-ad-tarma-2:7912772",
                "text": "Cusco FC Tarma Hoy / 21:15 CA",
            },
            {
                "bwin_event_id": "7912771",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/utc-de-cajamarca-cd-los-chankas-cyc-2:7912771",
                "text": "UTC de Cajamarca CD Los Chankas CYC Hoy / 19:00 CA",
            },
            {
                "bwin_event_id": "7912757",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/grecia-sub-21-islandia-sub-21-2:7912757",
                "text": "Grecia Sub-21 Islandia Sub-21 Hoy / 18:00 CA",
            },
            {
                "bwin_event_id": "7999001",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/alemania-pa%C3%ADses-bajos-f-2:7999001",
                "text": "Alemania F Países Bajos F Hoy / 20:45 En vivo",
            },
        ]
        f_ger_ned = {
            "fixture_id": 1528990, "home": "Germany", "away": "Netherlands",
            "country": "World", "league": "UEFA Nations League", "league_id": 5,
            "gender": "M", "kickoff_madrid": "2026-11-16T20:45:00+01:00",
            "category_level": "SENIOR", "category_evidence_status": "CORROBORATED",
        }
        cands = disc.match_catalog(f_ger_ned, catalog)
        self.assertEqual([c["bwin_event_id"] for c in cands], ["7905476"])
        self.assertEqual(cands[0]["team_order_status"], "HOME_AWAY_ALIAS")

        # Live and female events are never mixed into senior male Germany-Netherlands
        self.assertNotIn("7999001", [c["bwin_event_id"] for c in cands])

        # Reversed home/away fixture does NOT match 7905476
        f_reversed = {**f_ger_ned, "home": "Netherlands", "away": "Germany"}
        self.assertEqual(disc.match_catalog(f_reversed, catalog), [])

        # Youth U21 fixture is excluded / not confused with senior national alias
        f_u21 = {**f_ger_ned, "fixture_id": 999, "home": "Greece", "away": "Iceland"}
        self.assertEqual(disc.match_catalog(f_u21, [catalog[3]]), [])

        # Peruvian club aliases
        f_cusco = {
            "fixture_id": 1549558, "home": "Cusco", "away": "ADT",
            "country": "Peru", "league": "Primera División", "league_id": 281,
            "gender": "M", "kickoff_madrid": "2026-11-16T21:15:00+01:00",
        }
        self.assertEqual([c["bwin_event_id"] for c in disc.match_catalog(f_cusco, catalog)], ["7912772"])

        f_chankas = {
            "fixture_id": 1549556, "home": "UTC Cajamarca", "away": "Club Deportivo Los Chankas",
            "country": "Peru", "league": "Primera División", "league_id": 281,
            "gender": "M", "kickoff_madrid": "2026-11-16T19:00:00+01:00",
        }
        self.assertEqual([c["bwin_event_id"] for c in disc.match_catalog(f_chankas, catalog)], ["7912771"])

    def test_not_found_on_partial_catalog_stays_search_incomplete_never_absent(self):
        fixture = {
            "fixture_id": 1647127, "home": "Nacional", "away": "La Fama",
            "country": "Aruba", "league": "Division di Honor", "league_id": 421,
            "gender": "M", "kickoff_madrid": "2026-11-17T01:30:00+01:00",
            "category_level": "SENIOR", "category_evidence_status": "CORROBORATED",
        }
        res = audit.resolve_fixture(fixture, [], catalog_complete=False)
        self.assertEqual(res["link_status"], "SEARCH_INCOMPLETE")

    @unittest.skipUnless(REAL_EVAL3.is_dir(), "EVAL3 real no presente")
    def test_real_eval3_local_replay_recovers_all_9_missing_present_in_catalog(self):
        pre = json.loads((REAL_EVAL3 / "prospective_trial/enriched/preselection.json").read_text())["matches"]
        bwin_disc = json.loads((REAL_EVAL3 / "prospective_trial/bwin/shared/bwin_discovery.json").read_text())
        cat = bwin_disc["catalog_observed"]
        matched_ids = {f["fixture_id"]: [c["bwin_event_id"] for c in disc.match_catalog(f, cat)] for f in pre}
        expected_recovered = {
            1528990: "7905476",
            1528986: "7909699",
            1545617: "7909705",
            1528987: "7909698",
            1528989: "7909690",
            1545652: "7909663",
            1545662: "7909662",
            1549558: "7912772",
            1549556: "7912771",
        }
        for fid, eid in expected_recovered.items():
            self.assertIn(eid, matched_ids[fid], f"Fixture {fid} debe recuperar {eid}")
        # Genuine conflict (1649670) remains blocked in audit
        prepared = [audit.prepare_event(e) for e in cat]
        f_mali = next(f for f in pre if f["fixture_id"] == 1649670)
        res_mali = audit.resolve_fixture(f_mali, prepared, catalog_complete=False)
        self.assertEqual(res_mali["link_status"], "NAME_MISMATCH")
        self.assertIn("COMPETITION_NOT_CORROBORATED", res_mali["reason_codes"])

        # Statistical ranking in EVAL3 stays strictly separated from Bwin failures
        handoff = json.loads((REAL_EVAL3 / "prospective_trial/handoff_prematch.json").read_text())
        stat_rank = handoff["statistical_research_ranking"]
        self.assertEqual([r["fixture_id"] for r in stat_rank[:3]], [1549558, 1528986, 1545617])
        self.assertTrue(all(r.get("commercial_readiness_not_used") for r in stat_rank[:3]))
        self.assertTrue(all(r.get("actionable_bet") is False for r in stat_rank))


class TestImmutableRefreshForPreparedEvent(unittest.TestCase):
    def test_failure_state_does_not_misclassify_scraper_failure_as_identity_error(self):
        state = pmaudit.failure_state("SCRAPER_CAPTURE_FAILED: Evento preparado: EVENT_ID mismatch in log tail")
        self.assertEqual(state, "SIN_CUOTAS")

    def test_verified_preflight_accepts_immutable_capture_only_for_manifest_protected_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eid = "7905477"
            match_dir = root / "outputs/matches" / eid
            (match_dir / "inputs").mkdir(parents=True)
            (match_dir / "manifest.json").write_text('{"state":"FINALIZADO"}\n', encoding="utf-8")
            canonical = match_dir / "inputs" / f"cuotas_bwin_{eid}.txt"
            canonical.write_text("OLD CANONICAL SNAPSHOT\n", encoding="utf-8")

            # Prepare discovery + preselection + preflight with immutable archived capture
            disc_path = root / "bwin_discovery.json"
            disc_path.write_text(json.dumps({
                "matches": [{
                    "fixture_id": 1528991, "home": "Greece", "away": "Serbia",
                    "league": "UEFA Nations League", "league_id": 5, "country": "World",
                    "kickoff_madrid": "2026-11-16T20:45:00+01:00",
                    "status": "IDENTITY_VERIFIED_AUTOMATICALLY",
                    "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                    "bwin_event_id": eid,
                    "bwin_url": f"https://www.bwin.es/es/sports/eventos/grecia-serbia-2:{eid}",
                    "inspection": {
                        "status": "OPENED",
                        "page_title": "Grecia - Serbia UEFA Nations League Fútbol | bwin",
                        "event_id_from_url": eid,
                        "event_start_machine_readable": "2026-11-16T19:45:00+00:00",
                        "kickoff_difference_minutes": 0.0,
                        "is_live": False,
                        "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                        "title_teams_match": True,
                        "observed_at_utc": "2026-11-16T14:00:00+00:00",
                    },
                    "time_evidence": {"status": "DISPLAY_TIME_CORROBORATED"},
                }],
                "catalog_observed": [{
                    "bwin_event_id": eid,
                    "bwin_url": f"https://www.bwin.es/es/sports/eventos/grecia-serbia-2:{eid}",
                    "text": "Grecia Serbia Hoy / 20:45",
                    "text_observed_at_utc": "2026-11-16T14:00:00+00:00",
                }],
            }), encoding="utf-8")
            pre_path = root / "preselection.json"
            pre_path.write_text(json.dumps({"matches": [{"fixture_id": 1528991}]}), encoding="utf-8")

            pf_dir = root / "preflight/1528991_0"
            cap_dir = pf_dir / "captures" / eid / "20261116T150000Z"
            cap_dir.mkdir(parents=True)
            archived = cap_dir / f"cuotas_bwin_{eid}.txt"
            txt_content = (
                f"Event ID: {eid}\n"
                f"URL: https://www.bwin.es/es/sports/eventos/grecia-serbia-2:{eid}\n"
                "Captura: 2026-11-16T15:00:00+01:00\n"
                "Acordeones: 1 | Selecciones: 1\n"
                "MERCADO: Resultado del partido | Tiempo reglamentario\n"
                "Grecia : 1.44\n"
            )
            archived.write_text(txt_content, encoding="utf-8")
            arch_sha = trial.sha(archived)

            for csv_name in ("market_selection_review.csv", "market_research_shortlist.csv"):
                (pf_dir / csv_name).write_text("fixture_id,bwin_event_id\n1528991,7905477\n", encoding="utf-8")

            pf_path = pf_dir / "market_preflight.json"
            pf_path.write_text(json.dumps({
                "generated_at_utc": "2026-11-16T14:01:00+00:00",
                "source_discovery": str(disc_path),
                "source_sha256": trial.sha(disc_path),
                "source_preselection_sha256": trial.sha(pre_path),
                "matches": [{
                    "fixture_id": 1528991, "home": "Greece", "away": "Serbia",
                    "kickoff_madrid": "2026-11-16T20:45:00+01:00",
                    "bwin_event_id": eid,
                    "bwin_url": f"https://www.bwin.es/es/sports/eventos/grecia-serbia-2:{eid}",
                    "status": "MARKET_TRIPLES_EXTRACTED",
                    "time_evidence": {"status": "DISPLAY_TIME_CORROBORATED"},
                    "scraper": {"canonical_unchanged": True},
                    "snapshot": {
                        "snapshot_path": str(archived),
                        "sha256": arch_sha,
                        "event_id_verified_in_txt": True,
                        "capture_declared_madrid": "2026-11-16T15:00:00+01:00",
                        "selection_triplets_verified": 1,
                    },
                    "quality_gate": {
                        "join_status": "MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF",
                        "researchable_after_data_gate": 1,
                    },
                }],
            }), encoding="utf-8")

            fixture = {
                "fixture_id": 1528991, "home": "Greece", "away": "Serbia",
                "kickoff_madrid": "2026-11-16T20:45:00+01:00",
                "analysis_cutoff_utc": "2026-11-16T13:55:00+00:00",
            }
            args = type("Args", (), {"min_lead": 45, "max_odds_age": 120})()
            now_fixed = datetime.fromisoformat("2026-11-16T15:10:00+01:00")
            with patch.object(trial, "now_madrid", return_value=now_fixed):
                matched = runtime.verified_preflight(root, pf_path, fixture, pre_path, args)
            self.assertEqual(matched["snapshot"]["snapshot_path"], str(archived.resolve()))
            self.assertEqual(canonical.read_text(encoding="utf-8"), "OLD CANONICAL SNAPSHOT\n")

            # Without canonical_unchanged=True, it must reject arbitrary non-canonical paths
            pf_doc = json.loads(pf_path.read_text())
            pf_doc["matches"][0]["scraper"]["canonical_unchanged"] = False
            pf_path.write_text(json.dumps(pf_doc), encoding="utf-8")
            with patch.object(trial, "now_madrid", return_value=now_fixed):
                with self.assertRaises(trial.TrialError) as ctx:
                    runtime.verified_preflight(root, pf_path, fixture, pre_path, args)
            self.assertIn("SNAPSHOT_EVENT_DIRECTORY_MISMATCH", str(ctx.exception))


class TestDiagnoseFixtureReadOnlyCLI(unittest.TestCase):
    @unittest.skipUnless(REAL_EVAL3.is_dir(), "EVAL3 real no presente")
    def test_diagnose_fixture_cli_real_eval3_readonly(self):
        for fid, exp_phase, exp_eid in ((1528990, "bwin_discovery", "7905476"), (1528991, "capture_or_preflight", "7905477")):
            proc = subprocess.run(
                [
                    sys.executable, str(ROOT / "prematch_daily.py"),
                    "--diagnose-fixture", "--date", "2026-11-16",
                    "--fixture-id", str(fid), "--eval-dir", str(REAL_EVAL3),
                ],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.loads(proc.stdout)
            self.assertTrue(doc["reads_only"])
            self.assertFalse(doc["network"])
            self.assertFalse(doc["actionable_bet"])
            self.assertEqual(doc["blocking_phase"], exp_phase)
            if fid == 1528990:
                self.assertEqual(doc["bwin_identity"]["local_catalog_rematch_preview"][0]["bwin_event_id"], exp_eid)
            else:
                self.assertEqual(doc["bwin_identity"]["bwin_event_id"], exp_eid)
                self.assertTrue(doc["bwin_identity"]["canonical_manifest_present"])

    def test_diagnose_fixture_fails_closed_for_missing_fixture(self):
        if not REAL_EVAL3.is_dir():
            self.skipTest("EVAL3 real no presente")
        proc = subprocess.run(
            [
                sys.executable, str(ROOT / "prematch_daily.py"),
                "--diagnose-fixture", "--date", "2026-11-16",
                "--fixture-id", "999999999", "--eval-dir", str(REAL_EVAL3),
            ],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
