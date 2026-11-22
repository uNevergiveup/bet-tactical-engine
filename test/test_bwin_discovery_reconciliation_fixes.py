import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import bwin_discovery as bd
import prematch_steps as ps


def _fx(fid: int, home: str, away: str, league: str, country: str, ko_madrid: str, *, gender: str = "M", league_id: int = 78) -> dict:
    return {
        "fixture_id": fid,
        "home": home,
        "away": away,
        "league": league,
        "league_id": league_id,
        "country": country,
        "season": 2026,
        "gender": gender,
        "kickoff_madrid": ko_madrid,
        "kickoff_utc": bd.local_kickoff(ko_madrid).astimezone(timezone.utc).isoformat(),
    }


class TestBwinDiscoveryReconciliationFixes(unittest.TestCase):
    def test_napoli_localized_alias_and_absolute_time_boundaries(self):
        fixture = _fx(1550204, "Napoli", "Torino", "Serie A", "Italy", "2026-11-21T20:45:00+01:00", league_id=135)
        catalog = [{
            "bwin_event_id": "7911353",
            "bwin_url": "https://www.bwin.es/es/sports/eventos/n%C3%A1poles-torino-2:7911353",
            "text": "Nápoles Torino Hoy / 20:45 CA 4",
            "text_observed_at_utc": "2026-11-21T01:33:41+00:00",
        }]
        hits = bd.match_catalog(fixture, catalog)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["bwin_event_id"], "7911353")
        self.assertEqual(hits[0]["home_similarity"], 1.0)
        self.assertEqual(hits[0]["away_similarity"], 1.0)
        self.assertEqual(hits[0]["home_name_match_method"], "VERIFIED_ALIAS")
        self.assertEqual(hits[0]["away_name_match_method"], "LITERAL")
        self.assertEqual(hits[0]["name_match_status"], "VERIFIED_ALIAS_USED")
        self.assertEqual(hits[0]["team_order_status"], "HOME_AWAY_ALIAS")

        self.assertEqual(bd.absolute_kickoff_status(0), "TIME_VERIFIED")
        self.assertEqual(bd.absolute_kickoff_status(90), "TIME_VERIFIED")
        self.assertEqual(bd.absolute_kickoff_status(90.01), "TIME_REVIEW")
        self.assertEqual(bd.absolute_kickoff_status(None), "TIME_UNVERIFIED")

    def test_shared_institutional_prefix_uses_complete_alias_for_team_order(self):
        fixture = _fx(
            1575226,
            "Borussia Mönchengladbach",
            "Borussia Dortmund",
            "Bundesliga",
            "Germany",
            "2026-11-21T15:30:00+01:00",
        )
        catalog = [{
            "bwin_event_id": "7907990",
            "bwin_url": "https://www.bwin.es/es/sports/eventos/borussia-m%C2%B4gladbach-borussia-dortmund-2:7907990",
            "text": "Borussia M´gladbach Borussia Dortmund Hoy / 15:30 CA 3",
        }]
        hits = bd.match_catalog(fixture, catalog)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["team_order_status"], "HOME_AWAY_ALIAS")

    def test_1_and_2_bayern_and_koln_match_and_reject_wrong_colonia(self):
        fx_bayern = _fx(1575221, "Bayern München", "1. FC Köln", "Bundesliga", "Germany", "2026-11-20T20:30:00+01:00")
        catalog = [
            {
                "bwin_event_id": "7906958",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/bayern-m%C3%BAnich-colonia-2:7906958",
                "text": "Bundesliga Hoy / 20:30 Bayern Múnich - Colonia",
            },
            {
                "bwin_event_id": "7910837",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/tsv-havelse-viktoria-colonia-2:7910837",
                "text": "TSV Havelse Viktoria Colonia Hoy / 19:00 CA 1",
            },
        ]
        hits = bd.match_catalog(fx_bayern, catalog)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["bwin_event_id"], "7906958")
        self.assertEqual(hits[0]["home_similarity"], 1.0)
        self.assertEqual(hits[0]["away_similarity"], 1.0)
        self.assertEqual(hits[0]["team_order_status"], "HOME_AWAY_ALIAS")

    def test_3_and_4_racing_montevideo_and_utc_to_madrid_coincidence(self):
        fx_racing = _fx(1650505, "Racing Montevideo", "Central Espanol", "Primera División", "Uruguay", "2026-11-20T20:30:00+01:00", league_id=268)
        self.assertEqual(fx_racing["kickoff_utc"], "2026-11-20T19:30:00+00:00")
        catalog = [
            {
                "bwin_event_id": "7916472",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/racing-club-montevideo-central-espa%C3%B1ol-fc-2:7916472",
                "text": "Racing Club Montevideo Central Español FC Hoy / 20:30 CA",
            }
        ]
        hits = bd.match_catalog(fx_racing, catalog)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["bwin_event_id"], "7916472")

        # Verify Hoy / 20:30 in Europe/Madrid matches 19:30 UTC
        header_ko = bd.displayed_header_kickoff({
            "browser_timezone": "Europe/Madrid",
            "prematch_header": True,
            "date_label": "Hoy",
            "time_label": "20:30",
            "capture_started_at_utc": "2026-11-20T13:50:00Z",
            "captured_at_utc": "2026-11-20T13:50:10Z",
        })
        self.assertIsNotNone(header_ko)
        self.assertEqual(header_ko.astimezone(timezone.utc), datetime(2026, 11, 20, 19, 30, tzinfo=timezone.utc))

    def test_5_individual_scan_preserves_prior_verifications_and_detects_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            eval_dir = Path(td) / "eval"
            bwin_dir = eval_dir / "prospective_trial/bwin"
            bwin_dir.mkdir(parents=True)
            now_utc = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

            fx1 = _fx(1650505, "Racing Montevideo", "Central Espanol", "Primera División", "Uruguay", "2026-11-20T20:30:00+01:00")
            fx2 = _fx(1575221, "Bayern München", "1. FC Köln", "Bundesliga", "Germany", "2026-11-20T20:30:00+01:00")
            fx3 = _fx(999001, "Club Alpha", "Club Beta", "Liga Test", "Spain", "2026-11-20T21:00:00+01:00")
            major_doc = {"fixtures": [fx1, fx2, fx3]}

            # Step 1: Individual scan verifies fx1 (1650505)
            ps._persist_bwin_discovery_with_merge(
                bwin_dir,
                {
                    "schema_version": "0.5-bwin-discovery-v3",
                    "generated_at_utc": now_utc.isoformat(),
                    "catalog_observed": [{"bwin_event_id": "7916472", "bwin_url": "https://www.bwin.es/es/sports/eventos/racing-2:7916472", "text": "Racing"}],
                    "pages": [],
                    "matches": [{**fx1, "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "bwin_event_id": "7916472", "bwin_url": "https://www.bwin.es/es/sports/eventos/racing-2:7916472"}],
                },
                now_utc=now_utc,
                targeted_fids={1650505},
            )

            # Step 2: Individual scan on fx2 (1575221) NOT FOUND -> MUST preserve fx1 VERIFIED and mark fx3 NOT_SCANNED_IN_THIS_RUN
            later_utc = datetime(2026, 11, 20, 15, 10, tzinfo=timezone.utc)
            ps._persist_bwin_discovery_with_merge(
                bwin_dir,
                {
                    "schema_version": "0.5-bwin-discovery-v3",
                    "generated_at_utc": later_utc.isoformat(),
                    "catalog_observed": [],
                    "pages": [],
                    "matches": [{**fx2, "status": "NOT_FOUND_IN_SCANNED_PAGES", "bwin_event_id": None, "bwin_url": None}],
                },
                now_utc=later_utc,
                targeted_fids={1575221},
            )

            recon = ps._bwin_reconcile_records(eval_dir, major_doc, later_utc)
            by_fid = {r["fixture_id"]: r for r in recon["all"]}
            self.assertEqual(by_fid[1650505]["bwin_state"], "VERIFIED")
            self.assertTrue(by_fid[1650505]["reused_from_prior_scan"])
            self.assertFalse(by_fid[1650505]["scanned_in_latest_run"])
            self.assertEqual(by_fid[1575221]["reason"], "NOT_FOUND_IN_SCANNED_PAGES")
            self.assertEqual(by_fid[999001]["reason"], "NOT_SCANNED_IN_THIS_RUN")

            # Step 3: Contradiction check if another scan claims a different verified bwin_event_id for fx1
            even_later = datetime(2026, 11, 20, 15, 20, tzinfo=timezone.utc)
            ps._persist_bwin_discovery_with_merge(
                bwin_dir,
                {
                    "schema_version": "0.5-bwin-discovery-v3",
                    "generated_at_utc": even_later.isoformat(),
                    "catalog_observed": [],
                    "pages": [],
                    "matches": [{**fx1, "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "bwin_event_id": "9999999", "bwin_url": "https://www.bwin.es/es/sports/eventos/other-2:9999999"}],
                },
                now_utc=even_later,
                targeted_fids={1650505},
            )
            recon_conflict = ps._bwin_reconcile_records(eval_dir, major_doc, even_later)
            by_fid_c = {r["fixture_id"]: r for r in recon_conflict["all"]}
            self.assertEqual(by_fid_c[1650505]["bwin_state"], "CONFLICT")
            self.assertIn("CONTRADICTION", by_fid_c[1650505]["reason"] if "CONTRADICTION" in by_fid_c[1650505]["reason"] else "BWIN_IDENTITY_CONFLICT")

    def test_6_similar_clubs_do_not_merge_falsely(self):
        fx_paraguay = _fx(1632066, "Deportivo Recoleta", "Club Guarani", "Division Profesional - Clausura", "Paraguay", "2026-11-21T00:00:00+01:00", league_id=250)
        catalog = [
            {"bwin_event_id": "7916467", "bwin_url": "https://www.bwin.es/es/sports/eventos/deportes-recoleta-santiago-wanderers-2:7916467", "text": "Deportes Recoleta Santiago Wanderers Hoy / 22:00 CA"},
            {"bwin_event_id": "7914205", "bwin_url": "https://www.bwin.es/es/sports/eventos/recoleta-club-guarani-asunci%C3%B3n-2:7914205", "text": "Recoleta Club Guarani Asunción Mañana / 0:00 CA"},
        ]
        hits = bd.match_catalog(fx_paraguay, catalog)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["bwin_event_id"], "7914205")

    def test_7_and_8_hoy_manana_explicit_dates_and_contradictory_times(self):
        # Hoy / Manana / explicit date dd/mm/yy
        manana_ko = bd.displayed_header_kickoff({
            "browser_timezone": "Europe/Madrid",
            "prematch_header": True,
            "date_label": "Mañana",
            "time_label": "00:30",
            "capture_started_at_utc": "2026-11-20T15:00:00Z",
            "captured_at_utc": "2026-11-20T15:00:05Z",
        })
        self.assertEqual(manana_ko, datetime(2026, 11, 21, 0, 30, tzinfo=bd.TZ))

        explicit_ko = bd.displayed_header_kickoff({
            "browser_timezone": "Europe/Madrid",
            "prematch_header": True,
            "date_label": "22/11/26",
            "time_label": "16:15",
            "capture_started_at_utc": "2026-11-20T15:00:00Z",
            "captured_at_utc": "2026-11-20T15:00:05Z",
        })
        self.assertEqual(explicit_ko, datetime(2026, 11, 22, 16, 15, tzinfo=bd.TZ))

        # Wrong timezone rejected
        wrong_tz = bd.displayed_header_kickoff({
            "browser_timezone": "UTC",
            "prematch_header": True,
            "date_label": "Hoy",
            "time_label": "20:30",
            "capture_started_at_utc": "2026-11-20T15:00:00Z",
            "captured_at_utc": "2026-11-20T15:00:05Z",
        })
        self.assertIsNone(wrong_tz)

    def test_9_female_vs_male_false_positives_and_toulouse_marseille_w(self):
        fx_fem = _fx(1581016, "Toulouse W", "Marseille W", "Feminine Division 1", "France", "2026-11-20T21:00:00+01:00", gender="F", league_id=83)
        fx_male_fc = _fx(999002, "F.C. Copenhagen", "Aarhus", "Superliga", "Denmark", "2026-11-20T21:00:00+01:00", gender="M", league_id=119)
        catalog = [
            {"bwin_event_id": "7914117", "bwin_url": "https://www.bwin.es/es/sports/eventos/toulouse-fc-f-o-marsella-f-2:7914117", "text": "Toulouse FC F O. Marsella FHoy / 21:00 CA"},
            {"bwin_event_id": "7910951", "bwin_url": "https://www.bwin.es/es/sports/eventos/lens-toulouse-fc-2:7910951", "text": "Lens Toulouse FC Mañana / 20:45 CA"},
            {"bwin_event_id": "7999999", "bwin_url": "https://www.bwin.es/es/sports/eventos/f-c-copenhagen-aarhus-2:7999999", "text": "F.C. Copenhagen Aarhus Hoy / 21:00 CA"},
        ]
        hits_f = bd.match_catalog(fx_fem, catalog)
        self.assertEqual([h["bwin_event_id"] for h in hits_f], ["7914117"])
        self.assertEqual(hits_f[0]["category_compatibility"], "FEMALE_CORROBORATED")

        # Male F.C. Copenhagen must NOT be flagged female by 'F.C.'
        hits_m = bd.match_catalog(fx_male_fc, catalog)
        self.assertEqual([h["bwin_event_id"] for h in hits_m], ["7999999"])
        self.assertNotEqual(hits_m[0]["category_compatibility"], "GENDER_CONFLICT")

    def test_10_no_regression_on_previously_verified_fixtures(self):
        catalog = json.loads(Path("outputs/discovery/2026-11-20_1500_0300/prospective_trial/bwin/bwin_discovery.json").read_text())["catalog_observed"]
        expected = [
            (_fx(1570454, "Athletic Club", "Espanyol", "La Liga", "Spain", "2026-11-20T21:00:00+01:00", league_id=140), "7910856"),
            (_fx(1552827, "Estac Troyes", "Le Havre", "Ligue 1", "France", "2026-11-20T20:45:00+01:00", league_id=61), "7910807"),
            (_fx(1494808, "Molde", "Bodo/Glimt", "Eliteserien", "Norway", "2026-11-20T19:00:00+01:00", league_id=103), "7906891"),
            (_fx(1603044, "Al-Ahli Jeddah", "Al-Nassr", "Pro League", "Saudi-Arabia", "2026-11-20T18:30:00+01:00", league_id=307), "7912835"),
            (_fx(1581810, "Grasshopper W", "Basel W", "AXA Women’s Super League", "Switzerland", "2026-11-20T19:00:00+01:00", gender="F", league_id=576), "7914210"),
        ]
        for fx, exp_eid in expected:
            hits = bd.match_catalog(fx, catalog)
            self.assertTrue(hits, msg=f"No hits for {fx['home']} vs {fx['away']}")
            self.assertEqual(hits[0]["bwin_event_id"], exp_eid)


if __name__ == "__main__":
    unittest.main()
